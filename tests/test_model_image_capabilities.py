import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from app.models import client as llm
from app.models.image_capabilities import (
    image_capabilities,
    image_model_error,
    image_model_signature,
    image_test_challenge,
    record_image_capability,
)
from app.models.service import _find_enabled_model, get_enabled_models
from app.persistence.connection import get_db
from app.persistence.schema import init_db
from app.tasks.retries import (
    CheckRetryError,
    TaskRetryError,
    request_check_retry,
    request_task_retry,
)
from app.tasks.supervisor import TaskSupervisor
from tests import test_routes as routes
from tests.test_llm import FakeResponse, FakeSession


class ModelImageCapabilityTest(unittest.TestCase):
    setUp = routes.AdminSettingsRouteTest.setUp
    tearDown = routes.AdminSettingsRouteTest.tearDown
    _configure_provider = routes.AdminSettingsRouteTest._configure_provider

    def model(self, model_id):
        return _find_enabled_model(model_id, "ip:127.0.0.1")

    def submit(self, task_type, model_id):
        with self.app.app_context():
            check_id = (
                get_db()
                .execute(
                    "SELECT id FROM check_items WHERE task_type=? AND enabled=1 LIMIT 1",
                    (task_type,),
                )
                .fetchone()[0]
            )
        image = task_type == "image_check"
        return self.client.post(
            "/images" if image else "/videos",
            data={
                "model_id": model_id,
                "checks": str(check_id),
                "document" if image else "video": (
                    io.BytesIO(b"test upload"),
                    "test.pdf" if image else "test.mp4",
                ),
            },
            content_type="multipart/form-data",
        )

    def test_actual_image_response_required_and_text_test_keeps_unknown(self):
        model_id = self._configure_provider(image_verified=False)
        challenge = image_test_challenge()
        with self.app.app_context():
            model = self.model(model_id)
        for reply, verified in (
            ("OK", False),
            (challenge[1], True),
            (f"图片中的数字是 {challenge[1]}。", True),
            ("I cannot view images", False),
        ):
            with self.subTest(reply=reply):
                llm._reset_http_session_pools()
                session = FakeSession(
                    [FakeResponse(data={"choices": [{"message": {"content": reply}}]})]
                )
                with (
                    patch.object(llm.requests, "Session", return_value=session),
                    patch(
                        "app.web.models.image_test_challenge", return_value=challenge
                    ),
                ):
                    response = self.client.post("/models/test-image", json=model)
                self.assertEqual(response.status_code, 200 if verified else 400)
                self.assertEqual(len(session.calls), 1)
                payload = session.calls[0][1]["json"]
                content = payload["messages"][0]["content"]
                self.assertNotIn(challenge[1], content[0]["text"])
                self.assertEqual(content[1]["image_url"]["url"], challenge[0])
                with self.app.app_context():
                    self.assertEqual(
                        not image_model_error(model, "ip:127.0.0.1"), verified
                    )
        with patch(
            "app.web.models.test_model_connection", return_value="模型连通性测试通过。"
        ):
            self.assertEqual(
                self.client.post("/models/test", json=model).status_code, 200
            )
        with self.app.app_context():
            self.assertTrue(image_model_error(model, "ip:127.0.0.1"))
            self.assertEqual(
                image_capabilities("ip:127.0.0.1")[image_model_signature(model)],
                "failed",
            )
        llm._reset_http_session_pools()

    def test_failed_or_malformed_probe_never_authorizes_image_tasks(self):
        model_id = self._configure_provider(image_verified=False)
        with self.app.app_context():
            model = self.model(model_id)
        responses = (
            FakeResponse(data={"choices": []}),
            FakeResponse(data={"error": {"message": "image input unsupported"}}),
            FakeResponse(data=["unexpected"]),
            FakeResponse(status_code=400, text="image input unsupported"),
        )
        for response in responses:
            llm._reset_http_session_pools()
            with patch.object(
                llm.requests, "Session", return_value=FakeSession([response])
            ):
                result = self.client.post("/models/test-image", json=model)
            self.assertEqual(result.status_code, 400)
            with self.app.app_context():
                self.assertTrue(image_model_error(model, "ip:127.0.0.1"))
        llm._reset_http_session_pools()

    def test_verification_is_bound_to_owner_endpoint_key_and_model_configuration(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            model = self.model(model_id)
            self.assertFalse(image_model_error(model, "ip:127.0.0.1"))
            self.assertTrue(image_model_error(model, "ip:other"))
            for field, value in (
                ("api_base", "https://other.test/v1/chat/completions"),
                ("api_key", "different-key"),
                ("model_name", "different-model"),
                ("force_disable_thinking", True),
                ("reasoning_effort", "high"),
            ):
                with self.subTest(field=field):
                    self.assertTrue(
                        image_model_error({**model, field: value}, "ip:127.0.0.1")
                    )
            self.assertFalse(
                image_model_error({**model, "request_timeout": 900}, "ip:127.0.0.1")
            )

    def test_schema_initialization_preserves_verification(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            model = self.model(model_id)
            init_db()
            self.assertFalse(image_model_error(model, "ip:127.0.0.1"))

    def test_saving_forged_capability_does_not_authorize_a_model(self):
        model_id = self._configure_provider(image_verified=False)
        with self.app.app_context():
            model = self.model(model_id)
        response = self.client.post(
            "/models",
            data={
                **model,
                "name": "测试提供商",
                "is_active": "on",
                "model_configs": json.dumps(
                    [{"model_name": "model-a", "image_input_status": "verified"}]
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(self.model(model_id))
            self.assertTrue(image_model_error(self.model(model_id), "ip:127.0.0.1"))
            self.assertEqual(
                get_enabled_models("ip:127.0.0.1", task_type="video_check"), []
            )

    def test_invalid_stored_endpoint_keeps_settings_accessible_and_blocks_media(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            get_db().execute("UPDATE user_model_providers SET api_base='invalid-url'")
            get_db().commit()
            self.assertTrue(image_model_error(self.model(model_id), "ip:127.0.0.1"))
        for route in ("/models", "/images", "/videos"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("可检查图片和视频", response.text)
        with patch("app.tasks.submission._save_uploaded_file") as save:
            self.submit("image_check", model_id)
        save.assert_not_called()

    def test_unknown_models_are_hidden_and_post_is_rejected_before_file_save(self):
        model_id = self._configure_provider(image_verified=False)
        for task_type, route in (
            ("image_check", "/images"),
            ("video_check", "/videos"),
        ):
            with self.subTest(task_type=task_type):
                soup = BeautifulSoup(
                    self.client.get(route).get_data(as_text=True), "html.parser"
                )
                self.assertIsNone(
                    soup.select_one(
                        f'select[name="model_id"] option[value="{model_id}"]'
                    )
                )
                self.assertIsNotNone(
                    soup.select_one(
                        'form[data-require-image-model] button[type="submit"][disabled]'
                    )
                )
                self.assertIn("图片识别", soup.get_text())
                with patch("app.tasks.submission._save_uploaded_file") as save:
                    response = self.submit(task_type, model_id)
                self.assertEqual(response.status_code, 302)
                save.assert_not_called()
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0
            )
            self.assertEqual(len(get_enabled_models("ip:127.0.0.1")), 1)
        self.assertEqual(list(Path(self.app.config["UPLOAD_FOLDER"]).rglob("*")), [])

    def test_verified_models_submit_but_changed_credentials_block_new_tasks(self):
        model_id = self._configure_provider()
        for task_type in ("image_check", "video_check"):
            self.assertEqual(self.submit(task_type, model_id).status_code, 302)
        with self.app.app_context():
            self.assertEqual(
                get_db()
                .execute("SELECT COUNT(*) FROM tasks WHERE status='queued'")
                .fetchone()[0],
                2,
            )
            get_db().execute("UPDATE user_model_providers SET api_key='changed-key'")
            get_db().commit()
            self.assertEqual(
                get_enabled_models("ip:127.0.0.1", task_type="image_check"), []
            )
        with patch("app.tasks.submission._save_uploaded_file") as save:
            self.submit("video_check", model_id)
        save.assert_not_called()

    def test_scheduler_rejects_unverified_snapshot_without_claiming_a_slot_and_retry_rechecks(
        self,
    ):
        model_id = self._configure_provider()
        self.submit("video_check", model_id)
        with self.app.app_context():
            model = self.model(model_id)
            record_image_capability("ip:127.0.0.1", model, verified=False)
            supervisor = TaskSupervisor(self.app)
            self.assertEqual(supervisor._claim_available_tasks(), [])
            task = get_db().execute("SELECT * FROM tasks").fetchone()
            self.assertEqual(task["status"], "failed")
            self.assertIsNone(task["started_at"])
            self.assertIsNone(task["claim_token"])
            self.assertFalse(supervisor._active)
            with self.assertRaisesRegex(TaskRetryError, "测试图片识别"):
                request_task_retry(task["id"])
            code = json.loads(task["checks_snapshot_json"])[0]["code"]
            with self.assertRaisesRegex(CheckRetryError, "测试图片识别"):
                request_check_retry(task["id"], code, 0)
            record_image_capability("ip:127.0.0.1", model, verified=True)
            self.assertEqual(request_task_retry(task["id"]), 1)
            self.assertEqual(len(supervisor._claim_available_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
