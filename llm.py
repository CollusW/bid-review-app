# -*- coding: utf-8 -*-
"""LLM 语义审查模块（可选）：OpenAI 兼容接口，未配置时自动跳过
V1.3 改造：
1. 【招标文件核对】新增 run_tender_review()：把招标文件正文分块，逐块作为"要求原文"
   喂给大模型与标书对照，输出"标书 vs 招标文件"不一致问题（一标书一规则，无需手工粘贴）；
2. 原"招标要求"文本框升级为"补充检查项"：build_system_prompt 支持
   extra_rules（补充检查项）+ tender_snippet（招标文件开头参考）双注入，一并传给模型综合检查；
3. temperature 兼容：部分模型（如 kimi-k2.6）强制 temperature=1，遇 400 自动降级重试；
4. 调用失败可见化：分块失败原因返回给上层写入 summary，不再静默吞掉。
@author WorkBuddy-GLM, Collus Wang
"""
import json
import re
import requests

SYSTEM_PROMPT_TEMPLATE = """你是资深投标文件审查专家。你将收到两部分内容：
【审查依据】本次项目招标/询比价文件的要求（必要时附补充检查项），是本次审查的唯一依据。
【标书文本】我方投标/响应文件，按页标注。

请【以审查依据为唯一标准，逐条对照标书】检查：
1. 资料完整性：审查依据中列出的必备资料/文件，标书是否全部提供？缺失的逐条列出。
2. 技术要求符合性：审查依据中的技术参数、指标、功能、性能要求，标书是否逐条响应？响应内容是否满足要求？正偏离/负偏离标注是否与内容一致？
3. 商务符合性：交货期、质保、付款方式、服务等商务条款是否响应？
4. 标书自身质量：格式问题（错别字、语句不通、编号错误、前后表述不一致）、完整性问题（章节有标题无内容、表格空行）。

只输出一个 JSON 数组，不要任何其他文字、不要markdown代码块。每个元素格式：
{"title":"问题标题(20字内)","description":"问题说明","page":页码数字,"evidence":"标书原文引用(60字内)","suggestion":"修改建议","severity":"high|medium|low","category":"格式|技术|商务|完整性"}

若该片段没有发现与审查依据相悖或自身质量问题，输出 []。宁可漏报也不要编造页码或原文。"""

TENDER_SYSTEM_PROMPT = """你是资深投标文件审查专家。下面【招标文件原文】是本次项目的招标/询比价文件的一部分，其中包含多项具体要求（应提交资料清单、技术参数、商务条款、★重要条款等）。
请从本部分原文中逐条识别要求，并逐项核对【标书内容】是否满足：
1. 资料完整性：要求提交的资料/文件，标书是否提供？
2. 技术符合性：技术参数/指标/功能/性能是否响应并满足要求？
3. 商务符合性：交货期、质保、付款方式、服务等商务条款是否响应？
4. ★重要条款：是否逐条响应？未响应即视为重大风险。

只输出一个 JSON 数组，不要任何其他文字、不要markdown代码块。每个元素格式：
{"title":"问题标题(20字内)","requirement":"对应的招标要求原文摘录(60字内)","description":"标书与招标要求的差异说明(120字内)","page":页码数字,"evidence":"标书原文引用(60字内)","suggestion":"修改建议","severity":"high|medium|low","category":"格式|技术|商务|完整性"}

标书完全满足本部分全部要求时输出 []。宁可漏报也不要编造页码或原文。"""

VALID_SEVERITY = {"high", "medium", "low"}
VALID_CATEGORY = {"格式", "技术", "商务", "完整性", "疑似"}

MAX_RULES_CHARS = 20000   # 补充检查项 / 招标文件参考注入上限
MAX_TENDER_BLOCK_CHARS = 12000  # 招标文件每块字符上限
MAX_TENDER_BLOCKS = 6           # 招标文件最多核对块数（超出截断并如实说明）
MAX_BID_CONTEXT = 30000         # 招标核对时标书内容截断上限（控制单次调用长度）
MAX_LLM_BLOCKS = 2              # 标书语义审查最多分块数（每块≤18000字符，超出截断并如实说明）

