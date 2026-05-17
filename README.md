# 文档智能门禁

一个基于 Flask + SQLite 的文档检查网站，支持用户上传 docx、pdf、txt、md、html 文档，并通过管理员配置的 OpenAI Chat Completions 兼容模型执行规范性、一致性、错别字和跨文档一致性检查。

## 功能概览

- 用户面：创建文档检查和一致性检查任务、查看当前 IP 的任务、取消任务、删除历史任务、显示当前 IP 与管理员标识的用户名。
- 管理面：隐藏 URL 登录，查看和管理全部任务，维护 IP 用户标识与禁用状态，配置模型提供商、模型列表、检查项提示词、扩展检查项和任务并发度。
- 任务执行：后台线程从 SQLite 队列拉取任务，默认全局并发 3、单 IP 并发 1、单任务检查项并发 1，可在管理面调整。
- 本地存储：SQLite 数据库、上传文件和运行日志保存在 `instance/`，本地管理员配置和模型提供商配置保存在 `config.local.json`。
- 服务运行：使用 gevent WSGI 单进程运行，并在启动入口执行 monkey patch 以提升 I/O 并发吞吐。

## 快速启动

```bash
uv sync
uv run python run.py
```

`uv sync` 会按 `pyproject.toml` 和 `uv.lock` 创建/更新 `.venv`，后续启动统一使用 `uv run`。

用户面地址：

```text
http://127.0.0.1:5000/
```

首次启动会自动生成 `config.local.json`，默认管理员为 `admin / admin123`，管理入口默认是：

```text
http://127.0.0.1:5000/_gate_ops_9f2c7a/login
```

上线或给他人使用前，请修改 `config.local.json` 中的管理员密码、`secret_key` 和 `admin_url`。

## 本地配置

`config.local.json` 支持配置管理入口、监听地址和端口：

```json
{
  "secret_key": "请替换为随机长字符串",
  "admin": {
    "username": "admin",
    "password": "请替换为强密码"
  },
  "admin_url": "/_gate_ops_9f2c7a",
  "server": {
    "host": "0.0.0.0",
    "port": 5000
  }
}
```

`admin_url` 可以写成 `/_gate_ops_9f2c7a` 或 `_gate_ops_9f2c7a`，启动时会自动规范为合法路径。临时启动时也可以用 `HOST`、`PORT` 环境变量覆盖本地配置。

模型提供商配置也保存在 `config.local.json` 的 `providers` 列表中，管理页面的交互不变；新增、编辑、删除提供商都会直接更新本地配置文件，不写入 SQLite。

## 模型配置

进入管理面后，在“模型管理”页面创建提供商：

- API 地址填写完整 OpenAI Chat Completions 请求地址，例如 `https://api.example.com/v1/chat/completions`。
- API Key 可为空，非空时会以 `Authorization: Bearer ...` 发送。
- 代理模式支持直连、系统代理和自定义代理。默认直连；系统代理模式会读取系统代理环境变量；自定义代理模式使用管理员填写的代理地址。
- SSL 校验按提供商单独设置，默认关闭；开启后会校验 HTTPS 证书。
- 请求超时时间按提供商单独设置，默认 3600 秒。
- 单次请求文本上限按提供商单独设置，默认 80000 字。
- 模型列表每行一个模型名称，创建任务时会展示给用户选择。

## 检查流程

1. 用户或管理员在“文档检查”页面上传文档，选择模型和检查项后提交。
2. 系统先保存上传文件，再按文档类型提取可检查文本：
   - `docx`：提取段落和表格文本。
   - `pdf`：提取 PDF 页面中的文本层。
   - `txt`、`md`：按文本文件读取。
   - `html`：去除脚本和样式后提取页面文本。
3. 如果无法提取文本，或提取后的文本超过所选模型提供商的文本上限，系统会拒绝提交任务，并删除本次上传文件。
4. 校验通过后，任务进入队列，后台调度器按全局并发和单 IP 并发限制执行。
5. 执行时会按检查项并发调用模型，并持续写入结果和进度；单任务检查项并发数可在管理面调整。
6. 任务完成后可在任务列表进入报告页查看结果；完成状态的任务支持导出 HTML 报告。

“一致性检查”页面支持上传 1-3 个素材文档和 1-3 个资料文档。资料通常是根据素材文档写作生成的，系统会以素材文档作为依据，调用所选模型检查资料内容是否存在口径不一致、遗漏、偏差或需要人工确认的内容，并输出报告。

当前不会对图片内容做 OCR 或视觉理解。文档中嵌入的图片、截图、扫描版 PDF 图片内容不会被检查；只有能被提取出的文字会参与检查。

## 本地日志

运行日志同时输出到 console 和 `instance/logs/app.log`。日志文件单个最大 5MB，最多保留 3 个文件（当前文件和 2 个历史文件）。日志会记录任务 ID、模型名称、请求 ID、HTTP 状态、OpenAI Chat Completions 流式帧数量、`finish_reason`、`usage`、空响应诊断和截断后的响应帧样本，不记录 API Key 和完整文档内容。

如果页面出现“模型服务没有返回可用内容”，优先查看该日志中同一个 `request_id` 的记录，判断服务是否只返回了 `reasoning_content`、是否触发 `content_filter`、是否返回了 200 状态的错误 JSON，或是否根本没有输出 SSE 数据帧。

## 本地文件

- `instance/document_check.sqlite3`：SQLite 数据库。
- `instance/uploads/`：上传文档。
- `instance/logs/app.log`：本地运行日志。
- `config.local.json`：本地管理员账号、密码、隐藏管理入口、监听地址、启动端口、密钥和模型提供商配置。

以上文件均被 `.gitignore` 忽略，不应提交到仓库。
