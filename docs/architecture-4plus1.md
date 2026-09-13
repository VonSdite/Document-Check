# 4+1 架构视图

本文描述文档智能门禁当前的运行架构。系统保持现有 HTTP 接口、页面行为和任务状态接口不变。

## 总览

```text
浏览器
  |
  v
run.py 主进程
  |
  +-- Gunicorn master
  |     |
  |     +-- Web worker 1: gthread 原生线程 x16
  |     +-- Web worker 2: gthread 原生线程 x16
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

### Web 应用

- `app/__init__.py` 创建 Web 应用、配置日志、初始化数据库和注册路由。
- `app/routes.py` 注册用户页面、管理员页面、任务接口和健康检查接口，并编排请求参数、权限与响应。
- `app/model_service.py` 负责用户模型提供商、模型配置和可用模型快照。
- `app/task_submission.py` 负责各类任务的请求校验、文件入库和任务快照事务。
- `app/task_files.py` 负责上传路径、文件访问、下载打包和文件清理辅助。
- `app/reporting/constants.py` 定义报告字段、标注状态、统计口径和 Excel 格式。
- `app/reporting/service.py` 负责报告解析、去重排序、误报规则、人工复核和统计缓存。
- `app/reporting/excel.py` 负责报告工作簿导出与离线标注回填。
- `app/observability.py` 提供访问日志、启动自检和就绪探针。
- `app/config.py` 读取并规范化 `config.yaml`。

### 任务执行

- `app/task_supervisor.py` 负责单实例监督、任务认领、任务进程生命周期、租约恢复和后台维护。
- `app/tasks.py` 的 `TaskRunner` 编排单个任务并执行文本检查项。
- `app/task_runtime/preprocessing.py` 负责文档读取、图片准备、视频抽帧、多文档组装和预处理结果持久化。
- `app/task_runtime/image_checks.py` 和 `video_checks.py` 分别编排图片与视频检查。
- `app/task_runtime/multimodal_protocol.py` 负责多检查项模型请求和结构化返回解析。
- `app/task_runtime/multimodal_common.py` 提供批次、上下文、图片输入和结果摘要辅助。
- `app/task_runtime/common.py` 提供任务数据、结果合并和通用配置辅助。
- `app/task_runtime/state.py` 负责任务租约、取消、进度、中间结果和最终状态。
- `app/task_runtime/artifacts.py` 负责任务文件统计、缓存快照和清理。
- `app/llm.py` 负责模型请求、流式响应、重试、取消检查和输出解析。
- `app/documents.py` 提供稳定的文档提取入口，`app/extraction/` 按 PDF、DOCX、表格和文本标记格式实现解析。
- `app/images.py` 和 `app/videos.py` 负责图片提取、页面渲染和视频抽帧。
- PDF 文本优先由 PyMuPDF 提取，文本为空或包含异常字符时按页使用 pypdf 回退；表格识别只在页面包含候选向量边线时执行。
- 图片检查提取 PDF 文本上下文时跳过表格结构化，随后独立执行内嵌图片提取和页面截图渲染。
- 视频采样使用有界的两路 `ffmpeg` 并行抽帧，保留采样顺序和单帧失败回退策略。

### 持久化和文件

- `app/db.py` 管理 SQLite 连接、表结构、迁移、设置和默认数据。
- SQLite 使用 WAL 和每个应用上下文独立连接，支持多个 Web worker、任务 supervisor 和任务进程并发访问。
- `instance/document_check.sqlite3` 保存任务、配置、状态和报告数据。
- `instance/uploads/` 与 `instance/extracted_images/` 保存任务文件和处理产物。
- `instance/logs/` 保存应用日志和访问日志。

## 开发视图

```text
app/
  __init__.py            应用工厂与日志
  config.py              本地配置
  routes.py              HTTP 路由与请求编排
  model_service.py       用户模型配置服务
  task_submission.py     任务提交事务
  task_files.py          任务文件与上传路径
  observability.py       日志与健康检查
  db.py                  SQLite 连接与数据模型
  task_supervisor.py     任务监督器与任务进程入口
  tasks.py               TaskRunner 与检查项编排
  task_runtime/
    common.py            任务数据与结果辅助
    preprocessing.py     任务输入预处理
    image_checks.py      图片检查编排
    video_checks.py      视频检查编排
    multimodal_protocol.py  多模态请求与返回协议
    multimodal_common.py 多模态批次与结果辅助
    state.py             租约、取消、进度与结果状态
    artifacts.py         任务文件统计与清理
  reporting/
    constants.py         报告字段与状态定义
    service.py           报告解析、复核与统计
    excel.py             报告导出与离线回填
  llm.py                 模型客户端
  documents.py           文档提取入口
  extraction/
    pdf.py               PDF 文本与表格提取
    docx.py              DOCX 文本与链接提取
    spreadsheets.py      XLSX、XLSM 与 XLS 提取
    markup.py            TXT、Markdown 与 HTML 提取
  images.py              图片提取与页面渲染
  videos.py              视频抽帧
  templates/             Jinja 页面模板
  static/                前端脚本与样式
run.py                   Gunicorn 启动入口
tests/                   单元测试与并发行为测试
```

依赖方向为：路由调用报告与任务领域模块，任务监督器调用 `TaskRunner`，`TaskRunner` 调用任务运行和输入提取模块，持久化模块通过 `db.py` 访问 SQLite。Web worker 处理 HTTP 请求和轻量数据库操作，后台任务由 supervisor 和任务进程执行。

## 进程视图

### 进程和线程角色

| 角色 | 默认数量 | 执行内容 |
| --- | ---: | --- |
| Gunicorn master | 1 | 管理 Web worker、监听端口和优雅停止 |
| Web worker | 2 | Flask 请求处理 |
| Web worker 原生线程 | 每个 16 | 同一 worker 内的并发 HTTP 请求 |
| 任务 supervisor | 1 | SQLite 队列调度、任务进程管理和租约恢复 |
| 任务进程 | 最多 4 | 一个进程执行一个任务 |
| 任务内检查线程 | 按 `check_item_concurrency` | 一个任务内并行执行检查项 |

任务 supervisor 由 `run.py` 创建为独立 Python 子进程，再启动 Gunicorn master。任务进程使用 `spawn` 创建，任务进程只接收任务 ID 和租约令牌；每个进程自行创建应用对象和数据库连接。

Web 请求线程、任务进程和任务内检查线程都使用 Python 原生线程或进程。外部模型请求属于 I/O 操作，线程在等待网络响应时释放执行资源；文档解析和视频处理在独立任务进程中运行。

## 物理视图

### 单机部署

```text
客户端
  |
  v
可选反向代理（Nginx 等）
  |
  v
run.py
  +-- Gunicorn master + Web workers
  +-- task supervisor + task processes
  +-- instance/document_check.sqlite3
  +-- instance/uploads/
  +-- instance/extracted_images/
  +-- instance/logs/
  |
  v
外部模型服务
```

服务通过 `uv run python run.py` 启动。`config.yaml` 保存监听地址、端口、Gunicorn worker/线程数和任务进程上限。`uv.lock` 固定 Python 依赖版本。视频任务还需要系统提供 `ffmpeg` 和 `ffprobe`。

### 并发边界

- Web 并发容量由 `web_workers × web_threads` 提供，默认值为 `2 × 16`。
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