# 标书语义审查的固定检查类别：每个类别 = 一个检查任务 = 一次 LLM 调用
# （拆成小任务后单次调用聚焦单一类别，输出更短更快，且界面可实时显示"正在检查X"）
CHECK_TASKS = [
    ("否决项与★重要条款",
     "重点逐条核对招标文件中的★重要条款、否决项/废标项（如：必备资料缺失即废标、大小写金额不一致废标、★参数不响应废标等）在标书中是否全部响应。任何一条遗漏都是重大风险，必须逐条列出。"),
    ("图表、图片与附件",
     "重点检查标书中的图表、图片、附件、附录：是否存在图表标题存在但内容缺失、图片空白或无法辨认、附件/附录未提供或未编号、表格有空行等问题。"),
    ("技术参数与指标",
     "重点核对技术参数、性能指标、功能要求是否逐条响应且数值满足招标要求（如频段、功率、接口、防护等级等），响应表中正偏离/负偏离/无偏离标注是否与正文内容一致。"),
    ("商务条款",
     "重点核对交货期、质保期、付款方式、售后服务、培训等商务条款是否明确响应，三处（商务偏离表/技术方案/售后服务承诺）表述是否一致，有无前后矛盾。"),
    ("必备资料与完整性",
     "重点检查招标文件要求的必备资料/证明文件/资格文件标书是否全部提供，章节是否完整、有无“标题在但正文为空”的悬空章节。"),
    ("格式与自身质量",
     "重点检查格式规范、错别字、语句不通、编号错误、前后表述不一致、占位符残留（XXXXXX/待填）、版本号不统一等标书自身质量问题。"),
]


def build_system_prompt(extra_rules="", tender_snippet="", focus=""):
    """组装标书语义审查 system prompt：
    focus=本次检查任务的重点类别；extra_rules=用户填写的补充检查项；
    tender_snippet=招标文件开头参考。三者一并注入。
    都为空且无焦点时明确告知模型不要臆测。"""
    sys_prompt = SYSTEM_PROMPT_TEMPLATE
    parts = []
    if focus and focus.strip():
        parts.append("【本次检查任务的重点】\n" + focus.strip()[:500])
    if extra_rules and extra_rules.strip():
        parts.append("【补充检查项（用户要求重点核对）】\n" + extra_rules.strip()[:MAX_RULES_CHARS])
    if tender_snippet and tender_snippet.strip():
        parts.append("【招标文件开头参考】\n" + tender_snippet.strip()[:MAX_RULES_CHARS]
                     + "\n（此为招标文件开头部分内容，供对照参考）")
    if parts:
        sys_prompt += "\n\n" + "\n\n".join(parts)
    else:
        sys_prompt += "\n\n【审查依据】未提供。请仅检查标书自身的格式与完整性问题，不要臆测任何招标要求。"
    return sys_prompt


def _chat_once(base, key, model, sys_prompt, content, temperature, timeout, max_tokens=None):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    return requests.post(
        base + "/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=payload,
        timeout=timeout,
    )


def ping(base, key, model, timeout=10):
    """快速连通性预检：发一个最小请求（max_tokens=1）。
    用于审查启动前确认 LLM 配置可用，避免无效配置导致审查长时间无响应。
    返回 (ok: bool, message: str)"""
    base = (base or "").strip().rstrip("/")
    key = (key or "").strip()
    model = (model or "").strip()
    if not base:
        return False, "接口地址为空"
    if not key:
        return False, "API Key 为空"
    if not model:
        return False, "模型名为空"
    try:
        resp = requests.post(
            base + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model,
                  "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 1},
            timeout=timeout)
    except requests.exceptions.Timeout:
        return False, "连接超时（10秒）：请检查接口地址与网络"
    except Exception as e:
        return False, f"连接失败：{e}"
    if resp.status_code == 200:
        return True, "连接正常"
    return False, f"接口返回 HTTP {resp.status_code}：{resp.text[:120]}"


def _chat(base, key, model, sys_prompt, content, timeout=180):
    """调用 chat/completions；遇参数不兼容（HTTP 400）自动降级重试：
    temperature 限制 → 降为 1；max_tokens 不支持 → 去掉该参数。"""
    resp = _chat_once(base, key, model, sys_prompt, content, 0.1, timeout, max_tokens=4096)
    if resp.status_code == 400:
        text = resp.text or ""
        if "temperature" in text:
            resp = _chat_once(base, key, model, sys_prompt, content, 1, timeout, max_tokens=4096)
        elif "max_tokens" in text or "max_completion" in text or "max_token" in text:
            resp = _chat_once(base, key, model, sys_prompt, content, 0.1, timeout, max_tokens=None)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _chunk_pages(pages, size):
    """把页文本按字符上限切块，返回 list[str]"""
    blocks, cur, cur_chars = [], [], 0
    for t in pages:
        piece = t
        if cur and cur_chars + len(piece) > size:
            blocks.append(cur)
            cur, cur_chars = [], 0
        cur.append(piece)
        cur_chars += len(piece)
    if cur:
        blocks.append(cur)
    return ["\n".join(b) for b in blocks]


