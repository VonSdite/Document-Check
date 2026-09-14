"""使用真实进程验证阻塞解析、启动握手与监督器恢复。"""

import ctypes
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from app.infrastructure.runtime import create_task_app
from app.persistence.connection import get_db, now_text
from app.persistence.schema import init_db
from app.tasks import supervisor as supervisor_module
from app.tasks.processes import matching_process, process_is_running
from app.tasks.supervisor import TaskSupervisor, run_claimed_task, supervisor_state_path


def _run_blocked_task(task_id, claim_token, *, root_dir, start_signal):
    def blocked_preprocessing(*_args, **_kwargs):
        (Path(root_dir) / f"started-{task_id}").write_text("started", encoding="utf-8")
        # PyDLL 保留 GIL，模拟长时间阻塞 Python 线程的底层解析调用。
        if os.name == "nt":
            ctypes.PyDLL("kernel32").Sleep(10000)
        else:
            ctypes.PyDLL(None).sleep(10)
        return "测试文本", None

    with patch(
        "app.tasks.runner._prepare_task_inputs", side_effect=blocked_preprocessing
    ):
        run_claimed_task(
            task_id, claim_token, root_dir=root_dir, start_signal=start_signal
        )


def _run_crashable_supervisor(root_dir):
    app = create_task_app(Path(root_dir))
    with patch("app.tasks.supervisor.run_claimed_task", _run_blocked_task):
        TaskSupervisor(app).run(threading.Event())


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
        with patch("app.tasks.supervisor.run_claimed_task", _run_blocked_task):
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
            patch("app.tasks.supervisor.run_claimed_task", _run_blocked_task),
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
        reader, writer = multiprocessing.get_context("spawn").Pipe(duplex=False)
        writer.close()
        with patch("app.tasks.supervisor.TaskRunner") as runner:
            run_claimed_task(1, "claim", root_dir=self.root, start_signal=reader)
        runner.assert_not_called()

    def test_failed_identity_save_never_grants_execution_permission(self):
        task_id = self._insert_task()
        with (
            patch("app.tasks.supervisor.run_claimed_task", _run_blocked_task),
            patch(
                "app.tasks.supervisor._write_supervisor_state",
                side_effect=OSError("disk unavailable"),
            ),
        ):
            with self.assertRaises(OSError):
                self.supervisor.run_once()
        entry = self.supervisor._active[task_id]
        entry.process.join(timeout=5)
        self.assertFalse(entry.process.is_alive())
        self.assertFalse((self.root / f"started-{task_id}").exists())
        self.supervisor._reap_finished_processes()
        self.assertEqual(self._task(task_id)["status"], "queued")

    def test_restart_cleans_orphan_after_supervisor_is_killed(self):
        task_id = self._insert_task()
        parent = multiprocessing.get_context("spawn").Process(
            target=_run_crashable_supervisor, args=(str(self.root),)
        )
        orphan = None
        parent.start()
        try:
            self._wait(lambda: (self.root / f"started-{task_id}").exists())
            state = json.loads(
                supervisor_state_path(self.app).read_text(encoding="utf-8")
            )
            record = state["task_processes"][0]
            orphan = matching_process(record)
            self.assertIsNotNone(orphan)
            parent.kill()
            parent.join(timeout=5)
            self.assertFalse(parent.is_alive())
            self.assertTrue(process_is_running(orphan))
            self.supervisor._recover_previous_processes()
            self.assertFalse(process_is_running(orphan))
            self.assertEqual(self._task(task_id)["status"], "queued")
            self.assertIsNone(self._task(task_id)["claim_token"])
        finally:
            if parent.is_alive():
                parent.kill()
                parent.join(timeout=5)
            if orphan is not None and process_is_running(orphan):
                orphan.kill()
                psutil.wait_procs([orphan], timeout=3)


if __name__ == "__main__":
    unittest.main()
