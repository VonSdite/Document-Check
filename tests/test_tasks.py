import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.db import get_db, init_db, now_text
from app.tasks import _run_check_items_concurrently, _text_chunks_for_task


class ChunkedTaskExecutionTest(unittest.TestCase):
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

    def test_document_check_runs_long_text_in_chunks(self):
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
            kwargs["on_content"](f"片段{len(calls)}流式结果")
            return f"片段{len(calls)}最终结果"

        with patch("app.tasks.run_check", side_effect=fake_run_check):
            results = _run_check_items_concurrently(
                self.app,
                task,
                check_items,
                document_text,
                max_workers=1,
                stream_trace_enabled=False,
            )

        self.assertGreater(len(calls), 1)
        self.assertIn("片段 1/", calls[0]["check_name"])
        self.assertIn("只检查当前片段", calls[0]["prompt"])
        self.assertIn("[长文档片段 1/", calls[0]["document_text"])
        self.assertEqual(len(results), 1)
        self.assertIn("长文档已分为", results[0]["result"])
        self.assertIn("片段1最终结果", results[0]["result"])

    def test_document_check_rejects_too_many_chunks(self):
        task = {"task_type": "document_check", "max_input_chars": 5000}
        document_text = "\n\n".join("内容" * 1300 for _ in range(61))

        with self.assertRaisesRegex(RuntimeError, "超过当前系统上限"):
            _text_chunks_for_task(task, document_text)


if __name__ == "__main__":
    unittest.main()
