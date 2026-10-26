# -*- coding: utf-8 -*-
"""标书智能审查系统 V1 - 后端服务
FastAPI + SQLite：项目/上传/审查/问题状态闭环/报告导出
@author WorkBuddy-GLM, Collus Wang
"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import requests

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import engine
import llm

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "app.db"
SETTINGS_PATH = DATA / "settings.json"
CONFIG_PATH = BASE / "rules_config.json"

DATA.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="标书智能审查系统")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  doc_type TEXT NOT NULL,
  filename TEXT NOT NULL,
  path TEXT NOT NULL,
  uploaded_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  status TEXT DEFAULT 'running',
  progress INTEGER DEFAULT 0,
  message TEXT DEFAULT '',
  summary TEXT DEFAULT '',
  steps TEXT DEFAULT '[]',
  tasks TEXT DEFAULT '[]',
  started_at TEXT DEFAULT (datetime('now','localtime')),
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  run_id INTEGER,
  rule_id TEXT,
  source TEXT DEFAULT 'rule',
  category TEXT,
  severity TEXT,
  title TEXT,
  description TEXT,
  page INTEGER DEFAULT 0,
  evidence TEXT,
  suggestion TEXT,
  status TEXT DEFAULT 'open',
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS issue_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id INTEGER NOT NULL,
  from_status TEXT,
  to_status TEXT,
  note TEXT,
  actor TEXT DEFAULT '用户',
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS export_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  filename TEXT NOT NULL,
  exported_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS project_rules(
  project_id INTEGER PRIMARY KEY,
  content TEXT DEFAULT '',
  updated_at TEXT DEFAULT (datetime('now','localtime'))
);
"""

STATUS_LABEL = {"open": "待处理", "resolved": "已解决", "wip": "待改进", "ignored": "已忽略"}
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(SCHEMA)
    # 老库迁移：runs 表补 steps 列（进度时间线）、tasks 列（检查任务明细）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
    if "steps" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN steps TEXT DEFAULT '[]'")
    if "tasks" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN tasks TEXT DEFAULT '[]'")
    conn.commit()
    conn.close()


init_db()

ACTIVE_RUNS = {}  # project_id -> run_id


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text("utf-8"))
        except json.JSONDecodeError:
            pass
    return {"llm_base_url": "", "llm_api_key": "", "llm_model": ""}


def save_settings(s):
    SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), "utf-8")


def mask_key(key: str) -> str:
    """API Key 脱敏：仅显示前 4 后 4，中间用 **** 代替"""
    if not key:
        return ""
    key = str(key)
    if len(key) <= 8:
        return key[:2] + "****" + key[-2:] if len(key) > 4 else "****"
    return key[:4] + "****" + key[-4:]


def resolve_key(input_key: str, current_key: str) -> str:
    """保存设置时：若传入的是脱敏占位值（含 ****），视为未修改，保留原 Key；否则视为新 Key"""
    inp = (input_key or "").strip()
    if inp and ("****" in inp or inp == mask_key(current_key)):
        return current_key or ""
    return inp


def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text("utf-8"))
        except json.JSONDecodeError:
            pass
    return {"required_sections": engine.REQUIRED_SECTIONS_DEFAULT}


# ---------------- 审查线程 ----------------

