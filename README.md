# 文档智能门禁

一个基于 Flask + SQLite 的文档智能门禁网站，支持用户上传 docx、pdf、txt、md、html、xlsx、xlsm、xls 文档，并通过用户自行配置的 OpenAI Chat Completions 兼容模型执行单文档规范性、一致性、错别字检查、多文档对照检查和多模态图片检查。

## 功能概览

- 用户面：创建单文档检查、多文档对照检查和图片检查任务、维护自己的模型提供商和模型 ID、测试模型连通性、查看当前用户的任务、取消任务、删除历史任务，支持用 SSO 用户主体或 IP 兜底身份归属任务。
- 管理面：隐藏 URL 登录，查看和管理全部任务，配置检查项提示词、扩展检查项和任务并发度；用户身份和用户模型配置由用户侧管理。
- 任务执行：后台线程从 SQLite 队列拉取任务，默认全局并发 3、单用户并发 1、单任务检查项并发 1，可在管理面调整；文档文本会作为全文一次送入模型，图片检查会把文档文本和按位置命名的图片批次一起送入多模态模型。
- 本地存储：SQLite 数据库、用户模型配置、上传文件、提取图片和运行日志保存在 `instance/`，本地管理员配置保存在 `config.yaml`。
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

注意：PyInstaller 不能从 Linux/macOS 交叉打包 Windows exe，以上脚本需要在 Windows 上运行。交付前建议先在打包机运行一次，修改 exe 同目录的 `config.yaml` 中的管理员密码、`secret_key`、管理入口和端口；模型提供商由用户进入系统后在“模型管理”中自行配置。

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
# 管理入口需要登录；用户身份可先用 ip，后续按公司 SSO 情况切到 trusted_header 或 saml。
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

## 系统出站网络配置

`network` 控制本服务所有对外请求，包括拉取模型列表、测试模型连通性和后台执行检查任务。用户只能填写模型提供商、API 地址、API Key、超时、文本上限和模型 ID，不能在自己的模型配置里指定代理或 SSL 校验。

- `network.proxy_mode`：`direct` 为直连，默认值；`system` 读取运行本服务的机器上的 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 等环境变量；`custom` 使用 `network.proxy` 指定的代理地址。
- `network.proxy`：仅 `proxy_mode: custom` 时填写，例如 `http://127.0.0.1:7890`；其他模式会忽略该字段。
- `network.ssl_verify`：统一控制 HTTPS 证书校验，默认 `false`。如果模型服务使用公网正式证书，建议改为 `true`；如果是内网或自签名证书，可保持 `false`。

这些字段也可以在管理端“系统设置”里修改。保存后系统会直接写回本地 `config.yaml`，并立即更新当前进程配置；不会写入 SQLite。

## 用户身份与 SSO 预留

系统内部使用 `owner_subject` 作为任务归属，不再把 IP 当成唯一用户身份。配置里保留三种 `auth.mode`，默认先用 `ip` 方便本机运行、临时测试和先把平台跑起来；确认公司 SSO 接入方式后，再把 `mode` 切到对应模式。不同模式使用不同用户命名空间，模型配置、任务列表和统计概览只在当前模式内生效，不会互相串数据。IP 仍会记录在任务中用于审计。

- `ip`：不接 SSO，用户主体为 `ip:<访问 IP>`。
- `trusted_header`：公司网关或反向代理已经完成 SSO 登录，并把用户 ID/用户名注入可信 HTTP header，用户主体为 `trusted_header:<用户ID>`。
- `saml`：公司 SSO 是 SAML 2.0，本系统直接作为 SAML SP 对接，用户主体为 `saml:<用户ID>`。

无论用户从 `/` 用户入口还是从 `admin_url` 对应的 console 入口创建任务、选择模型或进入“模型管理”，系统都会按同一套 `auth.mode` 解析当前用户，使用同一个 `owner_subject` 读写该用户自己的模型配置。

平台模式且 `auth.mode: ip` 时，管理员后台“系统设置”会显示“IP 用户标记”，可以给 IP 设置显示用户名。该设置只改变页面显示和统计展示，不改变认证身份；统计概览会优先显示映射用户名，未设置时回退显示 IP。`trusted_header` 和 `saml` 模式不会显示这个入口。

SAML 1.0/1.1 已经过老，当前不作为支持的接入模式；如果公司只提到旧版 SAML，优先请对方提供 SAML 2.0，或由公司网关先完成登录并转换为可信 header。

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

此时系统会把 `X-SSO-User-Id` 解析为 `trusted_header:<用户ID>`，用 `X-SSO-User-Name` 作为显示名；用户入口没有收到 `X-SSO-User-Id` 时会返回 401，避免绕过 SSO 后退回 IP 身份。只有在该服务位于可信 SSO 网关之后、外部用户无法伪造这些请求头时才应启用该模式。用户启停、组织、角色等用户管理职责应放在公司 SSO 或身份平台中处理；本系统只保存任务归属快照、审计 IP、统计和并发控制所需的用户主体。后续如果公司提供 OIDC 或 CAS 接口，也需要明确映射到单独的用户主体命名空间。

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

进入用户侧“模型管理”页面，或在 console 中进入“模型管理”，都可以创建自己的模型提供商。两处使用同一个当前用户主体，模型配置按该用户主体存入 SQLite，不再写入 `config.yaml`；平台提交任务时只会使用当前用户自己启用的模型。

