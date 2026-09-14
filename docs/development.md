# 开发与模块边界

## 应用入口

项目通过 `uv run python run.py` 启动。`run.py` 调用 `app.bootstrap.factory.create_app()`，创建独立监督器进程，再通过 `app.bootstrap.server` 启动 Uvicorn。每个 Web worker 调用 `app.bootstrap.asgi.create_asgi_app()` 创建 Flask 应用和 a2wsgi 请求线程池。

`app/` 是 Python 命名空间包，根目录只包含模块目录。Web 工厂导入方式为：

```python
from app.bootstrap.factory import create_app

app = create_app()
```

工厂接受可选的 `root_dir: Path`，用于指定配置和运行数据所在目录。模板与静态资源定位到代码中的 `app/web/`；运行目录负责保存 `config.yaml` 和 `instance/`。启动器通过 `DOCUMENTCHECK_ROOT_DIR` 将此目录传给 Web worker 和监督器；该环境变量也可用于指定独立的运行目录。

`app.infrastructure.runtime.create_task_app()` 创建后台进程使用的应用上下文，并在上下文结束时关闭数据库连接。监督器通过 `python -m app.bootstrap.supervisor` 启动，任务通过独立的 `python -m app.bootstrap.task <task_id>` 入口启动，完成应用初始化后调用 `TaskRunner.run()`。监督器向任务进程传递运行根目录，使子进程使用相同的配置和数据库。

`app.infrastructure.subprocesses.python_module_command()` 为监督器和任务进程统一选择解释器、UTF-8 输出与运行根目录。Windows 虚拟环境中直接调用基础解释器，通过 `__PYVENV_LAUNCHER__` 保留虚拟环境路径与依赖。启动进程 PID 对应实际 Python 进程，监督器就绪检查验证该 PID 与新鲜心跳。启动失败信息区分子进程退出与等待超时，并提供退出码、启动 PID 和任务日志路径。

## 任务进程与执行权

`TaskSupervisor` 负责认领、续约、取消收尾和异常恢复；`TaskRunner` 负责预处理与检查执行。监督器每 10 秒按任务主键和令牌更新 90 秒租约，任务中的监测线程只读取取消状态。文件清理与报告统计使用监督器的独立维护线程。

`tasks/processes.TaskWorkerProcess` 使用 `subprocess.Popen` 创建独立模块进程。入口在获得许可前仅加载标准库，通过标准输出报告进入入口；进程身份保存到 `instance/task-supervisor.json` 后，父进程通过标准输入发送包含租约令牌的 JSON 许可并关闭输入管道。子进程收到许可后导入业务模块、创建应用、报告就绪并执行任务。专用读取线程持续消费输出并记录启动阶段，错误堆栈保留在控制台。

监督器从进程创建时起计时，60 秒内须收到业务就绪信号。启动超时、创建失败或就绪前异常退出会产生明确的启动错误；进程确认退出后，任务写为失败，用户可在页面重试。取消状态优先处理，业务就绪后的长任务由原有租约和取消机制管理。

状态文件采用临时文件加原子替换写入。权限或文件共享占用错误最多尝试 6 次，退避等待累计 1.55 秒；持续失败时保留上一份完整快照，并清理临时文件。启动失败时关闭许可管道并终止进程；终止及退出收尾尽力保存快照，状态写入失败时继续清理。`tasks/processes.py` 维护 PID、创建时间、租约令牌和待退出子进程快照；PID 和创建时间共同标识进程。监督器启动时先清理记录中的遗留进程，再恢复对应令牌的任务。

过期回收和队列选择排除 `_active` 中的任务 ID。正在退出的进程继续占用机器、全局及用户并发名额，数据库恢复成功后才移除进程记录。取消先由任务协作退出，10 秒宽限后发送终止信号，3 秒后仍存活则强制结束。数据库租约与运行数据沿用现有字段。

`documents.extraction` 接受可选的 `cancel_event`，在 PDF 页、表格和表格行以及 Excel 行边界抛出 `DocumentReadCanceled`。文档模块使用自身异常类型，TaskRunner 根据取消信号更新任务状态。新增耗时解析循环时同步设置取消检查点。

## 文档解析与列表读取

`documents/extraction/spreadsheets.py` 将 openpyxl 内部流式接口限定在 Excel 解析器内，使用已锁定的 `openpyxl==3.1.5`。`_WorksheetValuesAndFormulas` 继承 `WorkSheetParser`，一次解析保留缓存值和公式，并沿用其日期、时长、共享公式及单元格类型规则。超链接元数据单独流式读取，再通过行列范围索引参与文本生成。升级 openpyxl 时运行公式、日期、链接、稀疏行列和取消回归测试。