def run_tender_review(settings, tender_pages, bid_pages, progress_cb=None):
    """【招标文件核对】把招标文件正文分块，逐块作为要求原文与标书对照，输出不一致问题
    返回 (issues, msg, detail)：
      issues: list[dict] 核对出的问题（rule_id="LLM-T", source="llm"）
      msg:    供 summary 展示的一句话
      detail: dict 调用明细（总块/核对块/成功/失败/标书是否截断）
    """
    base = (settings.get("llm_base_url") or "").rstrip("/")
    key = settings.get("llm_api_key") or ""
    model = settings.get("llm_model") or ""
    sys_prompt = TENDER_SYSTEM_PROMPT

    blocks = _chunk_pages(tender_pages, MAX_TENDER_BLOCK_CHARS)
    total = len(blocks)
    checked = min(total, MAX_TENDER_BLOCKS)

    # 标书内容：截断控制单次调用长度
    full_bid = "\n".join(bid_pages)
    bid_truncated = len(full_bid) > MAX_BID_CONTEXT
    bid_text = full_bid[:MAX_BID_CONTEXT]
    if bid_truncated:
        bid_text += f"\n……（标书内容较长，此处仅截取前 {MAX_BID_CONTEXT} 字符；核对以招标要求原文为准，可结合人工补充核对后部章节）"

    issues, failed, errs = [], 0, []
    for idx, blk in enumerate(blocks[:checked]):
        if progress_cb:
            progress_cb(45 + int(25 * idx / max(1, checked)),
                        f"正在核对招标文件第 {idx + 1}/{checked} 部分的要求 ...",
                        task_begin="招标文件逐项核对",
                        task_pos=f"要求原文 第 {idx + 1}/{checked} 部分")
        n_items = 0
        try:
            content = _chat(
                base, key, model, sys_prompt,
                f"【招标文件原文（第{idx + 1}部分）】\n{blk}\n\n【标书内容】\n{bid_text}")
            items = _parse_json_array(content)
            n_items = len(items)
            for it in items:
                issue = _validate(it)
                if issue:
                    req = str(it.get("requirement", "")).strip()[:120]
                    if req:
                        issue["description"] = f"【招标要求】{req}\n" + issue["description"]
                    issue["rule_id"] = "LLM-T"
                    issue["source"] = "llm"
                    issues.append(issue)
        except Exception as e:
            failed += 1
            errs.append(f"第{idx + 1}部分: {str(e)[:120]}")
            if progress_cb:
                progress_cb(45 + int(25 * (idx + 1) / max(1, checked)),
                            f"✕ 招标文件第 {idx + 1} 部分核对失败（已完成 {idx + 1}/{checked}）",
                            task_end=-1, task_error=str(e)[:200])
            continue
        if progress_cb:
            if n_items:
                progress_cb(45 + int(25 * (idx + 1) / max(1, checked)),
                            f"✓ 招标文件第 {idx + 1} 部分核对完成：发现 {n_items} 条不一致"
                            f"（已完成 {idx + 1}/{checked}）",
                            task_end=n_items)
            else:
                progress_cb(45 + int(25 * (idx + 1) / max(1, checked)),
                            f"✓ 招标文件第 {idx + 1} 部分核对完成：本部分要求均已满足"
                            f"（已完成 {idx + 1}/{checked}）",
                            task_end=0)

    msg = f"招标文件核对：核对 {checked} 个部分，产出 {len(issues)} 条不一致问题"
    if total > MAX_TENDER_BLOCKS:
        msg += f"（招标文件共 {total} 部分，超出上限仅核对前 {MAX_TENDER_BLOCKS} 部分）"
    if failed:
        msg += f"，{failed} 个部分失败：" + "；".join(errs[:3])
    detail = {
        "blocks": total,
        "checked": checked,
        "ok": checked - failed,
        "failed": failed,
        "issues": len(issues),
        "errors": errs[:5],
        "bid_truncated": bid_truncated,
    }
    return issues, msg, detail


