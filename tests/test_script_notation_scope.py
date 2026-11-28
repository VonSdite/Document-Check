import json
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from app.checks.report_filters import report_filter_reason
from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.models import client as llm
from app.persistence.connection import get_db
from app.reporting.constants import REPORT_STATS_PREPARATION_VERSION
from app.reporting.excel import build_report_workbook
from app.reporting.reviews import final_review_progress, review_report
from app.reporting.rounds import build_round_result
from app.reporting.service import _prepare_task_results, _result_report_items
from app.reporting.statistics import _task_report_stat_rows_for_where
from app.tasks.runner import TaskRunner
from tests import test_document_rounds as rounds


def script_finding(**overrides):
    return (
        rounds.finding(
            "面积单位中的2未使用上标。",
            suggestion="将m2中的2设置为上标。",
            excerpt="面积 5m2",
            category="单位格式",
        )
        | overrides
    )


def content_finding():
    return rounds.finding(
        "同一工况下，正文的电压为5 V，表格为12 V，数值存在冲突。",
        suggestion="核对并统一电压值。",
    )


class ScriptNotationScopeTest(unittest.TestCase):
    def test_script_findings_are_out_of_text_review_scope(self):
        for finding in (
            script_finding(),
            script_finding(
                description="化学式中2的下标格式错误。", suggestion="改为下标。"
            ),
            script_finding(
                description="变量上下标样式不统一。", suggestion="统一样式。"
            ),
            script_finding(
                description="注释标记a位于辅助开关之后，与注释内容不对应。",
                suggestion="将上标a移至直流输入端子处。",
            ),
            script_finding(
                description="The superscript is missing.",
                suggestion="Set 2 as superscript.",
            ),
            script_finding(
                description="Use subscript for the variable index.",
                suggestion="Apply subscript formatting.",
            ),
            {"问题描述": "脚注上标位置错误。", "修改建议": "将上标a移动到名称之后。"},
        ):
            for scope in ("", "compliance", "consistency", "completeness"):
                with self.subTest(finding=finding, scope=scope):
                    self.assertEqual(
                        report_filter_reason(finding, review_scope=scope),
                        "script_notation",
                    )

    def test_equivalent_script_replacements_are_filtered_without_topic_word(self):
        for suggestion in (
            "将“m2”改为“m²”。",
            "CO2应写为CO₂。",
            "请将“x_{i}”改为“xi”。",
            '将"cm^{2}"改成"cm²"。',
            "建议将m²改为m2。",
        ):
            with self.subTest(suggestion=suggestion):
                self.assertEqual(
                    report_filter_reason(
                        script_finding(
                            description="符号书写格式不规范。",
                            suggestion=suggestion,
                        )
                    ),
                    "script_notation",
                )

    def test_content_errors_and_mentions_in_evidence_are_retained(self):
        for description, suggestion in (
            ("同一导线截面积分别写为4mm²与6mm²，数值不一致。", "核对导线截面积。"),
            ("公式中指数3错误。", "将“x³”改为“x²”。"),
            ("单位含义错误。", "将“m2”改为“cm²”。"),
            ("数组下标越界。", "将循环上界改为数组长度减1。"),
            ("The operation fails: subscript out of range.", "Check the array length."),
            ("图上标注的序号重复。", "更正序号。"),
            ("如下标注的单位错误。", "修改单位。"),
            ("按钮“上标”的名称与菜单不一致。", "统一按钮名称。"),
            ("单位存在错误。", "m2改为m²，同时核对电压。"),
        ):
            with self.subTest(description=description):
                self.assertEqual(
                    report_filter_reason(
                        script_finding(
                            description=description,
                            suggestion=suggestion,
                            excerpt="按下“上标”按钮，设置2为上标。",
                        )
                    ),
                    "",
                )

    def test_multiround_script_replacement_variants_remain_filtered_after_merging(self):
        sources = [
            rounds.completed(
                number,
                script_finding(
                    description="单位m2书写格式错误。",
                    suggestion=suggestion,
                ),
            )
            for number, suggestion in enumerate(
                ("将“m2”改为“m²”。", "请将“m2”改成“m²”。"),
                start=1,
            )
        ]
        result = {"code": "compliance", **build_round_result("compliance", 2, sources)}
        self.assertEqual(len(result["structured_report"]["items"]), 1)
        self.assertEqual(
            result["structured_report"]["items"][0]["source_rounds"], [1, 2]
        )
        prepared = _prepare_task_results([result], suppression_rules={})[0]
        self.assertEqual(prepared["report_items"], [])
        self.assertEqual(
            prepared["filtered_report_items"][0]["filter_reason"], "script_notation"
        )

    def test_text_tasks_share_filter_and_media_tasks_keep_their_review_scope(self):
        source = {
            "code": "custom",
            "result": rounds.report(script_finding(), content_finding()),
        }
        for task_type in (
            DOCUMENT_TASK_TYPE,
            CONSISTENCY_TASK_TYPE,
            LANGUAGE_CONSISTENCY_TASK_TYPE,
            IMAGE_TASK_TYPE,
            VIDEO_TASK_TYPE,
        ):
            with self.subTest(task_type=task_type):
                prepared = _prepare_task_results(
                    [source], task_type=task_type, suppression_rules={}
                )[0]
                expected_count = (
                    2 if task_type in {IMAGE_TASK_TYPE, VIDEO_TASK_TYPE} else 1
                )
                self.assertEqual(len(prepared["report_items"]), expected_count)

    def test_old_json_and_plain_reports_preserve_source_and_other_item_ids(self):
        for content in (
            rounds.report(script_finding(), content_finding()),
            "1. 问题描述：面积单位中的2未使用上标。\n修改建议：将m2中的2设置为上标。\n\n"
            "2. 问题描述：两处电压要求存在直接冲突。\n修改建议：核实电压要求。",
        ):
            source = {"code": "compliance", "result": content}
            items = _result_report_items(source)
            source["item_acceptances"] = {items[0]["id"]: {"status": "rejected"}}
            prepared = _prepare_task_results([source], suppression_rules={})[0]
            self.assertEqual(
                [item["id"] for item in prepared["report_items"]], [items[1]["id"]]
            )
            self.assertEqual(
                prepared["filtered_report_items"][0]["filter_reason"], "script_notation"
            )
            self.assertEqual(prepared["report_counts"]["rejected"], 1)
            self.assertEqual(prepared["report_counts"]["pending_review"], 1)
            self.assertEqual(source["result"], content)

    def test_saved_prompt_uses_current_execution_boundary_and_preserves_content(self):
        document = "面积5m²，E=mc^{2}，H_{2}O，数组索引i。"
        with patch.object(
            llm, "_run_payload_with_retries", return_value="检查完成"
        ) as request:
            llm.run_check(
                api_base="http://example.test/v1",
                api_key=None,
                model_name="test-model",
                check_name="规范性",
                prompt="检查上下标设置和数值冲突。",
                document_text=document,
            )
        messages = request.call_args.kwargs["payload"]["messages"]
        self.assertIn("上下标检查属于文档审查的排除范围", messages[0]["content"])
        self.assertIn("上下标检查属于文档审查的排除范围", messages[1]["content"])
        self.assertEqual(messages[1]["content"].split("待检查文档：\n", 1)[1], document)