`documents/extraction/pdf.py` 使用页面 rawdict 字符边界筛选可能含文字的单元格，字符信息不完整时保留原生提取回退。同页表格共用 TextPage，图片与绘图使用中心坐标索引。索引仅缩小候选范围，文本及非文本内容仍按现有坐标容差判定。

`web/task_lists.py` 按任务类型选择列表元数据。轮询先批量读取状态与统计缓存，仅对已结束且缓存过期的任务按主键检查报告存在性；候选 ID 来自已验证任务类型和归属的记录。`reporting/statistics.py` 优先使用统计行，报告写入由 `trg_tasks_report_stats_invalidate` 触发缓存删除。文件可用性检查在一次调用内复用已解析分组，并实时检查磁盘文件。

## 数据库索引初始化

`app/persistence/schema.py` 的 `QUERY_INDEXES` 保存性能索引定义。`init_db()` 记录初始化前已有的表，完成字段准备后，为新建表创建索引。索引初始化在应用工厂启动期间完成。

已有表的索引补齐由 `_ensure_existing_query_indexes()` 负责，该函数及其调用分别使用 `TODO(index-compat): BEGIN` 和 `TODO(index-compat): END` 包裹。函数读取索引元数据，只创建缺少的索引，并对受影响的表各执行一次 `ANALYZE`；创建和分析共用初始化写事务。

兼容区块的清理范围为上述两对 TODO 标记内的代码，保留 `QUERY_INDEXES`、`_initialize_query_indexes()` 中的新表创建路径及其在 `init_db()` 中的调用。新库创建测试单独验证新表索引定义，兼容测试验证已有库、部分缺失、重复启动、并发启动和失败重试。

## 模块依赖

| 模块 | 可依赖的项目模块 |
| --- | --- |
| `contracts` | 自身 |
| `persistence` | `contracts` |
| `infrastructure` | `persistence` |
| `identity` | `persistence` |
| `documents` | 自身 |
| `checks` | `contracts`、`persistence` |
| `models` | `checks`、`contracts`、`persistence` |
| `reporting` | `checks`、`contracts`、`persistence` |
| `tasks` | `checks`、`contracts`、`documents`、`identity`、`infrastructure`、`models`、`persistence`、`reporting` |
| `web` | 各领域模块、公共约束、基础设施和持久化 |
| `bootstrap` | `infrastructure`、`persistence`、`tasks`、`web` |

依赖关系构成有向无环图。跨模块使用显式绝对导入；模块内部按照格式解析、状态管理、请求协议等具体职责组织文件。

HTTP 请求、会话、页面渲染和跳转由 `web` 与认证适配器负责。任务和报告服务通过参数接受业务数据，返回结构化结果；数据库操作使用进程内的 Flask 应用上下文，日志使用模块级 logger。

- `TaskSubmission` 承载文件列表、检查项、模型选择和提交令牌。`submit_document_task` 等提交服务返回 `SubmissionResult`，Web 层将消息和任务类型转换为提示与页面跳转。
- `identity.models.UserIdentity` 是独立的身份数据类型。模型查询显式接收用户主体，任务提交使用身份快照保存归属信息。
- `models.service` 管理提供商与模型配置，`models.client` 管理模型协议请求，`models.discovery` 管理模型列表发现。
- `reporting.service.update_report_item_type(task, data)` 接收复核数据，`reporting.excel.build_report_workbook(task)` 返回工作簿内容；下载响应由 `web.reports` 构造。
- `reporting.statistics.refresh_stale_report_stats_batch()` 在后台应用上下文中运行。监督器直接调用报告模块。
- `documents.extraction` 按文件类型分派，具体格式由 `pdf.py`、`docx.py`、`spreadsheets.py` 和 `markup.py` 实现。

## 日志约定

各模块使用 `logging.getLogger(__name__)` 记录日志。`app.infrastructure.logging` 集中配置 INFO 级别、模块名、进程 ID、控制台输出及多进程安全文件轮转。`app.tasks` 及其子模块写入 `task.log`，`app.models` 及其子模块写入 `llm.log`，其余 `app` 模块和 Werkzeug 写入 `app.log`。启动入口使用 `app.run` logger。每条业务记录写入所属文件，同时输出到控制台。