- API 地址填写完整 OpenAI Chat Completions 请求地址，例如 `https://api.example.com/v1/chat/completions`。
- API Key 可为空，非空时会以 `Authorization: Bearer ...` 发送。
- 代理和 SSL 校验不允许用户单独配置，统一由 `config.yaml` 的 `network` 控制。
- 请求超时时间按提供商单独设置，默认 3600 秒。
- 单次请求文本上限按提供商单独设置，默认 80000 字。
- 模型 ID 列表使用表格维护，可手动新增、整理，也可从当前 API 地址拉取模型后在弹窗中选择加入。
- 每个模型 ID 行都有“测试”按钮，用于从平台服务端按当前 API 地址、API Key、系统出站网络配置和模型 ID 发起一次 Chat Completions 连通性测试。
- 每个模型可单独开启“强制关闭思考”。开启后，请求该模型时会附加 `enable_thinking=false`，并同时写入 `chat_template_kwargs: {"enable_thinking": false}`，用于关闭部分思考模型的思考模式；不支持这些参数的服务可能忽略或返回错误。

## 检查流程

1. 用户或管理员在“单文档检查”“多文档对照检查”或“图片检查”页面上传文档，选择当前用户已配置的模型和检查项后提交。
2. 系统先保存上传文件，再按文档类型提取可检查文本：
   - `docx`：提取段落和表格文本。
   - `pdf`：提取 PDF 页面中的文本层。
   - `txt`、`md`：按文本文件读取。
   - `html`：去除脚本和样式后提取页面文本。
   - `xlsx`、`xlsm`、`xls`：按工作表逐行提取单元格文本。
3. 如果无法提取文本，系统会拒绝提交任务，并删除本次上传文件。单文档检查会把文件名和提取出的全文一起发送给模型，不再按片段拆分；文本超过所选模型提供商的文本上限时会拒绝提交。
4. 校验通过后，任务进入队列，后台调度器按全局并发和单用户并发限制执行。
5. 执行时会按检查项并发调用模型，并持续写入结果和进度；图片检查会按图片位置分批调用多模态模型，每批最多 4 张图片，并只附带当前图片所在页附近的文档文本，用于检查图片内容、图片文字以及图文对应关系；单任务检查项并发数可在管理面调整。
6. 任务完成后可在任务列表进入报告页查看结果；完成状态的任务支持导出 HTML 报告。

## 文档上传处理

普通“单文档检查”任务会参考 Cherry Studio 的处理方式：上传文件先在本地提取为文本，再以 `file: 文件名` 加全文内容的形式放入模型提示词中。系统不会把长文档拆成多个片段，也不会把原始文件直接转发给模型。

所选模型提供商的“单次请求文本上限”用于控制全文请求大小。提取后的文本加文件名超过上限时，单文档检查会拒绝提交；“多文档对照检查”也会在合并素材文档和资料文档后做同样的上限校验。

“多文档对照检查”页面支持上传 1-5 个素材文档和 1-3 个资料文档。资料通常是根据素材文档写作生成的，系统会以素材文档作为依据，调用所选模型检查资料内容是否存在口径不一致、遗漏、偏差或需要人工确认的内容，并输出报告。多文档对照项可在系统设置中单独维护，支持修改内置提示词、新增扩展检查项、停用、删除和排序；提交任务时会保存所选检查项快照，后续修改提示词不会影响已提交任务。

“图片检查”页面会从文档中提取可识别图片并保存到 `instance/extracted_images/`，文件名包含图片在文档中的页码、段落、表格、工作表单元格或 Word 章节标题等位置线索。系统同时提取文档文本，执行时按图片页码筛出当前批次所在页及前后页文本，连同图片清单和当前图片批次一起发送给支持图片输入的 OpenAI Chat Completions 兼容多模态模型，避免长文档跨页误配。内置图片检查项包括图文对应、图片语种匹配、接线问题、图表标题规范、图片完整性和清晰度、画图规范，可在系统设置中修改提示词、新增扩展检查项、停用、删除和排序；提交任务时会保存所选检查项快照。图片检查报告会在每个检查项末尾汇总明确问题和需人工确认内容，便于快速浏览。

## 本地日志

运行日志同时输出到 console 和 `instance/logs/app.log`。日志文件单个最大 5MB，最多保留 3 个文件（当前文件和 2 个历史文件）。日志会记录任务 ID、模型名称、请求 ID、HTTP 状态、OpenAI Chat Completions 流式帧数量、`finish_reason`、`usage`、空响应诊断和截断后的响应帧样本，不记录 API Key 和完整文档内容。

如果页面出现“模型服务没有返回可用内容”，优先查看该日志中同一个 `request_id` 的记录，判断服务是否只返回了 `reasoning_content`、是否触发 `content_filter`、是否返回了 200 状态的错误 JSON，或是否根本没有输出 SSE 数据帧。

## 本地文件

- `instance/document_check.sqlite3`：SQLite 数据库。
- `instance/uploads/`：上传文档。
- `instance/extracted_images/`：图片检查任务从文档中提取出的图片。
- `instance/logs/app.log`：本地运行日志。
- `config.yaml`：本地管理员账号、密码、隐藏管理入口、监听地址、启动端口和密钥。

以上文件均被 `.gitignore` 忽略，不应提交到仓库。
