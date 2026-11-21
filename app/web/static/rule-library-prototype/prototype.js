(function () {
  "use strict";
  const M = window.RuleLibraryDemo;
  const STORAGE_KEY = "document-check:rule-library-prototype:v1";
  const labels = { overview: "流程总览", cases: "反馈案例", rules: "规则库", trial: "文档试运行", review: "复核与记录" };
  const resultLabels = { issue: "明确问题", exception: "符合例外", clear: "未发现问题", review: "证据待确认", skipped: "范围外跳过", conflict: "冲突待复核", reference: "历史参考" };
  const colors = { issue: "red", exception: "green", clear: "green", review: "amber", skipped: "", conflict: "amber" };
  let state;
  let storageAvailable = true;
  let page = "overview";
  let caseFilter = "all";
  let ruleFilter = "all";
  let search = "";
  let scenarioId = "conflict";
  let selectedRun = null;
  let dialogState = null;
  let toastTimer;
  const view = document.querySelector("#view");
  const dialog = document.querySelector("#detail-dialog");
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  const badge = (label, color = "") => `<span class="badge ${color}">${esc(label)}</span>`;
  const button = (label, action, id = "", style = "", extras = "") => `<button type="button" class="button ${style}" data-action="${action}" data-id="${esc(id)}" ${extras}>${label}</button>`;
  const time = (value) => new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  const quotes = (items) => items.map((item) => `<div class="quote">${esc(item)}</div>`).join("");
  const title = (heading, subtitle, actions = "") => `<div class="page-heading"><div><div class="eyebrow">CHECK RULE LIBRARY</div><h1>${heading}</h1><p>${subtitle}</p></div><div class="actions">${actions}</div></div>`;
  const empty = (heading, subtitle, actions = "") => `<div class="empty"><div class="empty-icon">◇</div><h3>${heading}</h3><p>${subtitle}</p>${actions}</div>`;
  const unresolved = () => state.runs.reduce((total, run) => total + run.results.filter((item) => ["conflict", "review"].includes(item.status) && !item.feedback).length, 0);
  function load() {
    try {
      const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (stored?.schema === 1 && ["cases", "rules", "runs", "audit"].every((key) => Array.isArray(stored[key])) && Number.isInteger(stored.counter)) return stored;
    } catch { storageAvailable = false; }
    return M.seed();
  }
  function save() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
    catch { storageAvailable = false; }
    document.querySelector("#save-state").textContent = storageAvailable ? "示例数据 · 已保存到本机" : "示例数据 · 当前页面暂存";
  }
  function toast(message, error = false) {
    const node = document.querySelector("#toast");
    clearTimeout(toastTimer);
    node.textContent = message;
    node.classList.toggle("error", error);
    node.hidden = false;
    toastTimer = setTimeout(() => { node.hidden = true; }, 4000);
  }
  function reportError(error) {
    const box = document.querySelector("#dialog-error");
    if (dialog.open && box) {
      box.hidden = false;
      box.textContent = error.message;
      box.scrollIntoView({ block: "nearest" });
    } else toast(error.message, true);
  }
  function navigate(next) {
    if (!labels[next]) return;
    if (page !== next) search = "";
    page = next;
    location.hash = next;
    if (dialog.open) dialog.close();
    render();
    window.scrollTo({ top: 0 });
  }
  function caseBadge(item) {
    if (item.feedback === "rejected") return badge(({ false_positive: "误报反例", not_applicable: "范围不适用", model_hallucination: "证据幻觉" })[item.rejection] || "不认可", "amber");
    return item.type === "suggestion" ? badge("认可核查价值", "purple") : badge("已确认问题", "green");
  }
  function ruleBadge(rule) {
    if (rule.kind === "legacy") return badge("待补充条件", "amber");
    if (rule.status === "draft") return badge("草稿", "");
    if (rule.status === "review") return badge("待审核", "purple");
    return badge(rule.enabled ? "已启用" : "已停用", rule.enabled ? "green" : "");
  }
  function overview() {
    const hasDraft = state.rules.some((rule) => rule.kind === "check" && rule.id !== "R-001");
    const validated = state.rules.some((rule) => rule.id !== "R-001" && rule.validation?.passed);
    const published = state.rules.some((rule) => rule.id !== "R-001" && rule.enabled && rule.published);
    const steps = [["收集反馈", "认可问题与误报分别留证", "cases", true], ["整理规则", "明确判错与例外条件", "rules", hasDraft], ["验证与审核", "同时验证问题和合理例外", "rules", validated], ["文档试运行", "按已发布版本逐项核对", "trial", published], ["复核再积累", "新反馈成为下一批案例", "review", state.runs.length > 0]];
    const metrics = [["待整理案例", state.cases.filter((item) => !item.collected).length, "人工反馈的起点", "▤"], ["已启用规则", state.rules.filter((rule) => rule.enabled).length, "新任务使用已发布版本", "◇"], ["旧误报参考", state.rules.filter((rule) => rule.kind === "legacy").length, "补充条件后关联为反例", "↳"], ["待复核条目", unresolved(), "保留可见，等待证据确认", "◷"]];
    return `<section class="hero"><div><div class="eyebrow">FROM FEEDBACK TO CONSISTENCY</div><h1>让每一次复核，<br>成为下一次检查的<em>依据。</em></h1><p>把已确认的问题变成检查条件，把误报变成有边界的例外。<br>用一份安装手册，体验从人工反馈到规则生效的完整过程。</p><div class="actions">${button("从一个已认可案例开始 <span>→</span>", "guide", "", "primary")}${button("查看流程说明", "help")}</div></div><div class="hero-map" aria-label="正反案例共同完善规则"><div class="map-node green">已认可问题<small>哪些情况应该报错</small></div><span class="map-arrow">＋</span><div class="map-node amber">历史误报<small>哪些情况属于例外</small></div><div class="map-node map-center">可复用的检查规则<small>适用条件 · 原文证据 · 例外边界</small></div></div></section>
      <div class="metrics">${metrics.map(([name, count, note, icon]) => `<div class="metric"><div class="metric-top"><span>${name}</span><span class="metric-symbol">${icon}</span></div><div class="metric-value">${count.toString().padStart(2, "0")}</div><small>${note}</small></div>`).join("")}</div>
      <section class="panel"><div class="panel-header"><div><h2>五步走完一条规则的生命周期</h2><p>点击任一步骤进入对应工作区，也可以从第一个案例顺序体验。</p></div>${badge("可交互流程", "blue")}</div><div class="steps">${steps.map(([name, note, target, done], index) => `<button class="step ${done ? "done" : ""}" data-action="navigate" data-id="${target}"><span class="step-number">${done ? "✓" : `0${index + 1}`}</span><strong>${name}</strong><small>${note}</small></button>`).join("")}</div></section>
      <div class="two-column"><section class="panel"><div class="panel-header"><h2>这次从一对正反案例开始</h2><button class="text-button" data-action="navigate" data-id="cases">查看全部 →</button></div>${["C-001", "C-002"].map((id) => { const item = M.findCase(state, id); return `<div class="case-preview"><div class="row-between">${caseBadge(item)}<span class="mono">${id}</span></div><h3>${esc(item.title)}</h3><p>${esc(item.reason)}</p><div class="row-between" style="margin-top:14px"><span class="caption">${esc(item.document)}</span><button class="text-button" data-action="case" data-id="${id}">查看证据 →</button></div></div>`; }).join("")}</section>
      <section class="panel"><div class="panel-header"><h2>体验时关注这三件事</h2></div><div class="principles"><div class="principle"><span class="number">01</span><div><strong>认可之后，先形成候选</strong><p>明确适用范围，验证正反案例，审核后才参与新任务。</p></div></div><div class="principle"><span class="number">02</span><div><strong>旧误报要有当前证据支持</strong><p>相似描述仅提示参考；与当前判错证据冲突时保持可见。</p></div></div><div class="principle"><span class="number">03</span><div><strong>规则变化，历史结果有据可查</strong><p>试运行保存规则快照，后续编辑与停用不会改写历史结论。</p></div></div></div></section></div>`;
  }
  function filterButtons(items, active, action) {
    return `<div class="filters">${items.map(([value, name]) => `<button class="filter ${active === value ? "active" : ""}" data-action="${action}" data-id="${value}" aria-pressed="${active === value}">${name}</button>`).join("")}</div>`;
  }
  function searchField() { return `<input class="search" id="search" type="search" placeholder="搜索名称、编号或文档…" aria-label="搜索当前列表" value="${esc(search)}">`; }
  function casesPage() {
    const rows = state.cases.filter((item) => (caseFilter === "all" || (caseFilter === "positive" && item.feedback === "accepted" && item.type === "issue") || (caseFilter === "negative" && item.feedback === "rejected") || (caseFilter === "pending" && item.type === "suggestion")) && `${item.id} ${item.title} ${item.document} ${item.reason}`.toLowerCase().includes(search.toLowerCase()));
    return `${title("反馈案例", "保留每一次人工判断的证据，再决定它如何进入规则。", button("体验第一个案例 →", "guide", "", "primary"))}<section class="panel"><div class="toolbar">${filterButtons([["all", "全部案例"], ["positive", "已确认问题"], ["negative", "误报与例外"], ["pending", "待核实建议"]], caseFilter, "case-filter")}${searchField()}</div><div class="table-scroll"><table><thead><tr><th>案例与来源</th><th>人工反馈</th><th>检查项</th><th>整理进度</th><th>操作</th></tr></thead><tbody>${rows.map((item) => `<tr><td><span class="mono">${esc(item.id)}</span><h3>${esc(item.title)}</h3><small>${esc(item.document)} · ${esc(item.section)}</small></td><td>${caseBadge(item)}</td><td>${esc(item.check)}</td><td>${badge(item.ruleId ? `关联 ${item.ruleId}` : item.collected ? "已收集" : "待整理", item.ruleId ? "blue" : "")}</td><td>${button("查看与整理", "case", item.id, "small")}</td></tr>`).join("")}</tbody></table></div>${!rows.length ? empty("暂无匹配案例", "可以切换反馈类型或清空搜索。") : ""}<div class="table-footer">共 ${rows.length} 条案例 · 认可建议代表值得核查；确认问题成立后可生成规则。</div></section><div class="explanation blue">推荐先打开 C-001 生成参数检查草稿，再把 C-002 的“不同工况”反例关联进去。C-005 演示模型幻觉反馈如何收集为证据校验案例。</div>`;
  }
  function rulesPage() {
    const rows = state.rules.filter((rule) => (ruleFilter === "all" || (ruleFilter === "draft" && ["draft", "review"].includes(rule.status)) || (ruleFilter === "enabled" && rule.enabled) || (ruleFilter === "legacy" && rule.kind === "legacy")) && `${rule.id} ${rule.title} ${rule.scope}`.toLowerCase().includes(search.toLowerCase()));
    return `${title("规则库", "把检出条件和合理例外放在一起管理，让每条规则的边界清楚。", button("从反馈生成规则", "navigate", "cases", "primary"))}<section class="panel"><div class="toolbar">${filterButtons([["all", "全部规则"], ["draft", "草稿与待审核"], ["enabled", "已启用"], ["legacy", "旧误报参考"]], ruleFilter, "rule-filter")}${searchField()}</div><div class="table-scroll"><table><thead><tr><th>规则名称</th><th>适用范围</th><th>正例 / 反例</th><th>状态与版本</th><th>操作</th></tr></thead><tbody>${rows.map((rule) => `<tr><td><span class="mono">${esc(rule.id)}</span><h3>${esc(rule.title)}</h3><small>${rule.kind === "legacy" ? "旧误报示例 · 描述相似仅供参考" : rule.family === "term" ? "程序规则 · 示例词表" : rule.family === "voltage" ? "语义检查 · 预设场景演示" : "待设计验证场景"}</small></td><td>${esc(rule.product === "ALL" ? "全部产品" : rule.product)}<small>${esc(rule.docType)}</small></td><td>${rule.positiveIds.length} / ${rule.negativeIds.length}</td><td>${ruleBadge(rule)}<small>${rule.published ? `生效版本 v${rule.version}${rule.enabled ? "" : "（已停用）"}` : "尚未发布"}${["draft", "review"].includes(rule.status) ? ` · 待发布 v${rule.version + 1}` : ""}</small></td><td><div class="actions">${button(rule.kind === "legacy" ? "补充条件" : "查看与编辑", "rule", rule.id, "small")}${rule.published ? button(rule.enabled ? "停用" : "恢复", "toggle-rule", rule.id, "small") : ""}</div></td></tr>`).join("")}</tbody></table></div>${!rows.length ? empty("暂无匹配规则", "从已认可案例创建规则，或切换筛选条件。") : ""}<div class="table-footer">共 ${rows.length} 条 · 草稿修改与已发布版本分开保存，审核启用后用于新任务。</div></section><div class="explanation amber"><strong>旧误报规则如何加入？</strong><p>打开 L-001 查看原始反馈，选择一条同主题检查规则，把“不同工况允许不同电压”补充为例外。旧条目保留为参考，当前证据仍需独立核对。</p></div>`;
  }
  function runReport(run, compact = false) {
    const issueCount = run.results.filter((item) => ["issue", "conflict"].includes(item.status)).length;
    const exceptionCount = run.results.filter((item) => item.status === "exception").length;
    return `<section class="panel"><div class="panel-header"><div><h2>${compact ? "本次试运行结果" : esc(run.id) + " · " + esc(run.scenario.title)}</h2><p>${esc(run.id)} · ${time(run.time)} · 示例判定，查看原文与规则依据</p></div>${badge("版本已冻结", "blue")}</div><div class="run-summary">${badge(`问题候选 ${issueCount}`, "red")}${badge(`符合例外 ${exceptionCount}`, "green")}${badge(`已执行 ${run.results.filter((item) => item.status !== "skipped").length} / ${run.snapshots.length} 条规则`, "blue")}</div>${run.results.map((item) => `<article class="result-card"><div class="row-between"><div class="actions">${badge(item.feedback ? (item.feedback.decision === "accepted" ? "人工认可" : "人工判为误报") : resultLabels[item.status], item.feedback ? "blue" : colors[item.status])}<span class="mono">${esc(item.ruleId)} · v${item.version}</span></div>${item.feedback ? badge(`已生成 ${item.feedback.caseId}`, "") : ""}</div><h3>${esc(item.title)}</h3><p>${esc(item.reason)}</p>${quotes(item.evidence)}${item.status === "conflict" ? `<div class="explanation amber"><strong>当前证据与旧误报参考存在冲突</strong><p>${item.references.map((ref) => `${esc(ref.id)}「${esc(ref.title)}」曾被标记误报。相似描述不能证明当前结论是误报，本条保持可见，等待复核。</p>`).join("")}</div>` : ""}${item.resolution ? `<div class="explanation blue"><strong>${item.resolution.decision === "pending" ? "人工暂缓判断" : "人工复核依据"}</strong><p>${esc(item.resolution.note)}</p></div>` : ""}${["issue", "conflict", "review"].includes(item.status) && !item.feedback ? `<div class="actions">${button("复核并反馈", "review-result", item.ruleId, item.status === "conflict" ? "soft" : "", `data-run="${run.id}"`)}</div>` : ""}</article>`).join("")}${!run.results.length ? empty("尚无已启用的检查规则", "请先完成规则验证、审核和启用。", button("前往规则库", "navigate", "rules")) : ""}<div class="panel-body"><details class="snapshot"><summary>查看本次规则快照与检查覆盖</summary><div class="snapshot-content">${run.snapshots.map((rule) => `<strong>${esc(rule.id)} v${rule.version} · ${esc(rule.title)}</strong><div>范围：${esc(rule.scope)}</div><div>判错：${esc(rule.condition)}</div><div>例外：${esc(rule.useException ? rule.exception : "本版本未启用例外条件")}</div>`).join("")}<p style="margin-top:12px">此快照跟随本次试运行保存；修改、停用规则后，可返回此处核对历史版本。</p></div></details></div></section>`;
  }
  function trialPage() {
    const scenario = M.scenarios.find((item) => item.id === scenarioId);
    const run = state.runs.find((item) => item.id === selectedRun);
    const voltageEnabled = state.rules.some((rule) => rule.family === "voltage" && rule.enabled && rule.published);
    return `${title("文档试运行", "选择一种文档情境，观察检查规则和误报参考如何共同影响结论。", button("运行当前示例 <span>▷</span>", "run-trial", "", "primary"))}<div class="trial-layout"><section class="panel"><div class="panel-header"><h2>选择演示场景</h2></div><div class="scenario-list">${M.scenarios.map((item) => `<button class="scenario ${item.id === scenarioId ? "active" : ""}" data-action="scenario" data-id="${item.id}" aria-pressed="${item.id === scenarioId}"><strong>${esc(item.title)}</strong><small>${esc(item.subtitle)}</small></button>`).join("")}</div></section><div><section class="panel"><div class="panel-header"><div class="actions">${badge("示例原文", "blue")}<span class="caption">${scenario.product} · ${scenario.docType}</span></div><span class="mono">DEMO DOCUMENT</span></div><div class="document-page"><div class="doc-label">当前场景 / ${esc(scenario.title)}</div><h3 style="margin-top:20px">${esc(scenario.product)} ${esc(scenario.docType)}</h3>${scenario.text.map((line) => `<p>${esc(line)}</p>`).join("")}${!scenario.evidence ? `<div class="explanation amber">本场景另有一条模拟模型候选：“原文存在 48V，与 220V 冲突”。试运行将检查它能否在上面的当前原文中取证。</div>` : ""}</div></section>${!voltageEnabled ? `<div class="explanation amber" style="margin-bottom:20px">参数检查规则尚未启用，当前只执行已发布的术语规则。完成参数规则的正反例验证与审核后，再体验参数冲突和合理例外。</div>` : ""}<div class="explanation blue" style="margin-bottom:20px">本页对预设场景进行模拟判定，演示适用范围、证据门槛和例外条件。规则中的自然语言用于展示审核流程，真实语义理解需接入后续检查服务。</div>${run ? runReport(run, true) : `<section class="panel">${empty("准备好后，运行当前示例", "每次运行都会保存一份独立的规则版本快照。", button("运行当前示例", "run-trial", "", "primary"))}</section>`}</div></div>`;
  }
  function reviewPage() {
    const run = state.runs.find((item) => item.id === selectedRun);
    return `${title("复核与记录", "查看当前证据、处理规则冲突，并追踪案例与规则的每一次变化。", button("继续试运行", "navigate", "trial", "primary"))}<div class="two-column"><div><section class="panel"><div class="panel-header"><h2>试运行历史</h2>${badge(`${state.runs.length} 次运行`, "blue")}</div>${state.runs.length ? `<div class="table-scroll"><table><thead><tr><th>任务与场景</th><th>规则版本</th><th>待复核</th><th>操作</th></tr></thead><tbody>${state.runs.map((entry) => `<tr><td><h3>${esc(entry.scenario.title)}</h3><small>${entry.id} · ${time(entry.time)}</small></td><td>${entry.snapshots.map((rule) => `<div class="mono">${esc(rule.id)} v${rule.version}</div>`).join("") || "—"}</td><td>${entry.results.filter((item) => ["conflict", "review"].includes(item.status) && !item.feedback).length}</td><td>${button("查看", "open-run", entry.id, "small")}</td></tr>`).join("")}</tbody></table></div>` : empty("还没有试运行记录", "启用规则后运行示例文档，即可在这里复核。", button("去试运行", "navigate", "trial"))}</section>${run ? runReport(run) : ""}</div><section class="panel"><div class="panel-header"><h2>操作记录</h2><span class="caption">当前演示工作区</span></div>${state.audit.length ? `<div class="timeline">${state.audit.slice(0, 25).map((entry) => `<div class="timeline-item"><small>${time(entry.time)}</small><br><strong>${esc(entry.action)}</strong><p>${esc(entry.detail)}</p></div>`).join("")}</div>` : empty("操作记录将显示在这里", "创建草稿、关联反例、验证、审核及人工反馈都会留下记录。")}</section></div>`;
  }
  function render() {
    const focus = document.activeElement?.id === "search";
    const selection = focus ? document.activeElement.selectionStart : null;
    document.querySelectorAll("[data-nav]").forEach((node) => {
      if (node.dataset.nav === page) node.setAttribute("aria-current", "page");
      else node.removeAttribute("aria-current");
    });
    document.querySelector("#breadcrumb-current").textContent = labels[page];
    document.querySelector("#case-count").textContent = state.cases.length;
    document.querySelector("#rule-count").textContent = state.rules.filter((item) => item.kind === "check").length;
    document.querySelector("#review-count").textContent = unresolved();
    view.innerHTML = ({ overview, cases: casesPage, rules: rulesPage, trial: trialPage, review: reviewPage })[page]();
    if (focus) {
      const input = document.querySelector("#search");
      input?.focus();
      if (input && selection !== null) input.setSelectionRange(selection, selection);
    }
  }
  function sheet(heading, subheading, body, footer = "") {
    return `<div class="dialog-header"><div><span class="eyebrow">${subheading}</span><h2 id="dialog-title">${heading}</h2></div><button type="button" class="icon-button" data-action="close" aria-label="关闭详情">×</button></div><div class="dialog-body"><div id="dialog-error" class="field-error" role="alert" hidden></div>${body}</div><div class="dialog-footer">${button("关闭", "close")}${footer}</div>`;
  }
  function candidateOptions(item) {
    return state.rules.filter((rule) => rule.kind === "check" && rule.family === item.family).map((rule) => `<option value="${esc(rule.id)}">${esc(rule.id)} · ${esc(rule.title)}</option>`).join("");
  }
  function caseSheet(id) {
    const item = M.findCase(state, id);
    const options = candidateOptions(item);
    let actionArea = "";
    let footer = "";
    if (item.feedback === "accepted" && item.type === "issue") {
      actionArea = `<div class="explanation blue">${item.ruleId ? "此案例已关联规则，可以继续完善条件与验证场景。" : "生成草稿后，请补充适用范围、判错条件与例外，再验证和审核。"}</div>`;
      footer = button(item.ruleId ? "查看关联规则 →" : "生成检查规则草稿 →", "create-draft", id, "primary");
    } else if (item.feedback === "accepted") {
      actionArea = `<div class="explanation amber">当前反馈只确认该建议值得核查。补充业务依据、确认问题成立后，可以生成检查规则。</div><label class="field"><span>确认依据</span><textarea id="confirm-reason" placeholder="例如：产品负责人确认该模块不支持带电更换，且完整操作章节未说明断电前提。"></textarea></label>`;
      footer = button("确认问题成立", "confirm-case", id, "primary");
    } else if (item.rejection === "model_hallucination") {
      actionArea = `<div class="explanation blue"><strong>用于完善证据门槛</strong><p>本案例提示：文本抽取不代表图像缺失。后续检查应核对原文件或多模态证据。收集此案例后，保留在案例库用于设计证据校验。</p></div>`;
      footer = button(item.collected ? "已收集" : "收集为证据校验案例", "collect-case", id, "primary", item.collected ? "disabled" : "");
    } else {
      actionArea = options ? `<div class="explanation amber">将误报原因补充为同主题规则的例外，审核发布后参与检查。</div><label class="field"><span>关联检查规则</span><select id="attach-rule">${options}</select></label>` : `<div class="explanation amber">尚无同主题检查规则。可以先收集此反例，再从已确认问题创建规则。</div>`;
      footer = options ? button("关联为例外条件 →", "attach-case", id, "primary") : button(item.collected ? "已收集" : "先收集案例", "collect-case", id, "primary", item.collected ? "disabled" : "");
    }
    return sheet(esc(item.title), `反馈案例 / ${esc(id)}`, `${caseBadge(item)}<dl class="metadata"><div><dt>来源文档</dt><dd>${esc(item.document)}</dd></div><div><dt>检查项 / 位置</dt><dd>${esc(item.check)} · ${esc(item.section)}</dd></div><div><dt>复核人</dt><dd>${esc(item.reviewer)} · 示例</dd></div><div><dt>整理进度</dt><dd>${item.ruleId ? `关联 ${esc(item.ruleId)}` : item.collected ? "已收集" : "待整理"}</dd></div></dl><section><div class="section-label">原文与人工证据</div>${quotes(item.excerpts)}</section><section><div class="section-label">人工反馈依据</div><p class="caption">${esc(item.reason)}</p></section>${actionArea}`, footer);
  }
  function field(name, label, value, area = false) {
    return `<label class="field"><span>${label}</span>${area ? `<textarea name="${name}">${esc(value)}</textarea>` : `<input name="${name}" value="${esc(value)}" maxlength="180">`}</label>`;
  }
  function ruleSheet(id) {
    const rule = M.findRule(state, id);
    if (rule.kind === "legacy") {
      const options = candidateOptions(rule);
      return sheet(esc(rule.title), `旧误报参考 / ${esc(id)}`, `${ruleBadge(rule)}<div class="explanation amber"><strong>历史描述相似，仍需核对当前条件</strong><p>本条模拟现有的描述相似匹配规则。不同工况是原始误报的原因，当前缺少完整适用范围。原型中保留为参考，命中真实问题时提示复核。</p></div><dl class="metadata"><div><dt>原检查项</dt><dd>单文档 / 全文一致性</dd></div><div><dt>原匹配方式</dt><dd>描述相似度达到 56%（示例来源）</dd></div></dl><section><div class="section-label">误报原因</div><p>${esc(rule.reason)}</p>${quotes(M.findCase(state, "C-002").excerpts)}</section>${rule.linkedRuleId ? `<div class="explanation green">已关联 ${esc(rule.linkedRuleId)}，可进入规则草稿检查例外条件和审核进度。</div>` : ""}${options ? `<label class="field"><span>将误报条件补充到哪条检查规则？</span><select id="attach-rule">${options}</select></label>` : `<div class="explanation blue">请先从 C-001 创建同主题检查规则，再关联这个误报反例。</div>`}`, options ? button("补充为例外条件 →", "link-legacy", id, "primary") : button("查看已认可案例", "case", "C-001", "primary"));
    }
    const validation = rule.validation;
    const valid = validation?.passed && validation.signature === M.draftSignature(rule);
    const negatives = state.cases.filter((item) => item.feedback === "rejected" && item.family === rule.family);
    const checks = rule.status === "review" ? `<section class="explanation blue"><strong>审核确认 · 当前草稿 v${rule.version + 1}</strong><div class="checks"><label class="check-row"><input type="checkbox" name="approval" value="scope">适用范围和判错条件明确，可以定位到当前文档。</label><label class="check-row"><input type="checkbox" name="approval" value="examples">正例、反例及已修复场景的验证结果符合预期。</label><label class="check-row"><input type="checkbox" name="approval" value="evidence">证据要求与例外条件可核对，冲突条目保持可见。</label></div></section>` : "";
    return sheet(esc(rule.title), `检查规则 / ${esc(id)}`, `<div class="actions">${ruleBadge(rule)}${badge(`草稿 v${rule.version + 1}`, "blue")}${rule.published ? badge(`已发布 v${rule.version}${rule.enabled ? " · 生效中" : " · 已停用"}`, "green") : ""}</div>${rule.published ? `<div class="explanation blue">编辑后保存为下一版草稿；新任务继续使用已启用的 v${rule.version}，直到新版本审核发布。</div>` : ""}<form id="rule-form"><div style="display:grid;gap:17px">${field("title", "规则名称", rule.title)}<div class="field-grid"><label class="field"><span>适用产品</span><select name="product">${["VX120", "AX200", "ALL"].map((value) => `<option value="${value}" ${rule.product === value ? "selected" : ""}>${value === "ALL" ? "全部产品" : value}</option>`).join("")}</select></label><label class="field"><span>文档类型</span><select name="docType">${["安装手册", "维护说明", "全部"].map((value) => `<option ${rule.docType === value ? "selected" : ""}>${value}</option>`).join("")}</select></label></div>${field("scope", "适用条件", rule.scope, true)}${field("condition", "判错条件", rule.condition, true)}${field("evidence", "证据要求", rule.evidence, true)}<div class="field"><label class="check-row"><input type="checkbox" name="useException" ${rule.useException ? "checked" : ""}><strong>启用明确的例外条件</strong></label><textarea name="exception" placeholder="例如：原文明示不同工况，并能分别对应两个参数值时，允许差异。">${esc(rule.exception)}</textarea><small>原型通过此开关演示例外生效；自然语言条件作为审核说明。</small></div></div></form><section><div class="section-label">关联正反案例 <span class="caption">正例 ${rule.positiveIds.length} / 反例 ${rule.negativeIds.length}</span></div><div class="link-chips">${rule.positiveIds.concat(rule.negativeIds).map((caseId) => `<button class="link-chip" data-action="case" data-id="${esc(caseId)}">${esc(caseId)} · ${esc(M.findCase(state, caseId).title)}</button>`).join("")}</div>${negatives.some((item) => !rule.negativeIds.includes(item.id)) ? `<div class="actions" style="margin-top:12px">${negatives.filter((item) => !rule.negativeIds.includes(item.id)).map((item) => button(`＋ 关联 ${esc(item.id)} 误报反例`, "quick-attach", item.id, "small soft", `data-rule="${id}"`)).join("")}</div>` : ""}</section><section><div class="section-label">正反案例验证 ${button("运行案例验证", "validate-rule", id, "small")}</div>${validation ? `<div>${validation.results.map((row) => `<div class="validation-row"><span class="${row.passed ? "pass" : "fail"}">${row.passed ? "✓" : "×"}</span><span>${esc(row.title)}<small class="caption" style="display:block">预期：${resultLabels[row.expected]} · 实际：${resultLabels[row.actual]}</small></span>${badge(row.passed ? "通过" : "需修正规则", row.passed ? "green" : "red")}</div>`).join("")}</div>${!valid ? `<p class="caption" style="margin-top:10px">请修正例外或范围后重新验证，通过全部场景才能提交审核。</p>` : ""}` : `<div class="explanation">验证正例、合理例外、已修复内容与证据不足等预设场景。修改当前草稿后，需要重新验证。</div>`}</section>${checks}`, `${button("保存草稿", "save-rule", id)}${rule.status === "review" ? button(`审核通过并启用 v${rule.version + 1}`, "publish-rule", id, "primary") : button("提交审核 →", "submit-rule", id, "primary", valid ? "" : "disabled")}`);
  }
  function reviewSheet(id, runId) {
    const run = state.runs.find((item) => item.id === runId);
    const result = run.results.find((item) => item.ruleId === id);
    return sheet("核对证据，保存人工反馈", `${esc(runId)} / ${esc(id)} v${result.version}`, `${badge(resultLabels[result.status], colors[result.status])}<h3>${esc(result.title)}</h3><p class="caption">${esc(result.reason)}</p><section><div class="section-label">当前文档证据</div>${quotes(result.evidence)}</section>${result.references.length ? `<div class="explanation amber"><strong>历史误报原因</strong><p>${result.references.map((item) => esc(item.reason)).join("；")}</p><p>请核对本次是否也满足该例外条件。复核结果只作用于本次条目，新增案例经整理和审核后才进入规则。</p></div>` : ""}<label class="field"><span>人工结论</span><select id="review-decision"><option value="accepted">认可当前结论</option><option value="rejected">判定为误报或合理例外</option><option value="pending">证据不足，继续待确认</option></select></label><label class="field"><span>当前原文支持的判断依据</span><textarea id="review-note" placeholder="例如：两处原文都标明 VX120 标准模式，历史的不同工况例外在本次不成立。"></textarea></label><div class="explanation blue">保存后保留原始检查结果和版本快照。明确的认可或不认可反馈会生成新案例；待确认项继续保留可见。</div>`, button("保存反馈并收集案例", "save-review", id, "primary", `data-run="${runId}"`));
  }
  function helpSheet() {
    return sheet("用 5 分钟走一遍完整流程", "原型使用说明", `<div class="explanation blue">这是可操作的产品流程原型。示例数据和操作记录保存在当前浏览器，可导出或重置；真实报告、模型请求和线上规则沿用现有功能。</div><div class="principles" style="padding:0">${[["收集一个已确认问题", "打开 C-001，查看两处原文，点击“生成检查规则草稿”。"], ["把误报补充为例外", "先运行一次案例验证，观察不同工况场景失败；再关联 C-002，重新验证。也可以从规则库里的 L-001 整理旧误报。"], ["审核并发布规则", "验证全部通过后提交审核，确认三项审核内容，启用第一个版本。"], ["试运行不同文档场景", "依次尝试真实冲突、合理例外、已修复和证据不足。旧误报命中真实问题时，会保留冲突供复核。"], ["复核后形成下一批案例", "填写当前原文支持的依据，保存反馈；再修改或停用规则，查看历史试运行快照仍然保留。"]].map(([heading, text], index) => `<div class="principle"><span class="number">0${index + 1}</span><div><strong>${heading}</strong><p>${text}</p></div></div>`).join("")}</div><div class="explanation amber"><strong>当前演示范围</strong><p>参数一致性和词表检查使用预设场景演示。其他案例可以整理和保存草稿；正式接入需要真实报告读取、规则存储、语义执行及证据校验服务。</p></div>`, button("从 C-001 开始 →", "guide", "", "primary"));
  }
  function openSheet(kind, id, extra) {
    dialogState = { kind, id, extra };
    renderDialog();
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
  }
  function renderDialog() {
    if (!dialogState) return;
    const { kind, id, extra } = dialogState;
    document.querySelector("#dialog-content").innerHTML = kind === "case" ? caseSheet(id) : kind === "rule" ? ruleSheet(id) : kind === "review" ? reviewSheet(id, extra) : kind === "help" ? helpSheet() : sheet("重置演示工作区", "示例数据", `<div class="explanation amber">将清空本原型保存的规则草稿、试运行和反馈，恢复初始示例。你可以先导出演示记录。</div>`, button("恢复初始示例", "confirm-reset", "", "danger"));
  }
  function syncRuleForm(id) {
    const form = document.querySelector("#rule-form");
    if (!form) return;
    const rule = M.findRule(state, id);
    const updates = Object.fromEntries(new FormData(form));
    updates.useException = form.elements.useException.checked;
    if (Object.keys(updates).some((key) => updates[key] !== rule[key])) M.saveDraft(state, id, updates);
  }
  function refresh(message, keepDialog = true) {
    save();
    render();
    if (keepDialog && dialog.open) renderDialog();
    if (message) toast(message);
  }
  function exportState() {
    const blob = new Blob([JSON.stringify({ prototype: "检查规则库交互原型", exportedAt: new Date().toISOString(), mode: "示例数据与预设场景", state }, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "检查规则库-演示记录.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    toast("已导出演示记录，可用于讨论规则和操作流程。");
  }
  function handle(action, node) {
    const id = node.dataset.id;
    if (action === "navigate") navigate(id);
    else if (action === "close") dialog.close();
    else if (action === "guide") { navigate("cases"); openSheet("case", "C-001"); }
    else if (action === "help") openSheet("help");
    else if (action === "reset") openSheet("reset");
    else if (action === "confirm-reset") { state = M.seed(); selectedRun = null; scenarioId = "conflict"; caseFilter = "all"; ruleFilter = "all"; search = ""; save(); navigate("overview"); toast("已恢复初始示例。"); }
    else if (action === "export") exportState();
    else if (action === "case") openSheet("case", id);
    else if (action === "rule") openSheet("rule", id);
    else if (action === "case-filter") { caseFilter = id; render(); }
    else if (action === "rule-filter") { ruleFilter = id; render(); }
    else if (action === "create-draft") { const rule = M.makeDraft(state, id); refresh(); openSheet("rule", rule.id); }
    else if (action === "confirm-case") { M.confirmCase(state, id, document.querySelector("#confirm-reason").value); refresh("已确认问题，可以生成规则草稿。"); }
    else if (action === "collect-case") { M.collectCase(state, id); refresh("案例已收集，规则生效仍需整理与审核。"); }
    else if (action === "attach-case" || action === "link-legacy") {
      const ruleId = document.querySelector("#attach-rule").value;
      if (action === "attach-case") M.attachException(state, id, ruleId);
      else M.linkLegacy(state, id, ruleId);
      refresh(); openSheet("rule", ruleId);
    } else if (action === "quick-attach") {
      syncRuleForm(node.dataset.rule); M.attachException(state, id, node.dataset.rule); refresh("反例已关联，请重新验证规则。");
    } else if (action === "save-rule") {
      syncRuleForm(id); refresh("草稿已保存，已发布版本继续独立保留。");
    } else if (action === "validate-rule") {
      syncRuleForm(id); M.regression(state, id); refresh("已完成预设正反案例验证。");
    } else if (action === "submit-rule") {
      syncRuleForm(id); M.submitReview(state, id); refresh("已提交审核，请核对三项审核内容。");
    } else if (action === "publish-rule") {
      const checks = [...document.querySelectorAll('input[name="approval"]')].map((input) => input.checked);
      syncRuleForm(id); M.publish(state, id, checks); refresh(); dialog.close(); navigate("trial"); toast("规则已启用，现在可以运行示例文档。");
    } else if (action === "toggle-rule") {
      M.toggleRule(state, id); refresh("规则状态已更新，历史试运行结果保留。");
    } else if (action === "scenario") { scenarioId = id; selectedRun = null; render(); }
    else if (action === "run-trial") { const run = M.runTrial(state, scenarioId); selectedRun = run.id; refresh("试运行完成，规则版本已保存。", false); }
    else if (action === "open-run") { selectedRun = id; render(); }
    else if (action === "review-result") openSheet("review", id, node.dataset.run);
    else if (action === "save-review") {
      const result = M.reviewResult(state, node.dataset.run, id, document.querySelector("#review-decision").value, document.querySelector("#review-note").value);
      dialog.close(); refresh(result ? `反馈已保存为 ${result.id}，可到案例库继续整理。` : "条目继续待确认，已记录复核说明。", false);
    }
  }
  document.addEventListener("click", (event) => {
    const node = event.target.closest("[data-action]");
    if (!node || node.disabled) return;
    event.preventDefault();
    try { handle(node.dataset.action, node); }
    catch (error) { save(); render(); reportError(error); }
  });
  document.addEventListener("submit", (event) => { if (event.target.id === "rule-form") event.preventDefault(); });
  document.addEventListener("input", (event) => {
    if (event.target.id === "search") { search = event.target.value; render(); }
  });
  dialog.addEventListener("close", () => { if (!dialog.open) dialogState = null; });
  window.addEventListener("hashchange", () => {
    const next = location.hash.slice(1);
    if (labels[next]) { if (page !== next) search = ""; page = next; render(); }
  });
  state = load();
  page = labels[location.hash.slice(1)] ? location.hash.slice(1) : "overview";
  if (!storageAvailable) document.querySelector("#save-state").textContent = "示例数据 · 当前页面暂存";
  render();
})();
