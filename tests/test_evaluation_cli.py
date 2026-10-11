import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app.checks.evaluation import score_runs
from scripts.evaluate_checks import (
    export_feedback,
    main,
    run_case_safely,
    validate_cases,
    write_json,
)


class EvaluationCLITest(unittest.TestCase):
    def test_duplicate_sample_ids_fail_before_any_api_request(self):
        case = {
            "id": "same",
            "documents": [{"filename": "a.txt", "text": "原文"}],
            "check": {"code": "c", "name": "检查", "prompt": "检查原文"},
        }
        with self.assertRaisesRegex(ValueError, "唯一"):
            validate_cases([case, case])

    def test_unreadable_file_is_saved_as_failed_run(self):
        case = {
            "id": "missing-file",
            "documents": [{"path": "missing.pdf"}],
            "check": {"code": "c", "name": "检查", "prompt": "检查原文"},
        }
        with patch("scripts.evaluate_checks.run_check") as model:
            result = run_case_safely(case, "standard", {}, Path.cwd())
        self.assertTrue(result["error"])
        self.assertEqual(result["case_id"], case["id"])
        self.assertFalse(result["metadata"]["usage_complete"])
        model.assert_not_called()

    def test_feedback_export_is_read_only_excludes_secrets_and_requires_gold_labels(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "feedback.sqlite3"
            with closing(sqlite3.connect(database)) as db:
                db.execute(
                    "CREATE TABLE tasks(id INTEGER PRIMARY KEY, task_type TEXT, status TEXT, original_filename TEXT, document_meta_json TEXT, checks_snapshot_json TEXT, result_json TEXT, api_key TEXT)"
                )
                check = {
                    "code": "consistency",
                    "name": "一致性",
                    "prompt": "核对参数",
                    "api_key": "snapshot-secret",
                }
                result = {
                    "code": "consistency",
                    "structured_report": {
                        "summary": "一个问题",
                        "items": [
                            {
                                "id": "finding",
                                "status": "issue",
                                "description": "电压存在冲突",
                                "excerpt": "220 V",
                            }
                        ],
                    },
                    "item_acceptances": {
                        "finding": {
                            "status": "rejected",
                            "rejection_reason": "false_positive",
                            "rejection_note": "不同型号",
                        }
                    },
                }
                documents = [
                    {
                        "filename": "guide.txt",
                        "role": "document",
                        "text": "型号A 220 V\n型号B 110 V",
                        "text_complete": True,
                        "api_key": "source-secret",
                    }
                ]
                for task_id, sources in ((1, documents), (2, [])):
                    db.execute(
                        "INSERT INTO tasks VALUES (?, 'document_check', 'completed', 'guide.txt', ?, ?, ?, 'database-secret')",
                        (
                            task_id,
                            json.dumps({"source_documents": sources}),
                            json.dumps([check]),
                            json.dumps([result]),
                        ),
                    )
                db.commit()
            before = database.read_bytes()
            exported = export_feedback(database)
            self.assertEqual(before, database.read_bytes())
            self.assertEqual(len(exported["cases"]), 1)
            self.assertEqual(exported["skipped"][0]["task_id"], 2)
            serialized = json.dumps(exported)
            for secret in (
                "snapshot-secret",
                "source-secret",
                "database-secret",
                "api_key",
            ):
                self.assertNotIn(secret, serialized)
            case = exported["cases"][0]
            self.assertFalse(case["gold_complete"])
            self.assertEqual(exported["runs"][0]["report"]["items"][0]["id"], "finding")
            annotation = exported["annotations"][0]
            self.assertEqual(annotation["label"], "unreviewed")
            self.assertEqual(
                annotation["feedback_hint"]["acceptance_status"], "rejected"
            )
            score = score_runs(
                exported["cases"], exported["runs"], exported["annotations"]
            )["production_standard"]
            self.assertIsNone(score["recall_against_gold"])
            self.assertIsNone(score["precision_on_reviewed"])

    def test_cli_quality_gate_requires_complete_review_and_latency_records(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            cases = [
                {
                    "id": "a",
                    "gold_complete": True,
                    "expected_issues": [{"id": "gold", "severity": "critical"}],
                }
            ]
            runs = [
                {
                    "case_id": "a",
                    "variant": "agent",
                    "report": {"items": [{"id": "one", "status": "issue"}]},
                    "metadata": {"elapsed_seconds": 5},
                }
            ]
            labels = [
                {
                    "case_id": "a",
                    "variant": "agent",
                    "item_id": "one",
                    "label": "true_positive",
                    "expected_id": "gold",
                }
            ]
            args = ["evaluate_checks", "score"]
            for name, rows in (
                ("cases", cases),
                ("runs", runs),
                ("annotations", labels),
            ):
                write_json(folder / f"{name}.json", rows)
                args.extend([f"--{name}", str(folder / f"{name}.json")])
            args.extend(
                [
                    "--output",
                    str(folder / "scores.json"),
                    "--min-precision",
                    "0.9",
                    "--min-critical-recall",
                    "0.9",
                    "--max-p95-seconds",
                    "10",
                ]
            )
            with patch("sys.argv", args), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            write_json(folder / "annotations.json", [])
            with patch("sys.argv", args), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
            write_json(folder / "annotations.json", labels)
            runs[0]["metadata"] = {}
            write_json(folder / "runs.json", runs)
            with patch("sys.argv", args), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)


if __name__ == "__main__":
    unittest.main()