def run_llm_review(settings, parsed, extra_rules="", tender_snippet="", progress_cb=None):
    """对标书做 LLM 语义审查，按【检查类别 × 标书分块】拆成小任务队列逐个执行：
    - 每个任务 = 一次 LLM 调用（聚焦单一类别，输出短、速度快）
    - 界面实时显示：正在检查哪一类、已完成 X/Y、每项完成是否发现问题
    返回 (issues, msg, detail)：
      issues: list[dict] 审查出的问题（rule_id="LLM", source="llm"）
      msg:    供 summary 展示的一句话
      detail: dict 调用明细（任务数/成功/失败/覆盖块数等）
    """
    base = (settings.get("llm_base_url") or "").rstrip("/")
    key = settings.get("llm_api_key") or ""
    model = settings.get("llm_model") or ""

    # 标书分块（每块≤18000字符），超过 MAX_LLM_BLOCKS 块只审前几块并如实说明
    chunks, cur, cur_chars = [], [], 0
    for i, t in enumerate(parsed["pages"]):
        piece = f"【第{i+1}页】\n{t}"
        if cur and cur_chars + len(piece) > 18000:
            chunks.append(cur)
            cur, cur_chars = [], 0
        cur.append(piece)
        cur_chars += len(piece)
    if cur:
        chunks.append(cur)
    total_blocks = len(chunks)
    blocks = chunks[:MAX_LLM_BLOCKS]
    blocks_cut = total_blocks > MAX_LLM_BLOCKS

    # 任务队列：类别 × 块
    tasks = [(name, focus, bi, blk) for bi, blk in enumerate(blocks)
             for name, focus in CHECK_TASKS]
    total = len(tasks)
    issues, failed, err_details = [], 0, []
    done = 0
    for name, focus, bi, blk in tasks:
        pos = f"标书 第{bi + 1}/{len(blocks)}部分"
        sys_prompt = build_system_prompt(extra_rules, tender_snippet, focus)
        if progress_cb:
            progress_cb(75 + int(18 * done / max(1, total)),
                        f"正在检查“{name}”（{pos}，已完成 {done}/{total}）...",
                        task_begin=name, task_pos=pos)
        n_items = 0
        try:
            content = _chat(base, key, model, sys_prompt, "\n".join(blk))
            items = _parse_json_array(content)
            n_items = len(items)
            for it in items:
                issue = _validate(it)
                if issue:
                    issue["rule_id"] = "LLM"
                    issue["source"] = "llm"
                    issues.append(issue)
        except Exception as e:
            failed += 1
            err_details.append(f"任务“{name}”（{pos}）: {str(e)[:120]}")
            if progress_cb:
                progress_cb(75 + int(18 * (done + 1) / max(1, total)),
                            f"✕ “{name}”检查失败（已完成 {done + 1}/{total}）",
                            task_end=-1, task_error=str(e)[:200])
            done += 1
            continue
        if progress_cb:
            if n_items:
                progress_cb(75 + int(18 * (done + 1) / max(1, total)),
                            f"✓ “{name}”检查完成：发现 {n_items} 条问题（已完成 {done + 1}/{total}）",
                            task_end=n_items)
            else:
                progress_cb(75 + int(18 * (done + 1) / max(1, total)),
                            f"✓ “{name}”检查无问题（已完成 {done + 1}/{total}）",
                            task_end=0)
        done += 1

    msg = f"LLM 审查：{total} 个检查任务，产出 {len(issues)} 条语义问题"
    if blocks_cut:
        msg += f"（标书共 {total_blocks} 部分，超出上限仅审查前 {MAX_LLM_BLOCKS} 部分）"
    if failed:
        msg += f"，{failed} 个任务失败：" + "；".join(err_details[:3])
    injected = []
    if extra_rules and extra_rules.strip():
        injected.append("补充检查项")
    if tender_snippet and tender_snippet.strip():
        injected.append("招标文件参考")
    if injected:
        msg += "（已注入：" + "+".join(injected) + "）"
    else:
        msg += "（未提供审查依据，仅做标书自身质量检查）"
    detail = {
        "tasks": total,
        "tasks_done": done,
        "ok": done - failed,
        "failed": failed,
        "issues": len(issues),
        "errors": err_details[:5],
        "blocks_total": total_blocks,
        "blocks_used": len(blocks),
        "blocks_cut": blocks_cut,
        "extra_rules_injected": bool(extra_rules and extra_rules.strip()),
        "tender_snippet_injected": bool(tender_snippet and tender_snippet.strip()),
    }
    return issues, msg, detail


def _parse_json_array(content):
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.S)
    s, e = content.find("["), content.rfind("]")
    if s == -1 or e == -1 or e <= s:
        return []
    try:
        data = json.loads(content[s:e + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _validate(it):
    if not isinstance(it, dict):
        return None
    title = str(it.get("title", "")).strip()
    if not title:
        return None
    severity = str(it.get("severity", "medium")).strip()
    if severity not in VALID_SEVERITY:
        severity = "medium"
    category = str(it.get("category", "技术")).strip()
    if category not in VALID_CATEGORY:
        category = "技术"
    try:
        page = int(it.get("page", 0) or 0)
    except (TypeError, ValueError):
        page = 0
    return {
        "category": category,
        "severity": severity,
        "title": title[:60],
        "description": str(it.get("description", "")).strip()[:500],
        "page": page,
        "evidence": str(it.get("evidence", "")).strip()[:300],
        "suggestion": str(it.get("suggestion", "")).strip()[:500],
        "source": "llm",
    }
