import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.db import get_db, init_db, now_text
from app.task_types import CONSISTENCY_TASK_TYPE
from app.tasks import TaskScheduler, _document_check_items, _run_check_items_concurrently


class TaskExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(Path(self.temp_dir.name) / "test.sqlite3")
        self.app.config["UPLOAD_FOLDER"] = str(Path(self.temp_dir.name) / "uploads")
        Path(self.app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
        self.context = self.app.app_context()
        self.context.push()
        init_db()

    def tearDown(self):
        self.context.pop()
        self.temp_dir.cleanup()

    def test_document_check_sends_full_text_once(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'long.txt', 'long.txt', 'txt', 1,
                ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (json.dumps([1]), created_at, created_at),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()
        check_items = [{"code": "typo", "name": "错别字检查", "prompt": "检查错别字"}]
        document_text = "\n\n".join(f"第{i}段 " + ("内容" * 700) for i in range(1, 5))
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            kwargs["on_content"]("流式结果")
            return "最终结果"

        with patch("app.tasks.run_check", side_effect=fake_run_check):
            results = _run_check_items_concurrently(
                self.app,
                task,
                check_items,
                document_text,
                max_workers=1,
                stream_trace_enabled=False,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["check_name"], "错别字检查")
        self.assertEqual(calls[0]["prompt"], "检查错别字")
        self.assertEqual(calls[0]["document_text"], document_text)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["result"], "最终结果")

    def test_passes_force_disable_thinking_to_llm(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, max_input_chars,
                force_disable_thinking, status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'doc.txt', 'doc.txt', 'txt', 1,
                ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                1, 'running', 0, ?, ?
            )
            """,
            (json.dumps([1]), created_at, created_at),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()
        check_items = [{"code": "typo", "name": "错别字检查", "prompt": "检查错别字"}]
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            return "完成"

        with patch("app.tasks.run_check", side_effect=fake_run_check):
            _run_check_items_concurrently(
                self.app,
                task,
                check_items,
                "文档",
                max_workers=1,
                stream_trace_enabled=False,
            )

        self.assertTrue(calls[0]["force_disable_thinking"])

    def test_document_check_items_prefers_snapshot_over_current_database(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO check_items(code, name, description, prompt, enabled, sort_order, created_at, updated_at)
            VALUES ('typo', '当前名称', '', '当前提示词', 0, 10, ?, ?)
            """,
            (created_at, created_at),
        )
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, checks_snapshot_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'doc.txt', 'doc.txt', 'txt', 1,
                ?, ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                json.dumps([1]),
                json.dumps(
                    [
                        {
                            "id": 1,
                            "code": "typo",
                            "name": "提交时名称",
                            "prompt": "提交时提示词",
                        }
                    ],
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()

        self.assertEqual(
            _document_check_items(db, task),
            [{"code": "typo", "name": "提交时名称", "prompt": "提交时提示词"}],
        )

    def test_run_task_uses_cached_document_text_when_original_file_is_missing(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, checks_snapshot_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'missing.txt', 'missing.txt', 'txt', 1,
                'file: missing.txt\n\n缓存文本', ?, ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                json.dumps([1]),
                json.dumps(
                    [
                        {
                            "id": 1,
                            "code": "typo",
                            "name": "错别字检查",
                            "prompt": "检查错别字",
                        }
                    ],
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task_id = db.execute("SELECT id FROM tasks").fetchone()["id"]
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            return "完成"

        with patch("app.tasks.run_check", side_effect=fake_run_check):
            TaskScheduler(self.app)._run_task(task_id)

        updated = db.execute("SELECT status, result_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(calls[0]["document_text"], "file: missing.txt\n\n缓存文本")

    def test_consistency_task_uses_selected_check_snapshot(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, checks_snapshot_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                ?, '127.0.0.1', '多文档对照检查：素材1个 / 资料1个', 'master.txt', '多文档', 1,
                '# 素材文档\n素材参数 10A\n\n# 资料\n资料参数 12A', ?, ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                CONSISTENCY_TASK_TYPE,
                json.dumps([7]),
                json.dumps(
                    [
                        {
                            "id": 7,
                            "code": "custom-consistency",
                            "name": "参数一致性检查",
                            "prompt": "只检查参数是否一致",
                        }
                    ],
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task_id = db.execute("SELECT id FROM tasks").fetchone()["id"]
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            return "发现参数不一致"

        with patch("app.tasks.run_check", side_effect=fake_run_check):
            TaskScheduler(self.app)._run_task(task_id)

        updated = db.execute("SELECT status, result_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(calls[0]["check_name"], "参数一致性检查")
        self.assertEqual(calls[0]["prompt"], "只检查参数是否一致")

    def test_consistency_task_supports_legacy_code_checks_json(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO check_items(task_type, code, name, description, prompt, enabled, sort_order, created_at, updated_at)
            VALUES (?, 'consistency-cross-document', '多文档对照检查', '', '默认多文档对照提示词', 1, 10, ?, ?)
            """,
            (CONSISTENCY_TASK_TYPE, created_at, created_at),
        )
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                ?, '127.0.0.1', '多文档对照检查：素材1个 / 资料1个', 'master.txt', '多文档', 1,
                '# 素材文档\n素材参数 10A\n\n# 资料\n资料参数 12A', ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                CONSISTENCY_TASK_TYPE,
                json.dumps(["consistency-cross-document"], ensure_ascii=False),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task_id = db.execute("SELECT id FROM tasks").fetchone()["id"]
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            return "完成"

        with patch("app.tasks.run_check", side_effect=fake_run_check):
            TaskScheduler(self.app)._run_task(task_id)

        self.assertEqual(calls[0]["check_name"], "多文档对照检查")
        self.assertEqual(calls[0]["prompt"], "默认多文档对照提示词")


if __name__ == "__main__":
    unittest.main()
