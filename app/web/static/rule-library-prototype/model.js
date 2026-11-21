/* 示例数据与规则生命周期，供交互原型和流程验证使用。 */
(function (root) {
  "use strict";

  const clone = (value) => JSON.parse(JSON.stringify(value));
  const stamp = () => new Date().toISOString();
  const CASES = [
    { id: "C-001", title: "同一设备的额定电压前后不一致", family: "voltage", feedback: "accepted", type: "issue", reason: "正文和参数表指向同一型号、同一工况，数值存在冲突。", document: "VX120 安装手册 V2.0", section: "2.1 供电要求 / 5.2 参数表", product: "VX120", docType: "安装手册", excerpts: ["VX120 在标准模式下的额定输入电压为 220V。", "表 5-2：VX120，标准模式，额定输入电压 380V。"], reviewer: "陈工", check: "全文一致性", collected: false, ruleId: null },
    { id: "C-002", title: "不同工况的电压差异属于合理例外", family: "voltage", feedback: "rejected", type: "issue", rejection: "false_positive", reason: "原文已明确区分标准模式和工业模式，两种工况允许不同电压。", document: "VX120 安装手册 V1.8", section: "3.1 工作模式", product: "VX120", docType: "安装手册", excerpts: ["VX120 标准模式的额定输入电压为 220V。", "VX120 工业模式的额定输入电压为 380V。"], reviewer: "李工", check: "全文一致性", collected: false, ruleId: null },
    { id: "C-003", title: "无线连接名称应使用规范写法 Wi-Fi", family: "term", feedback: "accepted", type: "issue", reason: "本示例的术语规范要求写为 Wi-Fi。", document: "VX120 安装手册 V2.0", section: "4.1 网络配置", product: "VX120", docType: "安装手册", excerpts: ["通过 WiFi 连接设备并完成配置。"], reviewer: "陈工", check: "常用词", collected: true, ruleId: "R-001" },
    { id: "C-004", title: "操作步骤可能缺少断电前提", family: "manual", feedback: "accepted", type: "suggestion", reason: "认可进一步核查的价值，安全条件尚待产品负责人确认。", document: "VX120 维护说明 V1.0", section: "6.2 更换模块", product: "VX120", docType: "维护说明", excerpts: ["打开侧盖，取下连接器，替换控制模块。"], reviewer: "王工", check: "内容完整性", collected: false, ruleId: null },
    { id: "C-005", title: "仅凭抽取文本断言图 3-1 缺失", family: "evidence", feedback: "rejected", type: "issue", rejection: "model_hallucination", reason: "原 PDF 中有图 3-1，抽取文本未包含图像内容。", document: "VX120 安装手册 V1.9", section: "3.2 安装位置", product: "VX120", docType: "安装手册", excerpts: ["安装位置见图 3-1。", "人工查看原 PDF：图 3-1 位于第 12 页。"], reviewer: "李工", check: "内容完整性", collected: false, ruleId: null },
    { id: "C-006", title: "室内设备被要求提供户外防护参数", family: "manual", feedback: "rejected", type: "issue", rejection: "not_applicable", reason: "此文档明确限定室内使用，应收窄该检查的适用范围。", document: "VX120 维护说明 V1.0", section: "1.1 使用范围", product: "VX120", docType: "维护说明", excerpts: ["本产品仅供室内干燥环境使用。"], reviewer: "王工", check: "内容完整性", collected: false, ruleId: null },
  ];
  const SCENARIOS = [
    { id: "conflict", title: "真实问题 + 旧误报参考", subtitle: "同一工况的参数冲突，应保持可见。", product: "VX120", docType: "安装手册", context: "same", values: [220, 380], evidence: true, text: ["2.1 供电要求：VX120 在标准模式下的额定输入电压为 220V。", "5.2 参数表：VX120，标准模式，额定输入电压 380V。", "4.1 网络配置：通过 WiFi 连接设备并完成配置。"], term: true },
    { id: "exception", title: "合理例外", subtitle: "原文明示不同工况，验证例外条件。", product: "VX120", docType: "安装手册", context: "different", values: [220, 380], evidence: true, text: ["3.1 工作模式：VX120 标准模式的额定输入电压为 220V。", "3.1 工作模式：VX120 工业模式的额定输入电压为 380V。", "4.1 网络配置：通过 Wi-Fi 连接设备并完成配置。"], term: false },
    { id: "fixed", title: "修复后的文档", subtitle: "参数和术语均已修复，验证停止报错。", product: "VX120", docType: "安装手册", context: "same", values: [220, 220], evidence: true, text: ["2.1 供电要求：VX120 在标准模式下的额定输入电压为 220V。", "5.2 参数表：VX120，标准模式，额定输入电压 220V。", "4.1 网络配置：通过 Wi-Fi 连接设备并完成配置。"], term: false },
    { id: "unclear", title: "适用条件不明确", subtitle: "无法确认两处是否同一工况，保留待确认。", product: "VX120", docType: "安装手册", context: "unknown", values: [220, 380], evidence: true, text: ["2.1 供电要求：VX120 的额定输入电压为 220V。", "5.2 参数表：VX120，额定输入电压 380V，适用工况待确认。"], term: false },
    { id: "missing-evidence", title: "模型引用无依据", subtitle: "候选结论声称有 48V，但当前原文没有。", product: "VX120", docType: "安装手册", context: "same", values: [220, 48], evidence: false, text: ["2.1 供电要求：VX120 在标准模式下的额定输入电压为 220V。", "5.2 参数表：VX120，标准模式，额定输入电压 220V。"], term: false },
    { id: "wrong-scope", title: "其他产品", subtitle: "限定 VX120 的规则在 AX200 文档中跳过。", product: "AX200", docType: "安装手册", context: "same", values: [220, 380], evidence: true, text: ["2.1 供电要求：AX200 的额定输入电压为 220V。", "5.2 参数表：AX200，额定输入电压 380V。"], term: false },
  ];

  function seed() {
    const term = {
      id: "R-001", kind: "check", family: "term", title: "Wi-Fi 术语规范写法", product: "VX120", docType: "安装手册",
      scope: "适用于 VX120 安装手册中的无线连接术语。", condition: "正文出现 WiFi 时，提示统一为公司示例词表规定的 Wi-Fi。",
      evidence: "引用当前文档中包含 WiFi 的原句和章节。", exception: "代码、网址及产品专有名称由人工核对。", useException: false,
      positiveIds: ["C-003"], negativeIds: [], status: "enabled", enabled: true, version: 1, validation: null,
    };
    term.published = clone(term);
    const legacy = {
      id: "L-001", kind: "legacy", family: "voltage", title: "额定电压前后不一致", product: "ALL", docType: "全部",
      scope: "单文档 / 全文一致性；适用型号和工况待补充。", condition: "问题描述与历史误报相似。", evidence: "当前原文证据待核对。",
      exception: "", useException: false, positiveIds: [], negativeIds: ["C-002"], status: "legacy", enabled: false, version: 0, published: null,
      reason: "不同工况允许不同电压；该条件尚未形成可执行规则。", linkedRuleId: null,
    };
    return { schema: 1, cases: clone(CASES), rules: [term, legacy], runs: [], audit: [], counter: 100 };
  }
  function audit(state, action, detail) {
    state.audit.unshift({ id: ++state.counter, time: stamp(), action, detail });
    state.audit = state.audit.slice(0, 150);
  }
  function findRule(state, id) {
    const rule = state.rules.find((item) => item.id === id);
    if (!rule) throw new Error("规则不存在。");
    return rule;
  }
  function findCase(state, id) {
    const item = state.cases.find((entry) => entry.id === id);
    if (!item) throw new Error("案例不存在。");
    return item;
  }
  function draftSignature(rule) {
    const fields = ["title", "product", "docType", "docVersion", "scope", "condition", "evidence", "exception", "useException", "positiveIds", "negativeIds", "family"];
    return JSON.stringify(fields.map((key) => rule[key]));
  }
  function makeDraft(state, caseId) {
    const item = findCase(state, caseId);
    if (item.feedback !== "accepted" || item.type !== "issue") throw new Error("仅已确认成立的问题可以生成检查规则；建议需先补充确认依据。");
    if (item.ruleId) return findRule(state, item.ruleId);
    const voltage = item.family === "voltage";
    const rule = {
      id: `R-${++state.counter}`, kind: "check", family: item.family, title: voltage ? "同一工况下的额定电压一致性" : item.title,
      product: item.product, docType: item.docType,
      scope: `适用于 ${item.product} 的${item.docType}，核对正文和参数表中的同一对象。`,
      condition: voltage ? "同一型号、版本和工况下，同名额定电压不同，且上下文没有合理解释时报告冲突。" : item.reason,
      evidence: voltage ? "必须引用当前文档的两处原文，标明型号、工况、参数值和章节；无法确认对应关系时转为待确认。" : "引用当前文档原文并核实判错依据。",
      exception: "", useException: false, positiveIds: [item.id], negativeIds: [], status: "draft", enabled: false, version: 0, published: null, validation: null,
    };
    state.rules.push(rule);
    item.ruleId = rule.id;
    item.collected = true;
    audit(state, "生成规则草稿", `${item.id} → ${rule.id} ${rule.title}`);
    return rule;
  }
  function confirmCase(state, id, reason) {
    const item = findCase(state, id);
    if (!String(reason || "").trim()) throw new Error("请填写确认问题成立的依据。");
    if (item.feedback !== "accepted" || item.type !== "suggestion") throw new Error("该案例无需执行建议确认。");
    item.type = "issue";
    item.reason = reason.trim();
    audit(state, "确认建议成立", `${id}：${item.reason}`);
  }
  function collectCase(state, id) {
    const item = findCase(state, id);
    item.collected = true;
    audit(state, "收集复核案例", `${id} ${item.title}`);
  }
  function saveDraft(state, id, updates) {
    const rule = findRule(state, id);
    if (rule.kind !== "check") throw new Error("误报参考需关联到有明确条件的检查规则。");
    for (const key of ["title", "product", "docType", "docVersion", "scope", "condition", "evidence", "exception"]) {
      if (key in updates) rule[key] = String(updates[key]).trim();
    }
    if ("useException" in updates) rule.useException = Boolean(updates.useException);
    rule.status = "draft";
    rule.validation = null;
    audit(state, "保存规则草稿", `${id} 待发布 v${rule.version + 1}；已发布版本独立保留。`);
    return rule;
  }
  function attachException(state, caseId, ruleId) {
    const item = findCase(state, caseId);
    const rule = findRule(state, ruleId);
    if (item.feedback !== "rejected" || item.family !== rule.family || rule.kind !== "check") throw new Error("请选择同一检查主题的规则关联反例。");
    if (!rule.negativeIds.includes(caseId)) rule.negativeIds.push(caseId);
    rule.exception = item.family === "voltage" ? "当前原文明示不同型号或不同工况，且数值分别对应各自对象时，允许参数不同。必须给出对应的原文依据。" : item.reason;
    rule.useException = true;
    rule.status = "draft";
    rule.validation = null;
    item.ruleId = rule.id;
    item.collected = true;
    audit(state, "关联误报反例", `${caseId} → ${ruleId}；例外条件进入草稿，审核后生效。`);
  }
  function linkLegacy(state, legacyId, ruleId) {
    const legacy = findRule(state, legacyId);
    if (legacy.kind !== "legacy") throw new Error("请选择旧误报参考。");
    attachException(state, legacy.negativeIds[0], ruleId);
    legacy.linkedRuleId = ruleId;
    audit(state, "整理旧误报参考", `${legacyId} 关联 ${ruleId}，保留原始反馈。`);
  }
  function evaluate(rule, scenario) {
    if ((rule.product !== "ALL" && rule.product !== scenario.product) || (rule.docType !== "全部" && rule.docType !== scenario.docType)) return { status: "skipped", reason: "当前产品或文档类型不在规则适用范围。", evidence: [] };
    if (rule.docVersion && rule.docVersion !== "全部" && rule.docVersion !== scenario.docVersion) return { status: "skipped", reason: "当前文档版本不在已审核范围。", evidence: [] };
    if (rule.family === "current") {
      const text = scenario.currentText || scenario.text.map((line) => line.replaceAll("电压", "电流").replaceAll("220V", "10A").replaceAll("380V", "16A"));
      const converted = { ...scenario, text, values: scenario.currentValues || (scenario.values[0] === scenario.values[1] ? [10, 10] : [10, 16]) };
      const result = evaluate({ ...rule, family: "voltage" }, converted);
      result.reason = result.reason.replaceAll("电压", "电流").replaceAll("220V", "10A").replaceAll("380V", "16A");
      return result;
    }
    if (rule.kind === "legacy") return { status: "reference", reason: "历史相似描述仅提供复核参考。", evidence: [] };
    if (rule.family === "term") return { status: scenario.term ? "issue" : "clear", reason: scenario.term ? "当前原文出现 WiFi，示例词表要求使用 Wi-Fi。" : "当前示例未出现需纠正的 WiFi 写法。", evidence: scenario.text.filter((line) => /Wi-?Fi/.test(line)) };
    if (rule.family !== "voltage") return { status: "review", reason: "此主题需人工设计验证案例，当前预设场景尚未覆盖。", evidence: [] };
    if (!scenario.evidence) return { status: "review", reason: "候选结论声称原文存在 48V，当前原文无法定位该数值；证据校验未通过。", evidence: scenario.text.slice(0, 2) };
    if (scenario.context === "unknown") return { status: "review", reason: "两处数值可以定位，但尚不能确认是否对应同一工况。", evidence: scenario.text.slice(0, 2) };
    if (scenario.values[0] === scenario.values[1]) return { status: "clear", reason: "同一工况下两处参数一致，已确认问题在当前示例中修复。", evidence: scenario.text.slice(0, 2) };
    if (scenario.context === "different" && rule.useException && rule.exception.trim()) return { status: "exception", reason: "两处原文明示标准模式和工业模式，满足已审核的例外条件。", evidence: scenario.text.slice(0, 2) };
    return { status: "issue", reason: scenario.context === "different" ? "检出了不同电压，但规则尚未配置不同工况的例外，会产生误报。" : "同一型号、同一工况下，220V 与 380V 冲突。", evidence: scenario.text.slice(0, 2) };
  }
  function draftErrors(rule) {
    const errors = [];
    for (const [key, label] of [["title", "规则名称"], ["scope", "适用条件"], ["condition", "判错条件"], ["evidence", "证据要求"]]) if (!rule[key]?.trim()) errors.push(`请填写${label}。`);
    if (!rule.positiveIds.length) errors.push("请关联至少一个已确认的问题案例。");
    if (rule.useException && !rule.exception.trim()) errors.push("启用例外时，请填写可核对的例外条件。");
    if (!["voltage", "current", "term"].includes(rule.family)) errors.push("此主题尚无预设验证场景，当前可保存草稿供讨论。");
    return errors;
  }
  function regression(state, id) {
    const rule = findRule(state, id);
    const errors = draftErrors(rule);
    if (errors.length) throw new Error(errors.join(" "));
    const samples = ["voltage", "current"].includes(rule.family)
      ? [["conflict", "issue"], ["exception", "exception"], ["fixed", "clear"], ["unclear", "review"], ["missing-evidence", "review"]]
      : [["conflict", "issue"], ["fixed", "clear"]];
    const results = samples.map(([scenarioId, expected]) => {
      const scenario = clone(SCENARIOS.find((entry) => entry.id === scenarioId));
      scenario.product = rule.product === "ALL" ? "VX120" : rule.product;
      scenario.docType = rule.docType === "全部" ? "安装手册" : rule.docType;
      scenario.docVersion = rule.docVersion;
      const actual = evaluate(rule, scenario);
      return { title: scenario.title, expected, actual: actual.status, passed: actual.status === expected, reason: actual.reason };
    });
    rule.validation = { signature: draftSignature(rule), passed: results.every((row) => row.passed), results, time: stamp() };
    audit(state, "运行正反案例验证", `${id}：${results.filter((row) => row.passed).length}/${results.length} 通过。`);
    return rule.validation;
  }
  function submitReview(state, id) {
    const rule = findRule(state, id);
    const errors = draftErrors(rule);
    if (errors.length) throw new Error(errors.join(" "));
    if (!rule.validation?.passed || rule.validation.signature !== draftSignature(rule)) throw new Error("请先让当前草稿通过全部正反案例验证。");
    rule.status = "review";
    audit(state, "提交规则审核", `${id} 待发布 v${rule.version + 1}`);
  }
  function publish(state, id, checks) {
    const rule = findRule(state, id);
    if (rule.status !== "review") throw new Error("请先提交审核。");
    if (!Array.isArray(checks) || checks.length !== 3 || !checks.every(Boolean)) throw new Error("请确认适用范围、正反案例和证据要求三项审核内容。");
    if (draftErrors(rule).length || !rule.validation?.passed || rule.validation.signature !== draftSignature(rule)) throw new Error("草稿已变化，请重新验证和提交审核。");
    rule.version += 1;
    rule.status = "enabled";
    rule.enabled = true;
    const release = clone(rule);
    delete release.published;
    release.publishedAt = stamp();
    rule.published = release;
    audit(state, "审核通过并启用", `${id} v${rule.version}；新任务使用此版本。`);
  }
  function toggleRule(state, id) {
    const rule = findRule(state, id);
    if (!rule.published) throw new Error("请先完成规则审核和发布。");
    rule.enabled = !rule.enabled;
    if (!["draft", "review"].includes(rule.status)) rule.status = rule.enabled ? "enabled" : "disabled";
    audit(state, rule.enabled ? "恢复已发布规则" : "停用规则", `${id} v${rule.version}；历史任务快照保持原样。`);
  }
  function runTrial(state, scenarioId) {
    const scenario = SCENARIOS.find((item) => item.id === scenarioId);
    if (!scenario) throw new Error("请选择演示文档。");
    const snapshots = state.rules.filter((rule) => rule.enabled && rule.published).map((rule) => clone(rule.published));
    const legacyRefs = state.rules.filter((rule) => rule.kind === "legacy").map(clone);
    const results = snapshots.map((rule) => {
      const result = evaluate(rule, scenario);
      const references = legacyRefs.filter((legacy) => legacy.family === rule.family);
      return { ruleId: rule.id, title: rule.title, version: rule.version, ...result, baseStatus: result.status, status: result.status === "issue" && references.length ? "conflict" : result.status, references, resolution: null, feedback: null };
    });
    const run = { id: `T-${++state.counter}`, time: stamp(), scenario: clone(scenario), snapshots, legacyRefs, results };
    state.runs.unshift(run);
    audit(state, "完成文档试运行", `${run.id} ${scenario.title}；冻结 ${snapshots.length} 个规则版本。`);
    return run;
  }
  function reviewResult(state, runId, ruleId, decision, note) {
    const run = state.runs.find((item) => item.id === runId);
    const result = run?.results.find((item) => item.ruleId === ruleId);
    if (!result) throw new Error("试运行条目不存在。");
    if (!["accepted", "rejected", "pending"].includes(decision)) throw new Error("请选择有效复核结论。");
    if (!String(note || "").trim()) throw new Error("请填写当前原文支持的复核依据。");
    if (result.feedback) throw new Error("该条目已保存反馈，请在案例库查看关联记录。");
    if (decision === "pending") {
      result.resolution = { decision, note: note.trim(), time: stamp() };
      audit(state, "保留待确认", `${runId} / ${ruleId}：${note.trim()}`);
      return null;
    }
    const snapshot = run.snapshots.find((rule) => rule.id === ruleId);
    const item = { id: `C-${++state.counter}`, title: result.title, family: snapshot.family, feedback: decision, type: result.baseStatus === "review" ? "suggestion" : "issue", rejection: decision === "rejected" ? "false_positive" : null, reason: note.trim(), document: `试运行 ${run.id} · ${run.scenario.title}`, section: "见原文证据", product: run.scenario.product, docType: run.scenario.docType, excerpts: result.evidence.slice(), reviewer: "体验者", check: snapshot.family === "term" ? "常用词" : "全文一致性", collected: false, ruleId: null, sourceRuleId: ruleId, sourceRunId: runId };
    state.cases.unshift(item);
    result.feedback = { caseId: item.id, decision, note: note.trim(), time: stamp() };
    result.resolution = result.feedback;
    audit(state, "保存人工复核", `${runId} / ${ruleId} → ${item.id}；案例待整理。`);
    return item;
  }
  const api = { seed, scenarios: clone(SCENARIOS), clone, findRule, findCase, makeDraft, confirmCase, collectCase, saveDraft, attachException, linkLegacy, evaluate, draftErrors, regression, submitReview, publish, toggleRule, runTrial, reviewResult, draftSignature };
  root.RuleLibraryDemo = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
