# 4+1 架构视图

本文描述文档智能门禁的场景、模块职责、依赖、进程与部署。HTTP 路由和 SQLite 结构以自动化兼容契约验证。

## 总览

```text
浏览器
  |
  v
run.py 主进程
  |
  +-- Uvicorn + Flask + a2wsgi 请求线程 x16
  |
  +-- 独立任务 supervisor
        |
        +-- 动态任务进程，机器上限 max_task_processes=4
              |
              +-- TaskRunner
                    |
                    +-- 任务内 ThreadPoolExecutor 检查项并发
                    +-- 文档解析、图片/视频处理、模型请求

所有进程和线程
  |
  +-- SQLite WAL：任务队列、状态、配置、报告
  +-- instance/：上传文件、提取产物、日志、监督器状态
  +-- 外部模型服务：OpenAI Chat Completions 兼容接口
```

## 场景视图（+1）

### 提交和执行任务

1. Web worker 接收上传、校验文件和模型配置。
2. Web worker 将任务与提交时的配置快照写入 SQLite，任务状态设为 `queued`。
3. supervisor 在 SQLite 事务中认领任务，应用全局并发、单用户并发和机器级进程上限。
4. supervisor 为每个已认领任务创建一个独立任务进程，并传递任务 ID 与租约令牌。
5. TaskRunner 在任务进程中读取任务快照，完成文档预处理并执行检查项。
6. TaskRunner 通过任务内线程池并发执行检查项，持续写入进度和中间结果。
7. TaskRunner 将任务状态更新为 `completed`、`partial`、`failed` 或 `canceled`。
8. Web worker 从 SQLite 读取任务状态和报告并返回页面或下载内容。

### 取消和恢复任务

- Web worker 将运行任务标记为 `canceling` 并写入 `cancel_requested`。
- TaskRunner 通过 SQLite 轮询取消标记，停止当前检查并写入 `canceled`。
- TaskRunner 使用租约心跳更新 `lease_expires_at`。
- supervisor 回收异常退出或租约过期的任务，并将可重试任务重新置为 `queued`。
- supervisor 在停止服务时等待任务退出，随后回收剩余任务的租约。

### 管理并发配置

- `worker.max_task_processes` 定义机器级任务进程上限，默认值为 `4`。
- 管理页面的 `global_concurrency` 定义实际任务并发数，取值范围为 `1` 到 `max_task_processes`。
- supervisor 每轮从 SQLite 读取 `global_concurrency`，配置保存后立即影响新任务认领。
- `check_item_concurrency` 定义单个任务内部的检查项线程并发数。

## 逻辑视图

### 模块职责

| 模块 | 职责与入口 |
| --- | --- |
| `bootstrap` | `factory.create_app` 装配 Web 应用；`supervisor` 提供监督器进程入口 |
| `contracts` | 任务类型、文件数量和输出条目限制等公共约束 |
| `infrastructure` | 本地配置、网络、日志、文件操作；`runtime` 创建进程应用上下文 |
| `persistence` | `connection` 管理连接，`schema` 管理初始化，`settings` 管理设置，`defaults` 管理默认检查项 |
| `identity` | 独立身份数据类型、IP/可信请求头身份解析与 SAML 适配 |
| `models` | 提供商与模型配置、模型发现、模型请求和流式协议 |
| `documents` | PDF、DOCX、表格和标记文本提取，图片提取、页面渲染与视频抽帧 |
| `checks` | 检查项目录、词表检查、链接校验、语种分析、报告证据约束 |
| `tasks` | 提交参数与结果、上传事务、调度认领、TaskRunner、进度、租约与文件生命周期 |
| `reporting` | 报告解析与规则过滤、人工复核、工作簿导入导出、统计缓存刷新 |
| `web` | 请求与响应适配、认证和权限编排、页面路由、模板和静态资源 |

### 服务与适配

Web 层将请求解析为 `TaskSubmission`，将提交服务返回的 `SubmissionResult` 转换为页面消息和跳转。模型服务显式接收用户主体，报告复核服务接收数据参数，工作簿服务返回文件内容。后台服务使用应用上下文访问数据库和日志。

用户任务、管理任务、模型管理、管理概览和系统设置分别注册路由。`web/__init__.py` 汇总注册过程；HTTP 路径、端点名称、认证、代理前缀和静态资源地址由兼容测试覆盖。

任务监督器直接调用 `tasks.runner.TaskRunner` 和 `reporting.statistics`。任务执行按文本、图片和视频分工，文档提取和检查规则具有独立模块入口。

```mermaid
flowchart TD
    bootstrap[bootstrap 应用装配] --> web[web HTTP 适配]
    bootstrap --> tasks[tasks 任务生命周期]
    web --> tasks
    web --> identity[identity 用户身份]
    web --> reporting[reporting 报告]
    web --> models[models 模型服务]
    tasks --> reporting
    tasks --> models
    tasks --> documents[documents 文档处理]
    tasks --> checks[checks 检查规则]
    tasks --> infrastructure[infrastructure 运行基础]
    models --> checks
    reporting --> checks
    checks --> persistence[persistence 持久化]
    models --> persistence
    reporting --> persistence
    infrastructure --> persistence
    identity --> persistence
    persistence --> contracts[contracts 公共约束]
```

