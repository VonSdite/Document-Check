import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.checks.language_consistency import compose_language_consistency_text
from app.models.document_input import compact_model_document_text
from app.persistence.connection import get_db
from app.tasks.runner import TaskRunner
from tests import test_routes as fixtures
from tests.fixtures.pdf_tables import normalized_pdf_table


class UploadPreflightTest(unittest.TestCase):
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

    def assert_rejected(self, response, message):
        self.assertEqual(response.status_code, 200)
        self.assertIn(message, response.get_data(as_text=True))
        self.assertEqual(self.tasks(), [])
        self.assertEqual(list(Path(self.app.config["UPLOAD_FOLDER"]).iterdir()), [])

    def test_single_document_limit_includes_filename_and_allows_exact_boundary(self):
        text = "正文😀" * 20
        expected = "file: doc.txt\n\n" + text
        for route in ("/", "/admin/tasks"):
            with self.subTest(route=route):
                self.set_limit(len(expected) - 1)
                self.assert_rejected(
                    self.document(text, route=route),
                    f"文档文本 {len(expected)} 字，超过当前模型文本上限 {len(expected) - 1} 字",
                )
        self.set_limit(len(expected))
        self.assertEqual(self.document(text).status_code, 200)
        task = self.tasks()[0]
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["document_text"], expected)
        self.assertEqual(
            json.loads(task["document_meta_json"])["preprocessing"]["status"],
            "completed",
        )

    def test_batch_over_limit_removes_all_files_and_creates_no_tasks(self):
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
            },
            follow_redirects=True,
        )
        self.assert_rejected(response, "超过当前模型文本上限 50 字")
        self.assertIn("long.txt", response.get_data(as_text=True))

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
            for prefix in ("", "/admin"):
                with self.subTest(kind=kind, prefix=prefix):
                    limit = len(expected) - 1
                    self.assertGreater(limit, len(text_a) + len(text_b))
                    self.set_limit(limit)
                    self.assert_rejected(
                        self.pair(kind, prefix),
                        f"文档文本 {len(expected)} 字，超过当前模型文本上限 {limit} 字",
                    )
        self.set_limit(len(language_text))
        self.assertEqual(self.pair("language-consistency").status_code, 200)
        self.assertEqual(self.tasks()[0]["document_text"], language_text)
        self.set_limit(len(consistency_text))
        self.assertEqual(self.pair("consistency").status_code, 200)
        self.assertEqual(self.tasks()[1]["document_text"], consistency_text)

    def test_grouped_parse_failure_removes_all_uploaded_files(self):
        for kind in ("consistency", "language-consistency"):
            with self.subTest(kind=kind):
                response = self.pair(kind, text_b="   ")
                self.assert_rejected(response, "未能提取到可检查文本")
                self.assertIn("b.txt", response.get_data(as_text=True))

    def test_pdf_limit_uses_compact_input_and_preserves_full_text_and_metadata(self):
        extracted = "电压 10^{2} V\n" + normalized_pdf_table()
        source = "file: table.pdf\n\n" + extracted
        model_text = compact_model_document_text(source)
        self.assertLess(len(model_text), len(source))
        with patch(
            "app.tasks.runtime.preprocessing.extract_document",
            return_value=(
                extracted,
                [{"target": "https://example.test/guide", "text": "指南"}],
            ),
        ):
            self.set_limit(len(model_text) - 1)
            self.assert_rejected(
                self.document("pdf fixture", "table.pdf"),
                f"文档文本 {len(model_text)} 字，超过当前模型文本上限",
            )
            self.set_limit(len(model_text))
            self.assertEqual(self.document("pdf fixture", "table.pdf").status_code, 200)
        task = self.tasks()[0]
        self.assertEqual(task["document_text"], source)
        metadata = json.loads(task["document_meta_json"])
        self.assertEqual(
            metadata["text_semantics"]["script_marker_format"], "caret_brace_v1"
        )
        self.assertEqual(metadata["hyperlinks"][0]["source"], "table.pdf")

    def test_worker_reuses_preflight_text_for_each_document_task_type(self):
        self.document("有效正文")
        self.pair("consistency")
        self.pair("language-consistency")
        tasks = self.tasks()
        self.assertEqual(len(tasks), 3)
        with self.app.app_context():
            get_db().execute("UPDATE tasks SET status = 'running'")
            get_db().commit()
        with (
            patch(
                "app.tasks.runtime.preprocessing.extract_document",
                side_effect=AssertionError("重复提取"),
            ),
            patch(
                "app.tasks.runtime.preprocessing.extract_text",
                side_effect=AssertionError("重复提取"),
            ),
            patch(
                "app.tasks.runner.run_check",
                return_value='{"summary":"检查完成","items":[]}',
            ) as model,
        ):
            for task in tasks:
                TaskRunner(self.app).run(task["id"])
        for before, after in zip(tasks, self.tasks(), strict=True):
            self.assertEqual(after["status"], "completed", after["error"])
            self.assertEqual(after["document_text"], before["document_text"])
        self.assertEqual(
            {call.kwargs["document_text"] for call in model.call_args_list},
            {task["document_text"] for task in tasks},
        )

    def test_local_rule_only_upload_uses_full_text_above_model_limit(self):
        self.document_checks = self._configure_document_scheme(
            self.check_ids["common-terms"]
        )
        self.set_limit(20)
        text = "完整正文" * 100
        self.assertEqual(self.document(text).status_code, 200)
        self.assertEqual(self.tasks()[0]["document_text"], "file: doc.txt\n\n" + text)


if __name__ == "__main__":
    unittest.main()