class ScriptNotationExecutionTest(unittest.TestCase):
    setUp = rounds.DocumentRoundExecutionTest.setUp
    tearDown = rounds.DocumentRoundExecutionTest.tearDown
    create_task = rounds.DocumentRoundExecutionTest.create_task
    task = rounds.DocumentRoundExecutionTest.task

    def test_single_multi_and_custom_checks_keep_raw_items_and_filter_main_reports(
        self,
    ):
        raw = rounds.report(script_finding(), content_finding())
        for planned, code in ((1, "compliance"), (2, "compliance"), (1, "pool-999")):
            with self.subTest(planned=planned, code=code):
                task_id = self.create_task(codes=(code,), rounds=planned)
                with patch("app.tasks.runner.run_check", return_value=raw):
                    TaskRunner(self.app).run(task_id)
                task = self.task(task_id)
                self.assertEqual(task["status"], "completed", task["error"])
                result = json.loads(task["result_json"])[0]
                self.assertEqual(len(json.loads(result["result"])["items"]), 2)
                self.assertEqual(
                    result["report_filter"]["items"],
                    [{"index": 1, "reason": "script_notation"}],
                )
                for entry in result.get("round_results", []):
                    self.assertEqual(entry["result"], raw)
                    self.assertEqual(entry["report"]["items"], json.loads(raw)["items"])
                    self.assertEqual(
                        entry["report"]["report_filter"]["items"],
                        [{"index": 1, "reason": "script_notation"}],
                    )
                self.assertEqual(
                    _task_report_stat_rows_for_where("t.id = ?", (task_id,))[0][
                        "issue"
                    ],
                    1,
                )
                self.assertEqual(final_review_progress([task_id])[task_id]["total"], 1)
                workbook = load_workbook(build_report_workbook(task))
                self.addCleanup(workbook.close)
                self.assertEqual(workbook["报告条目"].max_row, 2)

    def test_existing_report_and_review_caches_refresh_with_source_preserved(self):
        task_id = self.create_task(rounds=1)
        source = json.dumps(
            [
                {
                    "code": "compliance",
                    "result": rounds.report(script_finding(), content_finding()),
                }
            ]
        )
        db = get_db()
        db.execute(
            "UPDATE tasks SET result_json=?, status='completed' WHERE id=?",
            (source, task_id),
        )
        db.execute(
            "INSERT INTO task_report_stats "
            "(task_id, source_updated_at, suppression_version, issue_count, updated_at) "
            "VALUES (?, ?, '10-no-impact|0:0:', 2, 'now')",
            (task_id, self.task(task_id)["updated_at"]),
        )
        db.execute(
            "INSERT INTO task_final_review_stats "
            "(task_id, total, initial_count, confirmed, returned, deferred, pending, status, preparation_version) "
            "VALUES (?, 2, 0, 0, 0, 0, 0, 'not_required', '10-no-impact')",
            (task_id,),
        )
        db.commit()
        self.assertEqual(final_review_progress([task_id], refresh=False), {})
        self.assertEqual(final_review_progress([task_id])[task_id]["total"], 1)
        stats = _task_report_stat_rows_for_where("t.id = ?", (task_id,))[0]
        self.assertEqual(stats["issue"], 1)
        self.assertTrue(
            stats["suppression_version"].startswith(
                REPORT_STATS_PREPARATION_VERSION + "|"
            )
        )
        review = review_report(self.task(task_id))
        self.assertEqual(len(review["results"][0]["report_items"]), 1)
        self.assertEqual(self.task(task_id)["result_json"], source)


if __name__ == "__main__":
    unittest.main()
