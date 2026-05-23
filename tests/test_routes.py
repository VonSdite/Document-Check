import io
import json
import tempfile
import unittest
from pathlib import Path

from flask import Flask

from app.config import load_local_config, save_local_config
from app.db import get_db, get_setting, init_db, seed_defaults
from app.routes import _find_enabled_model, _upload_destination, get_enabled_models, register_routes
from app.task_types import CONSISTENCY_TASK_TYPE


class AdminSettingsRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root_dir = Path(self.temp_dir.name)
        project_root = Path(__file__).resolve().parents[1]
        self.app = Flask(
            __name__,
            template_folder=str(project_root / "app" / "templates"),
            static_folder=str(project_root / "app" / "static"),
        )
        self.app.config.update(
            SECRET_KEY="test-secret",
            ADMIN_URL="/admin",
            ROOT_DIR=root_dir,
            DATABASE=str(root_dir / "test.sqlite3"),
            UPLOAD_FOLDER=str(root_dir / "uploads"),
        )
        Path(self.app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
        with self.app.app_context():
            init_db()
            seed_defaults()
        register_routes(self.app)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def tearDown(self):
        self.temp_dir.cleanup()

    def _configure_provider(self):
        root_dir = Path(self.app.config["ROOT_DIR"])
        config = load_local_config(root_dir)
        config["providers"] = [
            {
                "id": "provider-1",
                "name": "测试提供商",
                "api_base": "https://example.test/v1/chat/completions",
                "api_key": "",
                "proxy_mode": "direct",
                "proxy": "",
                "ssl_verify": False,
                "request_timeout": 30,
                "max_input_chars": 80000,
                "is_active": True,
                "models": [{"model_name": "model-a", "force_disable_thinking": False}],
            }
        ]
        save_local_config(root_dir, config)

    def test_diagnostics_fetch_returns_saved_state(self):
        response = self.client.post(
            "/admin/settings",
            data={"action": "diagnostics", "llm_stream_trace_enabled": "on"},
            headers={"X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"llm_stream_trace_enabled": True})
        with self.app.app_context():
            self.assertTrue(get_setting("llm_stream_trace_enabled"))

    def test_diagnostics_fetch_can_disable_setting(self):
        self.client.post(
            "/admin/settings",
            data={"action": "diagnostics", "llm_stream_trace_enabled": "on"},
            headers={"X-Requested-With": "fetch"},
        )

        response = self.client.post(
            "/admin/settings",
            data={"action": "diagnostics", "llm_stream_trace_enabled": "off"},
            headers={"X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"llm_stream_trace_enabled": False})
        with self.app.app_context():
            self.assertFalse(get_setting("llm_stream_trace_enabled"))

    def test_diagnostics_accept_json_returns_saved_state(self):
        response = self.client.post(
            "/admin/settings",
            data={"action": "diagnostics", "llm_stream_trace_enabled": "on"},
            headers={"Accept": "application/json"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"llm_stream_trace_enabled": True})

    def test_admin_settings_shows_document_and_consistency_prompt_groups(self):
        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("文档检查-提示词设置", html)
        self.assertIn("跨文档一致性检查-提示词设置", html)
        self.assertIn("consistency_check", html)

    def test_admin_models_saves_model_force_disable_thinking(self):
        response = self.client.post(
            "/admin/models",
            data={
                "name": "测试提供商",
                "api_base": "https://example.test/v1/chat/completions",
                "api_key": "",
                "proxy_mode": "direct",
                "request_timeout": "30",
                "max_input_chars": "80000",
                "is_active": "on",
                "model_configs": json.dumps(
                    [
                        {"model_name": "model-a", "force_disable_thinking": True},
                        {"model_name": "model-b", "force_disable_thinking": False},
                    ],
                    ensure_ascii=False,
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        config = load_local_config(self.app.config["ROOT_DIR"])
        self.assertEqual(
            config["providers"][0]["models"],
            [
                {"model_name": "model-a", "force_disable_thinking": True},
                {"model_name": "model-b", "force_disable_thinking": False},
            ],
        )

    def test_admin_models_accepts_million_char_input_limit(self):
        response = self.client.post(
            "/admin/models",
            data={
                "name": "测试提供商",
                "api_base": "https://example.test/v1/chat/completions",
                "api_key": "",
                "proxy_mode": "direct",
                "request_timeout": "30",
                "max_input_chars": "1000000",
                "is_active": "on",
                "model_configs": json.dumps(
                    [{"model_name": "model-a", "force_disable_thinking": False}],
                    ensure_ascii=False,
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        config = load_local_config(self.app.config["ROOT_DIR"])
        self.assertEqual(config["providers"][0]["max_input_chars"], 1000000)

    def test_admin_models_allows_same_name_for_distinct_thinking_modes(self):
        response = self.client.post(
            "/admin/models",
            data={
                "name": "测试提供商",
                "api_base": "https://example.test/v1/chat/completions",
                "api_key": "",
                "proxy_mode": "direct",
                "request_timeout": "30",
                "max_input_chars": "80000",
                "is_active": "on",
                "model_configs": json.dumps(
                    [
                        {"model_name": "same-model", "force_disable_thinking": False},
                        {"model_name": "same-model", "force_disable_thinking": True},
                        {"model_name": "same-model", "force_disable_thinking": False},
                    ],
                    ensure_ascii=False,
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        config = load_local_config(self.app.config["ROOT_DIR"])
        self.assertEqual(
            config["providers"][0]["models"],
            [
                {"model_name": "same-model", "force_disable_thinking": False},
                {"model_name": "same-model", "force_disable_thinking": True},
            ],
        )

        with self.app.app_context():
            models = get_enabled_models()
            self.assertEqual(len(models), 2)
            self.assertEqual(len({model["id"] for model in models}), 2)
            by_mode = {model["force_disable_thinking"]: model for model in models}
            self.assertFalse(_find_enabled_model(by_mode[False]["id"])["force_disable_thinking"])
            self.assertTrue(_find_enabled_model(by_mode[True]["id"])["force_disable_thinking"])

    def test_admin_settings_creates_consistency_check_item(self):
        response = self.client.post(
            "/admin/settings",
            data={
                "action": "create_check_item",
                "task_type": CONSISTENCY_TASK_TYPE,
                "name": "遗漏内容检查",
                "description": "检查资料是否遗漏素材关键内容",
                "prompt": "只检查资料遗漏。",
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            item = get_db().execute(
                """
                SELECT task_type, code, name, description, prompt, enabled
                FROM check_items
                WHERE name = ?
                """,
                ("遗漏内容检查",),
            ).fetchone()
        self.assertEqual(item["task_type"], CONSISTENCY_TASK_TYPE)
        self.assertTrue(item["code"].startswith("custom-consistency-"))
        self.assertEqual(item["description"], "检查资料是否遗漏素材关键内容")
        self.assertEqual(item["prompt"], "只检查资料遗漏。")
        self.assertEqual(item["enabled"], 1)

    def test_upload_destination_uses_unique_name_for_same_second_uploads(self):
        with self.app.app_context():
            first_name, _ = _upload_destination("报告.txt", "127.0.0.1", "2026-05-22 12:00:00", "txt")
            second_name, _ = _upload_destination("报告.txt", "127.0.0.1", "2026-05-22 12:00:00", "txt")

        self.assertNotEqual(first_name, second_name)
        self.assertTrue(first_name.endswith(".txt"))
        self.assertTrue(second_name.endswith(".txt"))

    def test_create_task_rejects_disabled_check_item_before_saving_file(self):
        self._configure_provider()
        with self.app.app_context():
            item = get_db().execute("SELECT id FROM check_items WHERE code = 'typo'").fetchone()
            get_db().execute("UPDATE check_items SET enabled = 0 WHERE id = ?", (item["id"],))
            get_db().commit()

        response = self.client.post(
            "/",
            data={
                "document": (io.BytesIO("测试文档".encode("utf-8")), "doc.txt"),
                "checks": [str(item["id"])],
                "model_id": "provider-1:0:model-a",
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            total = get_db().execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"]
        self.assertEqual(total, 0)
        self.assertEqual(list(Path(self.app.config["UPLOAD_FOLDER"]).iterdir()), [])

    def test_create_task_saves_check_snapshot_and_extracted_text(self):
        self._configure_provider()
        with self.app.app_context():
            item = get_db().execute("SELECT id, code, name, prompt FROM check_items WHERE code = 'typo'").fetchone()

        response = self.client.post(
            "/",
            data={
                "document": (io.BytesIO("测试文档".encode("utf-8")), "doc.txt"),
                "checks": [str(item["id"])],
                "model_id": "provider-1:0:model-a",
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = get_db().execute("SELECT * FROM tasks").fetchone()
        snapshots = json.loads(task["checks_snapshot_json"])
        self.assertEqual(task["document_text"], "file: doc.txt\n\n测试文档")
        self.assertEqual(
            snapshots,
            [
                {
                    "id": item["id"],
                    "code": item["code"],
                    "name": item["name"],
                    "prompt": item["prompt"],
                }
            ],
        )

    def test_create_consistency_task_saves_combined_document_text(self):
        self._configure_provider()
        with self.app.app_context():
            item = get_db().execute(
                "SELECT id, code, name, prompt FROM check_items WHERE code = 'consistency-cross-document'"
            ).fetchone()

        response = self.client.post(
            "/consistency",
            data={
                "master_documents": (io.BytesIO("素材参数 10A".encode("utf-8")), "master.txt"),
                "related_documents": (io.BytesIO("资料参数 12A".encode("utf-8")), "related.txt"),
                "checks": [str(item["id"])],
                "model_id": "provider-1:0:model-a",
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = get_db().execute("SELECT task_type, document_text, checks_snapshot_json FROM tasks").fetchone()
        self.assertEqual(task["task_type"], "consistency_check")
        self.assertIn("## 素材文档1：master.txt", task["document_text"])
        self.assertIn("## 资料1：related.txt", task["document_text"])
        self.assertEqual(
            json.loads(task["checks_snapshot_json"]),
            [
                {
                    "id": item["id"],
                    "code": item["code"],
                    "name": item["name"],
                    "prompt": item["prompt"],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
