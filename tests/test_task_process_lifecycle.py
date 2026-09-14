"""使用真实进程验证阻塞解析、启动握手与监督器恢复。"""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil
from openpyxl import Workbook

from app.infrastructure.runtime import create_task_app
from app.infrastructure.subprocesses import PROJECT_ROOT, python_module_command
from app.persistence.connection import get_db, now_text
from app.persistence.schema import init_db
from app.tasks import supervisor as supervisor_module
from app.tasks.processes import TaskWorkerProcess, matching_process, process_is_running
from app.tasks.supervisor import TaskSupervisor, supervisor_state_path
from tests.fixtures.task_process import blocked_task_command


class TaskProcessLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        with patch("app.infrastructure.runtime._configure_logging"):
            self.app = create_task_app(self.root)
        self.app.config["MAX_TASK_PROCESSES"] = 1
        self.context = self.app.app_context()
        self.context.push()
        init_db()
        self.supervisor = TaskSupervisor(self.app)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        with patch("app.tasks.supervisor.SUPERVISOR_SHUTDOWN_GRACE_SECONDS", 0):
            self.supervisor._shutdown_active_processes()
        self.context.pop()
        self.temp.cleanup()

    def _insert_task(self):
        now = now_text()
        cursor = get_db().execute(
            """INSERT INTO tasks (
                ip, owner_subject, original_filename, stored_filename, file_type,
                file_size, checks_json, model_name, api_base, status, created_at, updated_at
            ) VALUES ('127.0.0.1', 'ip:test', 'test.txt', 'test.txt', 'txt', 1,
                      '[]', 'test', 'http://example.invalid', 'queued', ?, ?)""",
            (now, now),
        )
        get_db().commit()
        return cursor.lastrowid

    def _launch_blocked_task(self):
        task_id = self._insert_task()
        with patch(
            "app.tasks.processes.python_module_command",
            side_effect=blocked_task_command,
        ):
            self.supervisor.run_once()
        self._wait(lambda: (self.root / f"started-{task_id}").exists())
        return task_id, self.supervisor._active[task_id]

    def _task(self, task_id):
        return (
            get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )

    def _wait(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(0.05)
        self.fail("等待进程状态超时")

    def test_native_gil_block_does_not_expire_or_duplicate_task_at_full_capacity(self):
        with (
            patch("app.tasks.runtime.state.TASK_LEASE_SECONDS", 1),
            patch("app.tasks.supervisor.TASK_LEASE_RENEW_INTERVAL_SECONDS", 0.1),
        ):
            task_id, entry = self._launch_blocked_task()
            initial_lease = self._task(task_id)["lease_expires_at"]
            queued_id = self._insert_task()
            until = time.monotonic() + 2.2
            while time.monotonic() < until:
                self.supervisor.run_once()
                self.assertIs(self.supervisor._active[task_id], entry)
                self.assertEqual(self._task(task_id)["claim_token"], entry.claim_token)
                time.sleep(0.05)
            self.assertTrue(entry.process.is_alive())
            self.assertGreater(self._task(task_id)["lease_expires_at"], initial_lease)
            self.assertEqual(self._task(queued_id)["status"], "queued")
            self.assertEqual(len(self.supervisor._active), 1)
            entry.startup_at -= 120
            self.supervisor.run_once()
            self.assertIsNone(entry.startup_error)
            self.assertTrue(entry.process.is_alive())

    def test_cancel_terminates_native_block_before_releasing_capacity(self):
        task_id, entry = self._launch_blocked_task()
        get_db().execute(
            "UPDATE tasks SET status = 'canceling', cancel_requested = 1 WHERE id = ?",
            (task_id,),
        )
        get_db().commit()
        with patch("app.tasks.supervisor.TASK_CANCEL_GRACE_SECONDS", 0.2):
            self.supervisor.run_once()
            self.assertTrue(entry.process.is_alive())
            self.assertEqual(self._task(task_id)["status"], "canceling")

            def canceled():
                self.supervisor.run_once()
                return self._task(task_id)["status"] == "canceled"

            self._wait(canceled)
        self.assertFalse(entry.process.is_alive())
        self.assertFalse(self.supervisor._active)
        self.assertIsNone(self._task(task_id)["claim_token"])

    def test_start_permission_requires_a_saved_process_identity(self):
        task_id = self._insert_task()
        real_write = supervisor_module._write_supervisor_state
        snapshots = []

        def inspect_before_save(app, state):
            self.assertFalse((self.root / f"started-{task_id}").exists())
            snapshots.append(state)
            real_write(app, state)

        with (
            patch(
                "app.tasks.processes.python_module_command",
                side_effect=blocked_task_command,
            ),
            patch(
                "app.tasks.supervisor._write_supervisor_state",
                side_effect=inspect_before_save,
            ),
        ):
            self.supervisor.run_once()
        self._wait(lambda: (self.root / f"started-{task_id}").exists())
        record = snapshots[0]["task_processes"][0]
        self.assertIsNotNone(record["create_time"])
        self.assertEqual(record["pid"], self.supervisor._active[task_id].process.pid)

    def test_closed_start_pipe_prevents_task_execution(self):
        task_id = self._insert_task()
        process = TaskWorkerProcess(task_id, "claim", root_dir=self.root)
        process.start()
        try:
            self._wait(lambda: process.phase == "waiting_permission")
            self.assertFalse(process.ready.is_set())
            process.close_start()
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            self.assertIsNone(self._task(task_id)["document_text"])
            self.assertEqual(self._task(task_id)["status"], "queued")
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)

    def test_independent_worker_preprocesses_same_named_wide_workbooks(self):
        task_id = self._insert_task()
        book = Workbook()
        book.active.append([f"参数{column}" for column in range(1, 513)])
        for row in range(10):
            book.active.append([row * column for column in range(1, 513)])
        groups = []
        for role, label in (("material", "素材文档"), ("data", "对照资料")):
            filename = f"{role}.xlsx"
            book.save(Path(self.app.config["UPLOAD_FOLDER"]) / filename)
            groups.append(
                {
                    "role": role,
                    "label": label,
                    "files": [
                        {
                            "stored_filename": filename,
                            "original_filename": "电网参数.xlsx",
                            "file_type": "xlsx",
                        }
                    ],
                }
            )
        book.close()
        get_db().execute(
            "UPDATE tasks SET task_type = 'consistency_check', document_meta_json = ?, max_input_chars = 500000 WHERE id = ?",
            (json.dumps({"groups": groups}), task_id),
        )
        get_db().commit()
        self.supervisor.run_once()
        process = self.supervisor._active[task_id].process
        process.join(timeout=15)
        self.assertFalse(process.is_alive())
        self.assertTrue(process.ready.is_set())
        task = self._task(task_id)
        self.assertIn("# 素材文档", task["document_text"])
        self.assertIn("# 对照资料", task["document_text"])
        self.assertEqual(task["document_text"].count("参数512"), 2)
        self.assertEqual(task["error"], "没有可执行的检查项")
        self.assertEqual(process.exitcode, 0)

    def test_failed_identity_save_never_grants_execution_permission(self):
        task_id = self._insert_task()
        with (
            patch(
                "app.tasks.processes.python_module_command",
                side_effect=blocked_task_command,
            ),
            patch(
                "app.tasks.supervisor._write_supervisor_state",
                side_effect=OSError("disk unavailable"),
            ),
        ):
            self.supervisor.run_once()
        entry = self.supervisor._active[task_id]
        entry.process.join(timeout=5)
        self.assertFalse(entry.process.is_alive())
        self.assertFalse((self.root / f"started-{task_id}").exists())
        self.supervisor._reap_finished_processes()
        self.assertEqual(self._task(task_id)["status"], "failed")
        self.assertIn("disk unavailable", self._task(task_id)["error"])

    def _launch_mode(self, mode):
        task_id = self._insert_task()

        def command_for_mode(module, *, root_dir=None):
            command, environment = python_module_command(
                "tests.fixtures.task_process", root_dir=root_dir
            )
            return [*command, mode], environment

        with patch(
            "app.tasks.processes.python_module_command", side_effect=command_for_mode
        ):
            self.supervisor.run_once()
        return task_id, self.supervisor._active[task_id]

    def test_stalled_bootstrap_and_initialization_are_stopped_before_failure(self):
        for mode, phase in (
            ("bootstrap-hang", "created"),
            ("initialization-hang", "initializing"),
        ):
            with self.subTest(mode=mode):
                task_id, entry = self._launch_mode(mode)
                if mode == "initialization-hang":
                    self._wait(lambda: entry.process.phase == phase)
                entry.startup_at -= 61
                self.supervisor.run_once()
                self.assertIn("启动超时", entry.startup_error)
                self.assertEqual(self._task(task_id)["status"], "running")
                self.assertIs(self.supervisor._active[task_id], entry)
                entry.process.join(timeout=5)
                self.assertFalse(entry.process.is_alive())
                self.supervisor.run_once()
                task = self._task(task_id)
                self.assertEqual(task["status"], "failed")
                self.assertIn(phase, task["error"])
                self.assertIsNone(task["claim_token"])
                self.assertFalse(self.supervisor._active)

    def test_early_worker_exit_reports_failure_without_respawning(self):
        task_id, entry = self._launch_mode("exit")
        entry.process.join(timeout=5)
        self.supervisor.run_once()
        self.assertEqual(self._task(task_id)["status"], "failed")
        self.assertIn("退出码=7", self._task(task_id)["error"])
        self.assertFalse(self.supervisor._active)

    def test_state_write_failure_stops_a_child_that_never_reads_permission(self):
        with patch(
            "app.tasks.supervisor._write_supervisor_state",
            side_effect=PermissionError("file locked"),
        ):
            task_id, entry = self._launch_mode("bootstrap-hang")
        entry.process.join(timeout=5)
        self.assertFalse(entry.process.is_alive())
        self.supervisor.run_once()
        self.assertEqual(self._task(task_id)["status"], "failed")
        self.assertIn("file locked", self._task(task_id)["error"])
        self.assertFalse(self.supervisor._active)

    def test_restart_cleans_orphan_after_supervisor_is_killed(self):
        task_id = self._insert_task()
        command, environment = python_module_command(
            "tests.fixtures.task_process", root_dir=self.root
        )
        parent = subprocess.Popen(
            [*command, "supervisor"], cwd=PROJECT_ROOT, env=environment
        )
        orphan = None
        try:
            self._wait(lambda: (self.root / f"started-{task_id}").exists())
            state = json.loads(
                supervisor_state_path(self.app).read_text(encoding="utf-8")
            )
            record = state["task_processes"][0]
            orphan = matching_process(record)
            self.assertIsNotNone(orphan)
            parent.kill()
            parent.wait(timeout=5)
            self.assertIsNotNone(parent.poll())
            self.assertTrue(process_is_running(orphan))
            self.supervisor._recover_previous_processes()
            self.assertFalse(process_is_running(orphan))
            self.assertEqual(self._task(task_id)["status"], "queued")
            self.assertIsNone(self._task(task_id)["claim_token"])
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            if orphan is not None and process_is_running(orphan):
                orphan.kill()
                psutil.wait_procs([orphan], timeout=3)


if __name__ == "__main__":
    unittest.main()
