(function () {
  "use strict";
  const M = window.RuleLibraryDemo;
  const A = window.RuleLibraryAutomation;
  const STORAGE_KEY = "document-check:rule-library-prototype:v2";
  const labels = { overview: "待审核工作台", conflicts: "异常与冲突", rules: "已发布规则", cases: "反馈归档", trial: "文档试运行", history: "运行与版本" };
  const verdict = { issue: "机器检出问题", exception: "符合例外", clear: "未发现问题", review: "证据待确认", skipped: "范围外跳过", conflict: "冲突待复核" };
  const tones = { issue: "red", exception: "green", clear: "green", review: "amber", skipped: "", conflict: "amber" };
  const view = document.querySelector("#view");
  const dialog = document.querySelector("#detail-dialog");
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const badge = (text, tone = "") => `<span class="badge ${tone}">${esc(text)}</span>`;
  const button = (text, action, id = "", style = "", attrs = "") => `<button type="button" class="button ${style}" data-action="${action}" data-id="${esc(id)}" ${attrs}>${text}</button>`;
  const quote = (lines) => lines.map((line) => `<div class="quote">${esc(line)}</div>`).join("");
  const time = (stamp) => new Date(stamp).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  const heading = (name, description, actions = "") => `<div class="page-heading"><div><div class="eyebrow">FEEDBACK TO KNOWLEDGE</div><h1>${name}</h1><p>${description}</p></div><div class="actions">${actions}</div></div>`;
  const empty = (name, description, actions = "") => `<div class="empty"><div class="empty-icon">✓</div><h3>${name}</h3><p>${description}</p>${actions}</div>`;
  const scope = (item) => `${item.product} · ${item.docType} · ${item.docVersion}`;
  let page = "overview";
  let state;
  let selection = new Set();
  let scopeFilter = "all";
  let caseFilter = "all";
  let caseGroup = "all";
  let search = "";
  let casePage = 1;
  let scenarioId = "conflict";
  let selectedRun = null;
  let sheetState = null;
  let toastTimer;
  let storageAvailable = true;

  function load() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (saved?.schema === 2 && saved.automation && ["cases", "rules", "runs", "audit"].every((key) => Array.isArray(saved[key])) && Array.isArray(saved.automation.groups)) return saved;
    } catch { storageAvailable = false; }
    return A.seed();
  }
  function save() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
    catch { storageAvailable = false; }
    document.querySelector("#save-state").textContent = storageAvailable ? "示例数据 · 已保存到本机" : "示例数据 · 当前页面暂存";
  }
  function toast(text, error = false) {
    const target = document.querySelector("#toast");
    clearTimeout(toastTimer);
    target.textContent = text;
    target.classList.toggle("error", error);
    target.hidden = false;
    toastTimer = setTimeout(() => { target.hidden = true; }, 4500);
  }
  function errorMessage(error) {
    const target = document.querySelector("#dialog-error");
    if (dialog.open && target) {
      target.hidden = false;
      target.textContent = error.message;
      target.scrollIntoView({ block: "nearest" });
    } else toast(error.message, true);
  }
  function navigate(target) {
    if (!labels[target]) return;
    if (page !== target) { search = ""; casePage = 1; }
    page = target;
    location.hash = page;
    if (dialog.open) dialog.close();
    render();
    window.scrollTo({ top: 0 });
  }
  function readyGroups() { return state.automation.groups.filter((group) => group.status === "ready"); }
  function blockedGroups() { return state.automation.groups.filter((group) => group.status === "blocked"); }
  function pendingResults() { return state.runs.flatMap((run) => run.results.filter((item) => ["review", "conflict"].includes(item.status) && !item.feedback).map((item) => ({ run, item }))); }
  function caseBadge(item) {
    if (item.type === "suggestion") return badge("认可核查价值", "purple");
    if (item.rejection === "model_hallucination") return badge("证据幻觉反例", "amber");
    return badge(item.feedback === "accepted" ? "人工认可" : "人工不认可", item.feedback === "accepted" ? "green" : "amber");
  }
  function disposition(group) { return ({ ready: "已归入待审核主题", blocked: "主题待处理", archived: "已自动归档" })[group.status] || "自动整理中"; }
  function summaryMetrics() {
    const stats = A.metrics(state);
    return `<div class="metrics">${[["已接收反馈", stats.received, `来自 ${stats.independent} 份独立证据`, "▤"], ["合并重复反馈", stats.duplicates, "同源重复检查不重复计入支持数", "↳"], ["待审核规则", stats.ready, "审核一次覆盖一批同类案例", "◇"], ["需判断的主题", stats.blocked, "相反反馈或尚缺业务依据", "⊙"]].map(([name, count, note, icon]) => `<div class="metric"><div class="metric-top"><span>${name}</span><span class="metric-symbol">${icon}</span></div><div class="metric-value">${count.toLocaleString("zh-CN")}</div><small>${note}</small></div>`).join("")}</div>`;
  }
  function ruleCard(group) {
    const rule = M.findRule(state, group.ruleId);
    const stats = A.groupStats(state, group);
    const validation = rule.validation;
    const selectedRules = [...selection].map((id) => M.findRule(state, id));
    const otherScope = selectedRules.length > 0 && A.batchKey(selectedRules[0]) !== A.batchKey(rule);
    const limited = stats.independent < 3;
    return `<article class="candidate-card ${selection.has(rule.id) ? "selected" : ""}"><div class="candidate-top"><label class="candidate-select"><input type="checkbox" data-select-rule="${rule.id}" aria-label="选择 ${esc(rule.title)} ${esc(scope(rule))}" ${selection.has(rule.id) ? "checked" : ""} ${otherScope || limited ? "disabled" : ""}><span class="mono">${rule.id}</span></label><div class="actions">${badge(`待审核 v${rule.version + 1}`, "blue")}${badge("自动验证通过", "green")}</div></div><h2>${esc(rule.title)}</h2><div class="scope-line">${esc(scope(rule))} <span>· ${esc(group.check)}</span></div><div class="rule-conditions"><div><span class="condition-label">判错</span><p>${esc(rule.condition)}</p></div><div><span class="condition-label green">例外</span><p>${esc(rule.useException ? rule.exception : "当前草稿未启用例外条件")}</p></div></div><div class="evidence-metrics"><span><strong>${stats.positive}</strong> 正例</span><span><strong>${stats.negative}</strong> 反例</span><span><strong>${stats.independent}</strong> 独立证据</span><span><strong>${stats.received}</strong> 条原始反馈</span></div><div class="candidate-bottom"><span class="caption">历史样本 ${validation.history.length}/${validation.history.length} · 边界场景 ${validation.results.length}/${validation.results.length}${limited ? " · 来源较少，单独审核" : ""}</span><div class="actions">${button("查看依据与审核", "rule", rule.id, "small soft")}</div></div></article>`;
  }
  function overview() {
    const stats = A.metrics(state);
    const pending = readyGroups();
    const scopes = [...new Map(pending.map((group) => { const rule = M.findRule(state, group.ruleId); return [A.batchKey(rule), scope(rule)]; })).entries()];
    if (scopeFilter !== "all" && !scopes.some(([key]) => key === scopeFilter)) scopeFilter = "all";
    const groups = pending.filter((group) => scopeFilter === "all" || A.batchKey(M.findRule(state, group.ruleId)) === scopeFilter);
    return `${heading("待审核工作台", "反馈已自动整理好。查看关键证据，按规则审核，按异常介入。", button("＋ 模拟收到一批反馈", "simulate", "", "primary"))}${summaryMetrics()}
      <section class="automation-strip"><span class="automation-mark">✓</span><div><strong>自动整理已完成</strong><p>${stats.received.toLocaleString("zh-CN")} 条反馈 → ${stats.unique} 条去重案例 → ${stats.archivedReceipts} 条反馈已归档；人工只处理 ${stats.ready} 条候选规则与 ${stats.blocked} 类异常。</p></div>${button("查看归档明细", "navigate", "cases", "small")}</section>
      <div class="workspace-columns"><div><section class="panel candidate-panel"><div class="panel-header"><div><h2>待审核规则 <span class="count-inline">${pending.length}</span></h2><p>判错条件、例外与正反案例已自动归纳；展开查看代表性证据。</p></div>${badge("自动验证完成", "green")}</div><div class="batch-toolbar"><label class="scope-filter"><span class="sr-only">筛选审核范围</span><select id="scope-filter"><option value="all">全部适用范围</option>${scopes.map(([key, name]) => `<option value="${esc(key)}" ${scopeFilter === key ? "selected" : ""}>${esc(name)}</option>`).join("")}</select></label><div class="actions">${button("选择同范围规则", "select-scope", "", "small")}${selection.size ? button("清空", "clear-selection", "", "small") : ""}${button(`批量审核${selection.size ? `（${selection.size}）` : ""}`, "batch-review", "", "small primary", selection.size ? "" : "disabled")}</div></div><div class="batch-hint">同产品、文档类型、版本和检查项可一起审核；其他范围分别处理。</div><div class="candidate-list">${groups.map(ruleCard).join("") || empty("当前范围没有待审核规则", "系统会持续归档已覆盖的反馈，发现新范围或新例外时再生成待办。", button("模拟接收新反馈", "simulate"))}</div></section></div><aside class="workbench-aside"><section class="panel"><div class="panel-header"><h2>需要你判断</h2>${badge(`${stats.blocked} 类`, "amber")}</div>${blockedGroups().slice(0, 3).map((group) => `<div class="attention-item"><div class="actions">${badge(group.blockType === "contradiction" ? "相反人工反馈" : group.blockType === "validation" ? "验证未通过" : "待补业务依据", "amber")}</div><h3>${esc(group.title)}</h3><p>${esc(scope(group))}</p><p>${esc(group.reason)}</p><button class="text-button" data-action="conflict" data-id="${group.id}">查看并处理 →</button></div>`).join("") || empty("主题异常已处理", "新的矛盾或验证失败会出现在这里。")}</section><section class="panel"><div class="panel-header"><h2>人工审核到哪里为止？</h2></div><div class="principles"><div class="principle"><span class="number">01</span><div><strong>审核规则，无需重复标注案例</strong><p>报告里的认可和不认可自动进入整理流程。</p></div></div><div class="principle"><span class="number">02</span><div><strong>同类反馈持续积累</strong><p>满足已审核边界时自动归档，保持发布版本。</p></div></div><div class="principle"><span class="number">03</span><div><strong>新范围与冲突单独判断</strong><p>机器判定单独记录，人工认可始终有来源。</p></div></div></div></section></aside></div>`;
  }
  function conflictsPage() {
    const pending = pendingResults();
    return `${heading("异常与冲突", "只把无法自动消解的差异交给人。原始反馈保留，判断依据单独记录。")}<div class="conflict-grid">${blockedGroups().map((group) => { const stats = A.groupStats(state, group); return `<section class="panel"><div class="panel-header"><div>${badge(group.blockType === "contradiction" ? "同源反馈相反" : group.blockType === "validation" ? "规则验证失败" : "建议尚未确认", "amber")}<h2 style="margin-top:10px">${esc(group.title)}</h2></div><span class="mono">${group.id}</span></div><div class="panel-body"><p class="caption">${esc(scope(group))} · ${stats.unique} 条去重反馈 / ${stats.independent} 份独立证据</p><div class="explanation amber" style="margin:16px 0">${esc(group.reason)}</div>${group.pendingNote ? `<p class="caption">上次备注：${esc(group.pendingNote)}</p>` : ""}${button("查看证据并处理", "conflict", group.id, "soft")}</div></section>`; }).join("") || `<section class="panel">${empty("当前没有主题异常", "已解决的异常仍可在反馈归档与操作记录中回看。")}</section>`}</div><section class="panel"><div class="panel-header"><div><h2>试运行中的待确认条目</h2><p>明确的机器检出支持按需抽样复核；证据不足或冲突条目集中展示。</p></div>${badge(`${pending.length} 条`, "amber")}</div>${pending.length ? `<div class="table-scroll"><table><thead><tr><th>试运行</th><th>规则与原因</th><th>处理</th></tr></thead><tbody>${pending.map(({ run, item }) => `<tr><td>${run.id}<small>${esc(run.scenario.title)}</small></td><td>${esc(item.title)}<small>${esc(item.reason)}</small></td><td>${button("核对证据", "review-result", item.ruleId, "small", `data-run="${run.id}"`)}</td></tr>`).join("")}</tbody></table></div>` : empty("暂无试运行待确认项", "可到文档试运行体验缺少证据和适用条件不明的情况。", button("前往试运行", "navigate", "trial"))}</section>`;
  }
  function rulesPage() {
    const rules = state.rules.filter((rule) => rule.kind === "check" && rule.published);
    return `${heading("已发布规则", "新任务使用已启用版本。新增案例归档、规则修订和版本发布分别记录。", button("查看待审核规则", "navigate", "overview", "primary"))}<section class="panel"><div class="table-scroll"><table><thead><tr><th>规则</th><th>适用范围</th><th>当前版本</th><th>累计去重案例</th><th>操作</th></tr></thead><tbody>${rules.map((rule) => { const group = state.automation.groups.find((entry) => entry.id === rule.groupId); return `<tr><td><h3>${esc(rule.title)}</h3><small>${rule.id} · ${rule.family === "term" ? "词表检查" : "语义检查演示"}</small></td><td>${esc(scope(rule))}</td><td>${badge(rule.enabled ? "已启用" : "已停用", rule.enabled ? "green" : "")}<small>v${rule.version}${["draft", "review"].includes(rule.status) ? " · 有待审核修订" : ""}</small></td><td>${group ? A.groupStats(state, group).unique : "—"}</td><td><div class="actions">${button("查看", "rule", rule.id, "small")}${button(rule.enabled ? "停用" : "恢复", "toggle-rule", rule.id, "small")}</div></td></tr>`; }).join("")}</tbody></table></div></section><section class="panel"><div class="panel-header"><h2>旧误报参考的衔接</h2>${badge("条件可追溯", "blue")}</div><div class="panel-body">${state.rules.filter((rule) => rule.kind === "legacy").map((rule) => `<div class="row-between"><div><h3>${esc(rule.title)}</h3><p class="caption">${rule.id} · ${esc(rule.reason)}</p></div>${badge(rule.linkedRuleId ? `已关联 ${rule.linkedRuleId}` : "已进入规则归纳，待审核", rule.linkedRuleId ? "green" : "amber")}</div>`).join("")}<div class="explanation blue" style="margin-top:18px">例外条件审核后，系统在每次检查中核对当前工况。可以解释的历史误报差异自动处理；无法解释的冲突进入异常队列。</div></div></section>`;
  }
  function casesPage() {
    const matches = state.cases.filter((item) => {
      const group = A.groupFor(state, item.groupId);
      return (caseFilter === "all" || group.status === caseFilter) && (caseGroup === "all" || group.id === caseGroup) && `${item.id} ${item.title} ${item.document} ${item.reason}`.toLowerCase().includes(search.toLowerCase());
    });
    const pages = Math.max(1, Math.ceil(matches.length / 20));
    casePage = Math.min(casePage, pages);
    const rows = matches.slice((casePage - 1) * 20, casePage * 20);
    return `${heading("反馈归档", "自动收录报告里的人工反馈。同源重复折叠，正反反馈及原文分别保留。")}<section class="panel"><div class="toolbar"><div class="filters">${[["all", "全部反馈"], ["archived", "已归档"], ["ready", "规则待审核"], ["blocked", "主题待处理"]].map(([id, name]) => `<button class="filter ${caseFilter === id ? "active" : ""}" data-action="case-filter" data-id="${id}">${name}</button>`).join("")}</div><input type="search" class="search" id="search" value="${esc(search)}" placeholder="搜索案例、文档或依据…" aria-label="搜索反馈"></div><div class="group-filter"><label for="case-group">按主题查看</label><select id="case-group"><option value="all">全部主题</option>${state.automation.groups.map((group) => `<option value="${group.id}" ${caseGroup === group.id ? "selected" : ""}>${esc(group.title)} · ${esc(scope(group))}</option>`).join("")}</select></div><div class="table-scroll"><table><thead><tr><th>案例与来源</th><th>原人工反馈</th><th>合并条数</th><th>系统处理</th><th>详情</th></tr></thead><tbody>${rows.map((item) => { const group = A.groupFor(state, item.groupId); return `<tr><td><h3>${esc(item.title)}</h3><small>${esc(item.id)} · ${esc(item.document)}</small></td><td>${caseBadge(item)}</td><td>${item.receipts}<small>1 份原文证据</small></td><td>${badge(disposition(group), group.status === "archived" ? "green" : group.status === "ready" ? "blue" : "amber")}</td><td>${button("查看原文", "case", item.id, "small")}</td></tr>`; }).join("")}</tbody></table></div>${rows.length ? "" : empty("没有匹配反馈", "切换主题或清空搜索可以查看其他反馈。")}<div class="pagination-row"><span>共 ${matches.length} 条去重反馈 · 第 ${casePage}/${pages} 页</span><div class="actions">${button("上一页", "case-page", String(casePage - 1), "small", casePage === 1 ? "disabled" : "")}${button("下一页", "case-page", String(casePage + 1), "small", casePage === pages ? "disabled" : "")}</div></div></section>`;
  }
  function report(run) {
    return `<section class="panel"><div class="panel-header"><div><h2>${esc(run.id)} · ${esc(run.scenario.title)}</h2><p>${time(run.time)} · ${run.snapshots.length} 个规则版本已冻结</p></div>${badge("机器判定独立记录", "blue")}</div>${run.results.map((item) => `<article class="result-card"><div class="actions">${badge(verdict[item.status], tones[item.status])}<span class="mono">${item.ruleId} v${item.version}</span>${item.feedback ? badge(item.feedback.decision === "accepted" ? "另有人工认可" : "另有人工不认可", "blue") : ""}</div><h3>${esc(item.title)}</h3><p>${esc(item.reason)}</p>${quote(item.evidence)}${item.autoExplanation ? `<div class="explanation green">${esc(item.autoExplanation)}</div>` : ""}${item.status === "conflict" ? `<div class="explanation amber">旧误报参考尚不能解释当前证据，此条保持可见，进入待确认队列。</div>` : ""}${item.resolution ? `<div class="explanation blue">人工依据：${esc(item.resolution.note)}</div>` : ""}${["issue", "review", "conflict"].includes(item.status) && !item.feedback ? `<div class="actions">${button(item.status === "issue" ? "抽样复核" : "核对证据", "review-result", item.ruleId, "small", `data-run="${run.id}"`)}</div>` : ""}</article>`).join("") || empty("没有已启用规则", "先在工作台完成规则审核。") }<div class="panel-body"><details class="snapshot"><summary>查看规则版本快照</summary><div class="snapshot-content">${run.snapshots.map((rule) => `<strong>${rule.id} v${rule.version} · ${esc(rule.title)}</strong><p>${esc(scope(rule))}</p><p>判错：${esc(rule.condition)}</p><p>例外：${esc(rule.useException ? rule.exception : "未启用")}</p>`).join("")}<p style="margin-top:12px">规则后续修改和停用不会改写本次快照。</p></div></details></div></section>`;
  }
  function trialPage() {
    const scenario = M.scenarios.find((item) => item.id === scenarioId);
    const run = state.runs.find((item) => item.id === selectedRun);
    return `${heading("文档试运行", "观察自动检查、例外核对与版本快照。人工反馈按需采集，机器判定保持独立。", button("运行当前示例 ▷", "run-trial", "", "primary"))}<div class="trial-layout"><section class="panel"><div class="panel-header"><h2>选择演示场景</h2></div><div class="scenario-list">${M.scenarios.map((item) => `<button class="scenario ${item.id === scenarioId ? "active" : ""}" data-action="scenario" data-id="${item.id}"><strong>${esc(item.title)}</strong><small>${esc(item.subtitle)}</small></button>`).join("")}</div></section><div><section class="panel"><div class="panel-header"><h2>${scenario.product} · 安装手册</h2>${badge(scenario.product === "AX200" ? "V1.0" : "V2.0", "blue")}</div><div class="document-page"><div class="doc-label">当前场景：${esc(scenario.title)}</div>${scenario.text.map((line) => `<p>${esc(line)}</p>`).join("")}<p class="caption">电流规则采用相同工况情境，以 10A / 16A 演示。</p></div></section><div class="explanation blue" style="margin-bottom:20px">语义归纳与判定采用预设示例，支持验证界面流程。审核规则后，已有例外在检查中自动核对；证据不足时才进入待确认队列。</div>${run ? report(run) : `<section class="panel">${empty("运行一次，查看系统如何自动处理", "发布的参数规则和术语规则将按适用范围分别执行。", button("运行当前示例", "run-trial", "", "primary"))}</section>`}</div></div>`;
  }
  function historyPage() {
    const run = state.runs.find((item) => item.id === selectedRun);
    return `${heading("运行与版本", "查看原始机器判定、人工反馈和冻结版本，追踪自动整理与审核记录。")}<div class="two-column"><div><section class="panel"><div class="panel-header"><h2>试运行历史</h2>${badge(`${state.runs.length} 次`, "blue")}</div>${state.runs.length ? `<div class="table-scroll"><table><thead><tr><th>任务</th><th>冻结版本</th><th>操作</th></tr></thead><tbody>${state.runs.map((item) => `<tr><td>${item.id}<small>${esc(item.scenario.title)} · ${time(item.time)}</small></td><td>${item.snapshots.map((rule) => `<div class="mono">${rule.id} v${rule.version}</div>`).join("")}</td><td>${button("查看", "open-run", item.id, "small")}</td></tr>`).join("")}</tbody></table></div>` : empty("暂无试运行记录", "去文档试运行体验已发布规则。", button("开始试运行", "navigate", "trial"))}</section>${run ? report(run) : ""}</div><section class="panel"><div class="panel-header"><h2>操作与自动处理记录</h2></div><div class="timeline">${state.audit.slice(0, 30).map((item) => `<div class="timeline-item"><small>${time(item.time)}</small><br><strong>${esc(item.action)}</strong><p>${esc(item.detail)}</p></div>`).join("")}</div></section></div>`;
  }
  function render() {
    const focused = document.activeElement?.id === "search";
    const caret = focused ? document.activeElement.selectionStart : null;
    const eligible = new Set(readyGroups().map((group) => group.ruleId));
    selection = new Set([...selection].filter((id) => eligible.has(id)));
    const stats = A.metrics(state);
    document.querySelectorAll("[data-nav]").forEach((node) => { if (node.dataset.nav === page) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
    document.querySelector("#breadcrumb-current").textContent = labels[page];
    document.querySelector("#pending-count").textContent = stats.ready;
    document.querySelector("#review-count").textContent = stats.blocked + pendingResults().length;
    document.querySelector("#rule-count").textContent = stats.enabled;
    document.querySelector("#case-count").textContent = stats.unique;
    view.innerHTML = ({ overview, conflicts: conflictsPage, cases: casesPage, rules: rulesPage, trial: trialPage, history: historyPage })[page]();
    if (focused) { const input = document.querySelector("#search"); input?.focus(); if (input && caret !== null) input.setSelectionRange(caret, caret); }
  }
  function sheet(name, subtitle, body, footer = "") {
    return `<div class="dialog-header"><div><span class="eyebrow">${subtitle}</span><h2 id="dialog-title">${name}</h2></div><button class="icon-button" data-action="close" aria-label="关闭详情">×</button></div><div class="dialog-body"><div id="dialog-error" class="field-error" role="alert" hidden></div>${body}</div><div class="dialog-footer">${button("关闭", "close")}${footer}</div>`;
  }
  function representativeCases(group) {
    const cases = A.groupCases(state, group);
    const positive = cases.find((item) => item.feedback === "accepted");
    const negative = cases.find((item) => item.feedback === "rejected");
    return [positive, negative].filter(Boolean).map((item) => `<section class="representative">${caseBadge(item)}<p class="caption" style="margin-top:8px">${esc(item.document)} · ${esc(item.reviewer)}</p>${quote(item.excerpts)}<p class="caption" style="margin-top:8px">人工依据：${esc(item.reason)}</p></section>`).join("");
  }
  function validationDetails(rule) {
    const validation = rule.validation;
    if (!validation) return `<div class="explanation blue">此版本已发布，新的同类反馈自动归档。</div>`;
    const history = validation.history || [];
    const failures = history.filter((item) => !item.passed).concat(validation.results.filter((item) => !item.passed));
    return `<section><div class="section-label">自动验证结果 ${badge(validation.passed ? "通过" : "需调整条件", validation.passed ? "green" : "red")}</div><div class="validation-overview"><div><strong>${history.filter((item) => item.passed).length}/${history.length}</strong><span>历史已标注案例</span></div><div><strong>${validation.results.filter((item) => item.passed).length}/${validation.results.length}</strong><span>预设边界场景</span></div></div>${(validation.errors || []).map((error) => `<p class="field-error">${esc(error)}</p>`).join("")}${failures.slice(0, 8).map((item) => `<div class="validation-row"><span class="fail">×</span><span>${esc(item.title)}<small class="caption" style="display:block">预期 ${verdict[item.expected]} · 实际 ${verdict[item.actual]}</small></span>${badge("需复核", "red")}</div>`).join("")}<details class="snapshot"><summary>展开边界验证过程</summary>${validation.results.map((item) => `<div class="validation-row"><span class="${item.passed ? "pass" : "fail"}">${item.passed ? "✓" : "×"}</span><span>${esc(item.title)}</span>${badge(verdict[item.actual], item.passed ? "green" : "red")}</div>`).join("")}</details><p class="caption" style="margin-top:12px">此处验证预设流程；正式效果还需独立文档抽样评估。</p></section>`;
  }
  function ruleSheet(id, editing = false) {
    const rule = M.findRule(state, id);
    const group = A.groupFor(state, rule.groupId);
    const stats = A.groupStats(state, group);
    const reviewable = group.status === "ready" && rule.status === "review";
    const field = (key, label) => `<label class="field"><span>${label}</span><textarea name="${key}">${esc(rule[key])}</textarea></label>`;
    if (editing) return sheet("调整规则条件", `${id} / 保存后自动验证`, `<div class="explanation blue">${esc(scope(rule))}。保存后自动重新回放本主题案例，验证通过即进入待审核；已发布版本继续保留。</div><form id="rule-form" class="rule-edit-form">${field("scope", "适用条件")}${field("condition", "判错条件")}${field("evidence", "证据要求")}<label class="check-row"><input type="checkbox" name="useException" ${rule.useException ? "checked" : ""}><strong>启用明确的例外条件</strong></label>${field("exception", "例外条件")}</form>`, button("保存并自动验证", "save-rule", id, "primary"));
    return sheet(esc(rule.title), `${id} / ${reviewable ? `待审核 v${rule.version + 1}` : rule.published ? `已发布 v${rule.version}` : "待修订草稿"}`, `<div class="actions">${badge(scope(rule), "blue")}${badge(`${stats.received} 条反馈 / ${stats.unique} 条去重案例`, "")}</div><div class="explanation ${reviewable ? "blue" : "green"}">${reviewable ? "本主题已自动完成归类、规则草稿和案例回放。核对下面的判断边界与代表性原文后，审核一次即可覆盖这一批反馈。" : group.reason}</div><section><div class="section-label">判断边界</div><dl class="rule-definition"><dt>适用条件</dt><dd>${esc(rule.scope)}</dd><dt>判错条件</dt><dd>${esc(rule.condition)}</dd><dt>例外条件</dt><dd>${esc(rule.useException ? rule.exception : "未启用例外")}</dd><dt>证据门槛</dt><dd>${esc(rule.evidence)}</dd></dl></section>${validationDetails(rule)}<section><div class="section-label">代表性正反案例 ${button("查看本主题全部案例", "group-cases", group.id, "small")}</div>${representativeCases(group)}</section>${group.resolution ? `<div class="explanation blue">本主题另有人工裁定：${esc(group.resolution.note)}。原始相反反馈保留。</div>` : ""}`, `${button("调整条件", "edit-rule", id)}${reviewable ? button(`审核并启用 v${rule.version + 1}`, "approve-one", id, "primary") : ""}`);
  }
  function approvalSheet(entries) {
    const rules = entries.map((item) => M.findRule(state, item.id));
    return sheet(`批量审核 ${rules.length} 条规则`, "同范围规则 / 一次确认", `<div class="explanation blue"><strong>${esc(scope(rules[0]))}</strong><p>所选规则已分别完成自动验证。请核对判断边界和代表性证据；确认后统一启用，各规则分别保存版本。</p></div>${rules.map((rule) => { const group = A.groupFor(state, rule.groupId); const stats = A.groupStats(state, group); return `<section class="batch-review-item"><div class="row-between"><h3>${esc(rule.title)}</h3><span class="mono">${rule.id} v${rule.version + 1}</span></div><p class="caption">${stats.positive} 正例 / ${stats.negative} 反例 / ${stats.independent} 独立证据 · 验证通过</p><dl class="rule-definition"><dt>判错</dt><dd>${esc(rule.condition)}</dd><dt>例外</dt><dd>${esc(rule.useException ? rule.exception : "未启用")}</dd></dl><details class="snapshot"><summary>展开代表性原文</summary>${representativeCases(group)}</details></section>`; }).join("")}`, button(`确认启用 ${rules.length} 条规则`, "approve-batch", "", "primary"));
  }
  function conflictSheet(id) {
    const group = A.groupFor(state, id);
    if (group.blockType === "validation" && group.ruleId) return ruleSheet(group.ruleId);
    return sheet(esc(group.title), `${group.id} / ${esc(scope(group))}`, `<div class="explanation amber">${esc(group.reason)}</div>${representativeCases(group)}<label class="field"><span>处理方式</span><select id="conflict-decision"><option value="pending">继续待确认，记录需要补充的依据</option>${group.blockType === "contradiction" ? `<option value="issue">确认属于同一工况，问题成立</option><option value="exception">确认属于不同工况，作为例外归档</option>` : `<option value="archive">保留待核实案例，暂不形成规则</option>`}</select></label><label class="field"><span>当前证据支持的判断依据</span><textarea id="conflict-note" placeholder="请说明核对了什么，以及为何支持本次处理。">${esc(group.pendingNote || "")}</textarea></label><div class="explanation blue">相反反馈保持原样，新增人工裁定单独保存。问题成立后自动形成候选规则，审核发布后才用于新任务。</div>`, button("保存处理结果", "resolve-conflict", id, "primary"));
  }
  function caseSheet(id) {
    const item = M.findCase(state, id);
    const group = A.groupFor(state, item.groupId);
    return sheet(esc(item.title), `${id} / 自动归档明细`, `<div class="actions">${caseBadge(item)}${badge(disposition(group), "blue")}</div><dl class="metadata"><div><dt>原始文档与版本</dt><dd>${esc(item.document)}</dd></div><div><dt>检查项与原文位置</dt><dd>${esc(item.check)} · ${esc(item.section)}</dd></div><div><dt>复核人</dt><dd>${esc(item.reviewer)} · 示例</dd></div><div><dt>同源反馈合并</dt><dd>${item.receipts} 条反馈，对应 1 份证据</dd></div></dl>${quote(item.excerpts)}<div class="explanation"><strong>原人工反馈依据</strong><p>${esc(item.reason)}</p>${item.feedbackNotes?.length > 1 ? `<details class="snapshot"><summary>查看合并反馈中的补充依据</summary>${item.feedbackNotes.map((note) => `<p>${esc(note.reviewer)}：${esc(note.reason)}</p>`).join("")}</details>` : ""}</div><div class="explanation blue">${esc(group.reason)}${group.resolution ? `<p>后续人工裁定：${esc(group.resolution.note)}</p>` : ""}</div>`, group.ruleId ? button("查看关联规则", "rule", group.ruleId, "primary") : "");
  }
  function reviewSheet(ruleId, runId) {
    const run = state.runs.find((item) => item.id === runId);
    const result = run.results.find((item) => item.ruleId === ruleId);
    return sheet("核对当前证据", `${runId} / ${ruleId} v${result.version}`, `${badge(verdict[result.status], tones[result.status])}<h3>${esc(result.title)}</h3><p class="caption">${esc(result.reason)}</p>${quote(result.evidence)}<label class="field"><span>人工结论</span><select id="review-decision"><option value="accepted">认可当前结论</option><option value="rejected">判为误报或合理例外</option><option value="pending">证据不足，继续待确认</option></select></label><label class="field"><span>判断依据</span><textarea id="review-note" placeholder="填写当前原文支持的判断依据。"></textarea></label><div class="explanation blue">原始机器判定单独保留。人工反馈保存后自动归类，已覆盖的同类问题直接归档，新例外才产生规则待办。</div>`, button("保存并自动归类", "save-review", ruleId, "primary", `data-run="${runId}"`));
  }
  function helpSheet() {
    return sheet("从少量待办开始体验", "自动整理 / 按主题审核", `<div class="explanation blue">本工作区已自动处理 1,008 条示例反馈。归纳与语义验证采用预设数据，操作记录保存到当前浏览器；真实报告和模型服务继续沿用现有流程。</div><div class="principles" style="padding:0">${[["先审核同范围的两条规则", "在工作台点击“选择同范围规则”，核对电压和电流规则的条件，批量启用。"], ["模拟收到更多反馈", "选择“同类问题为主”，观察新反馈自动归档；选择“包含新产品范围”，观察仅新范围产生待审核规则。"], ["集中处理异常", "同一证据的相反反馈由人判断；认可核查价值的建议保留为待核实案例。无需重新标注整批反馈。"], ["按需查看明细与效果", "反馈归档支持搜索与分页；文档试运行展示例外自动核对、机器判定和独立的人工抽样反馈。"]].map(([name, detail], index) => `<div class="principle"><span class="number">0${index + 1}</span><div><strong>${name}</strong><p>${detail}</p></div></div>`).join("")}</div>`, button("进入待审核工作台", "navigate", "overview", "primary"));
  }
  function renderSheet() {
    if (!sheetState) return;
    const { kind, id, extra } = sheetState;
    let content;
    if (kind === "rule" || kind === "edit-rule") content = ruleSheet(id, kind === "edit-rule");
    else if (kind === "batch") content = approvalSheet(extra);
    else if (kind === "case") content = caseSheet(id);
    else if (kind === "conflict") content = conflictSheet(id);
    else if (kind === "review") content = reviewSheet(id, extra);
    else if (kind === "help") content = helpSheet();
    else if (kind === "simulate") content = sheet("模拟收到一批新反馈", "自动收录 / 只处理新增部分", `<div class="explanation blue">模拟报告中已有的人工认可与不认可。系统自动去重、归类、回放验证；已审核边界内的反馈直接归档。</div><label class="simulation-option"><input type="radio" name="simulation" value="repeat" checked><span><strong>同类问题为主 · 120 条反馈</strong><small>术语反馈 104 条 + VX120 电压反馈 16 条。对应规则审核后，新增同类反馈无需重复审核。</small></span></label><label class="simulation-option"><input type="radio" name="simulation" value="new-scope"><span><strong>包含新产品范围 · 136 条反馈</strong><small>另外加入 BX300 的 16 条反馈。新产品范围单独生成候选，等待人工审核。</small></span></label>`, button("接收并自动整理", "ingest", "", "primary"));
    else content = sheet("重置演示工作区", "恢复初始示例", `<div class="explanation amber">恢复初始反馈、待审核规则和异常；本版演示记录会清空。你可以先导出记录。</div>`, button("恢复初始示例", "confirm-reset", "", "danger"));
    document.querySelector("#dialog-content").innerHTML = content;
  }
  function openSheet(kind, id, extra) {
    sheetState = { kind, id, extra };
    if (kind === "rule") sheetState.token = A.approvalToken(state, id);
    renderSheet();
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
  }
  function refresh(message, close = false) { save(); if (close && dialog.open) dialog.close(); render(); if (message) toast(message); }
  function exportState() {
    const url = URL.createObjectURL(new Blob([JSON.stringify({ prototype: "检查规则库自动整理原型 V2", exportedAt: new Date().toISOString(), mode: "预设案例与模拟判定", state }, null, 2)], { type: "application/json;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = "检查规则库V2-演示记录.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    toast("演示记录已导出。");
  }
  function act(action, node) {
    const id = node.dataset.id;
    if (action === "navigate") navigate(id);
    else if (action === "close") dialog.close();
    else if (["rule", "edit-rule", "case", "conflict"].includes(action)) openSheet(action, id);
    else if (["help", "reset", "simulate"].includes(action)) openSheet(action);
    else if (action === "export") exportState();
    else if (action === "confirm-reset") { state = A.seed(); selection.clear(); scopeFilter = "all"; caseFilter = "all"; caseGroup = "all"; selectedRun = null; save(); navigate("overview"); toast("已恢复初始示例。"); }
    else if (action === "select-scope") {
      const groups = readyGroups().filter((group) => A.groupStats(state, group).independent >= 3);
      const key = scopeFilter !== "all" ? scopeFilter : selection.size ? A.batchKey(M.findRule(state, [...selection][0])) : groups.length ? A.batchKey(M.findRule(state, groups[0].ruleId)) : null;
      selection = new Set(groups.filter((group) => A.batchKey(M.findRule(state, group.ruleId)) === key).map((group) => group.ruleId)); render();
    } else if (action === "clear-selection") { selection.clear(); render(); }
    else if (action === "batch-review") openSheet("batch", "", [...selection].map((ruleId) => ({ id: ruleId, token: A.approvalToken(state, ruleId) })));
    else if (action === "approve-batch" || action === "approve-one") {
      const entries = action === "approve-batch" ? sheetState.extra : [{ id, token: sheetState.token }];
      const count = A.approve(state, entries); selection.clear(); refresh(`已启用 ${count} 条规则，关联反馈已归档。`, true);
    } else if (action === "ingest") {
      const mode = document.querySelector('input[name="simulation"]:checked').value;
      const before = A.metrics(state);
      const summary = A.simulate(state, mode);
      const after = A.metrics(state);
      refresh("", true); navigate("overview"); toast(`自动处理 ${summary.received} 条反馈，新增 ${Math.max(0, after.ready - before.ready)} 条候选规则；已覆盖反馈自动归档。`);
    } else if (action === "save-rule") {
      const form = document.querySelector("#rule-form");
      const updates = Object.fromEntries(new FormData(form)); updates.useException = form.elements.useException.checked;
      A.saveRule(state, id, updates); refresh("条件已保存并自动验证。"); openSheet("rule", id);
    } else if (action === "resolve-conflict") {
      A.resolve(state, id, document.querySelector("#conflict-decision").value, document.querySelector("#conflict-note").value); refresh("处理结果已保存，原始反馈保留。", true);
    } else if (action === "toggle-rule") { M.toggleRule(state, id); refresh("状态已更新，历史任务版本保持原样。"); }
    else if (action === "group-cases") { caseGroup = id; caseFilter = "all"; casePage = 1; navigate("cases"); }
    else if (action === "case-filter") { caseFilter = id; casePage = 1; render(); }
    else if (action === "case-page") { casePage = Number(id); render(); }
    else if (action === "scenario") { scenarioId = id; selectedRun = null; render(); }
    else if (action === "run-trial") { selectedRun = A.runTrial(state, scenarioId).id; refresh("试运行完成，机器结果与规则版本已保存。"); }
    else if (action === "open-run") { selectedRun = id; render(); }
    else if (action === "review-result") openSheet("review", id, node.dataset.run);
    else if (action === "save-review") { A.reviewResult(state, node.dataset.run, id, document.querySelector("#review-decision").value, document.querySelector("#review-note").value); refresh("人工反馈已保存，系统已自动归类。", true); }
  }
  document.addEventListener("click", (event) => {
    const node = event.target.closest("[data-action]"); if (!node || node.disabled) return;
    event.preventDefault(); try { act(node.dataset.action, node); } catch (error) { errorMessage(error); }
  });
  document.addEventListener("change", (event) => {
    const node = event.target;
    try {
      if (node.dataset.selectRule) {
        const id = node.dataset.selectRule;
        if (node.checked) {
          if (selection.size && A.batchKey(M.findRule(state, [...selection][0])) !== A.batchKey(M.findRule(state, id))) throw new Error("请选择同一适用范围的规则。");
          selection.add(id);
        } else selection.delete(id);
        render();
      } else if (node.id === "scope-filter") { scopeFilter = node.value; selection.clear(); render(); }
      else if (node.id === "case-group") { caseGroup = node.value; casePage = 1; render(); }
    } catch (error) { render(); errorMessage(error); }
  });
  document.addEventListener("input", (event) => { if (event.target.id === "search") { search = event.target.value; casePage = 1; render(); } });
  document.addEventListener("submit", (event) => { if (event.target.id === "rule-form") event.preventDefault(); });
  dialog.addEventListener("close", () => { if (!dialog.open) sheetState = null; });
  window.addEventListener("hashchange", () => { const next = location.hash.slice(1); if (labels[next]) { if (page !== next) search = ""; page = next; render(); } });
  state = load();
  page = labels[location.hash.slice(1)] ? location.hash.slice(1) : "overview";
  save();
  render();
})();
