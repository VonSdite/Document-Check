# 文档智能门禁

一个基于 Flask + SQLite 的文档智能门禁网站，支持用户上传 docx、pdf、txt、md、html、xlsx、xlsm、xls 文档和常见视频文件，并通过用户自行配置的 OpenAI Chat Completions 兼容模型执行单文档规范性、易理解性、全文一致性、内容完整性检查，以及多文档对照、跨语种文档一致性、多模态图片检查和视频抽帧检查。

## 功能概览

- 用户面：创建单文档检查、多文档对照检查、跨语种文档一致性检查和图片检查任务、维护自己的模型提供商和模型 ID、测试模型连通性、查看当前用户的任务、取消任务、删除历史任务，支持用 SSO 用户主体或 IP 兜底身份归属任务。
- 跨语种检查：输入两个不同语种或不同语言版本的资料文档，系统先静态抽取长度、语种、标题、数字、版本、URL、邮箱、IP 等硬线索，再调用大模型输出中文差异报告，重点识别内容不一致、缺失、增补和翻译偏差。
- 管理面：隐藏 URL 登录，查看和管理全部任务，配置检查项提示词、扩展检查项和任务并发度；用户身份和用户模型配置由用户侧管理。
- 任务执行：独立调度进程从 SQLite 队列拉取任务，默认全局并发 3、单用户并发 1、单任务检查项并发 1，可在管理面调整；机器级任务进程上限默认为 4；文档文本会作为全文一次送入模型，图片检查会把文档文本和按位置命名的图片批次一起送入多模态模型。
- 本地存储：SQLite 数据库、用户模型配置、上传文件、提取图片和运行日志保存在 `instance/`，本地管理员配置保存在 `config.yaml`。
- 服务运行：Windows 和 Linux 统一使用 Uvicorn 和 a2wsgi 适配 Flask，默认 1 个 Web 进程、每进程 16 个请求线程；后台任务由独立调度进程按配置动态创建任务进程，启动命令保持为 `uv run python run.py`。

完整运行拓扑和组件职责见 [4+1 架构视图](docs/architecture-4plus1.md)。

## 代码结构

`app/` 使用 Python 命名空间包，根目录只包含模块目录。应用工厂位于 `app.bootstrap.factory`，启动入口为 `run.py`。

| 模块 | 职责 |
| --- | --- |
| `app/bootstrap/` | Web 应用装配、监督器与任务进程入口 |
| `app/contracts/` | 任务类型、数量和输出限制等公共约束 |
| `app/infrastructure/` | 本地配置、网络、日志、文件操作和进程应用上下文 |
| `app/persistence/` | SQLite 连接、表结构初始化、设置和默认检查项 |
| `app/identity/` | 用户身份数据、IP/请求头认证和 SAML 适配 |
| `app/models/` | 模型配置、模型发现和推理客户端 |
| `app/documents/` | 文档文本与链接提取、图片处理和视频抽帧 |
| `app/checks/` | 检查项目录、词表检查、链接校验、语种分析和证据规则 |
| `app/tasks/` | 任务提交、调度认领、执行、进度和文件生命周期 |
| `app/reporting/` | 报告解析、人工复核、统计缓存和 Excel 数据处理 |
| `app/web/` | HTTP 路由、请求与响应适配、模板和静态资源 |

模块依赖和扩展方式见 [开发与模块边界](docs/development.md)。数据库查询、并发配置和压测方法见 [性能与容量验证](docs/performance.md)。

## 快速启动

运行环境为 Python 3.12 及以上版本，支持 Windows 和 Linux。两端统一使用 Uvicorn 与 a2wsgi，采用相同的进程、线程配置和启动方式。

```bash
uv sync
uv run python run.py
```

`uv sync` 会按 `pyproject.toml` 和 `uv.lock` 创建/更新 `.venv`，后续启动统一使用 `uv run`。

`server.web_workers` 默认值为 `1`，`server.web_threads` 默认值为 `16`。已有配置中的显式值优先；单进程运行时将本地 `config.yaml` 的 `server.web_workers` 设为 `1`，保存后重启服务。

视频检查还需要在运行本服务的服务器或容器内安装 `ffmpeg` 和 `ffprobe`，并确保应用进程的 `PATH` 可以找到它们；`uv sync` 不会安装这两个系统级可执行程序。

启动时自动初始化数据库和索引，并在接收请求前补齐已有数据库缺少的性能索引。索引补齐保持业务表、字段和记录不变；仅在补建索引时对相关表执行一次 `ANALYZE`。首次补建需要读取已有数据，启动耗时随数据量增加；索引完整后，索引检查只读取元数据。

运行依赖在 `pyproject.toml` 中直接声明，并由 `uv.lock` 固定版本：Flask 提供 Web 应用，Uvicorn 与 a2wsgi 提供跨平台多进程 Web 服务及 Flask 请求线程池，psutil 提供跨平台进程存活检测，Werkzeug 提供代理中间件与 HTTP 异常，MarkupSafe 提供 HTML 标记类型，`concurrent-log-handler` 与 `portalocker` 提供多进程安全日志和监督器单实例锁，文档解析、模型请求和报表处理依赖其对应的文档、网络和表格库。

`sqlite3`、`multiprocessing` 等模块由 Python 标准库提供。PDF 图片读取使用 `pypdf[image]` 声明的 Pillow 依赖；SAML 使用 `python3-saml` 声明的 lxml 与 xmlsec 依赖。

默认本机管理视图地址：

```text
http://127.0.0.1:31945/
```

首次启动没有配置文件时，会自动生成非平台模式的 `config.yaml`，默认管理员为 `admin / admin123`。非平台模式下根路径直接进入管理视图，无需登录。

```text
http://127.0.0.1:31945/
```

上线或给他人使用前，请修改 `config.yaml` 中的管理员密码、`secret_key` 和 `admin_url`。

## 本地配置

`config.yaml` 支持配置运行模式、管理入口、监听地址和端口。仓库中提供两份示例：

```text
config.platform.example.yaml
config.non-platform.example.yaml
```

选择对应模式的示例复制为 `config.yaml` 后再修改真实账号、密码和密钥。

平台服务模式示例：