`app.web.observability` 独立维护 `access.log`，记录 HTTP 请求生命周期。业务日志中的 `task_id` 用于关联任务过程和模型请求，模型 `request_id` 标识单次模型调用；访问日志中的 `request_id` 来自 HTTP 请求头或自动生成，标识一次 Web 请求。

## 扩展方式

1. 增加文件格式时，在 `documents/extraction/` 实现解析器，通过统一提取入口返回文本与链接证据。
2. 增加本地检查规则时，在 `checks/` 实现规则，通过 `tasks/runner.py` 编排执行和结果持久化。
3. 增加模型能力时，在 `models/` 明确协议输入、输出、超时和取消行为。
4. 增加页面或接口时，在 `web/` 对应职责文件中注册路由，调用任务、报告或模型服务。
5. 调整数据库查询时，使用参数绑定，检查现有索引的执行计划，并保持数据库结构兼容契约。

`web/__init__.py` 统一注册认证、用户任务、模型管理、管理概览、管理任务和系统设置路由。模板和静态资源分别保存在 `web/templates/` 与 `web/static/`。

五类任务的用户端和管理端共用 `_task_check_picker.html`，渲染时传入任务归属身份 `identity` 和任务类型 `active_nav`。`app.js` 按这两个维度在浏览器本地保存提交时的检查项 ID；多项首次不预选，恢复时仅匹配当前可用项，单项默认选中。选择计数、全选、清空和提交前校验由同一套前端逻辑处理。

## 回归验证

```bash
uv run ruff format .
uv run ruff check .
uv run python -m unittest discover -s tests
```

- `tests/test_architecture.py` 检查目录约束、模块依赖、后台导入独立性、HTTP 路由和数据库结构。
- `tests/fixtures/http_routes.json` 定义默认配置下的路由、端点名称与 HTTP 方法契约；代理前缀、认证、下载和参数行为由配置与路由测试覆盖。
- `tests/fixtures/database_schema.json` 定义 SQLite 表、索引和触发器的兼容契约。
- `tests/test_database_indexes.py` 检查新库索引初始化、已有库补齐、记录和表定义保持、重复与并发初始化及失败重试。
- `tests/test_performance.py` 检查用户状态统计与文件清理的索引使用、轮询字段读取与缓存失效、列表元数据选择、队列和统计刷新计算量、模型批量查询、文档定位及独立提交服务。
- `tests/test_document_performance.py` 检查 Excel 单工作簿读取、缓存与共享公式、日期类型、重叠链接、稀疏行列，以及 PDF 空单元格扫描次数和坐标索引等价性。`scripts/benchmark_documents.py` 测量合成宽表与 PDF 表格的解析耗时及输出摘要。
- `tests/test_server_runtime.py` 检查 WSGI 请求体、代理信息、线程配置和监督器退出通知；`tests/test_server_integration.py` 使用真实 HTTP 服务检查多进程、上传下载、任务执行和退出清理。
- `tests/test_tasks.py` 和 `tests/test_task_supervisor.py` 检查认领、取消、执行权、存活过期任务防重入、进程名额及检查项执行。
- `tests/test_task_process_lifecycle.py` 使用真实子进程检查 GIL 阻塞期间续约、满负载、强制取消、启动许可、入口与初始化超时、状态文件写入失败时终止进程，以及监督器被终止后的遗留进程恢复。`tests/test_server_runtime.py` 另验证 Windows 虚拟环境下监督器和任务进程的实际解释器命令。
- `tests/test_document_cancellation.py` 检查 PDF 分页与表格取消、512 列 Excel 分行取消及异常传播。
- `tests/test_logging.py` 检查业务日志分流、模块与进程标识、异常堆栈、重复初始化和独立轮转；`tests/test_observability.py` 检查访问日志及健康检查。

项目直接导入的第三方库在 `pyproject.toml` 中显式声明，由 `uv.lock` 锁定完整依赖树。Python 标准库由解释器提供；功能扩展依赖通过 extras 声明，例如 `pypdf[image]`。`ffmpeg`、`ffprobe` 由运行环境安装并加入 `PATH`。

Windows 和 Linux 使用相同的 Uvicorn 与 a2wsgi 依赖。默认单进程运行 Web 服务；配置多个 Web worker 时，Uvicorn 使用 `spawn` 管理子进程，事件循环统一使用 `asyncio`，HTTP 协议处理统一使用 `h11`。代理身份与地址由 Flask 的代理配置处理。`psutil` 提供跨平台进程存活检测，父进程通过标准输入管道通知监督器退出。

文本文件统一采用 UTF-8 和 LF。配置、数据库、上传文件和生成产物保存在 Git 忽略的运行目录中。
