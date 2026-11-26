import json
import unittest
from unittest.mock import patch

from app.checks.document_release import (
    DOCUMENT_RELEASE_CHECK_CODE,
    build_document_release_report,
)
from app.checks.revision_history import (
    exclude_revision_history,
    revision_history_sections,
)
from app.persistence.check_item_pool import profile_check_ids, save_profile_check_items
from app.persistence.connection import get_db
from app.persistence.defaults import seed_defaults
from app.reporting.service import _prepare_task_results
from app.tasks.runner import TaskRunner
from tests import test_document_rounds as rounds


def document(history=None, *, version="05", release_date="2026-01-30"):
    if history is None:
        history = (
            "文档版本 05（2026-01-30）\n更新5.1 安装线缆。\n"
            "文档版本 01（2024-09-20）\n软件版本：SMU V100R023\n"
            "删除8.3.9 旧设置。\n前言\niv\n"
            "[超链接] 除8.3.9 旧设置（超链接：#old-settings）\n"
        )
    return (
        f"file: guide.pdf\n[第1页]\n产品安装指南\n文档版本\n{version}\n"
        f"发布日期\n{release_date}\n[第2页]\n修订记录\n{history}\n"
        "[第3页]\n1 安装\n软件版本：SMU V500R003\n保留当前正文。\n"
    )