def review_worker(project_id, run_id):
    conn = db()

    def load_steps():
        row = conn.execute("SELECT steps FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row or not row["steps"]:
            return []
        try:
            return json.loads(row["steps"])
        except json.JSONDecodeError:
            return []

    def save_steps(steps):
        conn.execute("UPDATE runs SET steps=? WHERE id=?",
                     (json.dumps(steps, ensure_ascii=False), run_id))

    def load_tasks():
        row = conn.execute("SELECT tasks FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row or not row["tasks"]:
            return []
        try:
            return json.loads(row["tasks"])
        except json.JSONDecodeError:
            return []

    def save_tasks(tasks):
        conn.execute("UPDATE runs SET tasks=? WHERE id=?",
                     (json.dumps(tasks, ensure_ascii=False), run_id))

    def upd(progress, message, status=None, summary=None, step=None,
            task_begin=None, task_pos=None, task_end=None, task_error=None):
        """更新进度。
        step: 追加一条进行中的大步骤（上一条进行中自动转完成）；
        task_begin: 开始一个检查任务（上一条未收尾的任务自动标记完成）；
        task_end: 结束当前检查任务并记录结果（0=无问题，>0=发现N条，<0=失败）；
        status 为 done/error 时把最后一条进行中步骤收尾。"""
        if step is not None:
            steps = load_steps()
            if steps and steps[-1].get("s") == "doing":
                steps[-1]["s"] = "done"
            steps.append({"t": step, "s": "doing", "ts": time.strftime("%H:%M:%S")})
            save_steps(steps)
        if task_begin is not None:
            tasks = load_tasks()
            if tasks and tasks[-1].get("s") == "doing":
                # 防御：上一条任务未收到结果即开始新任务，视为检查无问题
                tasks[-1]["s"] = "done"
                tasks[-1]["n"] = tasks[-1].get("n", 0)
            tasks.append({"t": task_begin, "pos": task_pos or "",
                          "s": "doing", "ts": time.strftime("%H:%M:%S")})
            save_tasks(tasks)
        if task_end is not None:
            tasks = load_tasks()
            if tasks and tasks[-1].get("s") == "doing":
                tasks[-1]["s"] = "done" if task_end >= 0 else "error"
                tasks[-1]["n"] = task_end if task_end >= 0 else 0
                if task_end < 0:
                    tasks[-1]["err"] = task_error or "调用失败"
                save_tasks(tasks)
        if status in ("done", "error"):
            steps = load_steps()
            if steps and steps[-1].get("s") == "doing":
                steps[-1]["s"] = status
                save_steps(steps)
            tasks = load_tasks()
            if tasks and tasks[-1].get("s") == "doing":
                # 审查收尾时仍有未收尾任务，视为检查无问题
                tasks[-1]["s"] = "done"
                tasks[-1]["n"] = tasks[-1].get("n", 0)
                save_tasks(tasks)
        if status:
            conn.execute(
                "UPDATE runs SET progress=?, message=?, status=?, summary=?,"
                " finished_at=datetime('now','localtime') WHERE id=?",
                (progress, message, status,
                 json.dumps(summary, ensure_ascii=False) if summary else None,
                 run_id))
        else:
            conn.execute("UPDATE runs SET progress=?, message=? WHERE id=?",
                         (progress, message, run_id))
        conn.commit()

    try:
        upd(5, "读取文档 ...", step="读取项目文档")
        docs = conn.execute(
            "SELECT * FROM documents WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        bid_doc = next((d for d in reversed(docs) if d["doc_type"] == "标书"), None)
        tender_doc = next((d for d in reversed(docs) if d["doc_type"] == "招标文件"), None)
        if not bid_doc:
            raise ValueError("该项目尚未上传标书文件")

        upd(15, f"解析标书《{bid_doc['filename']}》...", step="解析标书")
        bid = engine.parse_file(bid_doc["path"])

        tender = None
        tender_error = ""
        if tender_doc:
            upd(30, f"解析招标文件《{tender_doc['filename']}》...", step="解析招标文件")
            try:
                tender = engine.parse_file(tender_doc["path"])
            except Exception as e:
                tender = None
                tender_error = str(e)
                upd(32, f"招标文件解析失败：{tender_error}", step="解析招标文件")

        # 读取该项目"补充检查项"文本（用户可选填写；招标文件正文由系统自动读取核对）
        rules_text = ""
        rrow = conn.execute(
            "SELECT content FROM project_rules WHERE project_id=?", (project_id,)).fetchone()
        if rrow and rrow["content"]:
            rules_text = rrow["content"]

        # 招标文件正文（文本层可读时）：规则引擎 R201 动态提取 + LLM 招标文件核对共用
        tender_text = ""
        if tender and tender["has_text_layer"]:
            tender_text = "\n".join(tender["pages"])

        upd(40, "运行规则引擎 ...", step="运行规则引擎（硬规则检查）")
        # 必备资料清单来源：补充检查项 + 招标文件正文（动态提取，无则跳过不误报）
        required_from_rules = engine.extract_required_from_text(
            (rules_text + " " + tender_text)) if (rules_text or tender_text) else None
        issues, summary = engine.run_rules(bid, load_config(), tender, required_from_rules,
                                           tender_error=tender_error)

        conn.execute("DELETE FROM issues WHERE project_id=?", (project_id,))
        for it in issues:
            conn.execute(
                "INSERT INTO issues(project_id, run_id, rule_id, source, category, severity,"
                " title, description, page, evidence, suggestion, status)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open')",
                (project_id, run_id, it.get("rule_id"), it.get("source", "rule"),
                 it.get("category"), it.get("severity"), it.get("title"),
                 it.get("description"), it.get("page", 0), it.get("evidence"),
                 it.get("suggestion")))
        conn.commit()

        settings = load_settings()
        llm_on = bool(settings.get("llm_base_url") and settings.get("llm_api_key")
                      and settings.get("llm_model"))

        # ---- LLM 段 1：招标文件逐项核对（需求一：招标文件正文传给大模型与标书对照）----
        if llm_on:
            # 连通性预检：无效配置直接跳过 LLM 段，避免审查长时间无响应
            ok, ping_msg = llm.ping(settings.get("llm_base_url"), settings.get("llm_api_key"),
                                    settings.get("llm_model"))
            if not ok:
                summary["招标文件核对"] = f"LLM 连接失败，跳过逐项核对（{ping_msg}）"
                summary["LLM 语义审查"] = f"LLM 连接失败，已跳过（{ping_msg}）"
            else:
                if tender and tender["has_text_layer"]:
                    upd(45, "LLM 启动：招标文件逐项核对 ...", step="LLM：招标文件逐项核对")
                    try:
                        t_issues, t_msg, t_detail = llm.run_tender_review(
                            settings, tender["pages"], bid["pages"],
                            lambda p, m, **kw: upd(p, m, **kw))
                        for it in t_issues:
                            conn.execute(
                                "INSERT INTO issues(project_id, run_id, rule_id, source, category,"
                                " severity, title, description, page, evidence, suggestion, status)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open')",
                                (project_id, run_id, "LLM-T", "llm", it["category"], it["severity"],
                                 it["title"], it["description"], it["page"], it["evidence"],
                                 it["suggestion"]))
                        conn.commit()
                        summary["招标文件核对"] = t_msg
                        if t_detail:
                            d = t_detail
                            summary["招标核对明细"] = (
                                f"招标文件共 {d['blocks']} 部分，已核对 {d['checked']} 部分"
                                f"（成功 {d['ok']} / 失败 {d['failed']}），产出 {d['issues']} 条"
                                + ("，标书内容较长已截取前部" if d["bid_truncated"] else ""))
                            if d.get("errors"):
                                summary["招标核对失败原因"] = "；".join(d["errors"][:5])
                    except Exception as e:
                        summary["招标文件核对"] = f"调用失败：{e}"
                else:
                    summary["招标文件核对"] = ("招标文件无文本层或未上传，跳过逐项核对"
                                               "（可在“补充检查项”中粘贴关键要求）")

                # ---- LLM 段 2：标书语义审查（检查任务队列：类别×分块，实时反馈进度）----
                upd(75, "LLM 语义审查启动 ...", step="LLM：标书语义审查（检查任务队列）")
                try:
                    llm_issues, llm_msg, llm_detail = llm.run_llm_review(
                        settings, bid, extra_rules=rules_text,
                        tender_snippet=tender_text[:20000],
                        progress_cb=lambda p, m, **kw: upd(p, m, **kw))
                    for it in llm_issues:
                        conn.execute(
                            "INSERT INTO issues(project_id, run_id, rule_id, source, category,"
                            " severity, title, description, page, evidence, suggestion, status)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open')",
                            (project_id, run_id, "LLM", "llm", it["category"], it["severity"],
                             it["title"], it["description"], it["page"], it["evidence"],
                             it["suggestion"]))
                    conn.commit()
                    summary["LLM 语义审查"] = llm_msg
                    # 调用明细落进 summary，供排查"是否真的调用了模型"
                    if llm_detail:
                        d = llm_detail
                        inj = []
                        if d.get("extra_rules_injected"):
                            inj.append("补充检查项")
                        if d.get("tender_snippet_injected"):
                            inj.append("招标文件参考")
                        summary["LLM 调用明细"] = (
                            f"检查任务 {d['tasks']}（成功 {d['ok']} / 失败 {d['failed']}），"
                            f"产出 {d['issues']} 条，已注入 {'+'.join(inj) if inj else '（无，仅标书自身检查）'}"
                            + (f"，标书共 {d['blocks_total']} 部分仅审查前 {d['blocks_used']} 部分"
                               if d.get("blocks_cut") else ""))
                        if d.get("errors"):
                            summary["LLM 失败原因"] = "；".join(d["errors"][:5])
                except Exception as e:
                    summary["LLM 语义审查"] = f"调用失败：{e}"
        else:
            summary["招标文件核对"] = "未配置 LLM，跳过逐项核对（仅规则引擎）"
            summary["LLM 语义审查"] = "未配置 LLM 接口，已跳过（可在右上角“设置”中配置）"

        sev_count = {"high": 0, "medium": 0, "low": 0}
        for it in issues:
            sev_count[it.get("severity", "low")] = sev_count.get(it.get("severity", "low"), 0) + 1
        summary["问题统计"] = f"高 {sev_count['high']} / 中 {sev_count['medium']} / 低 {sev_count['low']}"
        upd(100, "审查完成", status="done", summary=summary, step="审查完成")
    except Exception as e:
        upd(100, f"审查失败：{e}", status="error")
    finally:
        ACTIVE_RUNS.pop(project_id, None)
        conn.close()


# ---------------- API：项目 ----------------

class ProjectIn(BaseModel):
    name: str


@app.get("/api/projects")
def list_projects():
    conn = db()
    rows = conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/projects")
def create_project(body: ProjectIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "项目名不能为空")
    conn = db()
    cur = conn.execute("INSERT INTO projects(name) VALUES(?)", (name,))
    pid = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    conn.close()
    return dict(row)


# ---------------- API：上传 ----------------

@app.post("/api/documents")
async def upload_document(project_id: int = Form(...), doc_type: str = Form(...),
                          file: UploadFile = File(...)):
    if doc_type not in ("招标文件", "标书"):
        raise HTTPException(400, "doc_type 必须为 招标文件 或 标书")
    suffix = Path(file.filename or "f.bin").suffix or ".bin"
    dest_dir = UPLOADS / str(project_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex}{suffix}"
    content = await file.read()
    dest.write_bytes(content)
    conn = db()
    cur = conn.execute(
        "INSERT INTO documents(project_id, doc_type, filename, path) VALUES(?,?,?,?)",
        (project_id, doc_type, file.filename, str(dest)))
    did = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    conn.close()
    return dict(row)


@app.get("/api/documents/{did}/content")
def document_content(did: int):
    """查看已上传文件内容：文本类直接内联返回，PDF 等在浏览器内预览"""
    conn = db()
    row = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "文件不存在")
    p = Path(row["path"])
    if not p.exists():
        raise HTTPException(404, "文件已丢失，请删除后重新上传")
    suffix = p.suffix.lower()
    fn = quote(row["filename"])
    if suffix in (".txt", ".md"):
        try:
            content = p.read_text("utf-8")
        except UnicodeDecodeError:
            content = p.read_text("gbk", errors="replace")
        return Response(content=content, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f"inline; filename*=UTF-8''{fn}"})
    media = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }.get(suffix, "application/octet-stream")
    return Response(content=p.read_bytes(), media_type=media,
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{fn}"})


@app.delete("/api/documents/{did}")
def delete_document(did: int):
    """删除已上传文件（记录 + 磁盘文件）"""
    conn = db()
    row = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "文件不存在")
    conn.execute("DELETE FROM documents WHERE id=?", (did,))
    conn.commit()
    conn.close()
    try:
        Path(row["path"]).unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True, "deleted": row["filename"]}


