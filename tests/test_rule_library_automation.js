const assert = require("node:assert/strict");
const { test } = require("node:test");
const M = require("../app/web/static/rule-library-prototype/model.js");
const A = require("../app/web/static/rule-library-prototype/automation.js");

function vxRules(state) {
  return state.automation.groups.filter((group) => group.status === "ready" && group.product === "VX120" && group.docVersion === "V2.0").map((group) => M.findRule(state, group.ruleId));
}
function entries(state, rules) {
  return rules.map((rule) => ({ id: rule.id, token: A.approvalToken(state, rule.id) }));
}
function approvedState() {
  const state = A.seed();
  const rules = vxRules(state);
  A.approve(state, entries(state, rules));
  return { state, rules: rules.map((rule) => M.findRule(state, rule.id)) };
}

test("大量反馈自动去重分组，只有规则与异常进入工作台", () => {
  const state = A.seed();
  const stats = A.metrics(state);
  assert.equal(stats.received, 1008);
  assert.equal(stats.unique, 216);
  assert.equal(stats.independent, 215);
  assert.equal(stats.duplicates, 792);
  assert.equal(stats.ready, 3);
  assert.equal(stats.blocked, 2);
  for (const rule of vxRules(state)) {
    assert.equal(rule.enabled, false);
    assert.equal(rule.status, "review");
    assert.equal(rule.validation.passed, true);
    assert.ok(rule.validation.history.length > 40);
    assert.equal(rule.validation.results.length, 5);
  }
});

test("重复导入幂等，同源重复回合不增加独立支持数", () => {
  const state = A.seed();
  const before = A.metrics(state);
  const batch = A.ingest(state, A.initialRecords());
  assert.equal(batch.received, 0);
  assert.deepEqual(A.metrics(state), before);
  A.ingest(state, [{ ...A.initialRecords()[0], receiptId: "REPEAT-ROUND" }]);
  assert.equal(A.metrics(state).received, before.received + 1);
  assert.equal(A.metrics(state).independent, before.independent);
  assert.equal(A.metrics(state).unique, before.unique);
});

test("分组保留产品与文档版本边界，同源相反反馈不按数量投票", () => {
  const state = A.seed();
  const groups = state.automation.groups.filter((group) => group.family === "voltage");
  assert.equal(groups.length, 3);
  const conflict = groups.find((group) => group.docVersion === "V3.0");
  const stats = A.groupStats(state, conflict);
  assert.equal(stats.independent, 1);
  assert.equal(stats.positive, 1);
  assert.equal(stats.negative, 1);
  assert.equal(conflict.blockType, "contradiction");
  assert.equal(conflict.ruleId, null);
});

test("同范围规则一次审核发布，跨范围批量审核全部拒绝", () => {
  const state = A.seed();
  const vx = vxRules(state);
  const ax = state.rules.find((rule) => rule.product === "AX200");
  assert.throws(() => A.approve(state, entries(state, [vx[0], ax])), /相同产品/);
  assert.equal(M.findRule(state, vx[0].id).enabled, false);
  assert.equal(M.findRule(state, ax.id).enabled, false);
  assert.equal(A.approve(state, entries(state, vx)), 2);
  assert.equal(A.metrics(state).ready, 1);
  assert.equal(A.metrics(state).enabled, 3);
});

test("新增已覆盖反馈直接归档，保持已发布版本和待办数量", () => {
  const { state, rules } = approvedState();
  const versions = rules.map((rule) => JSON.stringify(rule.published));
  const before = A.metrics(state);
  const batch = A.simulate(state);
  const after = A.metrics(state);
  assert.equal(batch.received, 120);
  assert.equal(after.ready, before.ready);
  assert.equal(after.archivedReceipts - before.archivedReceipts, 120);
  assert.deepEqual(rules.map((rule) => JSON.stringify(M.findRule(state, rule.id).published)), versions);
});

test("新产品范围生成独立候选，机器反馈不会进入人工依据", () => {
  const { state } = approvedState();
  const before = A.metrics(state);
  A.simulate(state, "new-scope");
  assert.equal(A.metrics(state).ready, before.ready + 1);
  const rule = state.rules.find((item) => item.product === "BX300");
  assert.equal(rule.enabled, false);
  const snapshot = A.metrics(state);
  A.ingest(state, [{ ...A.initialRecords()[0], receiptId: "MACHINE-1", origin: "machine" }]);
  assert.deepEqual(A.metrics(state), snapshot);
  assert.equal(state.automation.machineCount, 1);
});

test("审核摘要打开后新增独立样本，旧摘要不能直接发布", () => {
  const state = A.seed();
  const rules = vxRules(state);
  const approval = entries(state, rules);
  A.simulate(state);
  assert.throws(() => A.approve(state, approval), /已更新/);
  for (const rule of rules) assert.equal(M.findRule(state, rule.id).enabled, false);
});