class ReleaseRuleTest(unittest.TestCase):
    def test_cover_matches_latest_history_and_retains_evidence(self):
        report = build_document_release_report(document())
        self.assertEqual(report["items"], [])
        self.assertEqual(report["comparisons"]["version"]["status"], "match")
        self.assertEqual(report["comparisons"]["date"]["status"], "match")
        self.assertIn("2026-01-30", report["summary"])

    def test_version_and_date_mismatches_are_independent(self):
        for version, day, fields in (
            ("04", "2026-01-30", {"version"}),
            ("05", "2026-01-29", {"date"}),
            ("04", "2026-01-29", {"version", "date"}),
        ):
            with self.subTest(version=version, date=day):
                report = build_document_release_report(
                    document(version=version, release_date=day)
                )
                self.assertEqual(
                    {
                        key
                        for key, value in report["comparisons"].items()
                        if value["status"] == "mismatch"
                    },
                    fields,
                )
                self.assertEqual(len(report["items"]), len(fields))
                for item in report["items"]:
                    self.assertEqual(item["status"], "issue")
                    self.assertIn("第1页", item["location"])
                    self.assertIn("第2页", item["location"])
                    self.assertIn("文档版本 05", item["excerpt"])

    def test_equivalent_versions_and_dates(self):
        for value in (
            "2026/1/30",
            "2026.01.30",
            "2026年1月30日",
            "January 30, 2026",
            "30 Jan 2026",
        ):
            with self.subTest(date=value):
                report = build_document_release_report(
                    document(version="V5", release_date=value)
                )
                self.assertEqual(report["items"], [])

    def test_latest_is_numeric_version_in_either_row_order(self):
        entries = [
            "文档版本 09（2026-01-30）",
            "文档版本 10（2026-02-01）",
            "文档版本 02（2024-01-01）",
        ]
        for rows in (entries, entries[::-1]):
            report = build_document_release_report(
                document("\n".join(rows), version="10", release_date="2026-02-01")
            )
            self.assertEqual(report["items"], [])

    def test_english_history_and_cover(self):
        text = (
            "[第1页]\nInstallation Guide\nIssue\n03\nRelease Date\n2026-01-30\n"
            "[第2页]\nChange History\nIssue 01 (2024-09-20)\nInitial release.\n"
            "Issue 03 (30 January 2026)\nUpdate wiring.\n[第3页]\n1 Installation\nBody."
        )
        self.assertEqual(build_document_release_report(text)["items"], [])

    def test_table_columns_determine_date_and_version(self):
        rows = (
            '<table data-confidence="high"><tr><td>Date</td><td>Version</td><td>Description</td></tr>'
            "<tr><td>2026-01-30</td><td>05</td><td>Replaces 07 (2027-01-01).</td></tr>"
            "<tr><td>2024-09-20</td><td>01</td><td>Initial release</td></tr></table>"
        )
        self.assertEqual(build_document_release_report(document(rows))["items"], [])
        for separator in (" | ", "\t"):
            text = (
                separator.join(("Date", "Version", "Description"))
                + "\n"
                + separator.join(("2026-01-30", "05", "Update 2027-01-01"))
            )
            self.assertEqual(build_document_release_report(document(text))["items"], [])

    def test_ambiguous_dates_and_duplicate_latest_require_confirmation(self):
        for history in (
            "文档版本 05（01/02/2026）",
            "文档版本 05（2026-02-30）",
            "文档版本 05（2026-01-30）\n文档版本 05（2026-02-01）",
        ):
            with self.subTest(history=history):
                report = build_document_release_report(document(history))
                self.assertEqual(report["comparisons"]["version"]["status"], "match")
                self.assertEqual(report["comparisons"]["date"]["status"], "unverified")
                self.assertTrue(
                    all(item["status"] == "suggestion" for item in report["items"])
                )

    def test_missing_and_unreliable_evidence_does_not_pass_or_assert_mismatch(self):
        for text in (
            document().replace("发布日期\n2026-01-30", "版权所有 2026"),
            document().replace("文档版本\n05", "产品型号 05"),
            "[第1页]\nVersion 05\nDate 2026-01-30\n[第2页]\n1 Overview\nBody",
            document(
                '<table data-confidence="low"><tr><td>Version</td><td>Date</td></tr><tr><td>05</td><td>2026-01-30</td></tr></table>'
            ),
        ):
            with self.subTest(text=text[:80]):
                report = build_document_release_report(text)
                self.assertTrue(
                    any(
                        value["status"] == "unverified"
                        for value in report["comparisons"].values()
                    )
                )
                self.assertEqual(
                    {item["status"] for item in report["items"]}, {"suggestion"}
                )

    def test_unreadable_latest_entry_does_not_fall_back_to_older_release(self):
        for newest in (
            "文档版本A（2026-02-01）",
            "Issue V6-beta (2026-02-01)",
            "06 2026-02-30",
            "06 01/02/2026",
        ):
            report = build_document_release_report(
                document(newest + "\n文档版本 05（2026-01-30）")
            )
            self.assertTrue(
                any(item["status"] == "suggestion" for item in report["items"])
            )
            self.assertNotEqual(report["comparisons"]["date"]["status"], "match")

    def test_missing_version_in_a_dated_table_row_requires_confirmation(self):
        for history in (
            "Version | Date\n05 | 2026-01-30\n | 2026-02-01 | Changed wiring",
            "Date | Version\n2026-01-30 | 05\n2026-02-01 | ",
        ):
            report = build_document_release_report(document(history))
            self.assertEqual(report["comparisons"]["version"]["status"], "unverified")

    def test_latest_table_row_uses_its_own_page(self):
        history = (
            "文档版本 01（2024-09-20）\n[第8页]\n"
            '<table data-confidence="high"><tr><td>Version</td><td>Date</td></tr>'
            "<tr><td>06</td><td>2026-02-01</td></tr></table>"
        )
        report = build_document_release_report(document(history))
        self.assertEqual(len(report["items"]), 2)
        for item in report["items"]:
            self.assertIn("第8页", item["location"])

    def test_multiple_independent_histories_require_confirmation(self):
        text = document() + "\n[第4页]\n修订记录\n文档版本 06（2026-02-01）"
        report = build_document_release_report(text)
        self.assertEqual({item["status"] for item in report["items"]}, {"suggestion"})

    def test_numbered_changes_and_page_numbers_are_not_release_versions(self):
        history = "Issue 01 (2024-09-20)\n1. Added wiring instructions.\nIssue 05 (2026-01-30)\n2. Updated settings.\n99\n"
        self.assertEqual(build_document_release_report(document(history))["items"], [])

    def test_cross_page_table_with_date_column_first(self):
        history = (
            "文档版本 01（2024-09-20）\n[第8页]\n"
            '<table data-confidence="high"><tr><td>Date</td><td>Version</td></tr>'
            "<tr><td>2026-01-30</td><td>05</td></tr></table>"
        )
        self.assertEqual(build_document_release_report(document(history))["items"], [])