完整依赖允许范围见 [开发与模块边界](development.md)。模块依赖保持单向，后台导入独立性由自动化测试验证。

### 持久化与查询

SQLite 使用 WAL，每个应用上下文持有独立连接，连接设置为外键约束开启、`synchronous=NORMAL`。数据库保存任务队列、配置、进度、报告、规则和复核统计；上传文件与提取产物保存在 `instance/`。

`persistence.schema` 统一维护索引定义。应用工厂在接收请求前完成表和索引初始化；新建表直接创建完整索引，已有表通过 `TODO(index-compat)` 区块补齐缺少的性能索引。补建及相关表的统计分析在初始化写事务中完成；索引完整时，索引检查只读取元数据。

- 用户列表使用类型、用户主体与创建时间组合索引；用户状态汇总使用类型、用户主体与状态覆盖索引；状态轮询按最多 100 个任务 ID 查询轻量状态和统计。
- 管理日期统计按支持的任务类型与日期范围使用现有索引。
- 模型配置按用户批量读取，模型选择按提供商主键与用户归属定位。
- 任务认领先通过状态与用户索引定位各用户的有限候选，再执行并发限制与全局排序。
- 报告汇总在 SQLite 内聚合统计字段，页面同步准备最近最多 20 个任务；监督器使用主键游标，每轮检查最多 512 个任务、刷新最多 100 个过期报告。
- 文件清理通过部分索引定位已结束且文件尚未清理的任务，按清理时间分批读取元数据。
- 删除任务使用任务 ID 索引清理误报命中记录；启用规则及版本统计使用类型与更新时间的部分索引。

详细查询边界与验证方法见 [性能与容量验证](performance.md)。数据库的表、字段、索引和触发器以结构契约约束。

### 文档处理

PDF 文本优先通过 PyMuPDF 提取，按页按需使用 pypdf 回退；候选向量边线触发表格识别。PDF 空格修正使用线性字符遍历，词条位置查找使用页面和工作表标记的二分索引。

图片检查的 PDF 文本提取跳过表格结构化，内嵌图片提取和页面截图按各自步骤执行。Excel 使用只读工作簿迭代；视频采样使用有界的两路 `ffmpeg` 抽帧，保留采样顺序和单帧失败回退。预处理结果持久化后供检查项和重试使用。

## 开发视图

```text
app/                              Python 命名空间包
  bootstrap/
    factory.py                    Web 应用装配
    server.py                     Uvicorn 启动与运行目录传递
    asgi.py                       Flask 工厂与 WSGI 请求线程池
    supervisor.py                 监督器进程入口
  contracts/
    task_types.py                 任务类型与文档数量约束
    limits.py                     输出数量约束
  infrastructure/
    config.py                     本地配置读写
    runtime.py                    配置、资源定位与进程应用上下文
    logging.py                    应用日志
    network.py                    网络配置与访问地址
    files.py                      文件系统操作
  persistence/
    connection.py                 SQLite 连接与时间
    schema.py                     表结构初始化
    settings.py                   设置、用户名与任务记录操作
    defaults.py                   默认检查项与配置同步
  identity/
    models.py                     用户身份数据
    service.py                    IP、可信请求头与会话身份
    saml.py                       SAML 协议适配
  models/
    service.py                    提供商与模型配置
    discovery.py                  模型发现
    client.py                     推理请求、流式响应与取消
  documents/
    extraction/                   PDF、DOCX、表格与标记文本解析
    images.py                     图片提取与页面渲染
    videos.py                     视频抽帧
  checks/
    catalog.py                    检查项目录与排序
    common_terms.py               常用词规则
    sensitive_terms.py            敏感词规则
    term_cache.py                 词表缓存
    term_locations.py             文档位置索引
    hyperlinks.py                 链接校验
    text_language.py              语种判断
    language_consistency.py       跨语种静态分析
    guardrails.py                 证据约束
  tasks/
    submission.py                 提交数据、验证、文件入库与任务事务
    files.py                      上传路径、任务文件与清理辅助
    supervisor.py                 调度、租约恢复与任务进程管理
    runner.py                     TaskRunner 与文本检查编排
    runtime/
      preprocessing.py            文档、图片、视频与多文档预处理
      image_checks.py             图片检查编排
      video_checks.py             视频检查编排
      multimodal_protocol.py      多模态请求与结构化结果
      multimodal_common.py        批次、输入与摘要
      common.py                   配置和结果合并
      state.py                    进度、取消、租约与结果状态
      artifacts.py                产物统计与清理
  reporting/
    constants.py                  报告字段、状态和导出定义
    service.py                    报告解析、规则与复核
    excel.py                      工作簿生成与标注回填
    statistics.py                 统计聚合与后台刷新
  web/
    auth.py                       认证路由与权限
    user_tasks.py                 用户任务路由
    admin_tasks.py                管理任务路由
    models.py                     模型配置请求适配
    settings.py                   系统设置
    overview.py                   管理概览
    task_lists.py                 分页列表与状态响应
    task_actions.py               任务访问与操作响应
    task_media.py                 文件下载、媒体与视频响应
    submission.py                 上传请求与提交结果适配
    reports.py                    报告复核、导出与回填响应
    presentation.py               模板上下文与异常响应
    observability.py              访问日志与健康检查
    formatting.py                 Markdown 展示
    common.py                     请求辅助
    constants.py                  展示约定
    templates/                    Jinja 模板
    static/                       脚本与样式
run.py                            跨平台 Web 与监督器启动入口
scripts/benchmark_internal.py     临时环境 HTTP 并发压测
tests/                            行为、兼容、依赖和性能回归
```

