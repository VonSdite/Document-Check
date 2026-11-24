# 前端开发与组件

前端使用 Vue 3、TypeScript strict 和 Vite，采用服务端多页入口与组件挂载。Flask 负责身份、权限、页面路由、原生表单提交和报告片段；Vue 负责应用侧栏、主题、任务创建、文件选择、模型清单、密码输入和通知。服务端片段的筛选、轮询、标注与设置交互由独立 ES modules 管理。

## 目录和依赖

```text
frontend/
  src/
    app/                  页面注册、组件挂载、侧栏、主题
    pages/                按页面加载的业务入口
    features/             任务、模型、报告、设置和权限
    components/ui/        基础 Vue 控件与 DOM 适配器
    api/                  同源请求、会话与模型接口
    styles/               语义变量、公共控件与页面布局
  scripts/build.mjs      构建并清理过期哈希资源
  tests/unit/            组件、请求与状态验证
  tests/e2e/             浏览器业务流程验证
app/web/
  frontend.py             manifest 资源解析与任务展示协议
  templates/components/   共享任务表单、列表和提供商弹窗
  static/frontend/        manifest 与生产资源
```

页面引用业务组件和基础控件，业务请求统一经过 `api/client.ts`。接口地址由服务端生成，支持应用路径前缀。导航从服务端按权限输出的链接读取，前端保留其地址、标题和当前页面状态；服务端对每次操作继续执行授权校验。

`TaskComposer` 从服务端原生表单读取上传字段、模型、检查项、检查轮数和提交令牌，并保留挂载前的文件与选择。五类任务的用户端和管理端共用表单组件及两种列表组件。`ModelEditor` 使用稳定行标识，通过隐藏字段提交模型配置，拉取与测试结果关联当前连接草稿。

`UserDomainEditor` 与 `UserDomainBulkEditor` 共用 `UserDomainDialog` 编辑领域标签，复用公共弹窗和同源请求。`UserDomainSelection` 管理本页多选与全选；筛选器和各行编辑器共享领域目录与保存结果，批量更新后可继续单人编辑。标签保存与权限自动保存分别提交，保存失败保留选择和草稿。

列表和报告保持服务端片段协议。`features/tasks/list.js` 管理筛选、分页、历史与增量刷新；`features/reports` 管理条目复核、详情轮询和模型输出。单文档报告由 `_report_table.html` 展示表格和初审控件，独立终审页由 `final_review.html` 筛选并展示初审不认可的条目；`data-task-detail` 与 `data-report-audit` 共用报告模块入口。初审与终审共用条目版本，保存时锁定当前条目的审核控件。表格初审更新保存状态和报告统计；审核条目通过服务端片段更新初审、终审、历史及进度。独立终审页仅展示初审不认可的条目，并将其计入终审进度。初审结论、原因、说明和审核人信息只读展示，保存终审后的片段更新保持只读；初审编辑在检查报告页完成。重新进入终审页时按最新初审结论筛选，无剩余条目时显示无需终审提示。审核历史持续保留在报告导出中。终审说明草稿和进行中的保存会阻止轮询替换，保存失败保留终审输入。共享事件通过明确模块导入或 DOM 事件交接。

## 开发与构建

使用 Node.js 24、npm 11 和项目的 uv 环境。在仓库根目录执行：

```bash
npm --prefix frontend ci
npm --prefix frontend run build
uv run python run.py
```

Windows PowerShell 7 可使用 `npm.cmd` 调用相同命令。生产资源随源码提交，运行 Python 服务需要同一版本的 manifest 和全部资源；运行时无需 Node.js。

持续开发可执行 `npm --prefix frontend run dev`，Vite 监听源文件并写入构建目录；通过 Flask 的实际页面访问，刷新页面读取最新资源。Python 注册和模板代码变化后重启服务。构建入口使用相对资源路径，Flask 通过 `url_for` 应用部署前缀。

构建脚本保留当前 manifest 中的入口、动态分块与样式，仅清理固定构建目录中的过期哈希文件。`src/report-export.ts` 同时生成离线报告专用的独立脚本和样式，导出的 HTML 内嵌这两项资源，直接打开文件即可查看与调整列宽。`node_modules`、测试结果和缓存不参与提交。

## 主题与交互

侧栏支持折叠，900px 以下使用移动端导航弹窗。默认浅色，手动选择的深浅主题通过 `localStorage.theme` 保存并优先使用；浏览器存储不可用时使用浅色。侧栏偏好单独保存。配置凭据只保留在表单与当前组件状态中。

基础控件使用 `styles/tokens.css` 中的语义颜色、间距和焦点变量。表格在自身容器内横向滚动，文件选择支持累加、去重、移除和数量限制。弹窗通过原生 dialog 提供焦点约束、Escape 关闭和焦点恢复。

报告表格在桌面端按容器宽度分配列宽，初审控件与正文同时显示。条目序号保持可见，有轮次或重复组信息时显示“来源”入口，默认折叠详情；展开时列宽保持稳定。在线页面与离线 HTML 共用条目来源组件。

新页面在 `app/pages.ts` 注册匹配标记与异步入口。新增 Vue 控件在 `components/ui/index.ts` 导出，业务组件放入对应 `features` 目录。SSR 片段控制器使用模块导入共享能力。用户输入作为组件 props、文本或原生字段传递；Vue 模板来自仓库源码。

## 验证

```bash
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test:unit
npm --prefix frontend run build
npm --prefix frontend run test:e2e
node --test tests/test_task_form_ui.js tests/test_task_list_ui.js tests/test_permissions_ui.js
uv run python -m unittest tests.test_routes tests.test_user_domains tests.test_permissions tests.test_cookie_session_routes tests.test_config tests.test_architecture tests.test_frontend
```

浏览器验证使用本机 Chrome 和 `frontend/tests/server.py` 提供的隔离工作区，示例模型请求由测试拦截。测试服务监听 `127.0.0.1:8768`，端口用于测试配置；实际工具的监听地址与端口由本地 `config.yaml` 控制。截图与 trace 保存在忽略的 `instance/frontend-test-results/`。组件测试覆盖文件合并、模型配置边界、草稿关联、请求取消、同源限制及用户资料版本；浏览器测试覆盖任务提交、筛选保留草稿、模型保存、报告标注、规则分页、权限、主题、移动端导航与 HTML 报告离线使用。
