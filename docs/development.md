# 开发与模块边界

## 应用入口

项目通过 `uv run python run.py` 启动。`run.py` 调用 `app.bootstrap.factory.create_app()`，创建独立监督器进程并启动 Gunicorn。

`app/` 是 Python 命名空间包，根目录只包含模块目录。Web 工厂导入方式为：

```python
from app.bootstrap.factory import create_app

app = create_app()
```

工厂接受可选的 `root_dir: Path`，用于指定配置和运行数据所在目录。模板与静态资源定位到代码中的 `app/web/`；运行目录负责保存 `config.yaml` 和 `instance/`。

`app.infrastructure.runtime.create_task_app()` 创建后台进程使用的应用上下文，并在上下文结束时关闭数据库连接。监督器通过 `python -m app.bootstrap.supervisor` 启动，任务执行入口为 `app.tasks.supervisor.run_claimed_task`。监督器向任务进程传递运行根目录，使子进程使用相同的配置和数据库。

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

HTTP 请求、会话、页面渲染和跳转由 `web` 与认证适配器负责。任务和报告服务通过参数接受业务数据，返回结构化结果；数据库和日志操作使用进程内的 Flask 应用上下文。

- `TaskSubmission` 承载文件列表、检查项、模型选择和提交令牌。`submit_document_task` 等提交服务返回 `SubmissionResult`，Web 层将消息和任务类型转换为提示与页面跳转。
- `identity.models.UserIdentity` 是独立的身份数据类型。模型查询显式接收用户主体，任务提交使用身份快照保存归属信息。
- `models.service` 管理提供商与模型配置，`models.client` 管理模型协议请求，`models.discovery` 管理模型列表发现。
- `reporting.service.update_report_item_type(task, data)` 接收复核数据，`reporting.excel.build_report_workbook(task)` 返回工作簿内容；下载响应由 `web.reports` 构造。
- `reporting.statistics.refresh_stale_report_stats_batch()` 在后台应用上下文中运行。监督器直接调用报告模块。
- `documents.extraction` 按文件类型分派，具体格式由 `pdf.py`、`docx.py`、`spreadsheets.py` 和 `markup.py` 实现。

## 扩展方式

1. 增加文件格式时，在 `documents/extraction/` 实现解析器，通过统一提取入口返回文本与链接证据。
2. 增加本地检查规则时，在 `checks/` 实现规则，通过 `tasks/runner.py` 编排执行和结果持久化。
3. 增加模型能力时，在 `models/` 明确协议输入、输出、超时和取消行为。
4. 增加页面或接口时，在 `web/` 对应职责文件中注册路由，调用任务、报告或模型服务。
5. 调整数据库查询时，使用参数绑定，检查现有索引的执行计划，并保持数据库结构兼容契约。

`web/__init__.py` 统一注册认证、用户任务、模型管理、管理概览、管理任务和系统设置路由。模板和静态资源分别保存在 `web/templates/` 与 `web/static/`。

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
- `tests/test_performance.py` 检查用户状态统计与文件清理的索引使用、队列和统计刷新计算量、模型批量查询、文档定位及独立提交服务。
- `tests/test_tasks.py` 和 `tests/test_task_supervisor.py` 检查多进程认领、取消、租约恢复与检查项执行。

Python 依赖由 `pyproject.toml` 和 `uv.lock` 管理，文本文件统一采用 UTF-8 和 LF。配置、数据库、上传文件和生成产物保存在 Git 忽略的运行目录中。