所有 Python 源码归属模块目录。Web 工厂以明确资源路径加载 `web/templates/` 和 `web/static/`；后台进程只装配运行上下文。源码、模板和运行数据目录分别承担代码、展示和持久化职责。

## 进程视图

### 进程和线程角色

| 角色 | 默认数量 | 执行内容 |
| --- | ---: | --- |
| Web 服务（run.py 主进程） | 1 | Uvicorn 监听端口，使用 Flask 应用和事件循环处理 HTTP 请求 |
| a2wsgi 请求线程 | 每个 Web 进程 16 | 执行 Flask 请求 |
| 任务 supervisor | 1 | SQLite 队列调度、任务进程管理和租约恢复 |
| 任务进程 | 最多 4 | 一个进程执行一个任务 |
| 任务内检查线程 | 按 `check_item_concurrency` | 一个任务内并行执行检查项 |

任务 supervisor 由 `run.py` 通过 `app.bootstrap.supervisor` 创建为独立 Python 子进程，再启动 Uvicorn。Web worker 和监督器通过 `DOCUMENTCHECK_ROOT_DIR` 继承运行根目录，读取相同的配置和数据库。父进程通过标准输入管道通知监督器退出，监督器完成任务进程清理；进程存活使用 psutil 检查。任务进程使用 `spawn` 创建，任务进程接收任务 ID、租约令牌和运行根目录；每个进程自行创建应用对象和数据库连接。

Windows 虚拟环境中的监督器通过基础解释器直接启动，使用 `__PYVENV_LAUNCHER__` 保留虚拟环境。启动器与监督器使用实际进程 PID 完成就绪和父进程存活检查。

Web 请求线程、任务进程和任务内检查线程都使用 Python 原生线程或进程。外部模型请求属于 I/O 操作，线程在等待网络响应时释放执行资源；文档解析和视频处理在独立任务进程中运行。

`server.web_workers` 默认为 1，Web 请求在启动进程中执行。显式设置大于 1 时，Uvicorn 创建相应数量的 Web 子进程并管理异常重启。

## 物理视图

### 单机部署

```text
客户端
  |
  v
可选反向代理（Nginx 等）
  |
  v
run.py（Uvicorn + Flask + a2wsgi）
  +-- task supervisor + task processes
  +-- instance/document_check.sqlite3
  +-- instance/uploads/
  +-- instance/extracted_images/
  +-- instance/logs/
  |
  v
外部模型服务
```

服务使用 Python 3.12 及以上版本，通过 `uv run python run.py` 在 Windows 和 Linux 启动 Uvicorn 与 a2wsgi。`config.yaml` 保存监听地址、端口、Web worker/线程数和任务进程上限。`uv.lock` 固定 Python 依赖版本。视频任务还需要系统提供 `ffmpeg` 和 `ffprobe`。

### 并发边界

- Windows 和 Linux 的 Web 请求线程容量均由 `web_workers × web_threads` 决定，默认 `1 × 16`；单进程配置直接在启动进程中提供 Web 服务。
- 任务并发容量由 `global_concurrency` 提供，并受 `max_task_processes` 约束。
- 单任务检查项并发由 `check_item_concurrency` 提供。
- SQLite 事务负责任务认领和状态转换，文件系统负责上传文件与提取产物。
- 模型服务连接数量还受任务数量、检查项并发和模型服务端限流共同约束。

## 设计约束

- Web worker 只处理 HTTP 请求和轻量数据库操作。
- supervisor 统一管理后台任务进程，系统同时运行一个 supervisor。
- 任务状态、取消信号、租约和中间结果写入 SQLite，跨进程可见。
- 任务进程完成或失败后立即清理任务表中的 API Key。
- `/health/live` 检查 Web 响应能力，`/health/ready` 同时检查 SQLite、运行目录和 supervisor 状态。
