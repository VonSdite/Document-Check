import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.checks.document_release import DOCUMENT_RELEASE_CHECK_CODE
from app.checks.document_scope import document_check_scope, first_chapter
from app.checks.term_locations import DocumentLocationIndex
from app.models.client import LLMError
from app.persistence.connection import get_db
from app.reporting.service import _prepare_task_results
from app.tasks.runner import TaskRunner
from tests import test_document_rounds as rounds


def source_text(*, prefix="前言模板内容", title="1 安全注意事项"):
    return (
        "file: guide.pdf\n\n[第1页]\n文档版本\n05\n发布日期\n2026-01-30\n"
        f"[第2页]\n前言\n{prefix}\n1. 阅读模板说明\n模板编号列表的解释。\n"
        "[第3页]\n修订记录\n文档版本 05（2026-01-30）\n历史变更模板\n"
        "[第4页]\n目 录\n1 安全注意事项\t1\n1.1 人身安全\t2\n2 安装\t3\n"
        "[超链接] 1 安全注意事项（超链接：#chapter1）\n"
        f"[第5页]\n{title}\n本章正文必须检查。\n1.1 人身安全\n正文安装要求。\n"
        "[第6页]\n2 安装\n后续正文继续检查。\n"
    )


class DocumentScopeTest(unittest.TestCase):
    def test_starts_at_real_chapter_and_preserves_all_body(self):
        text = source_text()
        scope = document_check_scope(text)
        self.assertEqual(scope.chapter.page, 5)
        self.assertEqual(scope.chapter.title, "1 安全注意事项")
        self.assertEqual(
            scope.ai_text[scope.ai_text.index("1 安全注意事项") :],
            text[scope.chapter.start :],
        )
        for excluded in (
            "前言模板内容",
            "历史变更模板",
            "目 录",
            "发布日期",
            "模板编号列表",
        ):
            self.assertNotIn(excluded, scope.ai_text)
        self.assertIn("[第5页]", scope.ai_text)
        self.assertIn("file: guide.pdf", scope.ai_text)
        self.assertIn("第一章之前", scope.ai_note)

    def test_chinese_english_markdown_and_html_headings(self):
        for heading in (
            "第一章 安装",
            "第1章 安装",
            "Chapter 1 Installation",
            "CHAPTER ONE Installation",
            "# 1 Introduction",
            "1. Introduction",
            "<h1>1 Introduction</h1>",
            "Chapter 1\nInstallation",
        ):
            with self.subTest(heading=heading):
                scope = document_check_scope(source_text(title=heading))
                self.assertEqual(scope.chapter.page, 5)
                self.assertNotIn("前言模板内容", scope.ai_text)
                self.assertIn("本章正文必须检查", scope.ai_text)

    def test_same_page_front_matter_is_excluded_at_heading(self):
        text = "file: doc.pdf\n[第2页]\n前言\n模板用语\n第一章 安装\n正文用语\n1.1 配置\n参数"
        scope = document_check_scope(text)
        self.assertEqual(scope.chapter.page, 2)
        self.assertNotIn("模板用语", scope.ai_text)
        self.assertIn("正文用语", scope.ai_text)

    def test_plain_text_chapter_after_preface(self):
        text = "前言\n模板内容\n1 安装\n正文段落\n1.1 配置\n操作步骤"
        scope = document_check_scope(text)
        self.assertIsNone(scope.chapter.page)
        self.assertTrue(scope.ai_text.startswith("1 安装"))

    def test_unknown_chapter_keeps_document(self):
        for text in (
            "说明文件\n没有章节号的正文。",
            "前言\n1. 阅读文档\n阅读说明\n2. 打开工具\n工具说明",
            "[第1页]\n目 录\n第一章 安装......3\n第二章 配置......4",
            "[第1页]\n前言\n```\nChapter 1 Example\nExample paragraph\n```\n真实正文",
            "[第1页]\n<table><tr><td>1 Installation</td></tr></table>\n真实正文",
            "file: chapter 1 guide.txt\n[第1页]\n请参见第一章 安装。\n真实正文",
            "file: data.xlsx\n# 工作表：数据\n表头\nChapter 1 Example\n其他数据",
        ):
            with self.subTest(text=text):
                self.assertIsNone(first_chapter(text))
                self.assertEqual(document_check_scope(text).ai_text, text)

    def test_table_of_contents_without_page_numbers_is_not_a_body(self):
        text = "[第2页]\nContents\nChapter 1 Introduction\nChapter 2 Setup\nChapter 3 Use\n[第3页]\nChapter 1 Introduction\nThis is the actual body."
        self.assertEqual(document_check_scope(text).chapter.page, 3)

    def test_terms_keep_original_page_line_and_offset(self):
        text = source_text(prefix="模板错词")
        scope = document_check_scope(text)
        terms = scope.term_text()
        self.assertEqual(len(terms), len(text))
        self.assertNotIn("模板错词", terms)
        position = text.index("正文安装要求")
        self.assertEqual(terms[position:], text[position:])
        self.assertEqual(
            DocumentLocationIndex(terms).location_for(position, "正文安装要求"),
            DocumentLocationIndex(text).location_for(position, "正文安装要求"),
        )

    def test_links_use_reference_page_and_preserve_body_targets(self):
        text = source_text(
            prefix="https://example.test/template\nhttps://example.test/shared"
        )
        text += "https://example.test/shared\n"
        scope = document_check_scope(text)
        links = [
            {"target": "#chapter1", "location": "第4页"},
            {"target": "https://example.test/shared", "location": "第2页"},
            {"target": "https://example.test/shared", "location": "第6页"},
            {"target": "https://example.test/template", "location": "第2段"},
            {"target": "https://unknown.test", "location": "未知"},
        ]
        self.assertEqual(scope.hyperlinks(links), links[2:3] + links[4:])

    def test_same_page_links_are_excluded_only_with_evidence(self):
        text = "[第1页]\n前言\n模板（超链接：https://example.test/template）\n第一章 安装\n正文（超链接：https://example.test/body）"
        scope = document_check_scope(text)
        links = [
            {"target": "https://example.test/" + name, "location": "第1页"}
            for name in ("template", "body", "unknown")
        ]
        self.assertEqual(scope.hyperlinks(links), links[1:])

    def test_revision_history_after_first_chapter_stays_excluded_from_ai(self):
        text = (
            source_text()
            + "[第7页]\nRevision History\nIssue 01 (2026-01-01)\nHistorical statement"
        )
        scope = document_check_scope(text)
        self.assertNotIn("Historical statement", scope.ai_text)
        self.assertIn("已排除修订记录", scope.ai_note)


