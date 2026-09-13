import logging
import tempfile
import unittest
from pathlib import Path

from flask import Flask

from app.db import init_db
from app.observability import (
    configure_access_logging,
    log_startup_self_check,
    register_observability,
)


class _FakeSupervisorProbe:
    def __init__(self, alive: bool = True):
        self.alive = alive

    def __call__(self) -> bool:
        return self.alive


class ObservabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root_dir = Path(self.temp_dir.name)
        instance_dir = root_dir / "instance"
        uploads_dir = instance_dir / "uploads"
        images_dir = instance_dir / "extracted_images"
        logs_dir = instance_dir / "logs"
        for path in (uploads_dir, images_dir, logs_dir):
            path.mkdir(parents=True, exist_ok=True)

        self.access_log_file = logs_dir / "access.log"
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            DATABASE=str(instance_dir / "test.sqlite3"),
            UPLOAD_FOLDER=str(uploads_dir),
            IMAGE_FOLDER=str(images_dir),
            LOG_FILE=str(logs_dir / "app.log"),
            ACCESS_LOG_FILE=str(self.access_log_file),
            PLATFORM=True,
            LISTEN_HOST="0.0.0.0",
            LISTEN_PORT=31945,
            APPLICATION_ROOT="/infoCheck",
            PROXY_FIX=True,
        )
        with self.app.app_context():
            init_db()
        self.supervisor_probe = _FakeSupervisorProbe()
        self.app.extensions["task_supervisor_probe"] = self.supervisor_probe
        configure_access_logging(self.app)
        register_observability(self.app)

        @self.app.get("/echo")
        def echo():
            return {"ok": True}

        @self.app.get("/boom")
        def boom():
            raise RuntimeError("test failure")

        self.client = self.app.test_client()

    def tearDown(self):
        access_logger = self.app.extensions["access_logger"]
        for handler in list(access_logger.handlers):
            if isinstance(handler, logging.FileHandler):
                handler.flush()
                handler.close()
                access_logger.removeHandler(handler)
        self.temp_dir.cleanup()

    def test_request_lifecycle_uses_gateway_request_id(self):
        response = self.client.get(
            "/echo?token=must-not-be-logged",
            headers={"X-Request-ID": "gateway-request-123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Request-ID"], "gateway-request-123")
        log_text = self._access_log_text()
        self.assertIn('"event":"request_start"', log_text)
        self.assertIn('"event":"request_end"', log_text)
        self.assertIn('"request_id":"gateway-request-123"', log_text)
        self.assertIn('"path":"/echo"', log_text)
        self.assertNotIn("must-not-be-logged", log_text)

    def test_invalid_request_id_is_replaced(self):
        response = self.client.get(
            "/echo", headers={"X-Request-ID": "invalid request id"}
        )

        request_id = response.headers["X-Request-ID"]
        self.assertEqual(len(request_id), 32)
        self.assertNotEqual(request_id, "invalid request id")
        int(request_id, 16)

    def test_request_error_is_recorded_with_request_id(self):
        with self.assertRaises(RuntimeError):
            self.client.get("/boom", headers={"X-Request-ID": "failed-request-1"})

        log_text = self._access_log_text()
        self.assertIn('"event":"request_start"', log_text)
        self.assertIn('"event":"request_error"', log_text)
        self.assertIn('"request_id":"failed-request-1"', log_text)
        self.assertIn('"error_type":"RuntimeError"', log_text)

    def test_health_checks_are_not_written_to_access_log(self):
        live_response = self.client.get("/health/live")
        ready_response = self.client.get("/health/ready")

        self.assertEqual(live_response.status_code, 200)
        self.assertEqual(live_response.get_json(), {"status": "ok"})
        self.assertEqual(ready_response.status_code, 200)
        self.assertEqual(ready_response.get_json()["status"], "ready")
        self.assertEqual(self._access_log_text(), "")

    def test_readiness_fails_when_scheduler_is_not_alive(self):
        self.supervisor_probe.alive = False

        with self.assertLogs(self.app.logger.name, level="WARNING") as captured:
            response = self.client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["status"], "not_ready")
        self.assertEqual(response.get_json()["checks"]["scheduler"], "error")
        self.assertIn("就绪状态变化 status=not_ready", "\n".join(captured.output))

    def test_startup_self_check_records_runtime_configuration(self):
        with self.assertLogs(self.app.logger.name, level="INFO") as captured:
            log_startup_self_check(self.app)

        log_text = "\n".join(captured.output)
        self.assertIn("启动自检 status=ok", log_text)
        self.assertIn("host=0.0.0.0", log_text)
        self.assertIn("port=31945", log_text)
        self.assertIn("url_prefix=/infoCheck", log_text)
        self.assertIn('"scheduler":"ok"', log_text)

    def _access_log_text(self) -> str:
        access_logger = self.app.extensions["access_logger"]
        for handler in access_logger.handlers:
            handler.flush()
        if not self.access_log_file.exists():
            return ""
        return self.access_log_file.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