```yaml
# 平台服务模式：适合部署到服务器或让局域网/公司入口访问。
# 管理入口需要登录；auth.mode 按公司身份服务选择 ip、trusted_header 或 saml。
platform: true
secret_key: 请替换为随机长字符串
admin:
  username: admin
  password: 请替换为强密码
admin_url: /console
server:
  # 0.0.0.0 表示监听所有网卡；也可以改成服务器指定内网 IP。
  # 对外开放前请务必修改 admin.password、secret_key 和 admin_url。
  host: 0.0.0.0
  port: 31945
  # 如果通过 Nginx 子路径发布，例如 https://example.com/infoCheck，请填写 /infoCheck；根路径发布则留空。
  url_prefix: ""
  # 如果 Nginx 会覆盖并注入真实客户端 IP，例如 proxy_set_header X-Real-IP $remote_addr，可填写 X-Real-IP。
  # 只有确认外部用户无法绕过 Nginx 直连本服务时才启用。
  real_ip_header: X-Real-IP
  # 如果 Nginx 同时注入 X-Forwarded-Proto/Host/Prefix 等标准代理头，可开启。
  proxy_fix: false
  max_upload_mb: 1024
  # Windows 和 Linux 统一使用 Uvicorn；web_workers 控制 Web 进程数。
  # web_threads 控制每个 Web 进程的 Flask 请求线程数。
  web_workers: 1
  web_threads: 16
worker:
  # 管理页面中的“系统同时执行任务数”不能超过该机器级上限。
  max_task_processes: 4
logging:
  # 控制台级别：INFO、WARNING、ERROR、CRITICAL；文件固定记录 INFO 及以上日志。
  console_level: WARNING
network:
  # 系统出站代理模式，控制本服务访问模型 API、拉取模型列表、测试模型连通性等所有对外请求。
  # 可选值：direct、system、custom。direct 为直连；system 读取本机 HTTP_PROXY/HTTPS_PROXY 等环境变量；custom 使用下面 proxy。
  proxy_mode: direct
  # proxy 仅在 proxy_mode: custom 时填写，例如 http://127.0.0.1:7890；用户模型配置里不允许再填写代理。
  proxy: ""
  # 是否校验 HTTPS 证书；默认 false，适合内网或自签名证书服务。公网正式证书环境建议改为 true。
  ssl_verify: false
auth:
  # 可选值：ip、trusted_header、saml。默认先用 ip，确认公司 SSO 接入方式后再切换。
  # ip：按访问 IP 区分用户；trusted_header：从可信网关注入的 HTTP header 取用户。
  # saml：直接对接 SAML 2.0。
  # 不同 mode 的用户数据相互隔离：ip:<IP>、trusted_header:<用户ID>、saml:<用户ID>。
  # 平台 ip 模式可在管理员后台给 IP 设置显示用户名；SSO 模式不会显示该入口。
  mode: ip
  # mode: trusted_header 时填写；只有公司网关已完成 SSO 并注入可信 header 才使用。
  trusted_header:
    # user_id 是“唯一用户 ID”所在的 HTTP header 名称，例如 X-SSO-User-Id；不要填姓名。
    user_id: ""
    # username 是“显示名”所在的 HTTP header 名称，例如 X-SSO-User-Name；可为空，空时显示 user_id。
    username: ""
  # mode: saml 时填写；公司 SSO 是 SAML 2.0 且本系统直接对接时使用。
  saml:
    # sp_entity_id 是本系统作为 SP 的唯一标识，通常可使用 https://你的域名/auth/saml/metadata。
    sp_entity_id: ""
    # acs_url 是公司 SSO 登录后 POST 回调本系统的地址，必须是外部可访问的 https://你的域名/auth/saml/acs。
    acs_url: ""
    # idp_entity_id 是公司 SSO 作为 IdP 的唯一标识，由公司 SSO 管理员提供。
    idp_entity_id: ""
    # idp_sso_url 是公司 SSO 的登录跳转地址，由公司 SSO 管理员提供。
    idp_sso_url: ""
    # idp_x509_cert 是公司 SSO 用于签名 SAML 响应的公钥证书内容，不是私钥。
    idp_x509_cert: ""
    # user_id_attribute 是 SAML Attribute 中稳定唯一用户 ID 的字段名；留空时使用 SAML NameID。
    user_id_attribute: ""
    # username_attribute 是 SAML Attribute 中显示名的字段名；留空时显示 user_id。
    username_attribute: ""
```

本机非平台模式示例：

```yaml
# 本机非平台模式：适合单机使用，根路径直接进入管理视图，无需管理员登录。
# 出于安全考虑，程序在 platform: false 时会强制监听 127.0.0.1。
platform: false
secret_key: 请替换为随机长字符串
admin:
  username: admin
  password: 请替换为强密码
admin_url: /console
server:
  # platform: false 时这里即使改成 0.0.0.0 或其他 IP，启动时也会被强制为 127.0.0.1。
  # 如果需要局域网或服务器访问，请改用 config.platform.example.yaml 的 platform: true。
  host: 127.0.0.1
  port: 31945
  url_prefix: ""
  real_ip_header: ""
  proxy_fix: false
  max_upload_mb: 1024
  # Windows 和 Linux 统一使用 Uvicorn；web_workers 控制 Web 进程数。
  # web_threads 控制每个 Web 进程的 Flask 请求线程数。
  web_workers: 1
  web_threads: 16
worker:
  # 管理页面中的“系统同时执行任务数”不能超过该机器级上限。
  max_task_processes: 4
logging:
  # 控制台级别：INFO、WARNING、ERROR、CRITICAL；文件固定记录 INFO 及以上日志。
  console_level: WARNING
network:
  # 系统出站代理模式，控制本服务访问模型 API、拉取模型列表、测试模型连通性等所有对外请求。
  # 可选值：direct、system、custom。direct 为直连；system 读取本机 HTTP_PROXY/HTTPS_PROXY 等环境变量；custom 使用下面 proxy。
  proxy_mode: direct
  # proxy 仅在 proxy_mode: custom 时填写，例如 http://127.0.0.1:7890；用户模型配置里不允许再填写代理。
  proxy: ""
  # 是否校验 HTTPS 证书；默认 false，适合内网或自签名证书服务。公网正式证书环境建议改为 true。
  ssl_verify: false
auth:
  # 可选值：ip、trusted_header、saml。默认先用 ip，本机模式通常不需要切换。
  # ip：按访问 IP 区分用户；trusted_header：从可信网关注入的 HTTP header 取用户。
  # saml：直接对接 SAML 2.0。
  # 不同 mode 的用户数据相互隔离：ip:<IP>、trusted_header:<用户ID>、saml:<用户ID>。
  # 平台 ip 模式可在管理员后台给 IP 设置显示用户名；SSO 模式不会显示该入口。
  mode: ip
  # mode: trusted_header 时填写；只有公司网关已完成 SSO 并注入可信 header 才使用。
  trusted_header:
    # user_id 是“唯一用户 ID”所在的 HTTP header 名称，例如 X-SSO-User-Id；不要填姓名。
    user_id: ""
    # username 是“显示名”所在的 HTTP header 名称，例如 X-SSO-User-Name；可为空，空时显示 user_id。
    username: ""
  # mode: saml 时填写；公司 SSO 是 SAML 2.0 且本系统直接对接时使用。
  saml:
    # sp_entity_id 是本系统作为 SP 的唯一标识，通常可使用 https://你的域名/auth/saml/metadata。
    sp_entity_id: ""
    # acs_url 是公司 SSO 登录后 POST 回调本系统的地址，必须是外部可访问的 https://你的域名/auth/saml/acs。
    acs_url: ""
    # idp_entity_id 是公司 SSO 作为 IdP 的唯一标识，由公司 SSO 管理员提供。
    idp_entity_id: ""
    # idp_sso_url 是公司 SSO 的登录跳转地址，由公司 SSO 管理员提供。
    idp_sso_url: ""
    # idp_x509_cert 是公司 SSO 用于签名 SAML 响应的公钥证书内容，不是私钥。
    idp_x509_cert: ""
    # user_id_attribute 是 SAML Attribute 中稳定唯一用户 ID 的字段名；留空时使用 SAML NameID。
    user_id_attribute: ""
    # username_attribute 是 SAML Attribute 中显示名的字段名；留空时显示 user_id。
    username_attribute: ""
```

