# 文档智能门禁

一个基于 Flask + SQLite 的文档智能门禁网站，支持用户上传 docx、pdf、txt、md、html、xlsx、xlsm、xls 文档，并通过管理员配置的 OpenAI Chat Completions 兼容模型执行单文档规范性、一致性、错别字检查和多文档对照检查。

## 功能概览

- 用户面：创建单文档检查和多文档对照检查任务、查看当前用户的任务、取消任务、删除历史任务，支持用 SSO 用户主体或 IP 兜底身份归属任务。
- 管理面：隐藏 URL 登录，查看和管理全部任务，维护用户主体标识与禁用状态，配置模型提供商、模型列表、检查项提示词、扩展检查项和任务并发度。
- 任务执行：后台线程从 SQLite 队列拉取任务，默认全局并发 3、单用户并发 1、单任务检查项并发 1，可在管理面调整；文档文本会作为全文一次送入模型，超过所选模型文本上限时拒绝提交或执行失败。
- 本地存储：SQLite 数据库、上传文件和运行日志保存在 `instance/`，本地管理员配置和模型提供商配置保存在 `config.yaml`。
- 服务运行：使用 gevent WSGI 单进程运行，并在启动入口执行 monkey patch 以提升 I/O 并发吞吐。

## 快速启动

```bash
uv sync
uv run python run.py
```

`uv sync` 会按 `pyproject.toml` 和 `uv.lock` 创建/更新 `.venv`，后续启动统一使用 `uv run`。

默认本机管理视图地址：

```text
http://127.0.0.1:31945/
```

首次启动没有配置文件时，会自动生成非平台模式的 `config.yaml`，默认管理员为 `admin / admin123`。非平台模式下根路径直接进入管理视图，无需登录。

```text
http://127.0.0.1:31945/
```

上线或给他人使用前，请修改 `config.yaml` 中的管理员密码、`secret_key` 和 `admin_url`。

## Windows 打包

在 Windows 打包机上运行：

```bat
scripts\build_windows.bat
```

脚本会使用 uv 同步 build 依赖并调用 PyInstaller，生成单文件可执行程序：

```text
dist\windows\DocumentCheck.exe
```

把 `DocumentCheck.exe` 发给其他 Windows 用户即可运行，对方不需要安装 Python 或项目依赖。双击后会启动本地服务并自动打开浏览器进入本机管理视图。首次启动会在 exe 同目录生成非平台模式的 `config.yaml` 和 `instance/`；上传文件、SQLite 数据库和日志也会保存在同目录的 `instance/` 中。

注意：PyInstaller 不能从 Linux/macOS 交叉打包 Windows exe，以上脚本需要在 Windows 上运行。交付前建议先在打包机运行一次，修改 exe 同目录的 `config.yaml` 中的管理员密码、`secret_key`、管理入口、端口和模型提供商配置，再把 exe 及需要预置的本地配置一起交付。

## 本地配置

`config.yaml` 支持配置运行模式、管理入口、监听地址和端口。仓库中提供两份示例：

```text
config.platform.example.yaml
config.non-platform.example.yaml
```

选择对应模式的示例复制为 `config.yaml` 后再修改真实账号、密码、密钥和模型提供商配置。

平台服务模式示例：

```yaml
platform: true
secret_key: 请替换为随机长字符串
admin:
  username: admin
  password: 请替换为强密码
admin_url: /console
server:
  host: 0.0.0.0
  port: 31945
auth:
  mode: ip
  trusted_header:
    user: ""
    name: ""
    email: ""
providers: []
```

本机非平台模式示例：

```yaml
platform: false
secret_key: 请替换为随机长字符串
admin:
  username: admin
  password: 请替换为强密码
admin_url: /console
server:
  host: 127.0.0.1
  port: 31945
auth:
  mode: ip
  trusted_header:
    user: ""
    name: ""
    email: ""
providers: []
```

`platform` 默认为 `false`，首次启动没有配置文件时会生成非平台模式配置：服务只监听 `127.0.0.1`，根路径直接进入管理视图，无需登录，不显示用户管理入口；该模式下 `HOST` 环境变量会被忽略，`PORT` 仍可临时覆盖端口。设置为 `true` 时进入平台服务模式：用户面和管理面分离，管理面需要登录，可按配置或环境变量监听指定地址。

`admin_url` 可以写成 `/console` 或 `console`，启动时会自动规范为合法路径。平台服务模式下临时启动时也可以用 `HOST`、`PORT` 环境变量覆盖本地配置。

## 用户身份与 SSO 预留

系统内部使用 `owner_subject` 作为任务归属，不再把 IP 当成唯一用户身份。默认 `auth.mode: ip` 时，用户主体为 `ip:<访问 IP>`，适合本机或尚未接入 SSO 的部署；IP 仍会记录在任务中用于审计。

如果公司网关或反向代理已经完成 SSO 登录，并能把登录用户写入可信请求头，可以改为：

```yaml
auth:
  mode: trusted_header
  trusted_header:
    user: X-SSO-User
    name: X-SSO-Name
    email: X-SSO-Email
```

