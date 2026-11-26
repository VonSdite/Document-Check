import json
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from app.checks.cross_references import (
    build_cross_reference_index,
    is_disproved_missing_reference,
)
from app.checks.document_scope import document_check_scope
from app.checks.guardrails import sanitize_text_check_result
from app.contracts.task_types import DOCUMENT_TASK_TYPE
from app.persistence.connection import get_db
from app.reporting.excel import build_report_workbook
from app.reporting.rounds import build_round_result
from app.reporting.service import _prepare_task_results
from app.tasks.runner import TaskRunner
from tests import test_document_rounds as rounds


def missing(description="图4-42在正文中不存在。", **kwargs):
    return rounds.finding(
        description,
        category="编号或交叉引用错误",
        location="4.2 安装",
        excerpt="安装方法见图4-42。",
        suggestion="核对所引用的图表编号。",
        **kwargs,
    )


def source_text():
    return (
        "file: manual.pdf\n[第1页]\n前言\n图4-40 模板示例\n"
        "[第2页]\n目录\n第一章 安装\t1\n图4-41 安装流程\t3\n"
        "[第3页]\n第一章 安装\n安装时应确认电源已断开。\n"
        "安装方法见图4-42，另见图4-41。\n"
        "图 4 – 42 机柜安装方法\n表4-42 工具规格\n"
        "图4-43 接线示意\n"
        "[第4页]\nChange History\nIssue 01 (2026-01-01)\n图4-44 历史示例\n"
    )


