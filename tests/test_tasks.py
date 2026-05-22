import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.db import get_db, init_db, now_text
from app.tasks import _run_check_items_concurrently


class TaskExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(Path(self.temp_dir.name) / "test.sqlite3")
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


if __name__ == "__main__":
    unittest.main()