test("修改条件自动验证，验证失败的修订不替换发布版本", () => {
  const { state, rules } = approvedState();
  const rule = rules.find((item) => item.family === "voltage");
  const original = JSON.stringify(rule.published);
  A.saveRule(state, rule.id, { useException: false });
  assert.equal(rule.validation.passed, false);
  assert.equal(A.groupFor(state, rule.groupId).status, "blocked");
  assert.throws(() => A.approve(state, entries(state, [rule])), /未通过验证/);
  A.simulate(state);
  assert.equal(A.groupFor(state, rule.groupId).status, "blocked");
  assert.equal(JSON.stringify(rule.published), original);
  assert.equal(A.runTrial(state, "exception").results.find((item) => item.ruleId === rule.id).status, "exception");
});

test("条件修订自动进入审核，反馈归档不会吞掉待审核修订", () => {
  const { state, rules } = approvedState();
  const rule = rules.find((item) => item.family === "voltage");
  A.saveRule(state, rule.id, { scope: "VX120 安装手册 V2.0，核对相同工况的对应项。" });
  assert.equal(A.groupFor(state, rule.groupId).status, "ready");
  A.simulate(state);
  assert.equal(A.groupFor(state, rule.groupId).status, "ready");
  A.approve(state, entries(state, [rule]));
  assert.equal(M.findRule(state, rule.id).version, 2);
});

test("冲突裁定保留原反馈，少量来源候选只能单独审核", () => {
  const state = A.seed();
  const conflict = state.automation.groups.find((group) => group.blockType === "contradiction");
  const originals = M.clone(A.groupCases(state, conflict));
  assert.throws(() => A.resolve(state, conflict.id, "issue", ""), /依据/);
  A.resolve(state, conflict.id, "pending", "等待产品负责人核实工况。");
  assert.equal(conflict.status, "blocked");
  A.resolve(state, conflict.id, "issue", "两处均为标准模式，参数冲突成立。");
  assert.equal(conflict.status, "ready");
  assert.deepEqual(A.groupCases(state, conflict).map((item) => [item.feedback, item.reason]), originals.map((item) => [item.feedback, item.reason]));
  const rule = M.findRule(state, conflict.ruleId);
  assert.equal(rule.enabled, false);
  assert.throws(() => A.approve(state, entries(state, [rule, vxRules(state)[0]])), /独立来源较少/);
  A.approve(state, entries(state, [rule]));
  assert.equal(M.findRule(state, rule.id).enabled, true);
});

test("人工裁定只作用于当时案例，后续反例仍进入验证", () => {
  const state = A.seed();
  const group = state.automation.groups.find((item) => item.blockType === "contradiction");
  A.resolve(state, group.id, "issue", "当前两处为标准模式，冲突成立。");
  const newRecord = M.clone(A.initialRecords().find((item) => item.family === "voltage" && item.product === "VX120" && item.feedback === "rejected" && item.docVersion === "V2.0"));
  newRecord.receiptId = "NEW-V3-COUNTEREXAMPLE";
  newRecord.sourceId = "NEW-V3-SOURCE";
  newRecord.docVersion = "V3.0";
  newRecord.facts.docVersion = "V3.0";
  delete newRecord.preferredId;
  A.ingest(state, [newRecord]);
  const rule = M.findRule(state, group.ruleId);
  assert.equal(rule.validation.history.length, 2);
  assert.ok(rule.validation.history.some((item) => item.expected === "exception"));
});

test("认可建议可以按原意归档，不升级为已确认错误", () => {
  const state = A.seed();
  const group = state.automation.groups.find((item) => item.blockType === "evidence");
  A.resolve(state, group.id, "archive", "当前只认可核查价值，保留案例等待安全条件确认。");
  assert.equal(group.status, "archived");
  assert.equal(group.ruleId, null);
  assert.ok(A.groupCases(state, group).every((item) => item.type === "suggestion"));
});

test("已审核例外自动解释历史误报，试运行不增加人工认可数", () => {
  const { state, rules } = approvedState();
  const before = A.metrics(state);
  const run = A.runTrial(state, "conflict");
  const voltage = run.results.find((item) => item.ruleId === rules.find((rule) => rule.family === "voltage").id);
  assert.equal(voltage.status, "issue");
  assert.match(voltage.autoExplanation, /已核对旧误报/);
  assert.equal(voltage.source, "machine");
  assert.equal(voltage.feedback, null);
  assert.deepEqual(A.metrics(state), before);
  const item = A.reviewResult(state, run.id, voltage.ruleId, "accepted", "两处原文均为标准模式，参数冲突成立。");
  assert.equal(item.origin, "human");
  assert.equal(item.collected, true);
  assert.equal(A.groupFor(state, item.groupId).status, "archived");
  assert.equal(voltage.source, "machine");
});

test("文档版本在检查时生效，停用不会改写历史快照或被新增反馈恢复", () => {
  const { state, rules } = approvedState();
  const rule = rules[0];
  const run = A.runTrial(state, "conflict");
  const snapshot = M.clone(run);
  const scenario = { ...M.scenarios[0], docVersion: "V3.0" };
  assert.equal(M.evaluate(rule.published, scenario).status, "skipped");
  M.toggleRule(state, rule.id);
  A.simulate(state);
  assert.equal(M.findRule(state, rule.id).enabled, false);
  assert.deepEqual(run, snapshot);
  assert.equal(A.runTrial(state, "conflict").snapshots.some((item) => item.id === rule.id), false);
});