class RevisionScopeTest(unittest.TestCase):
    def test_excludes_history_and_link_annotations_but_preserves_body_and_pages(self):
        source = document()
        view, sections = exclude_revision_history(source)
        self.assertEqual(len(sections), 1)
        self.assertNotIn("V100R023", view)
        self.assertNotIn("old-settings", view)
        self.assertIn("V500R003", view)
        self.assertIn("[第3页]", view)
        self.assertIn("保留当前正文", view)
        self.assertIn("V100R023", source)

    def test_same_page_heading_and_next_page_unknown_heading_preserve_body(self):
        for suffix in (
            "\n## Installation\nCurrent instructions.",
            "\n2 Installation\nCurrent instructions.",
            "\n[第3页]\nUnnumbered topic\nCurrent instructions.",
        ):
            source = (
                "[第2页]\nRevision History\nVersion 01 (2026-01-30)\nHistorical change."
                + suffix
            )
            view, sections = exclude_revision_history(source)
            self.assertEqual(len(sections), 1)
            self.assertNotIn("Historical change", view)
            self.assertIn("Current instructions", view)

    def test_multpage_history_preserves_markers_and_following_section(self):
        source = "[第2页]\nChange History\nIssue 03 (2026-01-30)\nChanged wiring.\n[第3页]\nIssue 02 (2025-01-01)\nRemoved settings.\n[第4页]\n1 Installation\nCurrent body."
        view, sections = exclude_revision_history(source)
        self.assertEqual(len(sections), 1)
        self.assertNotIn("Removed settings", view)
        self.assertIn("[第3页]", view)
        self.assertIn("Current body", view)

    def test_toc_mentions_and_code_samples_are_not_chapters(self):
        for source in (
            "Contents\nChange History .... 2\nInstallation .... 3\n[第3页]\n2026-01-30 Body",
            "See Change History for updates.\nVersion 02 (2026-01-30)",
            "```text\nChange History\nVersion 02 (2026-01-30)\n```\nCurrent body",
        ):
            self.assertEqual(exclude_revision_history(source), (source, []))

    def test_numbered_chinese_heading(self):
        source = "1.1 修订记录\n文档版本 01（2026-01-30）\n历史描述\n1.2 概述\n当前正文"
        self.assertEqual(len(revision_history_sections(source)), 1)
        self.assertIn("当前正文", exclude_revision_history(source)[0])

    def test_unrelated_table_on_following_page_is_preserved(self):
        source = (
            "[第2页]\n修订记录\n文档版本 01（2026-01-30）\n更新内容\n"
            "[第3页]\n<table><tr><td>Voltage</td><td>5V</td></tr></table>"
        )
        self.assertIn("<td>5V</td>", exclude_revision_history(source)[0])


