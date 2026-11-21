# 前端开发约定

- 使用 Vue 3 Composition API、TypeScript strict 和 Vite，依赖由 `package-lock.json` 固定。
- `app` 管理应用外壳、主题和页面组件注册；`pages` 组合业务模块；`features` 管理领域状态和服务端片段交互；`api` 管理同源请求、会话和用户资料。
- 基础 Vue 控件及原生 DOM 适配器位于 `components/ui`。业务组件通过公共控件复用按钮、弹窗、通知、上传和选择能力。
- Flask 提供页面路由、权限过滤后的表单和报告片段。Vue 仅编译仓库内的组件模板，用户数据作为文本或属性传入。
- 页面入口在 `app/pages.ts` 注册。页面按需加载，局部列表刷新保留任务创建组件和用户草稿。
- 新业务状态和 Vue 组件使用 TypeScript；服务端片段控制器使用 ES modules，并接受 ESLint 与浏览器回归验证。
- 请求使用 `api/client.ts`，组件卸载、弹窗关闭或请求所依据的草稿改变时取消对应请求。异步结果应用前核对请求状态。
- 颜色、间距、焦点、阴影和浮层使用 `styles/tokens.css`。默认深色，浅色使用相同的语义变量，主题保存在 `localStorage.theme`。
- 表格横向滚动限定在容器内，页面支持桌面侧栏、折叠侧栏和移动端导航。弹窗使用原生 dialog 管理焦点与 Escape。
- 文本使用 UTF-8 和 LF。构建目录 `app/web/static/frontend/` 只保存当前 manifest 及其资源，并随源码提交。
- 交付前运行 `npm run lint`、`npm run typecheck`、`npm run test:unit`、`npm run build`、`npm run test:e2e`，并执行仓库要求的 Python 格式化与相关后端回归。

详细分层、开发和部署见 [前端开发](../docs/frontend-development.md)。