class ScopeExecutionTest(unittest.TestCase):
    setUp = rounds.DocumentRoundExecutionTest.setUp
    tearDown = rounds.DocumentRoundExecutionTest.tearDown
    create_task = rounds.DocumentRoundExecutionTest.create_task
    task = rounds.DocumentRoundExecutionTest.task

    def test_all_content_checks_start_at_body_and_release_check_uses_cover(self):
        task_id = self.create_task(
            (
                "compliance",
                "sensitive-terms",
                "common-terms",
                "hyperlink-validity",
                DOCUMENT_RELEASE_CHECK_CODE,
            ),
            rounds=2,
        )
        source = source_text()
        meta = {
            "check_rounds": 2,
            "hyperlinks": [
                {"target": "https://example.test/template", "location": "第2页"},
                {"target": "https://example.test/body", "location": "第5页"},
            ],
        }
        get_db().execute(
            "UPDATE tasks SET document_text=?, document_meta_json=? WHERE id=?",
            (source, json.dumps(meta), task_id),
        )
        get_db().commit()
        empty = {"summary": "检查完成", "items": []}
        with (
            patch("app.tasks.runner.run_check", return_value=rounds.report()) as model,
            patch(
                "app.tasks.runner._run_sensitive_terms_check", return_value=empty
            ) as sensitive,
            patch(
                "app.tasks.runner._run_common_terms_check", return_value=empty
            ) as common,
            patch(
                "app.tasks.runner.build_hyperlink_report", return_value=empty
            ) as links,
        ):
            TaskRunner(self.app).run(task_id)
        self.assertEqual(model.call_count, 2)
        for call in model.call_args_list:
            self.assertNotIn("前言模板内容", call.kwargs["document_text"])
            self.assertIn("本章正文必须检查", call.kwargs["document_text"])
        for check in (sensitive, common):
            check.assert_called_once()
            self.assertNotIn("前言模板内容", check.call_args.args[1])
            self.assertIn("本章正文必须检查", check.call_args.args[1])
        self.assertEqual(links.call_args.args[0][0]["location"], "第5页")
        self.assertEqual(len(links.call_args.args[0]), 1)
        task = self.task(task_id)
        self.assertEqual(task["status"], "completed", task["error"])
        self.assertEqual(task["document_text"], source)
        results = _prepare_task_results(
            json.loads(task["result_json"]), suppression_rules={}
        )
        for result in results:
            if result["code"] == DOCUMENT_RELEASE_CHECK_CODE:
                self.assertIn("发布日期一致", result["result_summary"])
                self.assertNotIn("scope_note", result)
            else:
                self.assertIn("第一章之前", result["result_summary"])

    def test_input_budget_counts_scoped_body_and_retry_reuses_full_source(self):
        task_id = self.create_task(rounds=1)
        source = source_text(prefix="模板用语" * 2000)
        scope = document_check_scope(source)
        get_db().execute(
            "UPDATE tasks SET document_text=?, max_input_chars=? WHERE id=?",
            (source, len(scope.ai_text), task_id),
        )
        get_db().commit()
        with patch("app.tasks.runner.run_check", side_effect=LLMError("测试临时失败")):
            TaskRunner(self.app).run(task_id)
        self.assertEqual(self.task(task_id)["status"], "failed")
        rounds.DocumentRoundExecutionTest.retry(self, task_id)
        with patch("app.tasks.runner.run_check", return_value=rounds.report()) as model:
            TaskRunner(self.app).run(task_id)
        self.assertEqual(
            self.task(task_id)["status"], "completed", self.task(task_id)["error"]
        )
        self.assertEqual(model.call_args.kwargs["document_text"], scope.ai_text)
        self.assertEqual(self.task(task_id)["document_text"], source)

    def test_skill_budget_includes_scope_instruction(self):
        source = source_text(prefix="模板内容" * 1000)
        scope = document_check_scope(source)
        for delta in (0, -1):
            task_id = self.create_task(rounds=1)
            snapshot = json.loads(self.task(task_id)["checks_snapshot_json"])
            snapshot[0].update(check_method="skill", prompt="检查正文")
            limit = len(scope.ai_text) + len(scope.prompt("检查正文")) + delta
            get_db().execute(
                "UPDATE tasks SET document_text=?, checks_snapshot_json=?, max_input_chars=? WHERE id=?",
                (source, json.dumps(snapshot), limit, task_id),
            )
            get_db().commit()
            with patch(
                "app.tasks.runner.run_check", return_value=rounds.report()
            ) as model:
                TaskRunner(self.app).run(task_id)
            task = self.task(task_id)
            if delta:
                self.assertEqual(task["status"], "failed")
                self.assertIn("Skill", task["error"])
                model.assert_not_called()
            else:
                self.assertEqual(task["status"], "completed", task["error"])
                model.assert_called_once()

    def test_local_dictionary_reports_only_body_occurrences(self):
        task_id = self.create_task(("sensitive-terms",), rounds=1)
        path = Path(self.temp_dir.name) / "terms.csv"
        path.write_text(
            "不规范用语,规范用语\n错误称呼,正确称呼\n", encoding="utf-8", newline="\n"
        )
        self.app.config["SENSITIVE_TERMS_PATH"] = str(path)
        source = source_text(prefix="错误称呼").replace("正文安装要求", "错误称呼")
        get_db().execute(
            "UPDATE tasks SET document_text=? WHERE id=?", (source, task_id)
        )
        get_db().commit()
        TaskRunner(self.app).run(task_id)
        result = json.loads(self.task(task_id)["result_json"])[0]["structured_report"]
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("第5页", result["items"][0]["location"])
        self.assertNotIn("第2页", result["items"][0]["location"])


if __name__ == "__main__":
    unittest.main()