此时系统会把 `X-SSO-User` 解析为 `sso:<账号>`，优先用 `X-SSO-Name` 或 `X-SSO-Email` 作为显示名称。只有在该服务位于可信 SSO 网关之后、外部用户无法伪造这些请求头时才应启用该模式。后续如果公司提供 OIDC、SAML 或 CAS 接口，也只需要把认证回调解析出的账号映射到同一个用户主体格式即可，任务、并发、禁用和统计逻辑不需要再按 IP 改一遍。

模型提供商配置也保存在 `config.yaml` 的 `providers` 列表中，管理页面的交互不变；新增、编辑、删除提供商都会直接更新本地配置文件，不写入 SQLite。

## 模型配置

进入管理面后，在“模型管理”页面创建提供商：

- API 地址填写完整 OpenAI Chat Completions 请求地址，例如 `https://api.example.com/v1/chat/completions`。
- API Key 可为空，非空时会以 `Authorization: Bearer ...` 发送。
- 代理模式支持直连、系统代理和自定义代理。默认直连；系统代理模式会读取系统代理环境变量；自定义代理模式使用管理员填写的代理地址。
- SSL 校验按提供商单独设置，默认关闭；开启后会校验 HTTPS 证书。
- 请求超时时间按提供商单独设置，默认 3600 秒。
- 单次请求文本上限按提供商单独设置，默认 80000 字。
- 模型列表使用表格维护，可手动新增、整理，也可从当前 API 地址拉取模型后在弹窗中选择加入。
- 每个模型可单独开启“强制关闭思考”。开启后，请求该模型时会附加 `enable_thinking=false`，并同时写入 `chat_template_kwargs: {"enable_thinking": false}`，用于关闭部分思考模型的思考模式；不支持这些参数的服务可能忽略或返回错误。

## 检查流程

1. 用户或管理员在“单文档检查”页面上传文档，选择模型和检查项后提交。
2. 系统先保存上传文件，再按文档类型提取可检查文本：
   - `docx`：提取段落和表格文本。
   - `pdf`：提取 PDF 页面中的文本层。
   - `txt`、`md`：按文本文件读取。
   - `html`：去除脚本和样式后提取页面文本。
   - `xlsx`、`xlsm`、`xls`：按工作表逐行提取单元格文本。
3. 如果无法提取文本，系统会拒绝提交任务，并删除本次上传文件。单文档检查会把文件名和提取出的全文一起发送给模型，不再按片段拆分；文本超过所选模型提供商的文本上限时会拒绝提交。
4. 校验通过后，任务进入队列，后台调度器按全局并发和单用户并发限制执行。
5. 执行时会按检查项并发调用模型，并持续写入结果和进度；单任务检查项并发数可在管理面调整。
6. 任务完成后可在任务列表进入报告页查看结果；完成状态的任务支持导出 HTML 报告。

## 文档上传处理

普通“单文档检查”任务会参考 Cherry Studio 的处理方式：上传文件先在本地提取为文本，再以 `file: 文件名` 加全文内容的形式放入模型提示词中。系统不会把长文档拆成多个片段，也不会把原始文件直接转发给模型。

所选模型提供商的“单次请求文本上限”用于控制全文请求大小。提取后的文本加文件名超过上限时，单文档检查会拒绝提交；“多文档对照检查”也会在合并素材文档和资料文档后做同样的上限校验。

“多文档对照检查”页面支持上传 1-5 个素材文档和 1-3 个资料文档。资料通常是根据素材文档写作生成的，系统会以素材文档作为依据，调用所选模型检查资料内容是否存在口径不一致、遗漏、偏差或需要人工确认的内容，并输出报告。多文档对照项可在系统设置中单独维护，支持修改内置提示词、新增扩展检查项、停用、删除和排序；提交任务时会保存所选检查项快照，后续修改提示词不会影响已提交任务。

当前不会对图片内容做 OCR 或视觉理解。文档中嵌入的图片、截图、扫描版 PDF 图片内容不会被检查；只有能被提取出的文字会参与检查。

## 本地日志

运行日志同时输出到 console 和 `instance/logs/app.log`。日志文件单个最大 5MB，最多保留 3 个文件（当前文件和 2 个历史文件）。日志会记录任务 ID、模型名称、请求 ID、HTTP 状态、OpenAI Chat Completions 流式帧数量、`finish_reason`、`usage`、空响应诊断和截断后的响应帧样本，不记录 API Key 和完整文档内容。

如果页面出现“模型服务没有返回可用内容”，优先查看该日志中同一个 `request_id` 的记录，判断服务是否只返回了 `reasoning_content`、是否触发 `content_filter`、是否返回了 200 状态的错误 JSON，或是否根本没有输出 SSE 数据帧。

## 本地文件

- `instance/document_check.sqlite3`：SQLite 数据库。
- `instance/uploads/`：上传文档。
- `instance/logs/app.log`：本地运行日志。
- `config.yaml`：本地管理员账号、密码、隐藏管理入口、监听地址、启动端口、密钥和模型提供商配置。

以上文件均被 `.gitignore` 忽略，不应提交到仓库。
