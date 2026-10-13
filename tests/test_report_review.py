import copy
import json
import tempfile
import unittest
from pathlib import Path

from flask import Flask

from app.contracts.task_types import DOCUMENT_TASK_TYPE
from app.persistence.connection import get_db
from app.persistence.schema import init_db
from app.reporting.service import _count_report_items, _prepare_task_results
from app.reporting.statistics import _task_report_stat_rows_for_where


def report_result():
    return {
        "code": "consistency",
        "structured_report": {
            "items": [
                {"id": "issue-1", "status": "issue", "description": "电流参数相互冲突"},
                {
                    "id": "suggestion-1",
                    "status": "suggestion",
                    "description": "需核查适用范围",
                },
            ]
        },
        "item_acceptances": {
            "issue-1": {"status": "accepted"},
            "suggestion-1": {
                "status": "rejected",
                "rejection_reason": "classification_inaccurate",
            },
        },
    }


class ReportFeedbackTest(unittest.TestCase):
    def test_overall_rate_includes_both_types_and_excludes_pending(self):
        counts = _count_report_items(
            [
                {"type": "issue", "acceptance_status": "accepted"},
                {"type": "suggestion", "acceptance_status": "accepted"},
                {"type": "suggestion", "acceptance_status": "rejected"},
                {"type": "issue", "acceptance_status": "pending"},
            ]
        )
        self.assertEqual(counts["accepted"], 2)
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(counts["conclusion_acceptance_rate"], "66.7%")
        self.assertEqual(counts["review_coverage_rate"], "75.0%")
        self.assertNotIn("issue_acceptance_rate", counts)

    def test_original_ai_types_and_historical_feedback_are_preserved(self):
        source = report_result()
        source["item_classifications"] = {
            "issue-1": "non_issue",
            "suggestion-1": "issue",
        }
        original = copy.deepcopy(source)
        prepared = _prepare_task_results([source])[0]
        self.assertEqual(source, original)
        self.assertEqual(
            [
                (item["type"], item["acceptance_status"])
                for item in prepared["report_items"]
            ],
            [("issue", "accepted"), ("suggestion", "rejected")],
        )
        self.assertEqual(
            prepared["report_counts"]["conclusion_acceptance_rate"], "50.0%"
        )

    def test_saved_rejection_survives_filter_limit_deduplication_and_suppression(self):
        for mode in ("no_action", "limit", "duplicate", "suppression"):
            with self.subTest(mode=mode):
                source = report_result()
                rejected_item = source["structured_report"]["items"][1]
                rules = {}
                if mode == "no_action":
                    rejected_item.update(impact="无实质影响", suggestion="无需修改")
                elif mode == "limit":
                    source["issue_output_limit"] = 1
                elif mode == "duplicate":
                    rejected_item.update(status="issue", description="电流参数相互冲突")
                else:
                    rules = {
                        "consistency": [
                            {
                                "id": 1,
                                "reason": "模型误报",
                                "description": "需核查适用范围",
                            }
                        ]
                    }
                prepared = _prepare_task_results(
                    [source], task_type=DOCUMENT_TASK_TYPE, suppression_rules=rules
                )[0]
                self.assertEqual(len(prepared["report_items"]), 1)
                counts = prepared["report_counts"]
                self.assertEqual(counts["accepted"], 1)
                self.assertEqual(counts["rejected"], 1)
                self.assertEqual(counts["conclusion_acceptance_rate"], "50.0%")
                self.assertEqual(counts["reviewed"], 1)
                self.assertEqual(counts["review_coverage_rate"], "100.0%")

    def test_no_feedback_has_no_acceptance_rate(self):
        source = report_result()
        source.pop("item_acceptances")
        counts = _prepare_task_results([source])[0]["report_counts"]
        self.assertEqual(counts["conclusion_acceptance_rate"], "-")
        self.assertEqual(counts["review_coverage_rate"], "0.0%")


class ReportStatsMigrationTest(unittest.TestCase):
    def test_existing_database_adds_overall_counts_and_rebuilds_stale_cache(self):
        with tempfile.TemporaryDirectory() as root:
            app = Flask(__name__)
            app.config["DATABASE"] = str(Path(root) / "test.sqlite3")
            with app.app_context():
                init_db()
                db = get_db()
                source_json = json.dumps([report_result()], ensure_ascii=False)
                db.execute(
                    """
                    INSERT INTO tasks(
                        id, ip, original_filename, stored_filename, file_type, file_size,
                        checks_json, model_name, api_base, result_json, created_at, updated_at
                    ) VALUES (1, '127.0.0.1', 'test.txt', 'test.txt', 'txt', 1,
                              '[]', 'model', 'https://example.test', ?, 'now', 'now')
                    """,
                    (source_json,),
                )
                db.execute(
                    "ALTER TABLE task_report_stats RENAME COLUMN accepted_count TO accepted_issue_count"
                )
                db.execute(
                    "ALTER TABLE task_report_stats RENAME COLUMN rejected_count TO rejected_issue_count"
                )
                db.execute(
                    """
                    INSERT INTO task_report_stats(
                        task_id, source_updated_at, suppression_version,
                        accepted_issue_count, rejected_issue_count, updated_at
                    ) VALUES (1, 'now', '3|', 99, 0, 'now')
                    """
                )
                db.commit()

                init_db()
                rows = _task_report_stat_rows_for_where("t.id = ?", (1,))
                self.assertEqual(rows[0]["accepted"], 1)
                self.assertEqual(rows[0]["rejected"], 1)
                self.assertTrue(rows[0]["suppression_version"].startswith("4|"))
                init_db()
                cached = db.execute(
                    "SELECT * FROM task_report_stats WHERE task_id = 1"
                ).fetchone()
                self.assertEqual(cached["accepted_count"], 1)
                self.assertEqual(cached["rejected_count"], 1)
                self.assertEqual(
                    db.execute("SELECT result_json FROM tasks WHERE id = 1").fetchone()[
                        0
                    ],
                    source_json,
                )


if __name__ == "__main__":
    unittest.main()
