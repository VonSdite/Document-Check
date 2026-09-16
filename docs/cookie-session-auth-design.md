# Cookie 身份认证

## 身份与权限

`auth.mode` 支持 `ip` 和 `cookie_session`。配置对象或模式值不合法时启动失败。用户面与管理员管理面使用独立权限；管理员凭据在本地 `config.yaml` 中配置。

`cookie_session` 从浏览器标准 `Cookie` 头读取凭据，转发至 `userinfo_url`。上游请求头名称由 `cookie_header_name` 指定。HTTP 2xx 且响应包含有效用户 ID 时建立身份：

- `field_mapping.user_id`：稳定人员 ID，组成 `cookie_session:<稳定ID>`。
- `field_mapping.username`：显示名，缺省显示稳定 ID。
- `field_mapping.employee_number`：可更新的工号，配置后与姓名一起展示。
- `avatar_field` 与 `avatar_url_template`：头像 URL 的字段来源和模板。

普通用户的列表、详情、轮询、导出及修改操作均按 `owner_subject` 隔离。管理员可查看和管理全部任务，创建任务和管理模型时使用当前用户身份。任务中的 IP 用于记录访问来源。

## 用户资料

最新姓名、工号和头像以 `identity_profile:<稳定主体>` 为键保存在 `settings` 中。任务列表、详情、导出、搜索和统计优先展示最新资料，任务快照保留提交时的姓名。不同 Cookie 和 Web 进程共享同一份资料；任务与模型配置始终按稳定 ID 归属。

资料版本取上游查询开始时间，数据库仅接受更高版本。有效期内的缓存、故障宽限缓存和较早查询的迟到结果读取最新资料。资料记录仅用于展示，Cookie 验证成功或处于允许的宽限期内才建立身份。

同源请求通过 `X-User-Profile` 返回当前用户的展示资料，页面自动刷新时同步右上角姓名、工号和头像；浏览器按版本忽略迟到资料。管理员列表和任务详情的轮询同时检测归属用户的展示名变化。

## IP 数据归属

Cookie 身份解析成功时，`migrate_ip_owner_to_subject()` 将当前 IP 下归属为 `ip:<IP>` 的任务和模型提供商转移到当前稳定用户 ID。更新在同一数据库事务内提交，已有 Cookie 用户的数据保持原归属。

`current_identity()` 在 Flask 请求上下文中复用解析结果，每个请求最多检查一次迁移。只读检查待迁移数据：

```bash
uv run python -m scripts.audit_ip_owners
uv run python -m scripts.audit_ip_owners --database /path/to/document_check.sqlite3 --json
```

脚本按 IP 汇总用户名、任务数、提供商数和模型数，用户名依次取 `ip_usernames` 表和任务上的名称快照，使用 SQLite 只读连接。

## 缓存

`app/identity/cookie_session.py` 使用原始 Cookie 的 SHA-256 作为缓存键。每个 Web 进程独立缓存，容量上限为 4096 条，按最近访问顺序淘汰，并在请求中定期回收到期条目。

- 有效期默认为 600 秒，有效期内直接使用身份。
- 到期后重新查询上游，同一 Cookie 的并发请求共享一个查询结果。
- 临时故障时，已有身份可在默认 600 秒宽限期内使用；失败后的再次查询间隔为 5 秒。
- HTTP 401/403 或响应中的会话失效状态立即使缓存身份失效，不使用宽限期。
- 无可用缓存时拒绝认证，错误结果短暂缓存 5 秒以限制重复调用。
- 显式清理 Cookie 缓存时，清理前发起的查询结果不能重新写入缓存。

有效期和宽限期由 `cache_ttl`、`cache_grace` 控制，`cache_grace: 0` 关闭宽限。身份失效影响后续请求，已经提交的检查任务独立执行。

## 上游接口

接口支持直接对象及 `{"status":"success","data":{...}}` 两种响应。HTTP 401/403 或 JSON 中 `status` 为 401/403 表示会话失效。其他非成功状态、解析失败或网络异常按临时故障处理。

HTTP 请求遵循系统代理配置，证书校验使用 `auth.cookie_session.ssl_verify`。超时默认 3 秒，网络异常重试一次。生产 HTTPS 接口建议开启证书校验。

## 登录与回跳

用户端点和管理员已登录后的 console 用户端点要求 Cookie 身份。普通页面未认证时跳转到 `login_url`，通过 URL 编码的 `redirect` 参数携带原页面地址。登录地址的其他参数和片段保持完整。

同源异步请求携带当前页面路径。身份失效时返回 JSON 401 和 `X-Login-URL`，前端统一跳转到登录页并在登录后返回原页面。未配置登录地址时返回 401。

管理员登录、系统设置、概览、健康检查与静态资源不要求 Cookie 身份。任务创建页和模型管理需要当前用户身份。

## 验证

`tests/test_cookie_session.py` 验证缓存并发、失效、宽限、回收和接口解析；`tests/test_cookie_session_routes.py` 验证迁移、权限一致性、登录回跳及审计脚本。HTTP 压测脚本使用本地用户信息接口与独立 Cookie 验证共享 IP 下的多用户隔离。