`platform` 默认为 `false`，首次启动没有配置文件时会生成非平台模式配置：服务只监听 `127.0.0.1`，根路径直接进入管理视图，无需登录；该模式下 `server.host` 和 `HOST` 环境变量都会被忽略，`PORT` 仍可临时覆盖端口。设置为 `true` 时进入平台服务模式：用户面和管理面分离，管理面需要登录，可按配置或环境变量监听指定地址。

`admin_url` 可以写成 `/console` 或 `console`，启动时会自动规范为合法路径。平台服务模式下临时启动时也可以用 `HOST`、`PORT` 环境变量覆盖本地配置。

通过 Nginx 等反向代理部署时，可按实际网关配置维护 `server` 下的代理字段：

- `server.url_prefix`：本服务挂在域名子路径下时填写，例如外部访问路径为 `/infoCheck` 就填 `/infoCheck`；根路径发布留空。
- `server.real_ip_header`：当 Nginx 使用 `proxy_set_header X-Real-IP $remote_addr;` 覆盖并注入真实客户端 IP 时填写 `X-Real-IP`。该字段会影响 `auth.mode: ip` 的用户归属、任务审计 IP、统计和并发控制，必须保证外部用户不能绕过 Nginx 直连 Flask 服务，也不能伪造同名请求头。
- `server.proxy_fix`：只有在 Nginx 注入并覆盖 `X-Forwarded-Proto`、`X-Forwarded-Host`、`X-Forwarded-Prefix` 等标准代理头时开启，用于修正 Flask 看到的协议、域名和前缀。

## 系统出站网络配置

`network` 控制本服务所有对外请求，包括拉取模型列表、测试模型连通性和后台执行检查任务。用户只能填写模型提供商、API 地址、API Key、超时、文本上限和模型 ID，不能在自己的模型配置里指定代理或 SSL 校验。

- `network.proxy_mode`：`direct` 为直连，默认值；`system` 读取运行本服务的机器上的 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 等环境变量；`custom` 使用 `network.proxy` 指定的代理地址。
- `network.proxy`：仅 `proxy_mode: custom` 时填写，例如 `http://127.0.0.1:7890`；其他模式会忽略该字段。
- `network.ssl_verify`：统一控制 HTTPS 证书校验，默认 `false`。如果模型服务使用公网正式证书，建议改为 `true`；如果是内网或自签名证书，可保持 `false`。

这些字段也可以在管理端“系统设置”里修改。保存后系统会直接写回本地 `config.yaml`，并立即更新当前进程配置；不会写入 SQLite。

## 用户身份与 SSO

系统使用 `owner_subject` 作为任务归属。配置提供 `ip`、`trusted_header` 和 `saml` 三种 `auth.mode`，默认值为 `ip`。不同模式使用独立用户命名空间，模型配置、任务列表和统计概览按当前用户主体隔离。IP 始终记录在任务中用于审计。

- `ip`：不接 SSO，用户主体为 `ip:<访问 IP>`。
- `trusted_header`：公司网关或反向代理已经完成 SSO 登录，并把用户 ID/用户名注入可信 HTTP header，用户主体为 `trusted_header:<用户ID>`。
- `saml`：公司 SSO 是 SAML 2.0，本系统直接作为 SAML SP 对接，用户主体为 `saml:<用户ID>`。

无论用户从 `/` 用户入口还是从 `admin_url` 对应的 console 入口创建任务、选择模型或进入“模型管理”，系统都会按同一套 `auth.mode` 解析当前用户，使用同一个 `owner_subject` 读写该用户自己的模型配置。

平台模式且 `auth.mode: ip` 时，管理员后台“系统设置”会显示“IP 用户标记”，可以给 IP 设置显示用户名。该设置只改变页面显示和统计展示，不改变认证身份；统计概览会优先显示映射用户名，未设置时回退显示 IP。`trusted_header` 和 `saml` 模式不会显示这个入口。

系统支持 SAML 2.0；SAML 1.0/1.1 不在支持范围内。

常用字段含义：

- `trusted_header.user_id`：保存唯一用户 ID 的 HTTP header 名称，例如 `X-SSO-User-Id`，用于生成 `owner_subject = trusted_header:<用户ID>`。
- `trusted_header.username`：保存显示名的 HTTP header 名称，例如 `X-SSO-User-Name`，只用于页面显示和任务快照，可为空。
- `saml.sp_entity_id`：本系统作为 SP 的唯一标识，通常用 `https://你的域名/auth/saml/metadata`。
- `saml.acs_url`：公司 SSO 登录成功后 POST 回调本系统的地址，通常是 `https://你的域名/auth/saml/acs`。
- `saml.idp_entity_id`：公司 SSO 作为 IdP 的唯一标识，由公司 SSO 管理员提供。
- `saml.idp_sso_url`：公司 SSO 登录跳转地址，由公司 SSO 管理员提供。
- `saml.idp_x509_cert`：公司 SSO 用来签名 SAML 响应的公钥证书内容，不是私钥。
- `saml.user_id_attribute`：SAML Attribute 中稳定唯一用户 ID 的字段名，留空时使用 SAML `NameID`。
- `saml.username_attribute`：SAML Attribute 中显示名的字段名，留空时显示用户 ID。

