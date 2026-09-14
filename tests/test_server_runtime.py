import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

from app.bootstrap.asgi import create_asgi_app
from app.bootstrap.server import serve_application
from app.bootstrap.supervisor import _stop_on_stdin_close
from app.infrastructure.runtime import _runtime_root_dir
from app.tasks.supervisor import (
    _parent_is_alive,
    supervisor_is_ready,
    supervisor_state_path,
)
from run import _start_task_supervisor, _stop_task_supervisor


class ServerRuntimeTest(unittest.TestCase):
    def test_entrypoint_import_does_not_load_gunicorn(self):
        source = """
import importlib.abc
import sys
class RejectGunicorn(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'gunicorn' or fullname.startswith('gunicorn.'):
            raise AssertionError('启动入口只依赖跨平台 Web 服务')
sys.meta_path.insert(0, RejectGunicorn())
import run
assert 'gunicorn' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uvicorn_uses_worker_factory_and_application_proxy_handling(self):
        app = Flask(__name__)
        root = Path(tempfile.gettempdir()).resolve() / "documentcheck-runtime"
        app.config.update(ROOT_DIR=root, WEB_THREADS=12, WEB_WORKERS=3)

        def check_root(*args, **kwargs):
            self.assertEqual(_runtime_root_dir(), root)

        with (
            patch("app.bootstrap.server.uvicorn.run", side_effect=check_root) as run,
            patch.dict(os.environ, {"DOCUMENTCHECK_ROOT_DIR": "previous-root"}),
        ):
            serve_application(app, "127.0.0.1", 12345)
            self.assertEqual(os.environ["DOCUMENTCHECK_ROOT_DIR"], "previous-root")
        run.assert_called_once_with(
            "app.bootstrap.asgi:create_asgi_app",
            factory=True,
            host="127.0.0.1",
            port=12345,
            workers=3,
            interface="asgi3",
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="off",
            access_log=False,
            proxy_headers=False,
        )

    def test_supervisor_inherits_runtime_directory(self):
        app = Flask(__name__)
        app.config["ROOT_DIR"] = Path(tempfile.gettempdir()).resolve()
        with (
            patch("run.subprocess.Popen") as start,
            patch("run.wait_for_supervisor", return_value=True),
        ):
            self.assertIs(_start_task_supervisor(app), start.return_value)
        options = start.call_args.kwargs
        self.assertEqual(
            options["env"]["DOCUMENTCHECK_ROOT_DIR"], str(app.config["ROOT_DIR"])
        )
        self.assertEqual(options["stdin"], subprocess.PIPE)

    def test_windows_venv_supervisor_uses_base_interpreter_and_venv_environment(self):
        app = Flask(__name__)
        app.config["ROOT_DIR"] = Path(tempfile.gettempdir()).resolve()
        venv_python = r"C:\文档检查\.venv\Scripts\python.exe"
        base_python = r"C:\Python312\python.exe"
        with (
            patch(
                "run.sys",
                SimpleNamespace(
                    platform="win32",
                    executable=venv_python,
                    _base_executable=base_python,
                ),
            ),
            patch("run.subprocess.Popen") as start,
            patch("run.wait_for_supervisor", return_value=True),
        ):
            _start_task_supervisor(app)
        self.assertEqual(start.call_args.args[0][0], base_python)
        self.assertEqual(
            start.call_args.kwargs["env"]["__PYVENV_LAUNCHER__"], venv_python
        )

    def test_supervisor_preserves_interpreter_on_linux_and_windows_base_python(self):
        app = Flask(__name__)
        app.config["ROOT_DIR"] = Path(tempfile.gettempdir()).resolve()
        for platform, executable, base in (
            ("linux", "/project/.venv/bin/python", "/usr/bin/python3"),
            ("win32", r"C:\Python312\python.exe", r"C:\Python312\python.exe"),
        ):
            with (
                self.subTest(platform=platform),
                patch(
                    "run.sys",
                    SimpleNamespace(
                        platform=platform, executable=executable, _base_executable=base
                    ),
                ),
                patch("run.subprocess.Popen") as start,
                patch("run.wait_for_supervisor", return_value=True),
            ):
                _start_task_supervisor(app)
            self.assertEqual(start.call_args.args[0][0], executable)

    def test_supervisor_start_failure_distinguishes_exit_from_timeout(self):
        app = Flask(__name__)
        app.config["ROOT_DIR"] = Path(tempfile.gettempdir()).resolve()
        for exit_code, message in ((7, "退出码=7"), (None, "等待就绪超时")):
            with (
                self.subTest(exit_code=exit_code),
                patch("run.subprocess.Popen") as start,
                patch("run.wait_for_supervisor", return_value=False),
                patch("run._stop_task_supervisor") as stop,
            ):
                start.return_value.poll.return_value = exit_code
                with self.assertRaisesRegex(RuntimeError, message):
                    _start_task_supervisor(app)
                stop.assert_called_once_with(start.return_value)

    def test_readiness_checks_process_existence_without_sending_signals(self):
        with tempfile.TemporaryDirectory() as root:
            app = Flask(__name__, instance_path=root)
            supervisor_state_path(app).write_text(
                json.dumps(
                    {"pid": 1234, "status": "running", "heartbeat_at": time.time()}
                ),
                encoding="utf-8",
            )
            with (
                patch(
                    "app.tasks.supervisor.psutil.pid_exists", return_value=True
                ) as exists,
                patch("os.kill", side_effect=AssertionError("存活检查应保持只读")),
            ):
                self.assertTrue(supervisor_is_ready(app))
            exists.assert_called_once_with(1234)
            with patch("app.tasks.supervisor.psutil.pid_exists", return_value=False):
                self.assertFalse(supervisor_is_ready(app))

    def test_parent_existence_is_checked_even_if_parent_pid_is_unchanged(self):
        with (
            patch("app.tasks.supervisor.os.getppid", return_value=1234),
            patch("app.tasks.supervisor.psutil.pid_exists", return_value=False),
        ):
            self.assertFalse(_parent_is_alive(1234))
        self.assertTrue(_parent_is_alive(None))

    def test_stdin_eof_requests_supervisor_shutdown(self):
        stopped = threading.Event()
        with (
            patch("app.bootstrap.supervisor.sys.stdin", Mock()),
            patch("app.bootstrap.supervisor.os.read", return_value=b""),
        ):
            _stop_on_stdin_close(stopped)
        self.assertTrue(stopped.is_set())

    def test_parent_closes_control_pipe_before_waiting_for_supervisor(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = lambda **kwargs: self.assertTrue(
            process.stdin.close.called
        )
        _stop_task_supervisor(process)
        process.stdin.close.assert_called_once()
        process.wait.assert_called_once()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_asgi_adapter_preserves_chunked_body_limits_and_proxy_metadata(self):
        app = Flask(__name__)
        app.config.update(WEB_THREADS=3, MAX_CONTENT_LENGTH=8)
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

        @app.post("/echo")
        def echo():
            return jsonify(
                body=request.get_data(as_text=True),
                query=request.args["q"],
                ip=request.remote_addr,
                scheme=request.scheme,
                host=request.host,
                prefix=request.script_root,
            )

        with patch("app.bootstrap.asgi.create_app", return_value=app):
            adapter = create_asgi_app()
        self.assertEqual(adapter.executor._max_workers, 3)

        async def exchange(body, *, content_length=None):
            messages = []
            chunks = iter(
                [
                    {"type": "http.request", "body": body[:2], "more_body": True},
                    {"type": "http.request", "body": body[2:], "more_body": False},
                ]
            )

            async def receive():
                return next(chunks)

            async def send(message):
                messages.append(message)

            scope = {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/echo",
                "query_string": b"q=%E4%B8%AD%E6%96%87",
                "server": ("127.0.0.1", 31945),
                "client": ("127.0.0.1", 50000),
                "headers": [
                    (b"host", b"localhost"),
                    (b"x-forwarded-for", b"192.0.2.3"),
                    (b"x-forwarded-proto", b"https"),
                    (b"x-forwarded-host", b"example.test"),
                    (b"x-forwarded-prefix", b"/infoCheck"),
                ],
            }
            if content_length is not None:
                scope["headers"].append(
                    (b"content-length", str(content_length).encode())
                )
            await adapter(scope, receive, send)
            status = next(
                m["status"] for m in messages if m["type"] == "http.response.start"
            )
            data = b"".join(
                m.get("body", b"")
                for m in messages
                if m["type"] == "http.response.body"
            )
            return status, data

        try:
            status, data = asyncio.run(exchange(b"abc"))
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(data),
                {
                    "body": "abc",
                    "query": "中文",
                    "ip": "192.0.2.3",
                    "scheme": "https",
                    "host": "example.test",
                    "prefix": "/infoCheck",
                },
            )
            self.assertEqual(
                asyncio.run(exchange(b"123456789", content_length=9))[0], 413
            )
        finally:
            adapter.executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
