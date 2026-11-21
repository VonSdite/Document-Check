const assert = require("node:assert/strict");
const { test } = require("node:test");
const M = require("../app/web/static/rule-library-prototype/model.js");

function publishedVoltage() {
  const state = M.seed();
  const rule = M.makeDraft(state, "C-001");
  M.attachException(state, "C-002", rule.id);
  M.regression(state, rule.id);
  M.submitReview(state, rule.id);
  M.publish(state, rule.id, [true, true, true]);
  return { state, rule };
}

test("认可建议需要补充确认依据才能生成问题规则", () => {
  const state = M.seed();
  assert.throws(() => M.makeDraft(state, "C-004"), /建议需先/);
  assert.throws(() => M.confirmCase(state, "C-004", ""), /确认问题成立/);
  M.confirmCase(state, "C-004", "产品负责人确认更换控制模块必须断电，完整操作步骤缺少此要求。");
  const rule = M.makeDraft(state, "C-004");
  assert.equal(rule.status, "draft");
  assert.equal(rule.enabled, false);
  assert.throws(() => M.regression(state, rule.id), /尚无预设验证场景/);
});

test("规则草稿去重，缺少反例时验证失败并阻止发布", () => {
  const state = M.seed();
  const rule = M.makeDraft(state, "C-001");
  assert.equal(M.makeDraft(state, "C-001").id, rule.id);
  const validation = M.regression(state, rule.id);
  assert.equal(validation.passed, false);
  assert.equal(validation.results.find((row) => row.title === "合理例外").actual, "issue");
  assert.throws(() => M.submitReview(state, rule.id), /全部正反案例验证/);
  assert.throws(() => M.publish(state, rule.id, [true, true, true]), /提交审核/);
});

test("关联旧误报补充例外，审核三项确认后发布", () => {
  const state = M.seed();
  const rule = M.makeDraft(state, "C-001");
  M.linkLegacy(state, "L-001", rule.id);
  assert.equal(M.findRule(state, "L-001").linkedRuleId, rule.id);
  assert.equal(rule.useException, true);
  assert.equal(M.regression(state, rule.id).passed, true);
  M.submitReview(state, rule.id);
  assert.throws(() => M.publish(state, rule.id, [true, false, true]), /三项/);
  M.publish(state, rule.id, [true, true, true]);
  assert.equal(rule.version, 1);
  assert.equal(rule.enabled, true);
});

test("旧误报参考命中真实问题时保留冲突和原文证据", () => {
  const { state, rule } = publishedVoltage();
  const run = M.runTrial(state, "conflict");
  const result = run.results.find((item) => item.ruleId === rule.id);
  assert.equal(result.status, "conflict");
  assert.equal(result.baseStatus, "issue");
  assert.equal(result.references[0].id, "L-001");
  assert.match(result.evidence[0], /标准模式.*220V/);
  assert.match(result.evidence[1], /标准模式.*380V/);
});

test("例外依据当前工况，修复后的内容停止报错", () => {
  const { state, rule } = publishedVoltage();
  const resultFor = (scenario) => M.runTrial(state, scenario).results.find((item) => item.ruleId === rule.id);
  assert.equal(resultFor("exception").status, "exception");
  assert.equal(resultFor("fixed").status, "clear");
  assert.equal(resultFor("unclear").status, "review");
  assert.equal(resultFor("missing-evidence").status, "review");
  assert.equal(resultFor("wrong-scope").status, "skipped");
});

test("修改草稿使验证失效，继续执行旧发布版本", () => {
  const { state, rule } = publishedVoltage();
  M.saveDraft(state, rule.id, { useException: false, condition: "新的待审核说明" });
  assert.equal(rule.validation, null);
  assert.equal(rule.published.useException, true);
  assert.equal(rule.status, "draft");
  assert.throws(() => M.submitReview(state, rule.id), /全部正反案例/);
  const run = M.runTrial(state, "exception");
  const result = run.results.find((item) => item.ruleId === rule.id);
  assert.equal(result.version, 1);
  assert.equal(result.status, "exception");
});

test("修改范围或条件后无法沿用旧验证直接发布", () => {
  const state = M.seed();
  const rule = M.makeDraft(state, "C-001");
  M.attachException(state, "C-002", rule.id);
  M.regression(state, rule.id);
  M.submitReview(state, rule.id);
  rule.scope = "已变更为另一场景";
  assert.throws(() => M.publish(state, rule.id, [true, true, true]), /草稿已变化/);
  assert.equal(rule.enabled, false);
});

test("发布新版与停用均不改变历史快照和原始结果", () => {
  const { state, rule } = publishedVoltage();
  const first = M.runTrial(state, "conflict");
  const original = M.clone(first);
  M.saveDraft(state, rule.id, { scope: "VX120 安装手册的正文和参数表，仅限标准模式对应项。" });
  M.regression(state, rule.id);
  M.submitReview(state, rule.id);
  M.publish(state, rule.id, [true, true, true]);
  assert.equal(rule.version, 2);
  assert.equal(M.runTrial(state, "conflict").snapshots.find((item) => item.id === rule.id).version, 2);
  M.toggleRule(state, rule.id);
  assert.equal(M.runTrial(state, "conflict").snapshots.some((item) => item.id === rule.id), false);
  assert.deepEqual(first, original);
});

test("人工反馈新增候选案例，规则版本和原始结论保持独立", () => {
  const { state, rule } = publishedVoltage();
  const run = M.runTrial(state, "conflict");
  const result = run.results.find((item) => item.ruleId === rule.id);
  assert.throws(() => M.reviewResult(state, run.id, rule.id, "accepted", ""), /依据/);
  M.reviewResult(state, run.id, rule.id, "pending", "需要产品负责人再次确认对应工况。");
  assert.equal(result.feedback, null);
  const item = M.reviewResult(state, run.id, rule.id, "accepted", "两处原文均为 VX120 标准模式，历史例外不适用。");
  assert.equal(item.collected, false);
  assert.equal(item.ruleId, null);
  assert.equal(result.status, "conflict");
  assert.equal(result.feedback.caseId, item.id);
  assert.equal(rule.version, 1);
  assert.throws(() => M.reviewResult(state, run.id, rule.id, "rejected", "重复保存"), /已保存反馈/);
});

test("只有旧误报参考时不会生成或折叠参数结论", () => {
  const state = M.seed();
  const run = M.runTrial(state, "conflict");
  assert.equal(run.results.length, 1);
  assert.equal(run.results[0].ruleId, "R-001");
  assert.equal(run.legacyRefs.length, 1);
});

test("幻觉反例只能收集，不能直接变成肯定的检出规则", () => {
  const state = M.seed();
  assert.throws(() => M.makeDraft(state, "C-005"), /仅已确认/);
  M.collectCase(state, "C-005");
  assert.equal(M.findCase(state, "C-005").collected, true);
  assert.equal(state.rules.length, 2);
});