`trusted_header` 只有在公司已有统一网关或反向代理，并且网关已经完成 SSO 登录、能把登录用户写入可信请求头时才需要；直接对接 SAML 2.0 时不需要配置它。网关模式可以这样写：

```yaml
auth:
  mode: trusted_header
  trusted_header:
    user_id: X-SSO-User-Id
    username: X-SSO-User-Name
```

此时系统会把 `X-SSO-User-Id` 解析为 `trusted_header:<用户ID>`，用 `X-SSO-User-Name` 作为显示名；用户入口缺少 `X-SSO-User-Id` 时返回 401。该模式要求服务位于可信 SSO 网关之后，网关负责清理外部请求中的同名 header。本系统保存任务归属快照、审计 IP、统计和并发控制所需的用户主体。

实际接入时按下面顺序操作：

1. 向公司 SSO 管理员确认是否已有统一网关或反向代理能在登录后注入请求头，并确认“唯一用户 ID”和“显示名”分别对应哪个 header，例如 `X-SSO-User-Id`、`X-SSO-User-Name`。
2. 将本服务部署在该网关之后，禁止用户绕过网关直连 Flask 服务；网关转发前应清理外部请求自带的同名 header，再写入可信 header。
3. 把 `config.yaml` 的 `platform` 设为 `true`，`auth.mode` 设为 `trusted_header`，并按公司网关实际 header 名称填写 `trusted_header`。
4. 访问用户入口验证任务归属：提交任务后，管理端任务列表应显示 `trusted_header:<账号>` 和显示名称。
5. 管理员入口仍使用本系统 `admin.username`、`admin.password` 和 `admin_url` 登录；但在 console 内创建任务和管理模型时，仍会使用与 `/` 相同的 SSO 用户身份。若网关默认保护全部路径，需要让网关对 `admin_url` 放行或单独做管理员访问控制；建议同时限制为内网、VPN 或管理员来源 IP。

如果公司 SSO 提供的是 SAML 2.0，并且没有现成网关负责把 SAML 转成可信 header，可以让本系统作为 SAML SP 直接对接：

```yaml
auth:
  mode: saml
  saml:
    sp_entity_id: https://文档门禁域名/auth/saml/metadata
    acs_url: https://文档门禁域名/auth/saml/acs
    idp_entity_id: 公司 SSO 提供的 IdP Entity ID
    idp_sso_url: 公司 SSO 提供的 SSO 登录地址
    idp_x509_cert: |
      公司 SSO 提供的签名证书内容
    user_id_attribute: uid
    username_attribute: displayName
```

SAML 接入时需要把下面信息交给公司 SSO 管理员：SP Entity ID、ACS URL、SP metadata URL（`https://文档门禁域名/auth/saml/metadata`），并请对方把稳定唯一用户 ID 映射到 `user_id_attribute`，把显示名映射到 `username_attribute`。如果 `user_id_attribute` 留空，系统会使用 SAML `NameID` 作为用户 ID；不建议使用姓名作为用户 ID，因为同名用户无法区分。SAML 登录成功后会存为 `owner_subject = saml:<用户ID>`。管理员入口继续使用本系统本地管理员账号密码，不需要在公司 SSO 里设置管理员；console 内涉及当前用户的任务提交和模型配置时，仍使用同一个 SAML 用户身份。

咨询公司 SSO 管理员时可以直接发送下面这段：

```text
我们要把“文档智能门禁”接入公司 SSO，用于识别普通用户并按用户 ID 归属任务；管理员入口仍使用系统本地管理员账号，不需要通过 SSO 授权管理员，但 console 内创建任务和管理模型时仍使用当前 SSO 用户身份。

请帮忙确认公司 SSO 支持哪种接入方式：
1. 是否有统一网关/反向代理可先完成 SSO 登录，再向后端注入可信 HTTP header？如果可以，我们倾向使用 trusted_header。
2. 如果不能注入 header，是否支持 SAML 2.0？如果支持，我们使用 SAML 2.0 SP。

我们需要给你们的信息：
- 系统访问域名：https://文档门禁域名
- 管理员入口：/console，是否需要从 SSO 网关放行请一起确认
- trusted_header 模式：请告知你们希望注入的 header 名称；我们需要唯一用户 ID 和显示名
- SAML 2.0 模式：SP Entity ID = https://文档门禁域名/auth/saml/metadata，ACS URL = https://文档门禁域名/auth/saml/acs，Metadata URL = https://文档门禁域名/auth/saml/metadata

请你们提供给我们的信息：
- 推荐接入模式：trusted_header / SAML 2.0
- 唯一用户 ID 字段：不能是姓名，要稳定且唯一，例如工号、账号 ID、uid
- 显示名字段：例如 displayName、cn、name
- trusted_header 模式：用户 ID header 名称、显示名 header 名称，并确认网关会清理外部伪造的同名 header
- SAML 2.0 模式：IdP Entity ID、SSO 登录地址、X509 签名证书、用户 ID Attribute、显示名 Attribute
- 是否要求 HTTPS、内网/VPN、回调域名白名单、证书轮换周期
```

## 模型配置

进入用户侧“模型管理”页面，或在 console 中进入“模型管理”，都可以创建自己的模型提供商。两处使用同一个当前用户主体，模型配置按该用户主体存入 SQLite；`config.yaml` 保存系统级本地配置。平台提交任务时只会使用当前用户自己启用的模型。

