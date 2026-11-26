import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.checks.language_consistency import compose_language_consistency_text
from app.documents.extraction import DocumentReadError
from app.models.document_input import compact_model_document_text
from app.persistence.connection import get_db
from app.tasks.runner import TaskRunner
from tests import test_routes as fixtures
from tests.fixtures.pdf_tables import normalized_pdf_table


class TaskPreflightTest(unittest.TestCase):
    setUpFixture = fixtures.AdminSettingsRouteTest.setUp
    tearDown = fixtures.AdminSettingsRouteTest.tearDown
    _configure_provider = fixtures.AdminSettingsRouteTest._configure_provider
    _configure_document_scheme = (
        fixtures.AdminSettingsRouteTest._configure_document_scheme
    )

    def setUp(self):
        self.setUpFixture()
        self.model_id = self._configure_provider()
        with self.app.app_context():
            self.check_ids = {
                row["code"]: row["id"]
                for row in get_db().execute(
                    "SELECT id, code FROM check_items "
                    "WHERE task_type != 'document_check' OR pool_item_id = id"
                )
            }
        self.document_checks = self._configure_document_scheme(
            self.check_ids["compliance"]
        )

    def set_limit(self, limit):
        with self.app.app_context():
            get_db().execute(
                "UPDATE user_model_providers SET max_input_chars = ?", (limit,)
            )
            get_db().commit()

    def document(self, text, filename="doc.txt", route="/"):
        return self.client.post(
            route,
            data={
                "document": (io.BytesIO(text.encode("utf-8")), filename),
                "checks": self.document_checks,
                "model_id": self.model_id,
                "check_rounds": "1",
            },
            follow_redirects=True,
        )

    def pair(self, kind, prefix="", *, text_a="甲" * 60, text_b="乙" * 60):
        if kind == "consistency":
            fields = ("master_documents", "related_documents")
            code = "consistency-cross-document"
        else:
            fields = ("document_a", "document_b")
            code = "language-consistency-cross-lingual"
        return self.client.post(
            f"{prefix}/{kind}",
            data={
                fields[0]: (io.BytesIO(text_a.encode("utf-8")), "a.txt"),
                fields[1]: (io.BytesIO(text_b.encode("utf-8")), "b.txt"),
                "checks": [str(self.check_ids[code])],
                "model_id": self.model_id,
            },
            follow_redirects=True,
        )

    def tasks(self):
        with self.app.app_context():
            return [
                dict(row) for row in get_db().execute("SELECT * FROM tasks ORDER BY id")
            ]

    def run_task(self, task):
        with self.app.app_context():
            get_db().execute(
                "UPDATE tasks SET status = 'running' WHERE id = ?", (task["id"],)
            )
            get_db().commit()
        TaskRunner(self.app).run(task["id"])
        return next(row for row in self.tasks() if row["id"] == task["id"])

    def assert_failed(self, task, message):
        self.assertEqual(task["status"], "failed", task["error"])
        self.assertIn(message, task["error"])
        self.assertIsNotNone(task["finished_at"])
        self.assertTrue(
            (Path(self.app.config["UPLOAD_FOLDER"]) / task["stored_filename"]).is_file()
        )

    def test_uploads_queue_without_parsing_on_user_and_admin_routes(self):
        self.set_limit(20)
        with (
            patch(
                "app.tasks.runtime.preprocessing.extract_document"
            ) as extract_document,
            patch("app.tasks.runtime.preprocessing.extract_text") as extract_text,
        ):
            for prefix in ("", "/admin"):
                route = "/admin/tasks" if prefix else "/"
                self.assertEqual(
                    self.document("正文" * 50, route=route).status_code, 200
                )
                for kind in ("consistency", "language-consistency"):
                    self.assertEqual(self.pair(kind, prefix).status_code, 200)
        extract_document.assert_not_called()
        extract_text.assert_not_called()
        tasks = self.tasks()
        self.assertEqual(len(tasks), 6)
        for task in tasks:
            self.assertEqual(task["status"], "queued")
            self.assertIsNone(task["document_text"])
            self.assertEqual(
                json.loads(task["document_meta_json"])["preprocessing"]["status"],
                "pending",
            )

    def test_single_document_limit_includes_filename_and_allows_exact_boundary(self):
        text = "正文😀" * 20
        expected = "file: doc.txt\n\n" + text
        for route in ("/", "/admin/tasks"):
            for delta in (-1, 0):
                with self.subTest(route=route, delta=delta):
                    limit = len(expected) + delta
                    self.set_limit(limit)
                    self.assertEqual(self.document(text, route=route).status_code, 200)
                    with patch(
                        "app.tasks.runner.run_check",
                        return_value='{"summary":"检查完成","items":[]}',
                    ) as model:
                        task = self.run_task(self.tasks()[-1])
                    self.assertEqual(task["document_text"], expected)
                    if delta == -1:
                        message = f"文档文本 {len(expected)} 字，超过当前模型文本上限 {limit} 字"
                        self.assert_failed(task, message)
                        model.assert_not_called()
                        detail = self.client.get(f"/admin/tasks/{task['id']}")
                        self.assertEqual(detail.status_code, 200)
                        self.assertIn(message, detail.get_data(as_text=True))
                    else:
                        self.assertEqual(task["status"], "completed", task["error"])
                        model.assert_called_once()

    def test_batch_over_limit_fails_only_the_oversized_task(self):
        self.set_limit(50)
        response = self.client.post(
            "/",
            data={
                "document": [
                    (io.BytesIO(b"short"), "short.txt"),
                    (io.BytesIO(b"x" * 100), "long.txt"),
                ],
                "checks": self.document_checks,
                "model_id": self.model_id,
                "check_rounds": "1",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.tasks()), 2)
        with patch(
            "app.tasks.runner.run_check",
            return_value='{"summary":"检查完成","items":[]}',
        ) as model:
            short, long = [self.run_task(task) for task in self.tasks()]
        self.assertEqual(short["status"], "completed", short["error"])
        self.assert_failed(long, "超过当前模型文本上限 50 字")
        model.assert_called_once()
        self.assertEqual(len(list(Path(self.app.config["UPLOAD_FOLDER"]).iterdir())), 2)

    def test_grouped_limits_count_combined_input_and_static_summary(self):
        text_a, text_b = "甲" * 60, "乙" * 60
        consistency_text = f"# 素材文档\n\n## 素材文档1：a.txt\n{text_a}\n\n# 资料\n\n## 资料1：b.txt\n{text_b}"
        language_text, _ = compose_language_consistency_text(
            {"original_filename": "a.txt", "file_type": "txt", "text": text_a},
            {"original_filename": "b.txt", "file_type": "txt", "text": text_b},
        )
        for kind, expected in (
            ("consistency", consistency_text),
            ("language-consistency", language_text),
        ):
            for delta in (-1, 0):
                with self.subTest(kind=kind, delta=delta):
                    limit = len(expected) + delta
                    self.assertGreater(limit, len(text_a) + len(text_b))
                    self.set_limit(limit)
                    self.assertEqual(self.pair(kind).status_code, 200)
                    with patch(
                        "app.tasks.runner.run_check",
                        return_value='{"summary":"检查完成","items":[]}',
                    ) as model:
                        task = self.run_task(self.tasks()[-1])
                    self.assertEqual(task["document_text"], expected)
                    if delta == -1:
                        self.assert_failed(
                            task,
                            f"文档文本 {len(expected)} 字，超过当前模型文本上限 {limit} 字",
                        )
                        model.assert_not_called()
                    else:
                        self.assertEqual(task["status"], "completed", task["error"])
                        model.assert_called_once()

    def test_empty_documents_fail_in_worker_and_keep_source_files(self):
        self.assertEqual(self.document("   ").status_code, 200)
        for kind in ("consistency", "language-consistency"):
            self.assertEqual(self.pair(kind, text_b="   ").status_code, 200)
        with patch("app.tasks.runner.run_check") as model:
            for task in self.tasks():
                task = self.run_task(task)
                self.assert_failed(task, "可检查文本")
                if task["task_type"] != "document_check":
                    self.assertIn("b.txt", task["error"])
        model.assert_not_called()
        self.assertEqual(len(list(Path(self.app.config["UPLOAD_FOLDER"]).iterdir())), 5)

    def test_unreadable_document_fails_in_worker(self):
        self.document("docx fixture", "broken.docx")
        with (
            patch(
                "app.tasks.runtime.preprocessing.extract_document",
                side_effect=DocumentReadError("文档内容损坏，无法读取"),
            ),
            patch("app.tasks.runner.run_check") as model,
        ):
            task = self.run_task(self.tasks()[0])
        self.assert_failed(task, "文档内容损坏，无法读取")
        model.assert_not_called()

    def test_pdf_limit_uses_compact_input_and_preserves_full_text_and_metadata(self):
        extracted = "电压 10^{2} V\n" + normalized_pdf_table()
        source = "file: table.pdf\n\n" + extracted
        model_text = compact_model_document_text(source)
        self.assertLess(len(model_text), len(source))
        for delta in (-1, 0):
            with self.subTest(delta=delta):
                self.set_limit(len(model_text) + delta)
                self.document("pdf fixture", "table.pdf")
                with (
                    patch(
                        "app.tasks.runtime.preprocessing.extract_document",
                        return_value=(
                            extracted,
                            [{"target": "https://example.test/guide", "text": "指南"}],
                        ),
                    ),
                    patch(
                        "app.tasks.runner.run_check",
                        return_value='{"summary":"检查完成","items":[]}',
                    ) as model,
                ):
                    task = self.run_task(self.tasks()[-1])
                self.assertEqual(task["document_text"], source)
                metadata = json.loads(task["document_meta_json"])
                self.assertEqual(metadata["preprocessing"]["status"], "completed")
                self.assertEqual(
                    metadata["text_semantics"]["script_marker_format"], "caret_brace_v1"
                )
                self.assertEqual(metadata["hyperlinks"][0]["source"], "table.pdf")
                if delta == -1:
                    self.assert_failed(task, f"文档文本 {len(model_text)} 字，超过")
                    model.assert_not_called()
                else:
                    self.assertEqual(task["status"], "completed", task["error"])
                    self.assertEqual(
                        model.call_args.kwargs["document_text"], model_text
                    )

    def test_worker_reuses_cached_text_and_rechecks_limit_for_all_document_types(self):
        self.set_limit(20)
        self.document("有效正文" * 10)
        self.pair("consistency")
        self.pair("language-consistency")
        with patch("app.tasks.runner.run_check") as model:
            for task in self.tasks():
                self.assert_failed(self.run_task(task), "超过当前模型文本上限")
        model.assert_not_called()
        cached_tasks = self.tasks()
        with (
            patch(
                "app.tasks.runtime.preprocessing.extract_document"
            ) as extract_document,
            patch("app.tasks.runtime.preprocessing.extract_text") as extract_text,
            patch(
                "app.tasks.runner.run_check",
                return_value='{"summary":"检查完成","items":[]}',
            ) as model,
        ):
            for task in cached_tasks:
                self.assert_failed(self.run_task(task), "超过当前模型文本上限")
            model.assert_not_called()
            with self.app.app_context():
                get_db().execute("UPDATE tasks SET max_input_chars = 80000")
                get_db().commit()
            for task in cached_tasks:
                after = self.run_task(task)
                self.assertEqual(after["status"], "completed", after["error"])
                self.assertEqual(after["document_text"], task["document_text"])
        extract_document.assert_not_called()
        extract_text.assert_not_called()
        self.assertEqual(model.call_count, 3)


if __name__ == "__main__":
    unittest.main()
