import json
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from app.checks.guardrails import sanitize_text_check_result
from app.checks.report_filters import (
    REPORT_FILTER_VERSION,
    report_filter_reason,
    review_filter_scope,
)
from app.contracts.task_types import DOCUMENT_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE
from app.persistence.connection import get_db
from app.persistence.schema import init_db
from app.reporting.constants import REPORT_STATS_PREPARATION_VERSION
from app.reporting.excel import build_report_workbook
from app.reporting.reviews import final_review_progress, review_report
from app.reporting.rounds import build_round_result
from app.reporting.service import _prepare_task_results, _result_report_items
from app.reporting.statistics import _task_report_stat_rows_for_where
from app.tasks.runner import TaskRunner
from tests import test_document_rounds as rounds


def item(
    description="未说明各范围对应的工况。", suggestion="补充适用工况说明。", **kwargs
):
    return rounds.finding(description, suggestion=suggestion, **kwargs)


def actionable():
    return item(
        "同一端口在同一工况下，一处允许220 V，另一处禁止220 V，两处直接冲突。",
        "核实并更正冲突的电压要求。",
        category="参数或数据不一致",
        excerpt="端口允许220 V；端口禁止220 V",
    )


def no_impact_finding(**overrides):
    return (
        item(
            "表3-1中引用“如表3-3”指向电源输入模块，实际表3-3标题为“电源输入模块”，"
            "引用正确。表3-1中输出模块行引用“如表3-4”，实际表3-4标题为“电源输出模块”，"
            "引用正确。未发现明显错误。",
            "",
            impact="无影响。",
        )
        | overrides
    )