- API 地址填写完整 OpenAI Chat Completions 请求地址，例如 `https://api.example.com/v1/chat/completions`。
- API Key 可为空，非空时会以 `Authorization: Bearer ...` 发送。
- 代理和 SSL 校验不允许用户单独配置，统一由 `config.yaml` 的 `network` 控制。
- 请求超时时间按提供商单独设置，默认 3600 秒，限制连接和读取等待；持续收到流式数据时可以超过该时长。整个任务没有统一的总执行时限。
- 模型开启思考时，无正文的思考响应达到 64,000 段或 128,000 字符会提前中止，保留防止持续思考的保护；关闭思考时对应限制为 64 段或 256 字符。纯思考异常的关闭思考重试按模型服务兼容规则执行。
- 单次请求文本上限按提供商单独设置，默认 80000 字。
- 模型 ID 列表使用表格维护，可手动新增、整理，也可从当前 API 地址拉取模型后在弹窗中选择加入。
- 每个模型 ID 行都有“测试”按钮，用于从平台服务端按当前 API 地址、API Key、系统出站网络配置和模型 ID 发起一次 Chat Completions 连通性测试。
- 模型默认保留思考能力；每个模型仍可单独开启“强制关闭思考”。开启后，系统统一写入 `enable_thinking=false`、`thinking: {"type": "disabled"}` 和 `chat_template_kwargs: {"enable_thinking": false, "thinking": false}`，兼容使用不同关闭参数的模型服务。

## 检查流程

用户端和管理端的五类检查页面采用相同的检查项选择规则：只有一个可用检查项时默认选中；有多个时，首次使用需主动选择，之后恢复上次提交的选择。页面显示已选数量，支持全选和清空，提交时至少选择一项。选择记录按用户和检查类型保存在当前浏览器，新增检查项需主动选择。

1. 用户或管理员在“单文档检查”“多文档对照检查”或“图片检查”页面上传文档，选择当前用户已配置的模型和检查项后提交。
2. 系统先保存上传文件，再按文档类型提取可检查文本：
   - `docx`：提取段落和表格文本，并将链接显示文字对应的超链接目标地址写入抽取文本。
   - `pdf`：使用 PyMuPDF 提取 PDF 页面文本层并保留 pypdf 质量回退，同时基于字符坐标清理视觉上不存在的重叠空格，并提取页面链接注释中的目标地址。对于有文字层和矢量边框的表格，系统还会使用 PyMuPDF `find_tables()`、文字坐标和绘图边框重建行列及横向/纵向合并关系，将表格按页面位置放回正文阅读顺序。
   - `txt`、`md`：按文本文件读取，并提取 Markdown 中的超链接目标地址。
   - `html`：去除脚本和样式后提取页面文本，并保留超链接目标地址。
   - `xlsx`、`xlsm`、`xls`：按工作表逐行提取单元格文本，并保留单元格超链接目标地址。`xlsx`、`xlsm` 使用一个只读工作簿，在同一次单元格解析中读取缓存值与公式，按行列范围定位超链接，并跳过连续空白区域。
3. 如果无法提取文本，或提取后的文本超过所选模型提供商的“单次请求文本上限”，系统会拒绝提交任务，并删除本次上传文件。该上限同时适用于单文档、多文档对照和跨语种检查。
4. 校验通过后，任务进入队列；独立任务调度进程在 SQLite 事务中原子认领任务，按全局并发、单用户并发和机器级任务进程上限执行。监督器每 10 秒为存活任务续约，将有效期延长至当前时间后的 90 秒。存活且受管理的任务持续占用并发名额，进程确认退出后才允许重新排队；未受管理的失联任务按租约到期时间恢复。
5. 执行时会按检查项并发处理，并持续写入结果和进度；普通 AI 检查项会调用模型，单文档“敏感词检查”和“常用词检查”会读取本地词表做确定性匹配，不调用模型；图片检查仅支持 PDF，会生成页面截图并提取内嵌图片，按“页面级检查”和“图片资源检查”分组合并调用多模态模型，每批最多 4 张图，并只附带当前页附近的文档文本；单任务检查项并发数可在管理面调整。
6. 任务完成后可在任务列表进入报告页查看结果；完成状态的任务支持导出 HTML 和 Excel 报告。Excel 报告中的条目判定、是否接纳和不接纳原因提供下拉选项，离线标注后可在原报告页通过“回填 EXCEL”上传，系统校验任务与条目标识后批量回填人工复核结果。

用户端和管理端任务列表会分别展示“检查状态”和“标注进度”。标注进度按报告中实际可操作的条目计算：选择“认可”或“不认可”后视为该条已标注，仅修改问题、建议或非问题判定但仍为“未确认”时不计为完成；已忽略误报和受报告上限省略的条目不计入分母。列表显示未标注、标注中、已标注或无可标注条目，管理端还可按标注状态筛选。报告在新标签页完成标注后，返回列表会自动执行一次轻量刷新。

系统会缓存每个任务的报告条目统计和标注进度，仅在报告结果或误报规则变化后重新计算；任务运行期间及返回列表时的轻量刷新只读取当前页任务的状态、进度和统计缓存，不重复加载文档全文或报告正文。

用户界面和 console 的五类任务列表均支持按检查状态、标注状态、关键词组合筛选。用户筛选范围限定为自己的任务；关键词按输入文字包含匹配文件名（含对照任务中的各文件）、用户、账号或 IP，`%` 和 `_` 按普通字符匹配。点击“筛选”或按回车应用条件。分页、每页条数和浏览器前进后退保留筛选条件。筛选与分页只更新列表，保留上传文件、模型及检查项选择、列宽、仍可操作的勾选项和滚动位置；结果减少时保留当前视口所需的列表高度。

有活动任务时，任务列表和详情页每 10 秒局部更新。列表统一显示“排队、解析、检查、取消中、完成、部分完成、失败、已取消”；等待模型、思考、输出、重试和整理结果统一归为“检查”。详情页在每个检查项标题旁用小字显示当前阶段，检查项结束后移除小字。后台标签页暂停轮询，返回页面时同步状态；详情页保留滚动位置、展开内容和视频播放器，选中文本或编辑报告控件时暂缓替换内容。状态更新失败会显示提示并自动重试。任务列表的自动刷新状态旁提供刷新图标，暂停自动刷新或没有活动任务时，也可以手动更新当前筛选结果。

详情页在文档检查、多文档对照和跨语种对照的检查项标题旁提供“取消执行”，支持任务排队、解析文件、检查项待执行和模型请求期间取消。点击按钮后在旁边的气泡中确认；确认后显示“状态：取消中”，该项结束后隐藏状态文字。待执行项跳过检查，进行中的模型请求关闭连接，其他检查项继续执行。本地检查在返回时确认取消并记录结果。成功结果保留；成功与取消并存时任务为“部分完成”，全部检查项取消时为“已取消”。图片、视频保持多检查项合并请求，通过任务列表取消整个任务。任务列表的“重试”和详情页的“重试未完成项”批量执行失败或主动取消的检查项，保留成功结果与标注；任务全部取消后也可重试。任务提前失败或取消时，原执行范围内尚未生成结果的项一并补跑。批量和单项重试均沿用任务快照中的模型名称、接口地址、超时、思考设置和检查提示，服务无法提供快照模型时记录失败。