class CrossReferenceRuleTest(unittest.TestCase):
    def setUp(self):
        self.index = build_cross_reference_index(
            document_check_scope(source_text()).ai_text
        )

    def test_scope_excludes_front_matter_contents_and_revision_history(self):
        self.assertEqual(
            self.index,
            frozenset({("figure", "4-42"), ("figure", "4-43"), ("table", "4-42")}),
        )
        for number in ("4-40", "4-41", "4-44"):
            with self.subTest(number=number):
                self.assertFalse(
                    is_disproved_missing_reference(
                        missing(f"图{number}不存在。"), self.index
                    )
                )

    def test_normalizes_spacing_line_breaks_width_case_and_dashes(self):
        for caption in (
            "图4-42 机柜安装",
            "图 4 - 42 机柜安装",
            "图４－４２ 机柜安装",
            "图\n4‑42 机柜安装",
            "图\r\n4‑42 机柜安装",
            "图4-\n42 机柜安装",
            "图4-42\n机柜安装",
            "图4−42 机柜安装",
            "**图 4—42** 机柜安装",
            "Figure 4-42 Cabinet installation",
            "Figure4-42 Cabinet installation",
            "FIG. 4-42: Cabinet installation",
            "<p><b>图4-42</b> 机柜安装</p>",
            "<figure><figcaption>图4-42 机柜安装</figcaption></figure>",
        ):
            with self.subTest(caption=caption):
                index = build_cross_reference_index(caption)
                self.assertTrue(is_disproved_missing_reference(missing(), index))
        index = build_cross_reference_index("Table A–1 Hardware\n图4.42 机柜安装")
        self.assertEqual(index, frozenset({("table", "A-1"), ("figure", "4.42")}))

    def test_mentions_code_table_cells_and_contents_are_not_definitions(self):
        for text in (
            "请参见图4-42，完成安装。",
            "图4-42所示的操作步骤。",
            "图4-42展示了安装步骤",
            "图4-42 为安装示意，请参考。",
            "图4-42 和图4-43给出了安装步骤",
            "图4-42 不存在",
            "图4-42 安装..................9",
            "图4-42 安装\t9",
            "List of Figures\nFigure 4-42 Installation 9",
            "```\n图4-42 安装示例\n```",
            "<!-- 图4-42 安装示例 -->",
            "<table><tr><td>图4-42</td><td>安装示例</td></tr></table>",
            "[超链接]\n图4-42 安装示例",
            "图4-42\n\n安装操作",
            "| 图4-42 | 安装步骤 |",
        ):
            with self.subTest(text=text):
                self.assertEqual(build_cross_reference_index(text), frozenset())
        caption = (
            "<table><caption>表4-42 工具规格</caption><tr><td>扳手</td></tr></table>"
        )
        self.assertIn(("table", "4-42"), build_cross_reference_index(caption))

    def test_identity_is_exact_and_separate_for_figures_tables_and_documents(self):
        for text in (
            "图4-420 安装",
            "图4-42A 安装",
            "表4-42 工具",
            "图4.42 安装",
            "file: a.txt\n见图4-42。\nfile: b.txt\n图4-42 安装",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    is_disproved_missing_reference(
                        missing(), build_cross_reference_index(text)
                    )
                )

    def test_placeholder_captions_do_not_prove_a_valid_target(self):
        for title in (
            "待插入",
            "待补充",
            "此处插入接线图",
            "TBD",
            "TODO: Installation",
        ):
            with self.subTest(title=title):
                index = build_cross_reference_index(f"图4-42 {title}")
                self.assertFalse(is_disproved_missing_reference(missing(), index))

    def test_missing_claims_use_identifier_without_requiring_identical_titles(self):
        for description in (
            "图4-42在正文中不存在。",
            "未找到图 4 – 42对应的图题。",
            "文中引用图4-42，但全文未发现该编号对应的图题。",
            "全文未检索到图4-42。",
            "正文引用图4-42，但全文未见对应图题。",
            "图4-42（机柜就位）不存在。",
            "Figure 4-42 does not exist in the document.",
            "图4-42、图4-43和表4-42不存在。",
            "正文引用的图号不存在。",
        ):
            with self.subTest(description=description):
                self.assertTrue(
                    is_disproved_missing_reference(missing(description), self.index)
                )

    def test_retains_unresolved_targets_mixed_issues_placeholders_and_wrong_references(
        self,
    ):
        for description in (
            "图4-42与图4-99不存在。",
            "图4-42、4-99不存在。",
            "图4-42至4-99不存在。",
            "图4-42至图4-43不存在。",
            "图4-42不存在；第5章也缺失。",
            "图4-42不存在；图4-43缺少单位。",
            "图4-42不存在，且有错别字。",
            "图4-42标题与正文不一致。",
            "图4-42与图4-43标题完全相同。",
            "图4-42不存在，正文应引用图4-43。",
            "图4-42数据缺失。",
            "图4-42中的图例不存在。",
            "图4-42未被引用。",
            "图4-42并非不存在，只是引用错位。",
            "无法确认图4-42是否不存在。",
            "外部文档中图4-42不存在。",
            "图4-42图题缺少编号。",
            "图4-42内容缺失。",
            "表4-42数据未找到。",
            "Table 4-42 is missing mandatory units.",
            "Figure 4-42 has missing content.",
            "图4-42不存在，但表4-42电压为5 V，正文为10 V。",
            "图4-42(b)不存在。",
        ):
            with self.subTest(description=description):
                self.assertFalse(
                    is_disproved_missing_reference(missing(description), self.index)
                )
        for excerpt in (
            "TODO：此处插入图4-42",
            "见《安装指南》图4-42",
            "见other.pdf图4-42",
        ):
            item = missing()
            item["excerpt"] = excerpt
            self.assertFalse(is_disproved_missing_reference(item, self.index))

    def test_aliases_summaries_and_legacy_reports(self):
        for key in ("items", "issues", "report_items", "findings", "problems"):
            payload = {
                "summary": "发现2条问题。",
                key: [
                    {
                        "状态": "问题",
                        "问题描述": "未找到图4-42。",
                        "原文摘录": "见图4-42",
                    },
                    {"status": "suggestion", "description": "图4-99不存在。"},
                ],
            }
            content, removed = sanitize_text_check_result(
                "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```",
                cross_reference_index=self.index,
            )
            self.assertEqual(removed, 1)
            self.assertEqual(json.loads(content)[key], payload[key][1:])
            self.assertEqual(
                json.loads(content)["summary"], "检查完成，保留 1 条待确认建议。"
            )
        content, removed = sanitize_text_check_result(
            rounds.report(missing()),
            cross_reference_index=self.index,
        )
        self.assertEqual(removed, 1)
        self.assertEqual(
            json.loads(content)["summary"], "未发现具有充分文本证据的问题。"
        )
        self.assertEqual(
            sanitize_text_check_result(
                "图4-42不存在。", cross_reference_index=self.index
            ),
            ("图4-42不存在。", 0),
        )

    def test_existence_guard_preserves_unresolved_claims_and_visual_guard_still_applies(
        self,
    ):
        unresolved = missing("图4-99不存在。")
        mixed = missing("图4-42不存在，表格缺少单位。")
        generic = rounds.finding("没有找到对应截图。", excerpt="操作方法见图4-42。")
        content, removed = sanitize_text_check_result(
            rounds.report(missing(), unresolved, mixed, generic),
            cross_reference_index=self.index,
        )
        self.assertEqual(removed, 2)
        self.assertEqual(json.loads(content)["items"], [unresolved, mixed])
        content, removed = sanitize_text_check_result(
            rounds.report(unresolved),
            cross_reference_index=frozenset(),
        )
        self.assertEqual(removed, 0)
        self.assertEqual(json.loads(content)["items"], [unresolved])


