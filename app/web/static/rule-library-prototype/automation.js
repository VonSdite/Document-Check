/* 批量反馈的自动整理、按主题审核与增量归档演示。 */
(function (root) {
  "use strict";
  const M = typeof module !== "undefined" && module.exports ? require("./model.js") : root.RuleLibraryDemo;
  const clone = M.clone;
  const now = () => new Date().toISOString();
  const normalize = (text) => String(text || "").replace(/\s+/g, " ").trim();
  const TOPICS = { voltage: "同一工况下的额定电压一致性", current: "同一工况下的额定电流一致性", term: "Wi-Fi 术语规范写法", safety: "更换模块前的断电条件", evidence: "图像缺失结论的证据门槛" };

  function log(state, action, detail) {
    state.audit.unshift({ id: ++state.counter, time: now(), action, detail });
    state.audit = state.audit.slice(0, 200);
  }
  function groupKey(item) {
    return JSON.stringify([item.family, item.check, item.product, item.docType, item.docVersion]);
  }
  function evidenceKey(item) {
    return JSON.stringify([groupKey(item), item.sourceId, item.section, item.excerpts.map(normalize)]);
  }
  function caseKey(item) {
    return JSON.stringify([evidenceKey(item), item.feedback, item.type, item.rejection || "", item.conditionKey]);
  }
  function groupFor(state, id) {
    const group = state.automation.groups.find((entry) => entry.id === id);
    if (!group) throw new Error("主题不存在。");
    return group;
  }
  function groupCases(state, group) {
    const ids = new Set(group.caseIds);
    return state.cases.filter((item) => ids.has(item.id));
  }
  function scenarioFacts(family, product, version, negative = false) {
    const scenario = clone(M.scenarios.find((item) => item.id === (negative ? "exception" : "conflict")));
    scenario.product = product;
    scenario.docVersion = version;
    scenario.text = scenario.text.map((line) => line.replaceAll("VX120", product));
    if (family === "current") {
      scenario.text = scenario.text.slice(0, 2).map((line) => line.replaceAll("电压", "电流").replaceAll("220V", "10A").replaceAll("380V", "16A"));
      scenario.currentText = scenario.text;
      scenario.currentValues = [10, 16];
    }
    if (family === "term") {
      scenario.term = !negative;
      scenario.text = [negative ? "产品专有标识为 WiFi-Pro，网络设置项名称为 Wi-Fi。" : "通过 WiFi 连接设备并完成配置。"];
    }
    return scenario;
  }
  function sampleRecords({ prefix, family, product = "VX120", version = "V2.0", positive, negative, repeats, special = "" }) {
    const records = [];
    const total = positive + negative;
    for (let index = 0; index < total; index += 1) {
      const rejected = index >= positive;
      const facts = scenarioFacts(family, product, version, rejected);
      const sourceId = special === "contradiction" ? `${prefix}-DOC-1` : `${prefix}-DOC-${index + 1}`;
      const item = {
        family, product, docVersion: version, docType: family === "safety" ? "维护说明" : "安装手册",
        title: TOPICS[family], check: ["voltage", "current"].includes(family) ? "全文一致性" : family === "term" ? "常用词" : "内容完整性",
        feedback: rejected ? "rejected" : "accepted", type: family === "safety" ? "suggestion" : "issue", origin: "human",
        rejection: rejected ? "false_positive" : null, sourceId, document: `${product} ${family === "safety" ? "维护说明" : "安装手册"} ${version} · 样本 ${index + 1}`,
        section: ["voltage", "current"].includes(family) ? "2.1 要求 / 5.2 参数表" : "4.1 操作说明", reviewer: index % 2 ? "李工" : "陈工",
        reason: rejected ? "当前原文已明确区分不同适用条件，差异合理。" : "当前原文直接支持问题成立。",
        conditionKey: rejected ? "different-context" : "same-context", excerpts: facts.text.slice(0, family === "term" ? 1 : 2), facts,
        expected: rejected ? (family === "term" ? "clear" : "exception") : "issue", collected: true, ruleId: null,
      };
      if (special === "contradiction") {
        item.facts = scenarioFacts(family, product, version, false);
        item.excerpts = item.facts.text.slice(0, 2);
        item.conditionKey = "same-context";
        item.reason = rejected ? "复核人认为两处参数可能来自不同工况，尚无工况说明支持。" : "两处均标明标准模式，判定为参数冲突。";
      }
      if (family === "safety") {
        item.excerpts = ["打开侧盖，取下连接器，替换控制模块。"];
        item.reason = "认可核查价值；是否支持带电更换，仍待产品负责人确认。";
        item.expected = "review";
      }
      if (family === "evidence") {
        item.feedback = "rejected";
        item.rejection = "model_hallucination";
        item.excerpts = ["安装位置见图 3-1。", "人工查看原 PDF：图 3-1 位于第 12 页。"];
        item.reason = "仅凭文本抽取不能断言图像不存在，保留为证据校验反例。";
      }
      if (prefix === "VOLT-VX" && index === 0) item.preferredId = "C-001";
      if (prefix === "VOLT-VX" && index === positive) item.preferredId = "C-002";
      if (prefix === "TERM-VX" && index === 0) item.preferredId = "C-003";
      for (let repeat = 0; repeat < repeats; repeat += 1) records.push({ ...clone(item), receiptId: `${prefix}-${index}-${repeat}` });
    }
    return records;
  }
  function initialRecords() {
    return [
      { prefix: "VOLT-VX", family: "voltage", positive: 48, negative: 16, repeats: 6 },
      { prefix: "CURRENT-VX", family: "current", positive: 36, negative: 12, repeats: 5 },
      { prefix: "VOLT-AX", family: "voltage", product: "AX200", version: "V1.0", positive: 24, negative: 8, repeats: 4 },
      { prefix: "TERM-VX", family: "term", positive: 40, negative: 10, repeats: 4 },
      { prefix: "CONFLICT-VX", family: "voltage", version: "V3.0", positive: 1, negative: 1, repeats: 4, special: "contradiction" },
      { prefix: "SAFETY-VX", family: "safety", positive: 8, negative: 0, repeats: 3 },
      { prefix: "EVIDENCE-VX", family: "evidence", positive: 0, negative: 12, repeats: 2 },
    ].flatMap(sampleRecords);
  }
  function samplesFor(state, group) {
    const cases = groupCases(state, group);
    const resolvedIds = new Set(group.resolution?.caseIds || []);
    const additions = cases.filter((item) => !resolvedIds.has(item.id));
    if (group.resolution?.decision === "archive") return additions.filter((item) => item.type === "issue");
    if (group.resolution?.decision === "issue" || group.resolution?.decision === "exception") {
      const sample = clone(cases.find((item) => resolvedIds.has(item.id)));
      sample.feedback = group.resolution.decision === "issue" ? "accepted" : "rejected";
      sample.expected = group.resolution.decision;
      sample.type = "issue";
      sample.facts.context = group.resolution.decision === "issue" ? "same" : "different";
      return [sample, ...additions.filter((item) => item.type === "issue")];
    }
    return cases.filter((item) => item.type === "issue" && ["accepted", "rejected"].includes(item.feedback));
  }
  function sourceSignature(state, group) {
    return JSON.stringify([groupCases(state, group).map((item) => [item.id, caseKey(item), item.expected]), group.resolution || null]);
  }
  function validate(state, group, rule) {
    const structuralErrors = M.draftErrors(rule);
    const history = samplesFor(state, group).map((item) => {
      const result = M.evaluate(rule, item.facts);
      return { caseId: item.id, title: item.document, expected: item.expected, actual: result.status, passed: result.status === item.expected, reason: result.reason };
    });
    let boundaries = [];
    if (!structuralErrors.length) boundaries = M.regression(state, rule.id).results;
    const passed = !structuralErrors.length && history.length > 0 && history.every((item) => item.passed) && boundaries.every((item) => item.passed);
    rule.validation = { passed, signature: M.draftSignature(rule), sourceSignature: sourceSignature(state, group), history, results: boundaries, errors: structuralErrors, time: now() };
    rule.status = passed ? "review" : "draft";
    group.status = passed ? "ready" : "blocked";
    group.blockType = passed ? null : "validation";
    group.reason = passed ? "已自动整理正反案例并完成验证，等待审核规则。" : "自动验证发现不符合预期的案例，需要调整规则条件。";
    return rule.validation;
  }
  function ensureRule(state, group) {
    let rule = state.rules.find((item) => item.id === group.ruleId);
    if (rule) return rule;
    const samples = samplesFor(state, group);
    const positives = samples.filter((item) => item.feedback === "accepted");
    if (!positives.length) return null;
    const name = TOPICS[group.family] || group.title;
    rule = {
      id: `R-${++state.counter}`, kind: "check", family: group.family, title: name, product: group.product, docType: group.docType, docVersion: group.docVersion,
      scope: `${group.product} · ${group.docType} · ${group.docVersion}；同一对象、同一工况的对应参数。`,
      condition: "同一型号、版本和工况下，同名参数不同，且上下文没有合理解释时报告冲突。",
      evidence: "引用当前文档两处原文，标明对象、参数、工况与章节；对应关系不明确时保留待确认。",
      exception: "原文明示不同型号或不同工况，并能分别对应两个数值时，允许差异。",
      useException: true, positiveIds: positives.map((item) => item.id), negativeIds: samples.filter((item) => item.feedback === "rejected").map((item) => item.id),
      status: "draft", enabled: false, version: 0, published: null, validation: null, groupId: group.id,
    };
    state.rules.push(rule);
    group.ruleId = rule.id;
    return rule;
  }
  function processGroup(state, group) {
    const cases = groupCases(state, group);
    const signature = sourceSignature(state, group);
    if (signature === group.processedSignature) return;
    group.processedSignature = signature;
    const resolvedIds = new Set(group.resolution?.caseIds || []);
    if (group.resolution?.decision === "archive" && cases.every((item) => resolvedIds.has(item.id))) {
      group.status = "archived";
      group.reason = "人工保留为待核实案例，暂不形成检查规则。";
      return;
    }
    const evidenceVotes = new Map();
    samplesFor(state, group).forEach((item) => {
      const key = evidenceKey(item);
      if (!evidenceVotes.has(key)) evidenceVotes.set(key, new Set());
      evidenceVotes.get(key).add(item.feedback);
    });
    if ([...evidenceVotes.values()].some((votes) => votes.size > 1)) {
      group.status = "blocked";
      group.blockType = "contradiction";
      group.reason = "同一来源、同一原文出现相反人工反馈，需要核对适用条件，重复次数不参与投票。";
      return;
    }
    if (cases.some((item) => item.type === "suggestion" && !resolvedIds.has(item.id))) {
      group.status = "blocked";
      group.blockType = "evidence";
      group.reason = "认可的是核查价值，缺少将建议确认为问题的业务依据。";
      return;
    }
    const samples = samplesFor(state, group);
    if (!samples.some((item) => item.feedback === "accepted")) {
      group.status = "archived";
      group.reason = "已归档为反例或证据校验参考，待同主题规则需要时使用。";
      return;
    }
    let rule = state.rules.find((item) => item.id === group.ruleId);
    if (!rule) {
      rule = state.rules.find((item) => item.kind === "check" && item.published && item.family === group.family && item.product === group.product && item.docType === group.docType && item.docVersion === group.docVersion);
      if (rule) { group.ruleId = rule.id; rule.groupId = group.id; }
    }
    if (rule?.published && !["draft", "review"].includes(rule.status) && samples.every((item) => M.evaluate(rule.published, item.facts).status === item.expected)) {
      group.status = "archived";
      group.reason = rule.enabled ? "反馈符合已审核规则的判断边界，自动归档；规则版本保持不变。" : "反馈符合已发布规则，已归档；规则保持停用。";
      cases.forEach((item) => { item.ruleId = rule.id; });
      return;
    }
    rule = ensureRule(state, group);
    if (!rule) return;
    rule.positiveIds = samples.filter((item) => item.feedback === "accepted").map((item) => item.id);
    rule.negativeIds = samples.filter((item) => item.feedback === "rejected").map((item) => item.id);
    cases.forEach((item) => { item.ruleId = rule.id; });
    validate(state, group, rule);
  }
  function ingest(state, records) {
    const lookup = new Map(state.cases.map((item) => [caseKey(item), item]));
    const groups = new Map(state.automation.groups.map((group) => [group.key, group]));
    let received = 0;
    let newCases = 0;
    let machine = 0;
    for (const record of records) {
      if (!record.receiptId || state.automation.seen[record.receiptId]) continue;
      state.automation.seen[record.receiptId] = true;
      if (record.origin !== "human") { machine += 1; state.automation.machineCount += 1; continue; }
      received += 1;
      state.automation.received += 1;
      let item = lookup.get(caseKey(record));
      if (!item) {
        item = { ...clone(record), id: record.preferredId || `C-${++state.counter}`, receipts: 0, collected: true, feedbackNotes: [] };
        delete item.receiptId;
        delete item.preferredId;
        state.cases.push(item);
        lookup.set(caseKey(item), item);
        newCases += 1;
      }
      item.receipts += 1;
      if (!item.feedbackNotes) item.feedbackNotes = [];
      if (!item.feedbackNotes.some((note) => note.reviewer === record.reviewer && note.reason === record.reason)) item.feedbackNotes.push({ reviewer: record.reviewer, reason: record.reason });
      const key = groupKey(item);
      let group = groups.get(key);
      if (!group) {
        group = { id: `G-${++state.counter}`, key, title: TOPICS[item.family] || item.title, family: item.family, check: item.check, product: item.product, docType: item.docType, docVersion: item.docVersion, caseIds: [], status: "processing", ruleId: null };
        state.automation.groups.push(group);
        groups.set(key, group);
      }
      if (!group.caseIds.includes(item.id)) group.caseIds.push(item.id);
      item.groupId = group.id;
    }
    for (const group of state.automation.groups) processGroup(state, group);
    const summary = { received, newCases, duplicates: received - newCases, machine, time: now() };
    state.automation.lastBatch = summary;
    if (received || machine) log(state, "自动整理反馈", `${received} 条人工反馈 → ${newCases} 条新案例；合并 ${summary.duplicates} 条重复反馈，机器判定单独记录 ${machine} 条。`);
    return summary;
  }
  function seed() {
    const state = M.seed();
    state.schema = 2;
    state.cases = [];
    state.automation = { groups: [], seen: {}, received: 0, machineCount: 0, batchNumber: 0, lastBatch: null };
    const term = M.findRule(state, "R-001");
    term.docVersion = "V2.0";
    term.published.docVersion = "V2.0";
    ingest(state, initialRecords());
    return state;
  }
  function metrics(state) {
    const groups = state.automation.groups;
    const archived = new Set(groups.filter((group) => group.status === "archived").flatMap((group) => group.caseIds));
    return {
      received: state.automation.received, unique: state.cases.length, duplicates: state.automation.received - state.cases.length,
      independent: new Set(state.cases.map(evidenceKey)).size, ready: groups.filter((group) => group.status === "ready").length,
      blocked: groups.filter((group) => group.status === "blocked").length, archived: archived.size,
      archivedReceipts: state.cases.filter((item) => archived.has(item.id)).reduce((sum, item) => sum + item.receipts, 0),
      enabled: state.rules.filter((rule) => rule.kind === "check" && rule.enabled).length,
    };
  }
  function groupStats(state, group) {
    const cases = groupCases(state, group);
    return { received: cases.reduce((sum, item) => sum + item.receipts, 0), unique: cases.length, independent: new Set(cases.map(evidenceKey)).size, positive: cases.filter((item) => item.feedback === "accepted" && item.type === "issue").length, negative: cases.filter((item) => item.feedback === "rejected").length, pending: cases.filter((item) => item.type === "suggestion").length };
  }
  function batchKey(rule) { return JSON.stringify([rule.product, rule.docType, rule.docVersion, rule.family === "term" ? "常用词" : "全文一致性"]); }
  function approvalToken(state, id) {
    const rule = M.findRule(state, id);
    const group = groupFor(state, rule.groupId);
    return JSON.stringify([M.draftSignature(rule), rule.version, rule.status, sourceSignature(state, group)]);
  }
  function approve(state, entries) {
    if (!entries.length) throw new Error("请选择待审核规则。");
    const rules = entries.map((entry) => {
      const rule = M.findRule(state, entry.id);
      const group = groupFor(state, rule.groupId);
      if (group.status !== "ready" || rule.status !== "review" || !rule.validation?.passed) throw new Error("所选规则包含未通过验证或存在冲突的项目。");
      if (entry.token !== approvalToken(state, entry.id) || rule.validation.signature !== M.draftSignature(rule) || rule.validation.sourceSignature !== sourceSignature(state, group)) throw new Error("规则或样本已更新，请重新打开审核摘要。");
      if (entries.length > 1 && groupStats(state, group).independent < 3) throw new Error("独立来源较少的规则需单独审核。");
      return rule;
    });
    if (new Set(entries.map((entry) => entry.id)).size !== entries.length) throw new Error("请选择互不重复的规则。");
    if (new Set(rules.map(batchKey)).size !== 1) throw new Error("批量审核仅支持相同产品、文档类型、版本和检查项，请分组处理。");
    const working = clone(state);
    for (const rule of rules) {
      M.publish(working, rule.id, [true, true, true]);
      const group = groupFor(working, rule.groupId);
      group.status = "archived";
      group.reason = "规则已审核启用，后续符合条件的同类反馈自动归档。";
      if (rule.family === "voltage") {
        const legacy = working.rules.find((item) => item.kind === "legacy" && item.negativeIds.some((id) => rule.negativeIds.includes(id)));
        if (legacy) legacy.linkedRuleId = rule.id;
      }
    }
    log(working, entries.length > 1 ? "批量审核规则" : "审核规则", `${entries.length} 条规则已启用；自动归档关联案例。`);
    Object.assign(state, working);
    return rules.length;
  }
  function saveRule(state, id, updates) {
    const rule = M.saveDraft(state, id, updates);
    const group = groupFor(state, rule.groupId);
    validate(state, group, rule);
    log(state, "自动重新验证", `${id} 保存后自动回放历史案例与边界场景。`);
    return rule;
  }
  function resolve(state, groupId, decision, note) {
    const group = groupFor(state, groupId);
    if (group.status !== "blocked") throw new Error("该主题已处理，请查看最新状态。");
    if (!String(note || "").trim()) throw new Error("请填写当前证据支持的判断依据。");
    if (!['pending', 'archive', 'issue', 'exception'].includes(decision)) throw new Error("请选择有效处理方式。");
    if (group.blockType !== "contradiction" && ["issue", "exception"].includes(decision)) throw new Error("缺少业务依据的建议应保留待核实。");
    if (decision === "pending") {
      group.pendingNote = note.trim();
    } else {
      group.resolution = { decision, note: note.trim(), time: now(), caseIds: [...group.caseIds] };
      processGroup(state, group);
    }
    log(state, "处理主题异常", `${group.id}：${note.trim()}；原始反馈分别保留。`);
  }
  function simulate(state, mode = "repeat") {
    state.automation.batchNumber += 1;
    const batch = state.automation.batchNumber;
    const records = [
      ...sampleRecords({ prefix: `B${batch}-TERM`, family: "term", positive: 20, negative: 6, repeats: 4 }),
      ...sampleRecords({ prefix: `B${batch}-VOLT`, family: "voltage", positive: 3, negative: 1, repeats: 4 }),
    ];
    if (mode === "new-scope") records.push(...sampleRecords({ prefix: `B${batch}-NEW`, family: "voltage", product: "BX300", version: "V1.0", positive: 3, negative: 1, repeats: 4 }));
    return ingest(state, records);
  }
  function runTrial(state, scenarioId) {
    const scenario = clone(M.scenarios.find((item) => item.id === scenarioId));
    if (!scenario) throw new Error("请选择试运行场景。");
    scenario.docVersion = scenario.product === "AX200" ? "V1.0" : "V2.0";
    scenario.currentText = scenario.text.slice(0, 2).map((line) => line.replaceAll("电压", "电流").replaceAll("220V", "10A").replaceAll("380V", "16A"));
    const snapshots = state.rules.filter((rule) => rule.enabled && rule.published).map((rule) => clone(rule.published));
    const legacyRefs = state.rules.filter((rule) => rule.kind === "legacy").map(clone);
    const results = snapshots.map((rule) => {
      const result = M.evaluate(rule, scenario);
      const references = legacyRefs.filter((legacy) => legacy.family === rule.family);
      const linked = references.every((legacy) => legacy.linkedRuleId === rule.id && rule.useException);
      return { ruleId: rule.id, title: rule.title, version: rule.version, ...result, baseStatus: result.status, status: result.status === "issue" && references.length && !linked ? "conflict" : result.status, references, resolution: null, feedback: null, source: "machine", autoExplanation: references.length && linked ? "已核对旧误报的不同工况例外：当前条件未满足例外，按已审核规则保留结论。" : "" };
    });
    const run = { id: `T-${++state.counter}`, time: now(), scenario, snapshots, legacyRefs, results };
    state.runs.unshift(run);
    log(state, "完成自动试运行", `${run.id}；冻结 ${snapshots.length} 个版本，机器判定与人工反馈分别记录。`);
    return run;
  }
  function reviewResult(state, runId, ruleId, decision, note) {
    const item = M.reviewResult(state, runId, ruleId, decision, note);
    if (!item) return null;
    const run = state.runs.find((entry) => entry.id === runId);
    const rule = run.snapshots.find((entry) => entry.id === ruleId);
    state.cases = state.cases.filter((entry) => entry.id !== item.id);
    const expected = decision === "accepted" ? M.evaluate(rule, run.scenario).status : "exception";
    ingest(state, [{ ...item, preferredId: item.id, receiptId: `REVIEW-${runId}-${ruleId}`, origin: "human", docVersion: run.scenario.docVersion, sourceId: run.id, conditionKey: run.scenario.context, facts: clone(run.scenario), expected }]);
    return M.findCase(state, item.id);
  }
  const api = { seed, initialRecords, ingest, simulate, metrics, groupStats, groupFor, groupCases, approvalToken, approve, saveRule, resolve, runTrial, reviewResult, groupKey, evidenceKey, caseKey, batchKey };
  root.RuleLibraryAutomation = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
