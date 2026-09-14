import io
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from flask import Flask

from app.infrastructure.logging import _configure_logging
from app.web.observability import configure_access_logging


class LoggingTest(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.logs_dir = Path(temp_dir.name) / "logs"
        self.console = io.StringIO()
        for name in ("app", "werkzeug", "app.tasks", "app.models"):
            target = logging.getLogger(name)
            previous = (target.handlers[:], target.level, target.propagate)
            target.handlers = []
            self.addCleanup(self._restore_logger, target, previous)

        self.app = Flask("app", root_path=temp_dir.name)
        self.app.config.update(
            LOG_FILE=str(self.logs_dir / "app.log"),
            TASK_LOG_FILE=str(self.logs_dir / "task.log"),
            LLM_LOG_FILE=str(self.logs_dir / "llm.log"),
            ACCESS_LOG_FILE=str(self.logs_dir / "access.log"),
        )
        with redirect_stderr(self.console):
            _configure_logging(self.app)
            configure_access_logging(self.app)
        self.access_logger = self.app.extensions["access_logger"]
        self.addCleanup(
            self._restore_logger,
            self.access_logger,
            ([], logging.NOTSET, True),
        )

    @staticmethod
    def _restore_logger(target, previous):
        for handler in target.handlers[:]:
            target.removeHandler(handler)
            handler.close()
        target.handlers, target.level, target.propagate = previous

    def _read_log(self, filename):
        path = self.logs_dir / filename
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_events_are_written_once_to_their_file_and_console(self):
        cases = (
            ("app", "app.log"),
            ("app.web.auth", "app.log"),
            ("app.reporting.excel", "app.log"),
            ("werkzeug", "app.log"),
            ("app.tasks.submission", "task.log"),
            ("app.tasks.supervisor", "task.log"),
            ("app.tasks.runner", "task.log"),
            ("app.tasks.runtime.image_checks", "task.log"),
            ("app.tasks.runtime.video_checks", "task.log"),
            ("app.tasks.runtime.artifacts", "task.log"),
            ("app.models.client", "llm.log"),
            (self.access_logger.name, "access.log"),
        )
        for index, (name, expected_file) in enumerate(cases):
            with self.subTest(logger=name):
                marker = f"日志分流验证-{index:02d}-结束"
                logging.getLogger(name).info(
                    "%s task_id=42 request_id=trace-42", marker
                )
                for filename in ("app.log", "task.log", "llm.log", "access.log"):
                    content = self._read_log(filename)
                    self.assertEqual(
                        content.count(marker), int(filename == expected_file)
                    )
                content = self._read_log(expected_file)
                label = "access" if name == self.access_logger.name else name
                self.assertIn(f"[{label}] pid={os.getpid()}", content)
                self.assertIn("task_id=42 request_id=trace-42", content)
                self.assertEqual(self.console.getvalue().count(marker), 1)

    def test_task_exception_keeps_traceback_in_task_log(self):
        try:
            raise ValueError("任务异常验证")
        except ValueError:
            logging.getLogger("app.tasks.runner").exception("执行失败 task_id=42")

        task_log = self._read_log("task.log")
        self.assertIn("执行失败 task_id=42", task_log)
        self.assertIn("Traceback (most recent call last)", task_log)
        self.assertIn("ValueError: 任务异常验证", task_log)
        self.assertNotIn("任务异常验证", self._read_log("app.log"))
        self.assertNotIn("任务异常验证", self._read_log("llm.log"))

    def test_repeated_configuration_does_not_duplicate_events(self):
        with redirect_stderr(self.console):
            _configure_logging(self.app)
            configure_access_logging(self.app)
        for name, filename in (
            ("app.web.auth", "app.log"),
            ("app.tasks.runner", "task.log"),
            ("app.models.client", "llm.log"),
            (self.access_logger.name, "access.log"),
        ):
            marker = f"重复配置验证-{filename}"
            logging.getLogger(name).warning(marker)
            self.assertEqual(self._read_log(filename).count(marker), 1)
            self.assertEqual(self.console.getvalue().count(marker), 1)

    def test_debug_messages_are_filtered(self):
        for name in ("app.web.auth", "app.tasks.runner", "app.models.client"):
            logging.getLogger(name).debug("调试过滤验证")
        for filename in ("app.log", "task.log", "llm.log"):
            self.assertNotIn("调试过滤验证", self._read_log(filename))
        self.assertNotIn("调试过滤验证", self.console.getvalue())

    def test_model_log_rotates_independently_of_task_log(self):
        logging.getLogger("app.tasks.runner").info("保留的任务记录 task_id=42")
        task_log = self._read_log("task.log")
        app_log = self._read_log("app.log")
        for handler in logging.getLogger("app.models").handlers:
            if isinstance(handler, logging.FileHandler):
                handler.maxBytes = 512
        for index in range(30):
            logging.getLogger("app.models.client").info(
                "流式定位轮转验证 frame=%s raw=%s", index, "x" * 200
            )

        self.assertIn("frame=29", self._read_log("llm.log"))
        self.assertTrue((self.logs_dir / "llm.log.1").exists())
        self.assertTrue((self.logs_dir / "llm.log.2").exists())
        self.assertFalse((self.logs_dir / "llm.log.3").exists())
        self.assertEqual(self._read_log("task.log"), task_log)
        self.assertEqual(self._read_log("app.log"), app_log)


if __name__ == "__main__":
    unittest.main()
