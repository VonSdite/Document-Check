import ast
import json
import multiprocessing
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bootstrap.factory import create_app
from app.persistence.connection import get_db

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
MODULE_DEPENDENCIES = {
    "contracts": set(),
    "persistence": {"contracts"},
    "infrastructure": {"persistence"},
    "identity": {"persistence"},
    "documents": set(),
    "checks": {"contracts", "persistence"},
    "models": {"checks", "contracts", "persistence"},
    "reporting": {"checks", "contracts", "persistence"},
    "tasks": {
        "checks",
        "contracts",
        "documents",
        "identity",
        "infrastructure",
        "models",
        "persistence",
        "reporting",
    },
    "web": {
        "checks",
        "contracts",
        "documents",
        "identity",
        "infrastructure",
        "models",
        "persistence",
        "reporting",
        "tasks",
    },
    "bootstrap": {"infrastructure", "persistence", "tasks", "web"},
}


class ModuleArchitectureTest(unittest.TestCase):
    def test_app_root_contains_only_module_directories(self):
        entries = list((PROJECT_ROOT / "app").iterdir())
        self.assertTrue(entries)
        self.assertTrue(all(path.is_dir() for path in entries))

    def test_module_dependencies_follow_service_boundaries(self):
        for path in (PROJECT_ROOT / "app").rglob("*.py"):
            module = path.relative_to(PROJECT_ROOT / "app").parts[0]
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                targets = []
                if isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, str(path))
                    targets = [node.module or ""]
                    if node.module == "flask" and module in {
                        "contracts",
                        "persistence",
                        "checks",
                        "models",
                        "reporting",
                        "tasks",
                        "documents",
                    }:
                        self.assertFalse(
                            {alias.name for alias in node.names}
                            & {
                                "request",
                                "session",
                                "flash",
                                "redirect",
                                "url_for",
                                "render_template",
                                "send_file",
                                "abort",
                                "Response",
                            },
                            str(path),
                        )
                elif isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                for target in targets:
                    if not target.startswith("app."):
                        continue
                    dependency = target.split(".")[1]
                    if dependency != module:
                        self.assertIn(
                            dependency, MODULE_DEPENDENCIES[module], str(path)
                        )

    def test_worker_imports_without_loading_http_routes(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import app.tasks.supervisor; import app.reporting.statistics; "
                    "assert not any(n == 'app.web' or n.startswith('app.web.') for n in sys.modules)"
                ),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_spawned_task_process_uses_its_runtime_directory(self):
        from app.infrastructure.runtime import create_task_app
        from app.persistence.schema import init_db
        from app.tasks.supervisor import run_claimed_task

        with (
            tempfile.TemporaryDirectory() as root,
            patch("app.infrastructure.runtime._configure_logging"),
        ):
            app = create_task_app(Path(root))
            (Path(app.config["UPLOAD_FOLDER"]) / "example.txt").write_text(
                "独立任务进程读取测试文档。", encoding="utf-8"
            )
            with app.app_context():
                init_db()
                get_db().execute(
                    """
                    INSERT INTO tasks (
                        ip, owner_subject, original_filename, stored_filename, file_type,
                        file_size, checks_json, model_name, api_base, status, claim_token,
                        created_at, updated_at
                    ) VALUES ('127.0.0.1', 'ip:127.0.0.1', 'example.txt', 'example.txt',
                              'txt', 1, '[]', 'example', 'http://example.invalid',
                              'running', 'claim-test', '2026-09-01', '2026-09-01')
                    """
                )
                get_db().commit()
            process = multiprocessing.get_context("spawn").Process(
                target=run_claimed_task,
                args=(1, "claim-test"),
                kwargs={"root_dir": Path(root)},
            )
            process.start()
            try:
                process.join(timeout=15)
                self.assertFalse(process.is_alive(), "任务进程应完成并退出")
                self.assertEqual(process.exitcode, 0)
                with app.app_context():
                    task = (
                        get_db()
                        .execute("SELECT status, error FROM tasks WHERE id = 1")
                        .fetchone()
                    )
                    self.assertEqual(task["status"], "failed")
                    self.assertEqual(task["error"], "没有可执行的检查项")
            finally:
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
                process.close()

    def test_application_preserves_routes_and_database_schema(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch("app.infrastructure.runtime._configure_logging"),
            patch("app.bootstrap.factory.configure_access_logging"),
        ):
            app = create_app(Path(root))
            routes = sorted(
                [rule.rule, rule.endpoint, sorted(rule.methods)]
                for rule in app.url_map.iter_rules()
            )
            self.assertEqual(
                routes, json.loads((FIXTURES / "http_routes.json").read_text())
            )
            self.assertEqual(Path(app.static_folder), PROJECT_ROOT / "app/web/static")
            self.assertEqual(app.config["ROOT_DIR"], Path(root))
            with app.app_context():
                schema = [
                    list(row)
                    for row in get_db().execute(
                        "SELECT type, name, tbl_name, sql FROM sqlite_master "
                        "WHERE sql IS NOT NULL ORDER BY type, name"
                    )
                ]
                self.assertEqual(
                    schema, json.loads((FIXTURES / "database_schema.json").read_text())
                )