class ReportFilterRuleTest(unittest.TestCase):
    def test_no_impact_and_empty_suggestions_hide_mislabeled_long_findings(self):
        for impact in (
            "无影响",
            "无影响。",
            "无实质影响。",
            "没有实际影响",
            "无",
            "No impact.",
        ):
            for suggestion in (
                "",
                None,
                " \n ",
                "无",
                "无建议。",
                "—",
                "N/A",
                "None",
                "无需修改。",
            ):
                with self.subTest(impact=impact, suggestion=suggestion):
                    case = no_impact_finding(impact=impact, suggestion=suggestion)
                    self.assertEqual(report_filter_reason(case), "no_action")
        for status in ("issue", "suggestion", "non_issue"):
            self.assertEqual(
                report_filter_reason(no_impact_finding(status=status)), "no_action"
            )
        self.assertEqual(
            report_filter_reason(
                {
                    "状态": "问题",
                    "问题描述": no_impact_finding()["description"],
                    "影响说明": "无影响。",
                    "修改建议": "",
                }
            ),
            "no_action",
        )

    def test_uncertain_partial_and_quoted_impact_statements_are_retained(self):
        for impact in (
            "",
            None,
            "影响较小。",
            "基本无影响。",
            "可能无影响。",
            "暂时无影响。",
            "无影响？",
            "None",
            "N/A",
            "对显示无影响，但影响设备运行。",
            "无影响，但存在误接风险。",
            "若两处指向同一设备则无影响。",
            "原文称“无影响”。",
        ):
            with self.subTest(impact=impact):
                self.assertEqual(
                    report_filter_reason(no_impact_finding(impact=impact)), ""
                )

    def test_no_impact_does_not_override_defects_or_review_requests_in_description(
        self,
    ):
        for description in (
            "阈值误写为阀值。",
            "公司名称写成华未。",
            "两处设备型号不一致。",
            "同一工况下，两处电压要求存在直接冲突。",
            "单位应补上。",
            "缺少电压单位。",
            "未说明异常处理步骤。",
            "需核查适用范围。",
            "该连接方式需要重新评估。",
            "还存在其他待核对事项。",
            "当前引用正确，但另一处引用错误。",
            "参数5V应为12V。",
            "如果两处含义一致，则无需修改。",
            "不能认为没有问题。",
            "The voltage unit is wrong.",
            "Please verify the wiring.",
            "引用指向图3-1，却在目录标为图3-2。",
            "参数单位应标注。",
            "正文将电压标成电流。",
            "同一端口一处为5V，另一处为12V。",
            "无法判断该术语是否准确。",
            "假如参数一致即可保留。",
            "统一设备型号。",
            "修改参数值。",
            "补单位。",
            "Add units to the voltage.",
            "并非无问题。",
            "不是无需修改。",
            "两处是否指向同一设备？",
        ):
            with self.subTest(description=description):
                self.assertEqual(
                    report_filter_reason(no_impact_finding(description=description)), ""
                )
                for mixed in (
                    no_impact_finding()["description"] + description,
                    description + no_impact_finding()["description"],
                ):
                    self.assertEqual(
                        report_filter_reason(no_impact_finding(description=mixed)), ""
                    )
        self.assertEqual(
            report_filter_reason(no_impact_finding(excerpt="TODO：核实接线")), ""
        )
        self.assertEqual(
            report_filter_reason(
                no_impact_finding(description="修改记录与正文的标题一致。")
            ),
            "no_action",
        )

    def test_no_impact_field_aliases_and_merged_values_use_the_same_rule(self):
        for description_key, impact_key, suggestion_key in (
            ("description", "risk", "recommendation"),
            ("差异说明", "客户影响", "建议处理方式"),
            ("冲突或缺失说明", "可能影响", "需核对的依据"),
            ("偏差说明", "影响", "建议修改"),
        ):
            for suggestion, expected in (("无", "no_action"), ("核对图号。", "")):
                with self.subTest(
                    fields=(description_key, impact_key, suggestion_key),
                    suggestion=suggestion,
                ):
                    case = {
                        description_key: no_impact_finding()["description"],
                        impact_key: "无影响。\n没有实际影响。",
                        suggestion_key: suggestion,
                        "status": "issue",
                    }
                    content, count = sanitize_text_check_result(rounds.report(case))
                    self.assertEqual(count, int(bool(expected)))
                    self.assertEqual(json.loads(content)["items"], [case])
                    normalized = _result_report_items({"result": content})[0]
                    self.assertEqual(report_filter_reason(normalized), expected)
        self.assertEqual(
            report_filter_reason(no_impact_finding(suggestion="无。\n无需修改。")),
            "no_action",
        )

    def test_no_impact_with_an_actual_suggestion_is_retained(self):
        for suggestion in (
            "统一图号。",
            "核实引用目标。",
            "保持原文，但需要补充单位。",
            "N/A；请核对电压。",
            "Add units to the voltage.",
            "无需修改？",
        ):
            with self.subTest(suggestion=suggestion):
                self.assertEqual(
                    report_filter_reason(no_impact_finding(suggestion=suggestion)), ""
                )

    def test_no_impact_filter_preserves_source_and_ids_in_json_and_plain_text(self):
        for result in (
            {
                "code": "consistency",
                "result": rounds.report(no_impact_finding(), actionable()),
            },
            {
                "code": "consistency",
                "result": (
                    "1. 问题描述："
                    + no_impact_finding()["description"]
                    + "\n影响：无影响。\n修改建议：\n\n"
                    "2. 问题描述：同一端口两处电压要求存在直接冲突。\n"
                    "影响：可能错误接线。\n修改建议：核实电压要求。"
                ),
            },
        ):
            original = result["result"]
            items = _result_report_items(result)
            result["item_acceptances"] = {items[0]["id"]: {"status": "accepted"}}
            prepared = _prepare_task_results([result], suppression_rules={})[0]
            self.assertEqual(
                [entry["id"] for entry in prepared["report_items"]], [items[1]["id"]]
            )
            self.assertEqual(
                prepared["filtered_report_items"][0]["filter_reason"], "no_action"
            )
            self.assertEqual(
                prepared["result_summary"], "检查完成，保留 1 条明确问题。"
            )
            self.assertEqual(prepared["report_counts"]["accepted"], 1)
            self.assertEqual(prepared["report_counts"]["pending_review"], 1)
            self.assertEqual(result["result"], original)

    def test_supplement_only_cases_are_filtered_in_both_review_scopes(self):
        cases = [
            item(),
            item("输入电压三处不同，未说明工况。"),
            item("输入电压三处不一致，缺乏适用条件解释。"),
            item("给出了三个范围，但各范围对应关系不明确。"),
            item("操作步骤过于笼统，缺少具体操作说明。", "细化操作步骤。"),
            item("操作流程不够详细。", "展开操作说明。"),
            item("未说明直流柜并柜时如何操作。", "补充并柜操作步骤。"),
            item("未说明设备启动时如何配置。", "补充具体操作步骤。"),
            item("参数说明未解释选择依据。"),
            item("参数说明未明确适用条件。"),
            item("补充适用范围。", "补充温度范围。"),
            item("缺少环境温度范围。", "补充环境温度范围。"),
            item("引用《用户手册》未提供版本号及获取方式。", "补充版本号和下载地址。"),
            item("未注明手册的下载地址或获取入口。", "提供手册获取方式。"),
            item("请补充适用范围说明。"),
            item(
                "接口使用说明不完整。",
                "补充具体操作步骤及示例。",
                category="内容不完整",
            ),
        ]
        for case in cases:
            for scope in ("compliance", "consistency"):
                with self.subTest(description=case["description"], scope=scope):
                    self.assertEqual(
                        report_filter_reason(case, review_scope=scope),
                        "supplement_only",
                    )
            for scope in ("", "completeness", "understandability", "custom"):
                self.assertEqual(report_filter_reason(case, review_scope=scope), "")

    def test_concrete_errors_and_mixed_findings_are_retained(self):
        for case in (
            actionable(),
            item("未说明工况，且将阈值误写为阀值。", "补充工况并更正错别字。"),
            item("未说明工况，接线方式存在错误。", "核对接线方式。"),
            item("未说明工况。", "补充工况，并纠正计算错误。"),
            item("未说明工况，5 V应改为12 V。", "核对数值。"),
            item("缺少电压单位，未说明工况。", "补齐单位和工况说明。"),
            item("未说明工况，图4-3错指图4-2。", "修正引用并补充说明。"),
            item("同一条件下，两处取值分别为220 V和110 V，未说明选择依据。"),
            item(
                "表中输入电压1500 V与正文1600 V存在直接冲突。",
                "统一电压或补充工况说明。",
            ),
            item("术语表中存在名称误写。", "补充背景说明。", category="内容不完整"),
            item("手册版本未说明。", "将型号A更正为型号B，并补充手册版本。"),
            item(
                "说明不完整。",
                "补充示例。",
                category="内容完整性",
                excerpt="TODO：此处插入示例",
            ),
            item("图4-42不存在。", "核对引用编号。"),
            item("文档说明适用版本为V1，示例却使用V2的接口。", "核对接口名称。"),
            item("A明确适用范围仅包括型号甲，B要求型号乙也执行。", "核对型号要求。"),
            item(
                "未说明工况，且同一设备的产品型号在第2章为ABC-100，在第3章为ABC-200，型号不一致。",
                "核对并统一设备型号，同时补充工况说明。",
            ),
            item(
                "未说明适用范围，正文把公司名称写成华未。",
                "补充适用范围，并将华未替换成华为。",
            ),
            item("未说明工况。", "补充工况和单位。"),
            item("补充适用范围。", "补充温度范围，并需补单位。"),
            item("温度范围为0至50度，额定温度为60度，存在矛盾。", "补充温度范围。"),
            item("当前表述无需修改，但仍需添加单位。", ""),
            item("无需修改数值，但需补单位。", ""),
            item("未说明工况。", "改用另一种接线方案，并补充工况。"),
        ):
            with self.subTest(description=case["description"]):
                self.assertEqual(
                    report_filter_reason(case, review_scope="consistency"), ""
                )

    def test_no_action_does_not_require_impact_field_or_model_status(self):
        cases = (
            item("此处使用正常简称。", "无需修改。", impact=""),
            item("两处含义一致，不构成问题。", "", status="suggestion"),
            item("当前表述无须调整。", ""),
            item("检查完成，未发现明显问题。", "保持原文。"),
            item("未发现错别字或单位标注错误。", "无需修改。"),
            item("该词不是错别字，属于正常缩写。", "无需修改。"),
            item("两处规定不存在冲突。", ""),
            item("These descriptions are equivalent.", "No changes required."),
            {"状态": "问题", "问题描述": "该表述可接受。", "修改建议": "不需要修改。"},
            item("格式符合要求。", "", status="non_issue"),
            item("该术语结合上下文可以接受。", "无需修改。"),
            item("该交叉引用适用于当前章节。", "无需修改。"),
            item("该项不是问题。", "无需修改。"),
            item("明确非问题。", "", status="non_issue"),
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(report_filter_reason(case), "no_action")

    def test_no_action_negations_conditions_quotes_and_partial_actions_are_retained(
        self,
    ):
        for case in (
            item("不能认为无需修改。", "核对要求。"),
            item("并非无问题，仍需更正。", "核实参数。"),
            item("两处适用范围待核实。", "若确认为同一工况，则无需修改。"),
            item("单位需要核对。", "无需修改电压值，但需要补充单位。"),
            item("无需修改电压值，但必须补充单位。", ""),
            item("当前说明无需修改，但表8-8存在错别字。", "更正错字。"),
            item("两处参数不一致。", "核实参数。", excerpt="界面提示：无需修改"),
            item("原文“无需修改”与执行要求不一致。", "核实指令。"),
            item("未发现图4-42，引用缺失。", "核对图号。"),
            item("没有异常处理步骤。", ""),
            item("没有错误码的说明。", ""),
            item("影响较小，但应统一标点。", "更正标点。"),
            item("无需修改数值，但需补单位。", "", status="non_issue"),
            item("无需修改。", "Add units to the input voltage.", status="non_issue"),
            item("型号前后不一致。", "统一设备型号。", status="non_issue"),
            item("检查完成。", "", status="non_issue"),
            item("需核查适用范围。", "无需修改。"),
            item("该术语结合上下文可以接受，但需补单位。", "无需修改。"),
            item("该项不是问题。", "仍需添加单位。"),
        ):
            with self.subTest(case=case):
                self.assertEqual(report_filter_reason(case), "")

    def test_scope_uses_template_and_compatible_historical_names(self):
        for check, expected in (
            ({"code": "consistency"}, "consistency"),
            (
                {"code": "pool-31", "name": "软件文档方案 · 全文一致性检查"},
                "consistency",
            ),
            (
                {"code": "pool-1", "name": "交付审查", "template_code": "compliance"},
                "compliance",
            ),
            (
                {
                    "code": "pool-1",
                    "name": "规范性检查",
                    "template_code": "completeness",
                },
                "",
            ),
            ({"name": "内容完整性检查"}, ""),
            ({"code": "custom", "name": "自定义检查"}, ""),
        ):
            self.assertEqual(review_filter_scope(check), expected)

    def test_json_aliases_preserve_items_and_record_filter_reasons(self):
        for key in ("items", "issues", "report_items", "findings", "problems"):
            payload = {
                "summary": "发现3条问题。",
                key: [item(), item("表述可接受。", "无需修改。"), actionable()],
            }
            content, removed = sanitize_text_check_result(
                "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```",
                review_scope="consistency",
            )
            self.assertEqual(removed, 2)
            saved = json.loads(content)
            self.assertEqual(saved[key], payload[key])
            self.assertEqual(saved["summary"], payload["summary"])
            self.assertEqual(
                saved["report_filter"],
                {
                    "version": REPORT_FILTER_VERSION,
                    "scope": "consistency",
                    "items": [
                        {"index": 1, "reason": "supplement_only"},
                        {"index": 2, "reason": "no_action"},
                    ],
                },
            )
            prepared = _prepare_task_results(
                [{"code": "consistency", "result": content}],
                task_type=DOCUMENT_TASK_TYPE,
                suppression_rules={},
            )[0]
            self.assertEqual(len(prepared["report_items"]), 1)
            self.assertEqual(
                prepared["result_summary"], "检查完成，保留 1 条明确问题。"
            )
            self.assertEqual(
                [entry["filter_reason"] for entry in prepared["filtered_report_items"]],
                ["supplement_only", "no_action"],
            )
        content, removed = sanitize_text_check_result(
            rounds.report(item()), review_scope="compliance"
        )
        self.assertEqual(removed, 1)
        self.assertEqual(json.loads(content)["items"], [item()])

    def test_unknown_claims_in_any_position_keep_the_whole_finding(self):
        for unknown in (
            "还存在其他待核对事项",
            "该连接方式需要重新评估",
            "单位应补上",
            "公司名称写成华未",
        ):
            for separator in ("，", "；", "。", "\n", "，同时"):
                for description in (
                    f"未说明工况{separator}{unknown}",
                    f"{unknown}{separator}未说明工况",
                    f"无需修改{separator}{unknown}",
                ):
                    with self.subTest(description=description):
                        self.assertEqual(
                            report_filter_reason(
                                item(description, ""), review_scope="consistency"
                            ),
                            "",
                        )

    def test_quoted_conclusions_do_not_hide_the_surrounding_claim(self):
        for description in (
            "原文写作“无需修改，保持原文”，需要核对指令。",
            "未说明工况，且“公司名称写成华未”需要处理。",
            "正文写明“未说明工况”，无法判断其含义。",
        ):
            self.assertEqual(
                report_filter_reason(item(description, ""), review_scope="compliance"),
                "",
            )

    def test_non_issue_labels_do_not_override_actions_in_json_or_plain_text(self):
        for result in (
            {
                "code": "consistency",
                "result": rounds.report(
                    item("无需修改数值，但需补单位。", "", status="non_issue")
                ),
            },
            {"code": "consistency", "result": "1. 非问题：无需修改数值，但需补单位。"},
        ):
            prepared = _prepare_task_results(
                [result], task_type=DOCUMENT_TASK_TYPE, suppression_rules={}
            )[0]
            self.assertEqual(len(prepared["report_items"]), 1)
            self.assertIn(prepared["report_items"][0]["type"], {"issue", "suggestion"})

    def test_saved_filter_decision_is_recomputed_without_rewriting_source(self):
        content, _ = sanitize_text_check_result(
            rounds.report(item(), actionable()), review_scope="compliance"
        )
        result = {"code": "compliance", "result": content}
        kept_id = _result_report_items(result)[1]["id"]
        self.assertEqual(
            len(
                _prepare_task_results([result], suppression_rules={})[0]["report_items"]
            ),
            1,
        )
        with patch("app.checks.report_filters._supplement_claim", return_value=""):
            prepared = _prepare_task_results([result], suppression_rules={})[0]
        self.assertEqual(len(prepared["report_items"]), 2)
        self.assertIn(kept_id, [entry["id"] for entry in prepared["report_items"]])
        self.assertEqual(result["result"], content)
        self.assertEqual(
            json.loads(content)["report_filter"]["items"][0]["reason"],
            "supplement_only",
        )

    def test_multiround_retained_nonissues_keep_distinct_ids_from_actionable_items(
        self,
    ):
        positive = item("当前表述正确。", "无需修改。", status="non_issue")
        mixed = item("当前表述正确。", "需要补单位。", status="non_issue")
        result = {
            "code": "consistency",
            **build_round_result(
                "consistency",
                2,
                [
                    rounds.completed(1, positive, mixed),
                    rounds.completed(2, positive, mixed),
                ],
            ),
        }
        findings = result["structured_report"]["items"]
        self.assertEqual(len(findings), 2)
        self.assertEqual(len({finding["id"] for finding in findings}), 2)
        self.assertTrue(all(finding["source_rounds"] == [1, 2] for finding in findings))
        prepared = _prepare_task_results(
            [result], task_type=DOCUMENT_TASK_TYPE, suppression_rules={}
        )[0]
        self.assertEqual(len(prepared["report_items"]), 1)
        self.assertEqual(prepared["report_items"][0]["suggestion"], mixed["suggestion"])
        with patch("app.reporting.service.report_filter_reason", return_value=""):
            restored = _prepare_task_results(
                [result], task_type=DOCUMENT_TASK_TYPE, suppression_rules={}
            )[0]
        self.assertEqual(len(restored["report_items"]), 2)

    def test_historical_json_and_legacy_text_use_same_filter_without_changing_ids(self):
        result = {"code": "compliance", "result": rounds.report(item(), actionable())}
        kept_id = _result_report_items(result)[1]["id"]
        prepared = _prepare_task_results(
            [result], task_type=DOCUMENT_TASK_TYPE, suppression_rules={}
        )[0]
        self.assertEqual([entry["id"] for entry in prepared["report_items"]], [kept_id])
        self.assertEqual(prepared["result_summary"], "检查完成，保留 1 条明确问题。")
        legacy = {
            "code": "compliance",
            "result": "1. 问题描述：步骤过于笼统。\n修改建议：细化步骤。\n\n"
            "2. 问题描述：原文可接受。\n修改建议：无需修改。\n\n"
            "3. 问题描述：未发现图4-42，引用缺失。\n修改建议：核对图号。",
            "cross_reference_guard": 1,
        }
        prepared = _prepare_task_results(
            [legacy], task_type=DOCUMENT_TASK_TYPE, suppression_rules={}
        )[0]
        self.assertEqual(len(prepared["report_items"]), 1)
        self.assertIn("未发现图4-42", prepared["report_items"][0]["description"])

    def test_other_task_types_retain_completeness_claims(self):
        results = [
            {
                "code": "consistency",
                "result": rounds.report(item(), item("两处等价。", "无需修改。")),
            }
        ]
        prepared = _prepare_task_results(
            results, task_type=LANGUAGE_CONSISTENCY_TASK_TYPE, suppression_rules={}
        )[0]
        self.assertEqual(len(prepared["report_items"]), 1)
        self.assertEqual(
            prepared["report_items"][0]["description"], item()["description"]
        )


class ReportFilterExecutionTest(unittest.TestCase):
    setUp = rounds.DocumentRoundExecutionTest.setUp
    tearDown = rounds.DocumentRoundExecutionTest.tearDown
    create_task = rounds.DocumentRoundExecutionTest.create_task
    task = rounds.DocumentRoundExecutionTest.task
    retry = rounds.DocumentRoundExecutionTest.retry

    def test_mixed_findings_survive_execution_reviews_statistics_and_export(self):
        mixed = item(
            "未说明工况，同一设备型号在两处分别写作ABC-100和ABC-200，型号不一致。",
            "核对并统一设备型号，同时补充工况说明。",
            excerpt="设备型号ABC-100；设备型号ABC-200",
        )
        mislabeled = item("无需修改数值，但需补单位。", "", status="non_issue")
        raw = rounds.report(item(), item("表述正确。", "无需修改。"), mixed, mislabeled)
        for planned in (1, 2):
            with self.subTest(planned=planned):
                task_id = self.create_task(codes=("consistency",), rounds=planned)
                with patch("app.tasks.runner.run_check", return_value=raw):
                    TaskRunner(self.app).run(task_id)
                task = self.task(task_id)
                self.assertEqual(task["status"], "completed", task["error"])
                result = json.loads(task["result_json"])[0]
                self.assertEqual(len(json.loads(result["result"])["items"]), 4)
                prepared = _prepare_task_results(
                    [result], task_type=DOCUMENT_TASK_TYPE, suppression_rules={}
                )[0]
                self.assertEqual(
                    {entry["description"] for entry in prepared["report_items"]},
                    {mixed["description"], mislabeled["description"]},
                )
                self.assertTrue(
                    all(
                        entry["type"] in {"issue", "suggestion"}
                        for entry in prepared["report_items"]
                    )
                )
                db = get_db()
                db.execute(
                    "INSERT OR REPLACE INTO task_report_stats "
                    "(task_id, source_updated_at, suppression_version, issue_count, updated_at) "
                    "VALUES (?, ?, '7-report-filters|0:0:', 0, 'now')",
                    (task_id, task["updated_at"]),
                )
                db.execute(
                    "INSERT OR REPLACE INTO task_final_review_stats "
                    "(task_id, total, initial_count, confirmed, returned, deferred, pending, status, preparation_version) "
                    "VALUES (?, 0, 0, 0, 0, 0, 0, 'empty', '7-report-filters')",
                    (task_id,),
                )
                db.commit()
                stats = _task_report_stat_rows_for_where("t.id = ?", (task_id,))[0]
                self.assertEqual(stats["issue"] + stats["suggestion"], 2)
                self.assertEqual(final_review_progress([task_id])[task_id]["total"], 2)
                review = review_report(task)
                self.assertEqual(len(review["results"][0]["report_items"]), 2)
                workbook = load_workbook(build_report_workbook(task))
                self.addCleanup(workbook.close)
                self.assertEqual(workbook["报告条目"].max_row, 3)
                self.assertEqual(self.task(task_id)["result_json"], task["result_json"])

    def test_single_multi_and_renamed_pool_checks_preserve_filtered_sources(self):
        for planned, code in ((1, "compliance"), (2, "compliance"), (1, "pool-999")):
            with self.subTest(planned=planned, code=code):
                task_id = self.create_task(codes=(code,), rounds=planned)
                if code.startswith("pool-"):
                    snapshot = json.loads(self.task(task_id)["checks_snapshot_json"])
                    snapshot[0].update(template_code="consistency", name="参数审查")
                    get_db().execute(
                        "UPDATE tasks SET checks_snapshot_json=? WHERE id=?",
                        (json.dumps(snapshot), task_id),
                    )
                    get_db().commit()
                raw = rounds.report(
                    item(),
                    item("表述符合要求。", "", status="non_issue"),
                    no_impact_finding(),
                    actionable(),
                )
                with patch("app.tasks.runner.run_check", return_value=raw):
                    TaskRunner(self.app).run(task_id)
                task = self.task(task_id)
                self.assertEqual(task["status"], "completed", task["error"])
                result = json.loads(task["result_json"])[0]
                self.assertEqual(len(json.loads(result["result"])["items"]), 4)
                self.assertEqual(len(result["report_filter"]["items"]), 3)
                self.assertEqual(
                    result["template_code"],
                    "consistency" if code.startswith("pool-") else "compliance",
                )
                if planned > 1:
                    for entry in result["round_results"]:
                        self.assertEqual(entry["result"], raw)
                        self.assertEqual(
                            entry["report"]["items"], json.loads(raw)["items"]
                        )
                        self.assertEqual(
                            len(entry["report"]["report_filter"]["items"]), 3
                        )
                stats = _task_report_stat_rows_for_where("t.id = ?", (task_id,))[0]
                self.assertEqual(stats["issue"], 1)
                workbook = load_workbook(build_report_workbook(task))
                self.addCleanup(workbook.close)
                self.assertEqual(workbook["报告条目"].max_row, 2)

    def test_completeness_check_retains_omissions(self):
        task_id = self.create_task(codes=("completeness",), rounds=1)
        with patch(
            "app.tasks.runner.run_check",
            return_value=rounds.report(item(), item("表述符合要求。", "无需修改。")),
        ):
            TaskRunner(self.app).run(task_id)
        result = json.loads(self.task(task_id)["result_json"])[0]
        self.assertEqual(len(json.loads(result["result"])["items"]), 2)
        prepared = _prepare_task_results(
            [result], task_type=DOCUMENT_TASK_TYPE, suppression_rules={}
        )[0]
        self.assertEqual(len(prepared["report_items"]), 1)
        self.assertEqual(
            prepared["report_items"][0]["description"], item()["description"]
        )

    def test_historical_caches_reviews_and_export_refresh_without_rewriting_report(
        self,
    ):
        task_id = self.create_task(rounds=1)
        result = {
            "code": "compliance",
            "result": rounds.report(
                item(),
                item("表述可接受。", "无需修改。"),
                no_impact_finding(),
                actionable(),
            ),
        }
        source_json = json.dumps([result], ensure_ascii=False)
        db = get_db()
        db.execute(
            "UPDATE tasks SET result_json=?, status='completed' WHERE id=?",
            (source_json, task_id),
        )
        db.execute(
            "INSERT INTO task_report_stats "
            "(task_id, source_updated_at, suppression_version, issue_count, updated_at) "
            "VALUES (?, ?, '9-unique-report-item-ids|0:0:', 4, 'now')",
            (task_id, self.task(task_id)["updated_at"]),
        )
        db.execute(
            "INSERT INTO task_final_review_stats "
            "(task_id, total, initial_count, confirmed, returned, deferred, pending, status) "
            "VALUES (?, 4, 0, 0, 0, 0, 0, 'not_required')",
            (task_id,),
        )
        db.execute(
            "ALTER TABLE task_final_review_stats DROP COLUMN preparation_version"
        )
        db.commit()
        init_db()
        self.assertEqual(final_review_progress([task_id], refresh=False), {})
        self.assertEqual(final_review_progress([task_id])[task_id]["total"], 1)
        stats = _task_report_stat_rows_for_where("t.id = ?", (task_id,))[0]
        self.assertEqual(stats["issue"], 1)
        self.assertTrue(
            stats["suppression_version"].startswith(
                REPORT_STATS_PREPARATION_VERSION + "|"
            )
        )
        task = self.task(task_id)
        review = review_report(task)
        self.assertEqual(len(review["results"][0]["report_items"]), 1)
        self.assertEqual(
            review["results"][0]["report_items"][0]["description"],
            actionable()["description"],
        )
        workbook = load_workbook(build_report_workbook(task))
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["报告条目"].max_row, 2)
        self.assertEqual(self.task(task_id)["result_json"], source_json)

    def test_other_task_review_cache_keeps_supplement_claims(self):
        task_id = self.create_task(rounds=1)
        db = get_db()
        db.execute(
            "UPDATE tasks SET task_type=?, result_json=?, status='completed' WHERE id=?",
            (
                LANGUAGE_CONSISTENCY_TASK_TYPE,
                json.dumps([{"code": "consistency", "result": rounds.report(item())}]),
                task_id,
            ),
        )
        db.commit()
        self.assertEqual(final_review_progress([task_id])[task_id]["total"], 1)

    def test_previous_filter_version_refreshes_final_review_cache(self):
        task_id = self.create_task(rounds=1)
        result = {
            "code": "consistency",
            "result": rounds.report(no_impact_finding(), actionable()),
        }
        db = get_db()
        db.execute(
            "UPDATE tasks SET result_json=?, status='completed' WHERE id=?",
            (json.dumps([result]), task_id),
        )
        db.execute(
            "INSERT INTO task_final_review_stats "
            "(task_id, total, initial_count, confirmed, returned, deferred, pending, "
            "status, preparation_version) "
            "VALUES (?, 2, 0, 0, 0, 0, 0, 'not_required', '9-unique-report-item-ids')",
            (task_id,),
        )
        db.commit()
        self.assertEqual(final_review_progress([task_id], refresh=False), {})
        self.assertEqual(final_review_progress([task_id])[task_id]["total"], 1)
        saved = db.execute(
            "SELECT preparation_version FROM task_final_review_stats WHERE task_id=?",
            (task_id,),
        ).fetchone()
        self.assertEqual(saved["preparation_version"], REPORT_STATS_PREPARATION_VERSION)

    def test_retry_filters_retained_rounds_and_preserves_reviewed_finding_identity(
        self,
    ):
        task_id = self.create_task(rounds=2)
        previous = rounds.completed(1, item(), actionable())
        result = {
            "code": "compliance",
            "name": "compliance",
            **build_round_result("compliance", 2, [previous]),
        }
        kept_id = next(
            entry["id"]
            for entry in result["structured_report"]["items"]
            if entry["description"] == actionable()["description"]
        )
        result["item_acceptances"] = {kept_id: {"status": "accepted"}}
        get_db().execute(
            "UPDATE tasks SET result_json=? WHERE id=?", (json.dumps([result]), task_id)
        )
        get_db().commit()
        self.retry(task_id)
        with patch(
            "app.tasks.runner.run_check", return_value=rounds.report(item())
        ) as model:
            TaskRunner(self.app).run(task_id)
        model.assert_called_once()
        result = json.loads(self.task(task_id)["result_json"])[0]
        self.assertEqual(result["round_results"][0]["result"], previous["result"])
        self.assertEqual(
            result["round_results"][0]["report"]["items"], [item(), actionable()]
        )
        self.assertEqual(
            next(
                entry["id"]
                for entry in result["structured_report"]["items"]
                if entry["description"] == actionable()["description"]
            ),
            kept_id,
        )
        prepared = _prepare_task_results(
            [result], task_type=DOCUMENT_TASK_TYPE, suppression_rules={}
        )[0]
        self.assertEqual(prepared["report_items"][0]["acceptance_status"], "accepted")


if __name__ == "__main__":
    unittest.main()