class CrossReferenceExecutionTest(unittest.TestCase):
    setUp = rounds.DocumentRoundExecutionTest.setUp
    tearDown = rounds.DocumentRoundExecutionTest.tearDown
    create_task = rounds.DocumentRoundExecutionTest.create_task
    task = rounds.DocumentRoundExecutionTest.task
    retry = rounds.DocumentRoundExecutionTest.retry

    def source_task(self, planned):
        task_id = self.create_task(rounds=planned)
        get_db().execute(
            "UPDATE tasks SET document_text=? WHERE id=?", (source_text(), task_id)
        )
        get_db().commit()
        return task_id

    def test_single_and_multiple_rounds_persist_and_export_only_valid_findings(self):
        for planned in (1, 2):
            with self.subTest(planned=planned):
                task_id = self.source_task(planned)
                retained = missing("图4-99不存在。")
                raw = rounds.report(missing(), retained)
                with (
                    patch("app.tasks.runner.run_check", return_value=raw) as model,
                    patch(
                        "app.tasks.runner.build_cross_reference_index",
                        wraps=build_cross_reference_index,
                    ) as build,
                ):
                    TaskRunner(self.app).run(task_id)
                task = self.task(task_id)
                self.assertEqual(task["status"], "completed", task["error"])
                self.assertEqual(model.call_count, planned)
                build.assert_called_once()
                self.assertNotIn("历史示例", build.call_args.args[0])
                result = json.loads(task["result_json"])[0]
                self.assertEqual(result["cross_reference_guard"], 1)
                self.assertEqual(len(json.loads(result["result"])["items"]), 1)
                prepared = _prepare_task_results(
                    [result], task_type=DOCUMENT_TASK_TYPE, suppression_rules={}
                )[0]
                self.assertEqual(len(prepared["report_items"]), 1)
                self.assertEqual(
                    prepared["report_items"][0]["description"], retained["description"]
                )
                if planned > 1:
                    self.assertEqual(len(result["structured_report"]["items"]), 1)
                    for entry in result["round_results"]:
                        self.assertEqual(entry["report"]["items"], [retained])
                        self.assertEqual(entry["result"], raw)
                workbook = load_workbook(build_report_workbook(task))
                self.addCleanup(workbook.close)
                sheet = workbook["报告条目"]
                self.assertEqual(sheet.max_row, 2)
                self.assertIn(
                    retained["description"], [cell.value for cell in sheet[2]]
                )

    def test_retry_rechecks_retained_reports_and_preserves_raw_output_and_item_ids(
        self,
    ):
        task_id = self.source_task(2)
        keep = rounds.finding()
        previous = rounds.completed(1, missing(), keep)
        result = {
            "code": "compliance",
            "name": "compliance",
            **build_round_result("compliance", 2, [previous]),
        }
        kept_id = next(
            item["id"]
            for item in result["structured_report"]["items"]
            if item["description"] == keep["description"]
        )
        result["item_acceptances"] = {kept_id: {"status": "accepted"}}
        get_db().execute(
            "UPDATE tasks SET result_json=? WHERE id=?", (json.dumps([result]), task_id)
        )
        get_db().commit()
        self.retry(task_id)
        with patch(
            "app.tasks.runner.run_check", return_value=rounds.report(missing())
        ) as model:
            TaskRunner(self.app).run(task_id)
        model.assert_called_once()
        task = self.task(task_id)
        self.assertEqual(task["status"], "completed", task["error"])
        result = json.loads(task["result_json"])[0]
        self.assertEqual(result["round_results"][0]["report"]["items"], [keep])
        self.assertEqual(result["round_results"][0]["result"], previous["result"])
        self.assertEqual(result["round_results"][1]["report"]["items"], [])
        self.assertEqual(len(result["structured_report"]["items"]), 1)
        self.assertEqual(result["structured_report"]["items"][0]["id"], kept_id)
        prepared = _prepare_task_results(
            [result], task_type=DOCUMENT_TASK_TYPE, suppression_rules={}
        )[0]
        self.assertEqual(prepared["report_items"][0]["acceptance_status"], "accepted")
        self.assertEqual(
            result["structured_report"]["items"][0]["source_items"],
            [{"round": 1, "item": 1}],
        )


if __name__ == "__main__":
    unittest.main()
