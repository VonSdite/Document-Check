import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.db import get_db, init_db, now_text, set_setting
from app.task_types import CONSISTENCY_TASK_TYPE, IMAGE_TASK_TYPE
from app.tasks import (
    TaskScheduler,
    cleanup_expired_task_reports,
    _document_check_items,
    _document_text_for_image_batch,
    _format_image_check_issue_summary,
    _image_check_target,
    _run_check_items_concurrently,
)
from app.translation_coverage import TRANSLATION_COVERAGE_CHECK_CODE


class TaskExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(Path(self.temp_dir.name) / "test.sqlite3")
        self.app.config["UPLOAD_FOLDER"] = str(Path(self.temp_dir.name) / "uploads")
        self.app.config["IMAGE_FOLDER"] = str(Path(self.temp_dir.name) / "images")
        self.app.config["NETWORK"] = {"proxy_mode": "direct", "proxy": "", "ssl_verify": False}
        Path(self.app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
        Path(self.app.config["IMAGE_FOLDER"]).mkdir(parents=True, exist_ok=True)
        self.context = self.app.app_context()
        self.context.push()
        init_db()

    def tearDown(self):
        self.context.pop()
        self.temp_dir.cleanup()

    def test_cleanup_expired_task_reports_removes_old_terminal_tasks_and_files(self):
        upload_dir = Path(self.app.config["UPLOAD_FOLDER"])
        image_root = Path(self.app.config["IMAGE_FOLDER"])
        old_upload = upload_dir / "old.pdf"
        recent_upload = upload_dir / "recent.pdf"
        running_upload = upload_dir / "running.pdf"
        old_image_dir = image_root / "old-images"
        old_image = old_image_dir / "old.png"
        old_upload.write_text("old", encoding="utf-8")
        recent_upload.write_text("recent", encoding="utf-8")
        running_upload.write_text("running", encoding="utf-8")
        old_image_dir.mkdir(parents=True, exist_ok=True)
        old_image.write_bytes(b"png")
        set_setting("report_retention_days", 1)
        db = get_db()
        old_meta = {
            "images": [
                {
                    "filename": "old.png",
                    "relative_path": "old-images/old.png",
                    "mime_type": "image/png",
                    "position": "page001-image001",
                }
            ],
            "page_images": [],
        }
        rows = [
            ("old.pdf", "old.pdf", IMAGE_TASK_TYPE, json.dumps(old_meta, ensure_ascii=False), "completed", "2000-01-01 00:00:00"),
            ("recent.pdf", "recent.pdf", IMAGE_TASK_TYPE, "{}", "completed", now_text()),
            ("running.pdf", "running.pdf", IMAGE_TASK_TYPE, "{}", "running", "2000-01-01 00:00:00"),
        ]
        for original, stored, task_type, meta, status, finished_at in rows:
            db.execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type, file_size,
                    document_meta_json, checks_json, model_name, api_base, request_timeout,
                    max_input_chars, status, progress, created_at, updated_at, finished_at
                )
                VALUES (?, '127.0.0.1', ?, ?, 'pdf', 1, ?, '[]', 'model-a',
                        'http://example.test/v1/chat/completions', 30, 5000,
                        ?, 100, ?, ?, ?)
                """,
                (task_type, original, stored, meta, status, finished_at, finished_at, finished_at),
            )
        db.commit()

        self.assertEqual(cleanup_expired_task_reports(self.app), 1)

        remaining = {
            row["stored_filename"]: row["status"]
            for row in db.execute("SELECT stored_filename, status FROM tasks").fetchall()
        }
        self.assertEqual(remaining, {"recent.pdf": "completed", "running.pdf": "running"})
        self.assertFalse(old_upload.exists())
        self.assertFalse(old_image.exists())
        self.assertFalse(old_image_dir.exists())
        self.assertTrue(recent_upload.exists())
        self.assertTrue(running_upload.exists())

    def test_cleanup_expired_task_reports_skips_locked_files(self):
        upload_dir = Path(self.app.config["UPLOAD_FOLDER"])
        old_upload = upload_dir / "old.pdf"
        old_upload.write_text("old", encoding="utf-8")
        set_setting("report_retention_days", 1)
        db = get_db()
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_meta_json, checks_json, model_name, api_base, request_timeout,
                max_input_chars, status, progress, created_at, updated_at, finished_at
            )
            VALUES (?, '127.0.0.1', 'old.pdf', 'old.pdf', 'pdf', 1, '{}', '[]', 'model-a',
                    'http://example.test/v1/chat/completions', 30, 5000,
                    'completed', 100, '2000-01-01 00:00:00', '2000-01-01 00:00:00', '2000-01-01 00:00:00')
            """,
            (IMAGE_TASK_TYPE,),
        )
        db.commit()

        with patch("app.tasks.remove_file", return_value=(False, "[WinError 32] 文件正被占用")):
            self.assertEqual(cleanup_expired_task_reports(self.app), 0)

        task = db.execute("SELECT * FROM tasks WHERE stored_filename = 'old.pdf'").fetchone()
        self.assertIsNotNone(task)
        self.assertTrue(old_upload.exists())

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

    def test_task_execution_uses_system_network_config(self):
        self.app.config["NETWORK"] = {
            "proxy_mode": "custom",
            "proxy": "http://127.0.0.1:7890",
            "ssl_verify": True,
        }
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
                '127.0.0.1', 'doc.txt', 'doc.txt', 'txt', 1,
                ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
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

        self.assertEqual(calls[0]["proxy_mode"], "custom")
        self.assertEqual(calls[0]["proxy"], "http://127.0.0.1:7890")
        self.assertTrue(calls[0]["ssl_verify"])

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

    def test_consistency_translation_coverage_check_runs_locally(self):
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
                '# 素材文档\n## 素材文档1：cn.txt\n### 1 Overview\n1. 支持断电记忆功能。\n2. 最大输入电流 10A。\n\n# 资料\n## 资料1：en.txt\n### 1 Overview\n1. Memory retention is supported.', ?, ?, 'local-rule', 'local://translation-coverage', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                CONSISTENCY_TASK_TYPE,
                json.dumps([TRANSLATION_COVERAGE_CHECK_CODE], ensure_ascii=False),
                json.dumps(
                    [
                        {
                            "code": TRANSLATION_COVERAGE_CHECK_CODE,
                            "name": "跨语言条目完整性检查",
                            "prompt": "本地规则",
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

        with patch("app.tasks.run_check", side_effect=AssertionError("不应调用模型")):
            TaskScheduler(self.app)._run_task(task_id)

        updated = db.execute("SELECT status, result_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
        results = json.loads(updated["result_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(results[0]["code"], TRANSLATION_COVERAGE_CHECK_CODE)
        self.assertIn("疑似漏翻译或条目缺失", results[0]["result"])

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

    def test_image_task_runs_multimodal_check_for_extracted_images(self):
        image_dir = Path(self.app.config["IMAGE_FOLDER"]) / "task-images"
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "0120_page094-image001.bin").write_bytes(b"unknown-bytes")
        (image_dir / "0001_page001-image001.png").write_bytes(b"png-bytes")
        db = get_db()
        created_at = now_text()
        image_meta = {
            "images": [
                {
                    "id": "image-0120",
                    "filename": "0120_page094-image001.bin",
                    "relative_path": "task-images/0120_page094-image001.bin",
                    "mime_type": "application/octet-stream",
                    "position": "page094-image001",
                    "source": "图纸.pdf",
                    "size_bytes": 13,
                },
                {
                    "id": "image-0001",
                    "filename": "0001_page001-image001.png",
                    "relative_path": "task-images/0001_page001-image001.png",
                    "mime_type": "image/png",
                    "position": "page001-image001",
                    "source": "图纸.pdf",
                    "size_bytes": 9,
                }
            ]
        }
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                ?, '127.0.0.1', '图纸.pdf', '图纸.pdf', 'pdf', 1,
                ?, ?, ?, ?, 'qwen-vl', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                IMAGE_TASK_TYPE,
                "file: 图纸.pdf\n\ndocument_text:\n图 1 是电源接线图。",
                json.dumps(image_meta, ensure_ascii=False),
                json.dumps([9]),
                json.dumps(
                    [
                        {
                            "id": 9,
                            "code": "image-small-language-text",
                            "name": "图片语种匹配检查",
                            "prompt": "检查图片文字语种是否和文档一致",
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

        def fake_run_multimodal_document_check(**kwargs):
            calls.append(kwargs)
            kwargs["on_content"]("流式图文结果")
            return "图文最终结果\n发现问题：图片中中文说明与英文文档语种不一致。\n需人工确认：截图底部文字较小。"

        with patch("app.tasks.run_multimodal_document_check", side_effect=fake_run_multimodal_document_check):
            TaskScheduler(self.app)._run_task(task_id)

        updated = db.execute("SELECT status, result_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
        results = json.loads(updated["result_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["check_name"], "图片资源检查合并检查（1项）")
        self.assertIn("image-small-language-text", calls[0]["prompt"])
        self.assertIn("图片语种匹配检查", calls[0]["prompt"])
        self.assertIn("图 1 是电源接线图", calls[0]["document_text"])
        self.assertEqual(calls[0]["batch_index"], 1)
        self.assertEqual(calls[0]["batch_count"], 1)
        self.assertEqual(calls[0]["image_items"][0]["index"], 2)
        self.assertEqual(calls[0]["image_items"][0]["name"], "0001_page001-image001.png")
        self.assertEqual(calls[0]["image_items"][0]["position"], "PDF第1页（page001-image001）")
        self.assertTrue(calls[0]["image_items"][0]["data_url"].startswith("data:image/png;base64,"))
        self.assertIn("PDF第1页（page001-image001）：0001_page001-image001.png", results[0]["result"])
        self.assertIn("0001_page001-image001.png", results[0]["result"])
        self.assertIn("0120_page094-image001.bin", results[0]["result"])
        self.assertIn("已跳过的图片", results[0]["result"])
        self.assertIn("图文最终结果", results[0]["result"])
        self.assertIn("检查汇总", results[0]["result"])
        self.assertIn("明确问题", results[0]["result"])
        self.assertIn("图片中中文说明与英文文档语种不一致", results[0]["result"])
        self.assertIn("需人工确认", results[0]["result"])

    def test_image_task_merges_page_level_checks_for_page_screenshots(self):
        image_dir = Path(self.app.config["IMAGE_FOLDER"]) / "task-pages"
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "0001_page001-screenshot.png").write_bytes(b"page-png-bytes")
        db = get_db()
        created_at = now_text()
        image_meta = {
            "page_selection": {
                "total_pages": 150,
                "selected_pages": [1],
                "omitted_pages": 149,
                "max_pages": 1,
                "strategy": "candidate-and-segment-sampling",
            },
            "images": [],
            "page_images": [
                {
                    "id": "page-0001",
                    "filename": "0001_page001-screenshot.png",
                    "relative_path": "task-pages/0001_page001-screenshot.png",
                    "mime_type": "image/png",
                    "position": "page001-screenshot",
                    "source": "图纸.pdf",
                    "size_bytes": 14,
                    "kind": "page",
                    "page_number": 1,
                }
            ],
        }
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                ?, '127.0.0.1', '图纸.pdf', '图纸.pdf', 'pdf', 1,
                ?, ?, ?, ?, 'qwen-vl', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                IMAGE_TASK_TYPE,
                "file: 图纸.pdf\n\ndocument_text:\n[第1页]\n3.1 参数\n项目 参数 单位",
                json.dumps(image_meta, ensure_ascii=False),
                json.dumps([35, 38]),
                json.dumps(
                    [
                        {
                            "id": 35,
                            "code": "image-figure-table-title-standard",
                            "name": "图表标题规范检查",
                            "prompt": "检查图标题和表标题是否缺失",
                        },
                        {
                            "id": 38,
                            "code": "image-integrity-clarity",
                            "name": "图片完整性和清晰度检查",
                            "prompt": "检查图片完整性和清晰度",
                        },
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

        def fake_run_multimodal_document_check(**kwargs):
            calls.append(kwargs)
            return """### 检查项：image-figure-table-title-standard｜图表标题规范检查
#### 总体判断
发现表标题缺失。
明确问题
- page001 表格缺少表标题。
需人工确认
- 未发现需人工确认项。
#### 未发现问题
- 未发现其他图表标题问题。
### 检查项：image-integrity-clarity｜图片完整性和清晰度检查
#### 总体判断
未发现明确清晰度问题。
**明确问题**
- 未发现明确问题。
**需人工确认**
- 页面截图较小，需人工确认清晰度。
#### 未发现问题
- 未发现明显异常。"""

        with patch("app.tasks.run_multimodal_document_check", side_effect=fake_run_multimodal_document_check):
            TaskScheduler(self.app)._run_task(task_id)

        updated = db.execute("SELECT status, result_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
        results = json.loads(updated["result_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["check_name"], "页面级检查合并检查（2项）")
        self.assertIn("image-figure-table-title-standard", calls[0]["prompt"])
        self.assertIn("image-integrity-clarity", calls[0]["prompt"])
        self.assertIn("PDF 页码", calls[0]["prompt"])
        self.assertEqual(calls[0]["image_items"][0]["position"], "PDF第1页（page001-screenshot）")
        self.assertEqual([item["code"] for item in results], ["image-figure-table-title-standard", "image-integrity-clarity"])
        self.assertIn("PDF第1页（page001-screenshot）：0001_page001-screenshot.png", results[0]["result"])
        self.assertIn("page001 表格缺少表标题", results[0]["result"])
        self.assertIn("page001 表格缺少表标题", results[0]["result"].split("### 检查汇总", 1)[-1])
        self.assertIn("页面截图较小，需人工确认清晰度", results[1]["result"])
        self.assertIn("页面截图较小，需人工确认清晰度", results[1]["result"].split("### 检查汇总", 1)[-1])
        self.assertIn("未覆盖 149 页", results[0]["result"])

    def test_qwen_vl_optimized_image_checks_use_expected_targets(self):
        self.assertEqual(_image_check_target({"code": "image-text-correspondence"}), "page")
        self.assertEqual(_image_check_target({"code": "image-wiring"}), "resource")
        self.assertEqual(_image_check_target({"code": "image-ui-step-consistency"}), "page")
        self.assertEqual(_image_check_target({"code": "image-device-installation"}), "resource")

    def test_image_issue_summary_reads_bare_page_level_sections(self):
        summary = _format_image_check_issue_summary(
            [
                {
                    "batch_index": 23,
                    "batch_count": 30,
                    "images": [
                        {
                            "filename": "0089_page089-screenshot.png",
                            "position": "page089-screenshot",
                            "page_number": 89,
                        }
                    ],
                    "content": """### 检查项：image-figure-table-title-standard｜图表标题规范检查
总体判断
发现明确问题：图片 89 页面顶部的表格缺失规范的表编号和标题。

明确问题
- 图片 89（page089-screenshot）：页面顶部的表格缺失表编号。
  - 线索：表格上方仅有“设置时间”文字，未见“表X-X”格式标题。

需人工确认
- 未发现需人工确认项。""",
                }
            ],
            [],
        )

        self.assertIn("批次 23/30", summary)
        self.assertIn("PDF第89页", summary)
        self.assertIn("图片 89（page089-screenshot）：页面顶部的表格缺失表编号", summary)
        self.assertNotIn("线索：表格上方仅有", summary)
        self.assertNotIn("未汇总到明确问题", summary)

    def test_image_issue_summary_filters_normal_items_from_clear_issues(self):
        summary = _format_image_check_issue_summary(
            [
                {
                    "batch_index": 1,
                    "batch_count": 1,
                    "images": [
                        {
                            "filename": "0003_page003-screenshot.png",
                            "position": "page003-screenshot",
                            "page_number": 3,
                        }
                    ],
                    "content": """### 检查项：image-integrity-clarity｜图片完整性和清晰度检查
#### 明确问题
- PDF第3页：页面显示正常，文字清晰。
- PDF第3页：未发现明确问题。
- PDF第3页：页面截图较小，需人工确认清晰度。
- PDF第3页：右下角表格标题缺失。
#### 需人工确认
- 未发现需人工确认项。""",
                }
            ],
            [],
        )

        issue_section = summary.split("#### 明确问题", 1)[-1].split("#### 需人工确认", 1)[0]
        manual_section = summary.split("#### 需人工确认", 1)[-1]
        self.assertIn("PDF第3页", summary)
        self.assertIn("右下角表格标题缺失", issue_section)
        self.assertNotIn("页面显示正常", issue_section)
        self.assertNotIn("未发现明确问题", issue_section)
        self.assertNotIn("页面截图较小", issue_section)
        self.assertIn("页面截图较小，需人工确认清晰度", manual_section)

    def test_image_batch_uses_nearby_page_text_context(self):
        document_text = "\n\n".join(
            [
                "file: 图纸.pdf",
                "document_text:",
                "[第68页]\n前一页说明",
                "[第69页]\n图 7-5 叠光控制器 ESN码位置",
                "[第70页]\n图 7-6 光伏优化器 ESN码位置",
                "[第71页]\n后一页说明",
                "[第10页]\n无关安装步骤",
                "extracted_images: 1\n- 0105_page069-image001.png: page069-image001",
            ]
        )

        scoped = _document_text_for_image_batch(
            document_text,
            [
                {
                    "filename": "0105_page069-image001.png",
                    "position": "page069-image001",
                    "mime_type": "image/png",
                }
            ],
        )

        self.assertIn("document_text_scope", scoped)
        self.assertIn("[第68页]", scoped)
        self.assertIn("[第69页]", scoped)
        self.assertIn("[第70页]", scoped)
        self.assertIn("0105_page069-image001.png", scoped)
        self.assertNotIn("[第10页]", scoped)
        self.assertNotIn("extracted_images: 1", scoped)


if __name__ == "__main__":
    unittest.main()