模型请求常规失败时最多自动重试两次，分别在失败后等待 2 秒和 4 秒；等待期间支持取消。

检查项取消后或执行失败时，标题旁显示“重试”。点击后仅重新执行该项，沿用任务快照中的模型名称、接口地址、超时、思考设置和检查提示；服务无法提供原模型时，该项继续记录失败。进行中的文档任务在旧请求退出后使用原线程池重试，其他项继续执行；已结束任务通过调度队列重试单项，保留其他项结果和标注。任务结束后访问密钥会被清除，重试时从原提供商读取密钥；原提供商凭据不可用时显示错误。图片和视频在任务结束后提供单项重试。重试请求成功后，页面立即清空该项旧错误和输出，保持展开状态；旧执行迟到的响应通过执行编号隔离。

每个模型检查项的内容顶部提供通用的“模型输出内容”折叠区，分开显示服务返回的思考和正文。展示按 OpenAI Chat Completions 兼容响应字段识别，适用于不同模型名称、自定义别名和新模型；服务返回 `reasoning_content`、`reasoning`、`reasoning_text` 或 `reasoning_details` 时展示思考，`content` 展示正文，仅返回正文时显示“未返回思考内容”。展开后约每秒读取新增文本并追加；每项仅展示最新一次模型请求的思考与正文，新请求替换该项旧输出，图片与视频标注合并请求。活动任务在折叠时仅同步轻量检查状态，以便自动重试时及时清空旧错误和输出；后台标签页暂停同步，报告仍每 10 秒局部更新。页面保留输出区的展开状态和滚动位置，位于底部时跟随新增文本。未保存思考的任务显示已有报告正文，本地规则检查直接展示检查报告。

任务列表按类型读取展示所需的元数据，多文档任务保留分组和原文件下载信息。轮询对已结束且统计过期的任务按主键检查报告是否存在；有效缓存直接返回复核进度。原文件可用性根据当前文件状态判断。

报告页支持人工复核每条 AI 检查条目，可标记为问题、建议或非问题，并记录是否接纳。若条目被标记为“非问题 + 不接纳”，且不接纳原因为“模型幻觉”“模型误报”或“不适用”，系统会自动生成一条误报忽略候选规则；管理员可在系统设置中启用该规则。启用后，后续检查中完全相同的条目会折叠到“已忽略误报”中，不参与主报告统计，并记录规则命中次数。

## 文档上传处理

任务租约用于失联恢复。文档解析和任务执行可以持续超过 90 秒，监督器独立维护租约。取消信号由任务线程每秒读取，PDF 按页和表格行、Excel 按行检查取消；监督器观察到取消后给予 10 秒退出时间，再发送终止信号，3 秒后仍未退出则强制结束。并发名额在进程确认退出后释放。

监督器通过独立的 `app.bootstrap.task` 模块启动每个任务，在 `instance/task-supervisor.json` 中记录 PID、创建时间和租约令牌，完成记录后通过标准输入授予执行许可。Windows 和 Linux 使用相同的任务启动协议。状态文件短暂被占用时进行有限重试；启动许可仅在身份保存成功后发送，终止与退出收尾在状态写入失败时继续执行。服务重新启动时先核验并清理记录中的遗留进程，再恢复对应任务。

任务日志分别记录进程创建、进入入口、运行环境初始化、业务就绪和预处理耗时。进程创建后 60 秒内须确认业务就绪；启动超时或就绪前异常退出时，监督器先确认进程退出，再将任务标为失败并显示具体阶段或退出码，可通过页面重试。业务就绪后的文档处理沿用任务租约和各检查项的超时设置。

普通“单文档检查”任务会在任务进程中提取文本，再以 `file: 文件名` 加全文内容的形式一次性放入模型提示词，不做长文本分段。PDF 文本优先使用 PyMuPDF，文本为空或包含异常字符时按页使用 pypdf 回退；表格识别仅处理包含候选向量边线的页面。图片检查只提取 PDF 文本上下文，不执行表格结构化，随后单独生成页面截图和读取内嵌图片。视频按时间轴采样，最多同时执行两路 `ffmpeg` 抽帧并保持采样顺序。系统不会把原始文件直接转发给模型。

普通文本检查会明确告知模型：抽取文本不代表原文的全部视觉内容，图片、图标、矢量图形、公式和表格版式可能未进入输入；不得仅因抽取结果中没有这些对象就报告原文缺失。“见图”“见表”等引用本身也不能证明对象不存在，只有原文明确包含 `TODO`、`TBD`、待插入、待补充等占位证据时才能据此报告。后端和报告展示还会过滤缺少这类直接证据的图片、图标或表格对象缺失结论，但不会过滤表格字段、单位、图题或编号等有文本证据的具体问题，也不会过滤多模态图片检查根据页面画面得出的结论。

PDF 结构化表格会展开为固定行列的归一化 HTML 后放入模型上下文；原始合并范围记录在 `data-original-range`，展开位置填入相同文字并通过 `data-inherited-from` 指向原始锚点，因此便于模型逐行理解，又不会把继承值当作原文重复填写或独立数据。表格区域原始线性文字会从普通正文中移除，避免结构化表格与正文重复。真正的空单元格、非文本图形和未能可靠还原的位置分别标记，只有高置信度表格中不属于合并继承、明确标记为空、能够定位到表格 ID 和锚点坐标，并且上下文证明必须填写时，模型才允许报告数据缺失；后端会再次核对原始合并关系和坐标。无文字层的扫描 PDF 不在该规则解析范围内。

PDF 表格使用已有字符边界索引定位文字候选，空白区域直接进入空单元格判定；有文字候选时复用单页文本对象进行原生提取。图片和有效绘图按中心坐标索引，单元格只检查相邻候选。文档解析与页面并发的测试方法和测量结果见 [性能与容量验证](docs/performance.md)。

