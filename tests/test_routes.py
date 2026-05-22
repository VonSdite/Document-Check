import json
import tempfile
import unittest
from pathlib import Path

from flask import Flask

from app.config import load_local_config
from app.db import get_setting, init_db, seed_defaults
from app.routes import _find_enabled_model, get_enabled_models, register_routes


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
        with self.app.app_context():
            init_db()
            seed_defaults()
        register_routes(self.app)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def tearDown(self):
        self.temp_dir.cleanup()

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


if __name__ == "__main__":
    unittest.main()