# ---------------- API：项目招标要求 ----------------

class RulesBody(BaseModel):
    content: str = ""


@app.get("/api/projects/{pid}/rules")
def get_project_rules(pid: int):
    """读取项目"招标要求"文本（用户粘贴的关键要求）"""
    conn = db()
    row = conn.execute(
        "SELECT content, updated_at FROM project_rules WHERE project_id=?", (pid,)).fetchone()
    conn.close()
    if not row:
        return {"content": "", "updated_at": ""}
    return {"content": row["content"], "updated_at": row["updated_at"]}


@app.post("/api/projects/{pid}/rules")
def set_project_rules(pid: int, body: RulesBody):
    """保存项目"招标要求"文本：审查时作为动态规则注入规则引擎与 LLM"""
    conn = db()
    conn.execute(
        "INSERT INTO project_rules(project_id, content) VALUES(?,?)"
        " ON CONFLICT(project_id) DO UPDATE SET content=excluded.content,"
        " updated_at=datetime('now','localtime')",
        (pid, body.content.strip()))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- API：审查与总览 ----------------

@app.post("/api/projects/{pid}/review")
def start_review(pid: int):
    if pid in ACTIVE_RUNS:
        raise HTTPException(409, "该项目正在审查中，请等待完成")
    conn = db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(404, "项目不存在")
    has_bid = conn.execute(
        "SELECT 1 FROM documents WHERE project_id=? AND doc_type='标书' LIMIT 1",
        (pid,)).fetchone()
    if not has_bid:
        conn.close()
        raise HTTPException(400, "请先上传标书文件")
    cur = conn.execute("INSERT INTO runs(project_id) VALUES(?)", (pid,))
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    ACTIVE_RUNS[pid] = run_id
    threading.Thread(target=review_worker, args=(pid, run_id), daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/projects/{pid}/overview")
def overview(pid: int):
    conn = db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(404, "项目不存在")
    docs = [dict(r) for r in conn.execute(
        "SELECT id, doc_type, filename, uploaded_at FROM documents WHERE project_id=? ORDER BY id",
        (pid,)).fetchall()]
    run_row = conn.execute(
        "SELECT * FROM runs WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    run = dict(run_row) if run_row else None
    if run and run.get("summary"):
        try:
            run["summary"] = json.loads(run["summary"])
        except json.JSONDecodeError:
            pass
    if run and run.get("steps"):
        try:
            run["steps"] = json.loads(run["steps"])
        except json.JSONDecodeError:
            run["steps"] = []
    if run and run.get("tasks"):
        try:
            run["tasks"] = json.loads(run["tasks"])
        except json.JSONDecodeError:
            run["tasks"] = []
    issues = [dict(r) for r in conn.execute(
        "SELECT * FROM issues WHERE project_id=?"
        " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id",
        (pid,)).fetchall()]
    hist = {}
    for h in conn.execute(
            "SELECT * FROM issue_history WHERE issue_id IN"
            " (SELECT id FROM issues WHERE project_id=?) ORDER BY id", (pid,)).fetchall():
        hist.setdefault(h["issue_id"], []).append(dict(h))
    conn.close()
    return {"project": dict(proj), "documents": docs, "run": run,
            "issues": issues, "history": hist}


# ---------------- API：问题状态 ----------------

class IssuePatch(BaseModel):
    status: str
    note: str = ""


@app.patch("/api/issues/{issue_id}")
def patch_issue(issue_id: int, body: IssuePatch):
    if body.status not in STATUS_LABEL:
        raise HTTPException(400, f"状态必须为 {'/'.join(STATUS_LABEL)}")
    conn = db()
    row = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "问题不存在")
    conn.execute(
        "UPDATE issues SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
        (body.status, issue_id))
    conn.execute(
        "INSERT INTO issue_history(issue_id, from_status, to_status, note) VALUES(?,?,?,?)",
        (issue_id, row["status"], body.status, body.note))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------- API：报告导出 ----------------

@app.get("/api/projects/{pid}/report.md")
def export_report(pid: int):
    conn = db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        conn.close()
        raise HTTPException(404, "项目不存在")
    docs = conn.execute(
        "SELECT * FROM documents WHERE project_id=? ORDER BY id", (pid,)).fetchall()
    run_row = conn.execute(
        "SELECT * FROM runs WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    run = dict(run_row) if run_row else None
    issues = [dict(r) for r in conn.execute(
        "SELECT * FROM issues WHERE project_id=?"
        " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id",
        (pid,)).fetchall()]
    hist = {}
    for h in conn.execute(
            "SELECT * FROM issue_history WHERE issue_id IN"
            " (SELECT id FROM issues WHERE project_id=?) ORDER BY id", (pid,)).fetchall():
        hist.setdefault(h["issue_id"], []).append(dict(h))
    # 记录本次导出并生成唯一文件名：版本号 + 时间戳（到秒），避免多次导出相互覆盖
    cur = conn.execute("INSERT INTO export_logs(project_id, filename) VALUES(?, '')", (pid,))
    log_id = cur.lastrowid
    ver = conn.execute("SELECT COUNT(*) AS c FROM export_logs WHERE project_id=?",
                       (pid,)).fetchone()["c"]
    ts = time.strftime("%Y%m%d-%H%M%S")
    fname = quote(f"{proj['name']}-审查报告-v{ver}-{ts}.md")
    conn.execute("UPDATE export_logs SET filename=? WHERE id=?", (fname, log_id))
    conn.commit()
    conn.close()

    L = [f"# 标书审查报告：{proj['name']}", ""]
    L.append(f"- 生成时间：{(run or {}).get('finished_at') or (run or {}).get('started_at') or ''}")
    for d in docs:
        L.append(f"- {d['doc_type']}：{d['filename']}")
    if run and run.get("summary"):
        try:
            for k, v in json.loads(run["summary"]).items():
                L.append(f"- {k}：{v}")
        except (json.JSONDecodeError, TypeError):
            pass
    stats = {s: 0 for s in STATUS_LABEL}
    for it in issues:
        stats[it["status"]] = stats.get(it["status"], 0) + 1
    L.append("")
    L.append(f"**问题总计 {len(issues)} 条：** "
             + "，".join(f"{STATUS_LABEL[k]} {v}" for k, v in stats.items() if v))
    L.append("")
    sev_map = {"high": "高", "medium": "中", "low": "低"}
    for it in issues:
        L.append(f"## [{sev_map.get(it['severity'], '?')}] {it['title']}")
        L.append("")
        L.append(f"- 状态：**{STATUS_LABEL.get(it['status'], it['status'])}**"
                 f"｜类别：{it['category']}｜规则：{it['rule_id']}（{'AI' if it['source']=='llm' else '规则'}）"
                 + (f"｜位置：第 {it['page']} 页" if it["page"] else ""))
        if it["description"]:
            L.append(f"- 说明：{it['description']}")
        if it["evidence"]:
            L.append(f"- 证据：{it['evidence']}")
        if it["suggestion"]:
            L.append(f"- 建议：{it['suggestion']}")
        hs = hist.get(it["id"], [])
        if hs:
            L.append("- 处理历史：")
            for h in hs:
                note = f"｜备注：{h['note']}" if h["note"] else ""
                L.append(f"  - {h['created_at']} {STATUS_LABEL.get(h['from_status'], '')}"
                         f" → {STATUS_LABEL.get(h['to_status'], '')}{note}")
        L.append("")
    md = "\n".join(L)
    return Response(content=md.encode("utf-8"),
                    media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"})


# ---------------- API：设置 ----------------

class SettingsBody(BaseModel):
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    key = s.get("llm_api_key") or ""
    return {
        "llm_base_url": s.get("llm_base_url") or "",
        "llm_api_key": mask_key(key),
        "llm_model": s.get("llm_model") or "",
        "key_masked": bool(key),
    }


@app.post("/api/settings")
def set_settings(body: SettingsBody):
    cur = load_settings()
    save_settings({
        "llm_base_url": body.llm_base_url.strip(),
        "llm_api_key": resolve_key(body.llm_api_key, cur.get("llm_api_key") or ""),
        "llm_model": body.llm_model.strip(),
    })
    return get_settings()


@app.post("/api/settings/test")
def test_settings(body: SettingsBody):
    """测试 LLM 连接：用当前表单配置发一个最小请求，返回明确成功/失败原因"""
    base = body.llm_base_url.strip().rstrip("/")
    key = body.llm_api_key.strip()
    model = body.llm_model.strip()
    if "****" in key:  # 表单里是脱敏占位，取已保存的真实 Key
        key = (load_settings().get("llm_api_key") or "").strip()
    if not base:
        return {"ok": False, "message": "接口地址为空，请先填写"}
    if not key:
        return {"ok": False, "message": "API Key 为空，请先填写"}
    if not model:
        return {"ok": False, "message": "模型名为空，请先选择或输入"}
    try:
        resp = requests.post(
            base + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model,
                  "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 5},
            timeout=30)
    except requests.exceptions.Timeout:
        return {"ok": False, "message": "连接超时（30秒）：请检查接口地址与网络是否可达"}
    except Exception as e:
        return {"ok": False, "message": f"连接失败：{e}"}
    if resp.status_code == 200:
        return {"ok": True, "message": f"测试成功：接口与 API Key 均有效，模型 {model} 可正常调用"}
    hint = ""
    try:
        m = requests.get(base + "/models",
                         headers={"Authorization": f"Bearer {key}"}, timeout=15)
        if m.status_code == 200:
            names = [x.get("id") for x in m.json().get("data", []) if isinstance(x, dict)]
            if names:
                hint = "。该接口可用的模型：" + "、".join(str(n) for n in names[:20])
    except Exception:
        pass
    return {"ok": False,
            "message": f"接口返回 HTTP {resp.status_code}：{resp.text[:150]}{hint}"}


app.mount("/", StaticFiles(directory=str(BASE / "static"), html=True), name="static")