“超链接有效性检查”是独立的本地规则检查项，不调用大模型。系统会检查超链接目标格式、内部书签/页面/工作表目标，以及 HTTP/HTTPS 链接的受控可达性；相同链接只检测一次并合并引用位置。HTTP 404/410、目标缺失和不安全协议可判定为问题；登录权限、超时、DNS、证书、限流、服务端故障和安全策略拦截仅标记为需人工确认。为防止服务端请求伪造，规则不会访问回环、私网、链路本地或保留地址，且不会在报告或日志中暴露敏感查询参数。

单文档内置 AI 检查项按边界拆分为“文档规范性检查”“易理解性检查”“全文一致性检查”和“内容完整性检查”。规范性检查覆盖错别字、漏字、多字、标点、明确语法、术语书写、数字单位格式、客户表达、结构层级、编号引用、技术信息呈现和发布残留。易理解性只处理内容已提供但难以理解的问题；全文一致性跨章节核对结构引用、名称术语、事实参数、条件步骤、结论和约束口径；完整性只处理能够由上下文证明为必需但尚未提供的信息。超链接格式、HTTP 状态、重定向、网络可达性、证书和访问权限由独立的“超链接有效性检查”处理。文中引用外部资料且已给出超链接、URL、文件路径或明确获取入口时，完整性检查按入口信息进行判断。四项均优先使用章节路径定位，页码仅作为抽取文本中明确存在时的辅助线索。

所选模型提供商的“单次请求文本上限”是单文档全文检查的硬上限；每个 AI 检查项均使用完整文档发起一次请求。单文档文本请求不发送 `max_completion_tokens` 参数，输出长度由模型服务自身能力决定；多文档对照、跨语种、图片和视频请求使用对应的输出 Token 限制。

系统设置中的“每次模型回复最多问题条数”会随任务结果保存。结构化报告要求模型为每条结果提供严重程度和证据可信度；后端会先合并内容相同、仅位置不同的重复问题，再按严重程度、证据可信度和条目类型排序，最后按该上限展示。原始模型回复仍会完整保留；如果模型遗漏分级字段，系统会保留全部条目并在报告中提示。

报告条目被判定为“非问题”、选择“不接纳”，且原因为“模型幻觉”“模型误报”或“不适用”时，系统会生成误报忽略候选规则。管理员可在系统设置中按问题描述、检查项、问题类型、位置或原因即时搜索规则，并按候选或已启用状态筛选；启用后，规则会在相同任务类型和检查项内按问题描述进行相似匹配，描述相似度达到 56% 的条目会折叠到“已忽略误报”，不参与主报告统计，原始条目仍保留供查看。

单文档检查内置“敏感词检查”。请将公司内部词表放到 `instance/sensitive_terms.xlsx`，也支持同目录下的 `sensitive_terms.xlsm`、`sensitive_terms.xls`、`sensitive_terms.csv`、`sensitive_words.xlsx`、`sensitive_words.xlsm`、`sensitive_words.xls`、`sensitive_words.csv`。词表至少需要包含“不规范用语”和“规范用语”两列；可参考 `data/sensitive_terms_example.csv` 的表头。`instance/` 和 `data/sensitive_terms.*` 已加入 `.gitignore`，真实内部词表不会被提交。

单文档检查还内置“常用词检查”。请将检查表放到 `instance/common_terms.xlsx`；同目录下的 `common_terms`、`common_words`、`常用词检查表`、`常用词表` 文件名均可使用，支持 `xlsx`、`xlsm`、`xls`、`csv` 格式。检查表必须包含“常用词”和“常见错误/不推荐用法”两列；“常用词”列是唯一正确写法，英文大小写严格敏感，文档中的写法只有与该单元格完全一致才视为正确。“常见错误/不推荐用法”单元格可使用换行、逗号、分号或顿号填写多个写法。可选增加“适用语种”列：留空或填写“全部/all”表示所有文档均执行，填写“中文/zh”表示仅在中文为主的文档中执行；语种条件会同时约束错误用法匹配和自动大小写检查。语种估计会排除同时含字母和数字的型号、版本号等连续技术标识，避免中文文档因大量型号被误判为中英混合；真实英文语句仍会正常参与判断。中英混合、语种特征不足或无法识别时，系统会保守跳过仅中文规则并在报告摘要中说明。未包含该列的检查表默认全部适用。可参考 `data/common_terms_example.csv`；真实内部检查表路径已加入 `.gitignore`，不会提交到仓库。

“多文档对照检查”页面支持上传 1-5 个素材文档和 1-3 个资料文档。资料通常是根据素材文档写作生成的，系统会以素材文档作为依据，调用所选模型检查资料内容是否存在口径不一致、遗漏、偏差或需要人工确认的内容，并输出报告。多文档对照项可在系统设置中单独维护，支持修改内置提示词、新增扩展检查项、停用、删除和排序；提交任务时会保存所选检查项快照，后续修改提示词不会影响已提交任务。

“图片检查”页面仅支持 PDF。系统会渲染 PDF 页面截图，同时尝试提取内嵌图片并保存到 `instance/extracted_images/`；页面截图用于图文与界面步骤一致性、图表标题可见性、完整性和清晰度等版式类检查，内嵌图片优先用于语种匹配、设备安装与接线、画图规范等资源类检查，缺少首选图片源时会自动回退到另一类图像并在报告中提示人工确认。长文档会优先覆盖含图、含表和内嵌图片所在页，超过页面上限时按段抽样并汇总未覆盖页数。执行时系统按当前页附近文本裁剪上下文，每批最多发送 4 张图片、每次最多合并 3 个检查项，模型按包含检查项 `code` 的结构化 JSON 返回结果；缺少部分 `code` 时，系统针对缺失项补偿请求一次，并同时支持 Markdown 返回格式。内置图片检查项包括图文与界面步骤一致性、图片语种匹配、设备安装与接线、图表标题可见性复核、图片完整性和清晰度、画图规范。检查项可在系统设置中修改提示词、新增扩展检查项、停用、删除和排序；提交任务时会保存所选检查项快照。图片检查报告会在每个检查项末尾汇总明确问题和需人工确认内容，便于快速浏览。