class ReleaseExecutionTest(unittest.TestCase):
    setUp = rounds.DocumentRoundExecutionTest.setUp
    tearDown = rounds.DocumentRoundExecutionTest.tearDown
    create_task = rounds.DocumentRoundExecutionTest.create_task
    task = rounds.DocumentRoundExecutionTest.task

    def test_local_rule_runs_once_without_model_and_ignores_ai_input_limit(self):
        task_id = self.create_task((DOCUMENT_RELEASE_CHECK_CODE,), rounds=3)
        get_db().execute(
            "UPDATE tasks SET document_text=?, max_input_chars=1 WHERE id=?",
            (document(), task_id),
        )
        get_db().commit()
        with (
            patch("app.tasks.runner.run_check") as model,
            patch(
                "app.tasks.runner.build_document_release_report",
                wraps=build_document_release_report,
            ) as rule,
        ):
            TaskRunner(self.app).run(task_id)
        model.assert_not_called()
        rule.assert_called_once()
        task = self.task(task_id)
        self.assertEqual(task["status"], "completed", task["error"])
        result = json.loads(task["result_json"])[0]
        self.assertEqual(result["structured_report"]["items"], [])
        self.assertNotIn("round_results", result)

    def test_ai_rounds_receive_scoped_text_and_local_rule_receives_original(self):
        for count in (1, 3):
            task_id = self.create_task(
                ("compliance", DOCUMENT_RELEASE_CHECK_CODE), rounds=count
            )
            get_db().execute(
                "UPDATE tasks SET document_text=? WHERE id=?", (document(), task_id)
            )
            get_db().commit()
            with patch(
                "app.tasks.runner.run_check", return_value=rounds.report()
            ) as model:
                TaskRunner(self.app).run(task_id)
            self.assertEqual(model.call_count, count)
            for call in model.call_args_list:
                self.assertNotIn("V100R023", call.kwargs["document_text"])
                self.assertIn("V500R003", call.kwargs["document_text"])
                self.assertIn("历史变更说明", call.kwargs["prompt"])
            task = self.task(task_id)
            self.assertIn("V100R023", task["document_text"])
            self.assertEqual(task["status"], "completed", task["error"])
            local = next(
                entry
                for entry in json.loads(task["result_json"])
                if entry["code"] == DOCUMENT_RELEASE_CHECK_CODE
            )
            self.assertEqual(
                local["structured_report"]["comparisons"]["date"]["status"], "match"
            )
            prepared = _prepare_task_results(
                json.loads(task["result_json"]), suppression_rules={}
            )
            ai = next(entry for entry in prepared if entry["code"] == "compliance")
            self.assertIn("已排除第一章之前的模板内容", ai["result_summary"])
            self.assertNotIn("scope_note", local)

    def test_aliased_local_executor_is_preserved_by_snapshot(self):
        task_id = self.create_task(("pool-release",), rounds=3)
        snapshot = json.loads(self.task(task_id)["checks_snapshot_json"])
        snapshot[0]["executor_code"] = DOCUMENT_RELEASE_CHECK_CODE
        get_db().execute(
            "UPDATE tasks SET document_text=?, checks_snapshot_json=?, max_input_chars=1 WHERE id=?",
            (document(), json.dumps(snapshot), task_id),
        )
        get_db().commit()
        with patch("app.tasks.runner.run_check") as model:
            TaskRunner(self.app).run(task_id)
        model.assert_not_called()
        task = self.task(task_id)
        self.assertEqual(task["status"], "completed", task["error"])
        self.assertEqual(
            json.loads(task["result_json"])[0]["executor_code"],
            DOCUMENT_RELEASE_CHECK_CODE,
        )

    def test_existing_pool_receives_new_shared_rule(self):
        seed_defaults()
        get_db().execute(
            "DELETE FROM check_items WHERE template_code=?",
            (DOCUMENT_RELEASE_CHECK_CODE,),
        )
        get_db().execute(
            "DELETE FROM settings WHERE key='document_release_check_seeded_v1'"
        )
        get_db().commit()
        seed_defaults()
        row = (
            get_db()
            .execute(
                "SELECT id, template_code FROM check_items WHERE profile_id='default' AND code=?",
                (DOCUMENT_RELEASE_CHECK_CODE,),
            )
            .fetchone()
        )
        for profile in ("default", "software"):
            self.assertIn(row["id"], profile_check_ids(profile))
        self.assertEqual(row["template_code"], DOCUMENT_RELEASE_CHECK_CODE)

    def test_builtin_schemes_share_rule_and_preserve_deselection_on_restart(self):
        seed_defaults()
        row = (
            get_db()
            .execute(
                "SELECT * FROM check_items WHERE profile_id='default' AND code=?",
                (DOCUMENT_RELEASE_CHECK_CODE,),
            )
            .fetchone()
        )
        self.assertEqual(row["template_code"], DOCUMENT_RELEASE_CHECK_CODE)
        for profile in ("default", "software"):
            self.assertIn(row["id"], profile_check_ids(profile))
            save_profile_check_items(profile, profile_check_ids(profile) - {row["id"]})
        get_db().commit()
        seed_defaults()
        for profile in ("default", "software"):
            self.assertNotIn(row["id"], profile_check_ids(profile))


if __name__ == "__main__":
    unittest.main()
