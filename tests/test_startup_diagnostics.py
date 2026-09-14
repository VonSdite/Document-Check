"""在独立进程验证初始化失败的退出状态、文件诊断与控制台输出。"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = (
    ("import runpy; runpy.run_path('run.py', run_name='__main__')", "app.log"),
    (
        "import runpy; runpy.run_module('app.bootstrap.supervisor', run_name='__main__')",
        "task.log",
    ),
    (
        "import runpy; sys.argv = ['task', '1']; "
        "runpy.run_module('app.bootstrap.task', run_name='__main__')",
        "task.log",
    ),
    (
        "from app.bootstrap.asgi import create_asgi_app; create_asgi_app()",
        "app.log",
    ),
)


class StartupDiagnosticsTest(unittest.TestCase):
    def _run(self, root, code, *, missing_dependency=None):
        source = "import sys\n"
        if missing_dependency:
            source += f"""
import importlib.abc
class MissingDependency(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == {missing_dependency!r}:
            raise ModuleNotFoundError('dependency unavailable: ' + fullname)
sys.meta_path.insert(0, MissingDependency())
"""
        source += code
        return subprocess.run(
            [sys.executable, "-c", source],
            cwd=PROJECT_ROOT,
            env={
                **os.environ,
                "DOCUMENTCHECK_ROOT_DIR": str(root),
                "PYTHONIOENCODING": "utf-8",
            },
            input='{"start":true,"claim_token":"diagnostics-test"}\n',
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )

    def test_all_entrypoints_record_dependency_import_failures(self):
        for code, filename in ENTRYPOINTS:
            with self.subTest(entrypoint=code), tempfile.TemporaryDirectory() as root:
                result = self._run(root, code, missing_dependency="flask")
                self.assertEqual(result.returncode, 1, result.stderr)
                content = (Path(root) / "instance/logs" / filename).read_text(
                    encoding="utf-8"
                )
                for output in (content, result.stderr):
                    self.assertIn(
                        "ModuleNotFoundError: dependency unavailable: flask", output
                    )
                    self.assertEqual(output.count("进程异常退出"), 1)
                    self.assertEqual(
                        output.count("Traceback (most recent call last)"), 1
                    )
                self.assertNotIn("进程异常退出", result.stdout)

    def test_invalid_config_is_recorded_before_application_logging_is_ready(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "config.yaml").write_text("server: [", encoding="utf-8")
            result = self._run(root, ENTRYPOINTS[0][0])
            self.assertEqual(result.returncode, 1, result.stderr)
            content = (Path(root) / "instance/logs/app.log").read_text(encoding="utf-8")
            self.assertIn("yaml.parser.ParserError", content)
            self.assertIn("进程异常退出", result.stderr)
            self.assertEqual(
                (Path(root) / "config.yaml").read_text(encoding="utf-8"), "server: ["
            )

    def test_missing_logging_dependency_uses_process_specific_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            result = self._run(
                root, ENTRYPOINTS[0][0], missing_dependency="concurrent_log_handler"
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            files = list((Path(root) / "instance/logs").glob("startup-*.log"))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")
            self.assertIn("dependency unavailable: concurrent_log_handler", content)
            self.assertEqual(result.stderr.count("进程异常退出"), 1)

    def test_runtime_failure_keeps_existing_task_log_routing(self):
        with tempfile.TemporaryDirectory() as root:
            result = self._run(
                root,
                """
from app.infrastructure.runtime import create_task_app
from app.bootstrap.diagnostics import run_entrypoint
create_task_app()
run_entrypoint(lambda: 1 / 0, logger_name='app.tasks.process')
""",
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            logs = Path(root) / "instance/logs"
            content = (logs / "task.log").read_text(encoding="utf-8")
            self.assertEqual(content.count("进程异常退出"), 1)
            self.assertIn("ZeroDivisionError", content)
            self.assertNotIn(
                "ZeroDivisionError", (logs / "app.log").read_text(encoding="utf-8")
            )

    def test_unwritable_log_directory_preserves_original_stderr_error(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "instance").mkdir()
            (Path(root) / "instance/logs").write_text("occupied", encoding="utf-8")
            result = self._run(
                root,
                """
from app.bootstrap.diagnostics import run_entrypoint
def fail():
    raise RuntimeError('original failure')
run_entrypoint(fail)
""",
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("RuntimeError: original failure", result.stderr)
            self.assertEqual(result.stderr.count("进程异常退出"), 1)


if __name__ == "__main__":
    unittest.main()