“视频检查”支持一次选择多个视频，每个视频使用相同的模型和检查项独立创建任务；某个视频上传、抽帧或任务入库失败时，已成功创建的其他视频任务会保留，页面会汇总成功数量和失败文件原因。系统会优先按首个视频流的实际时长均匀抽取视频帧，避免因音频轨更长而采样到无画面区域；单个采样点无法解码时会在前后 0.5 秒和 1 秒附近重试，少量孤立坏帧会记录后跳过，可用采样帧低于约 75% 时仍会拒绝任务，避免生成覆盖严重不足的报告。每批最多发送 4 帧、每次最多合并 3 个检查项，并使用与图片检查相同的多检查项 JSON 协议、缺失项补偿和 Markdown 返回格式。系统会保留每批结构化条目，按检查项合并重复问题和多个证据时间点，再使用与文档检查一致的严重程度、证据可信度、问题类型、位置、原文/证据、问题描述、影响和修改建议字段逐条展示、标注及导出；报告中的时间点和关键帧可以跳转原视频复核。多图片消息采用一个文本说明块后连续附加图片的 OpenAI 兼容结构，以兼容不接受图片之间插入额外文本块的模型服务。连续两次都无法识别任何检查项结果，或补偿后仍有检查项缺失时，任务会标记为失败并保留已写入的中间报告。

系统设置中的“任务数据保留天数”用于自动清理任务结束后不再需要的数据，`0` 表示不自动清理；填写正整数后，系统会清理超过保留天数的已完成、失败或已取消任务所对应的上传文件、提取图片、页面截图、视频帧、模型输出记录和执行时提取的正文。任务并发度下方显示上传文件和提取产物的实际磁盘占用；“快速清理”按任务列出可清理内容，支持按任务结束时间和大小排序，并可跳转查看对应报告。成功清理的任务不再出现在弹窗中。任务历史、检查报告、人工复核结果和统计继续保留，排队中和运行中的任务不会被清理。任务列表和详情页根据磁盘实际状态显示原文件是否可用；手动删除文件后会提示已清理或缺失，恢复到原存储路径后可重新下载。多文档任务缺少任意一份原文件时不会生成残缺压缩包。提交任务时会保存所选提供商和模型配置快照，包括 API Base、API Key、请求超时、输入上限和思考模式设置；后台执行使用提交时的快照，提供商后续被修改、停用或删除不会影响已提交任务。任务进入完成、失败或取消状态后，任务表中的 API Key 会立即清除。

## 本地日志

控制台默认显示启动摘要、访问地址和 `WARNING` 及以上日志。运行日志以 `INFO` 及以上级别写入 `instance/logs/`，按用途分为以下文件：

| 文件 | 内容 |
| --- | --- |
| `app.log` | 服务启动、就绪状态、认证、Web、文档与报告模块的应用事件及异常，以及 Uvicorn 运行日志。 |
| `task.log` | 任务提交、调度、执行、重试、取消、租约、任务文件清理及异常。 |
| `llm.log` | 模型请求、HTTP 状态、流式响应统计、请求重试、超时和诊断。 |
| `access.log` | HTTP 请求开始、结束、状态码、耗时及请求异常。 |

`app.log`、`task.log` 和 `llm.log` 各自按 5MB 轮转，每类保留当前文件和 2 个历史文件。日志包含时间、级别、模块名和进程 ID，任务与模型请求记录携带对应的 `task_id`、`request_id`，便于跨文件关联。上述四类日志文件使用 UTF-8 编码，并支持多进程并发写入和轮转。

本地 `config.yaml` 的 `logging.console_level` 支持 `INFO`、`WARNING`、`ERROR`、`CRITICAL`，默认值为 `WARNING`。启动时会为缺少该配置的文件补齐默认值，修改后重启生效。排查问题时设为 `INFO` 可在控制台查看详细日志；文件记录级别保持 `INFO`，启动摘要和访问地址始终显示。

主进程和 Web worker 的入口异常写入 `app.log`，调度器和任务进程的入口异常写入 `task.log`，并在控制台保留异常堆栈。依赖加载失败且日志组件不可用时，标准库将诊断保存到 `instance/logs/startup-<PID>.log`；每个失败进程使用独立文件。日志目录不可写时，异常继续输出到标准错误。

`llm.log` 记录模型名称、OpenAI Chat Completions 流式帧数量、`finish_reason`、`usage`、空响应诊断和截断后的响应帧样本。系统设置中的“模型流式定位日志”控制请求发送、响应建立、每个 chunk 短预览及结束标记的详细记录，默认关闭。模型日志只记录请求元信息和响应诊断，API Key 与完整文档正文作为请求数据处理。

HTTP 访问日志独立保存在 `instance/logs/access.log`，单个文件最大 10MB，最多保留 5 个文件（当前文件和 4 个历史文件）。每个普通请求会分别记录 `request_start` 和 `request_end`，包含 `X-Request-ID`、方法、路径、状态码、耗时及必要的反向代理字段；不记录查询参数、Cookie、请求体和认证信息。应用会优先沿用网关传入的合法 `X-Request-ID`，否则自动生成，并在响应头中返回同一个 ID。健康检查请求不会写入访问日志，避免探针产生大量重复记录；就绪状态发生变化时会写入 `app.log`。

服务提供无需认证的健康检查接口：`/health/live` 只确认 Web 进程能够响应；`/health/ready` 同时检查 SQLite、运行目录和任务调度器，全部正常时返回 200，否则返回 503。通过 `/infoCheck` 等子路径发布时，外部地址对应为 `/infoCheck/health/live` 和 `/infoCheck/health/ready`，代理仍需按部署约定去除前缀后转发。

如果页面出现“模型服务没有返回可用内容”，优先查看 `llm.log` 中同一个 `request_id` 的记录，判断服务是否只返回了 `reasoning_content`、是否触发 `content_filter`、是否返回了 200 状态的错误 JSON，或是否根本没有输出 SSE 数据帧。分析任务执行过程时，先按 `task_id` 筛选 `task.log`，再用同一个 `task_id` 关联 `llm.log` 中的模型请求。

## 本地文件

- `instance/document_check.sqlite3`：SQLite 数据库。
- `instance/uploads/`：上传文档。
- `instance/extracted_images/`：图片检查任务从文档中提取出的图片。
- `instance/sensitive_terms.xlsx`：单文档敏感词检查使用的本地词表。
- `instance/common_terms.xlsx`：单文档常用词检查使用的本地检查表。
- `instance/logs/app.log`：应用运行日志。
- `instance/logs/task.log`：任务调度与执行日志。
- `instance/logs/llm.log`：模型请求与诊断日志。
- `instance/logs/access.log`：独立 HTTP 访问日志。
- `config.yaml`：本地管理员账号、密码、隐藏管理入口、监听地址、启动端口和密钥。

以上文件均被 `.gitignore` 忽略，不应提交到仓库。
