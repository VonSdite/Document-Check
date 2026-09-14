"""在独立运行目录验证真实 Web 服务、任务进程与生命周期。"""

import concurrent.futures
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

import psutil
import requests
import yaml
from openpyxl import Workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ServerIntegrationTest(unittest.TestCase):
    def test_real_server_workers_upload_task_download_and_shutdown(self):
        for workers in (1, 2):
            with self.subTest(workers=workers), tempfile.TemporaryDirectory() as root:
                self._check_server(Path(root), workers)

    def _check_server(self, root, workers):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = {
            "platform": True,
            "secret_key": "isolated-server-test",
            "admin_url": "/test-console",
            "admin": {"username": "test-admin", "password": "test-password"},
            "server": {"host": "127.0.0.1", "port": port, "web_workers": workers},
            "auth": {
                "mode": "trusted_header",
                "trusted_header": {"user_id": "X-Test-User"},
            },
        }
        (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        environment = {k: v for k, v in os.environ.items() if k not in {"HOST", "PORT"}}
        environment["DOCUMENTCHECK_ROOT_DIR"] = str(root)
        environment["PYTHONIOENCODING"] = "utf-8"
        base_url = f"http://127.0.0.1:{port}"
        output_path = root / "server-output.log"
        with output_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                [sys.executable, str(PROJECT_ROOT / "run.py")],
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32"
                    else 0
                ),
                start_new_session=sys.platform != "win32",
            )
            descendants = []
            try:
                with requests.Session() as client:
                    client.trust_env = False
                    client.headers["X-Test-User"] = "integration"

                    def ready():
                        self.assertIsNone(
                            process.poll(), output_path.read_text(encoding="utf-8")
                        )
                        try:
                            return client.get(base_url + "/health/ready", timeout=1).ok
                        except requests.RequestException:
                            return False

                    self._wait_until(ready)
                    normalized = yaml.safe_load(
                        (root / "config.yaml").read_text(encoding="utf-8")
                    )
                    self.assertEqual(normalized["server"]["web_threads"], 16)
                    self.assertEqual(normalized["worker"]["max_task_processes"], 4)

                    self._exercise_workers(base_url, root, workers)
                    if workers > 1:
                        original_pids = self._web_pids(root)
                        psutil.Process(next(iter(original_pids))).terminate()
                        self._wait_until(
                            lambda: (
                                output_path.read_text(encoding="utf-8").count(
                                    "Started server process"
                                )
                                >= 3
                            )
                        )
                        self._exercise_workers(base_url, root, workers + 1)
                        self.assertTrue(self._web_pids(root) - original_pids)

                    login = client.post(
                        base_url + "/test-console/login",
                        data={"username": "test-admin", "password": "test-password"},
                        allow_redirects=False,
                        timeout=10,
                    )
                    self.assertEqual(login.status_code, 302)
                    self.assertIn("session", client.cookies)
                    self.assertEqual(
                        client.get(
                            base_url + "/test-console/tasks", timeout=10
                        ).status_code,
                        200,
                    )

                    check_id, model_id = self._seed_local_check(root)
                    document = "这里出现旧称，需要统一。".encode()
                    uploaded = client.post(
                        base_url + "/",
                        data={"checks": str(check_id), "model_id": model_id},
                        files={"document": ("中文文档.txt", document, "text/plain")},
                        allow_redirects=False,
                        timeout=10,
                    )
                    self.assertEqual(uploaded.status_code, 302)

                    def completed():
                        with closing(
                            sqlite3.connect(root / "instance/document_check.sqlite3")
                        ) as db:
                            db.row_factory = sqlite3.Row
                            task = db.execute(
                                "SELECT id, status, error, result_json FROM tasks ORDER BY id DESC LIMIT 1"
                            ).fetchone()
                        self.assertIsNotNone(task, "上传应创建任务")
                        self.assertNotEqual(task["status"], "failed", task["error"])
                        return task if task["status"] == "completed" else None

                    task = self._wait_until(completed)
                    self.assertIn("旧称", task["result_json"])
                    downloaded = client.get(
                        base_url + f"/tasks/{task['id']}/document", timeout=10
                    )
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertEqual(downloaded.content, document)
                    self.assertIn(
                        "attachment", downloaded.headers["Content-Disposition"]
                    )
                    task_log = (root / "instance/logs/task.log").read_text(
                        encoding="utf-8"
                    )
                    for stage in (
                        "任务进程已创建并发送启动许可",
                        "任务入口已进入",
                        "任务运行环境初始化",
                        "任务业务已就绪",
                    ):
                        self.assertIn(stage, task_log)

                descendants = psutil.Process(process.pid).children(recursive=True)
                process.send_signal(
                    signal.CTRL_BREAK_EVENT
                    if sys.platform == "win32"
                    else signal.SIGTERM
                )
                process.wait(timeout=30)
                self._wait_until(lambda: not self._alive(descendants), timeout=10)
                self.assertFalse((root / "instance/task-supervisor.json").exists())
                self.assertNotIn("Traceback", output_path.read_text(encoding="utf-8"))
            finally:
                if process.poll() is None:
                    descendants.extend(
                        psutil.Process(process.pid).children(recursive=True)
                    )
                    process.kill()
                    process.wait(timeout=5)
                for child in self._alive(descendants):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                psutil.wait_procs(descendants, timeout=5)

    def _exercise_workers(self, base_url, root, expected):
        def request_page(index):
            with requests.Session() as client:
                client.trust_env = False
                response = client.get(
                    base_url + "/",
                    headers={
                        "X-Test-User": "integration",
                        "X-Request-ID": f"worker-probe-{index}",
                    },
                    timeout=10,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.headers["X-Request-ID"], f"worker-probe-{index}"
                )

        def all_workers_served():
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
                list(executor.map(request_page, range(24)))
            return len(self._web_pids(root)) >= expected

        self._wait_until(all_workers_served)

    @staticmethod
    def _web_pids(root):
        return {
            int(match.group(1))
            for line in (root / "instance/logs/access.log")
            .read_text(encoding="utf-8")
            .splitlines()
            if "worker-probe-" in line and (match := re.search(r"pid=(\d+)", line))
        }

    @staticmethod
    def _seed_local_check(root):
        workbook = Workbook()
        workbook.active.append(["不规范用语", "规范用语"])
        workbook.active.append(["旧称", "新称"])
        workbook.save(root / "instance/sensitive_terms.xlsx")
        workbook.close()
        with closing(sqlite3.connect(root / "instance/document_check.sqlite3")) as db:
            check_id = db.execute(
                "SELECT id FROM check_items WHERE code = 'sensitive-terms'"
            ).fetchone()[0]
            provider_id = db.execute(
                "INSERT INTO user_model_providers(owner_subject, name, api_base, created_at, updated_at) VALUES (?, '本地检查', 'http://127.0.0.1:1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                ("trusted_header:integration",),
            ).lastrowid
            db.execute(
                "INSERT INTO user_model_configs(provider_id, model_name, created_at, updated_at) VALUES (?, 'local-check', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (provider_id,),
            )
            db.commit()
        return check_id, f"{provider_id}:local-check"

    @staticmethod
    def _alive(processes):
        alive = []
        for process in processes:
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    alive.append(process)
            except psutil.NoSuchProcess:
                pass
        return alive

    def _wait_until(self, check, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result := check():
                return result
            time.sleep(0.1)
        self.fail("真实服务未在期限内达到预期状态")


if __name__ == "__main__":
    unittest.main()
