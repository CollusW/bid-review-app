# 标书智能审查系统

AI 驱动的投标文件审查工具，基于规则引擎 + LLM 语义审查双轨制，帮助审查人员快速发现标书中的格式、技术、商务、完整性等问题。

## 功能

- **规则引擎审查**：内置硬规则（R101~R202），检查格式规范、必备章节、签章页码等
- **LLM 语义审查**：6 类检查任务（否决项/图表/技术参数/商务/完整性/格式），基于招标文件逐项核对
- **招标文件核对**：上传招标文件后自动提取必备资料清单，与标书交叉核对
- **实时进度反馈**：审查过程中显示当前检查项、已完成项数/总项数、每项结果
- **支持格式**：PDF / DOCX / DOC / TXT（.doc 解析依赖本机 Microsoft Word）
- **报告导出**：审查完成后可导出 Markdown 报告

## 快速开始

### 环境要求

- **Python 3.10+**（[下载](https://www.python.org/downloads/)，安装时务必勾选 "Add Python to PATH"）
- **Microsoft Word**（可选，用于解析 .doc 格式招标文件）
- **API Key**（[Moonshot 开放平台](https://platform.moonshot.cn/console/api-keys)注册获取）

### 启动

1. 双击 `启动.bat`
2. 首次启动自动安装依赖（约 1-2 分钟）
3. 浏览器自动打开 `http://127.0.0.1:8000`
4. 首次使用自动弹出设置框，填写 API Key 并测试连通性
5. 新建项目 → 上传标书/招标文件 → 点击审查

### 停止

关闭命令行窗口即停止服务。

## 配置说明

### API Key

- 存储于 `data/settings.json`（首次启动从 `data/settings.json.template` 自动复制）
- 在系统右上角"设置"按钮中填写，支持任意 OpenAI 兼容接口
- 充值地址：[Moonshot 控制台](https://platform.moonshot.cn/console/account)

### 配置文件结构

```json
{
  "llm_base_url": "https://api.moonshot.cn/v1",
  "llm_api_key": "你的 API Key",
  "llm_model": "kimi-k2.6"
}
```

## 技术架构

- **后端**：FastAPI + SQLite + Uvicorn
- **前端**：原生单页应用（无框架依赖，全内联）
- **文档解析**：PyMuPDF（PDF）/ python-docx（DOCX）/ win32com（DOC）
- **LLM 调用**：requests 直连 OpenAI 兼容接口

### 依赖

```
fastapi>=0.100.0
uvicorn>=0.20.0
pydantic>=2.0.0
python-multipart>=0.0.6
requests>=2.28.0
PyMuPDF>=1.23.0
python-docx>=1.0.0
pywin32>=305
```

## 常见问题

**Q: 启动时提示"未检测到 Python"？**
A: 未安装 Python 或安装时未勾选 "Add to PATH"。请重新安装并勾选。

**Q: 启动时提示安装依赖失败？**
A: 检查网络连接，或手动执行：`python -m pip install -r requirements.txt`

**Q: 审查时报错"API Key 无效"？**
A: 点击右上角"设置"检查 Key 是否正确，或前往 Moonshot 控制台充值。

**Q: .doc 招标文件无法解析？**
A: 用 Word 打开另存为 .docx 或 .pdf 后重新上传。

**Q: 审查过程很慢？**
A: 正常现象，AI 审查约需 10-20 分钟，请耐心等待。期间会显示检查进度。

## 许可

私有项目，未授权不得分发。
