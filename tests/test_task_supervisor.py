import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil
from flask import Flask

from app.persistence.connection import get_db, now_text
from app.persistence.schema import init_db
from app.persistence.settings import set_setting
from app.tasks.supervisor import (
    SUPERVISOR_HEARTBEAT_STALE_SECONDS,
    TaskSupervisor,
    _write_supervisor_state,
    supervisor_is_ready,
    supervisor_state_path,
)


class _FakeProcess:
    next_pid = 1000

    def __init__(self, task_id, claim_token, *, root_dir=None):
        self.args = (task_id, claim_token)
        self.ready = threading.Event()
        self.phase = "created"
        self.permitted = False
        self.pid = None
        self.exitcode = None
        self._alive = False

    def start(self):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self._alive = True

    def allow_start(self):
        self.permitted = True
        self.ready.set()
        self.phase = "ready"

    def close_start(self):
        pass

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        return None

    def terminate(self):
        self._alive = False
        self.exitcode = -15

    def kill(self):
        self._alive = False
        self.exitcode = -9


class TaskSupervisorTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.app = Flask(__name__, instance_path=str(root / "instance"))
        Path(self.app.instance_path).mkdir(parents=True)
        self.app.config.update(
            DATABASE=str(root / "instance" / "test.sqlite3"),
            UPLOAD_FOLDER=str(root / "instance" / "uploads"),
            IMAGE_FOLDER=str(root / "instance" / "images"),
            MAX_TASK_PROCESSES=4,
            WEB_WORKERS=2,
            WEB_THREADS=16,
        )
        Path(self.app.config["UPLOAD_FOLDER"]).mkdir()
        Path(self.app.config["IMAGE_FOLDER"]).mkdir()
        self.identity_patch = patch(
            "app.tasks.supervisor.process_identity", return_value=1.0
        )
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        self.context = self.app.app_context()
        self.context.push()
        init_db()

    def tearDown(self):
        self.context.pop()
        self.temp_dir.cleanup()

    def test_supervisor_heartbeat_must_be_fresh_and_process_alive(self):
        path = supervisor_state_path(self.app)
        path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "status": "running",
                    "heartbeat_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(supervisor_is_ready(self.app))
        self.assertFalse(
            supervisor_is_ready(
                self.app,
                now=time.time() + SUPERVISOR_HEARTBEAT_STALE_SECONDS + 1,
            )
        )

    def test_supervisor_only_claims_available_process_slots(self):
        for owner in ("ip:1", "ip:2", "ip:3"):
            self._insert_task(owner)
        set_setting("global_concurrency", 4)
        set_setting("user_concurrency", 1)
        supervisor = TaskSupervisor(self.app, process_factory=_FakeProcess)

        claimed = supervisor._claim_available_tasks(max_claims=2)

        self.assertEqual(len(claimed), 2)
        running = (
            get_db()
            .execute("SELECT COUNT(*) AS total FROM tasks WHERE status = 'running'")
            .fetchone()["total"]
        )
        self.assertEqual(running, 2)

    def test_state_replace_retries_transient_permission_errors(self):
        path = supervisor_state_path(self.app)
        path.write_text('{"status":"previous"}', encoding="utf-8")
        replace = os.replace
        attempts = []

        def locked_twice(source, target):
            attempts.append(source)
            if len(attempts) < 3:
                raise PermissionError(13, "file locked")
            replace(source, target)

        with (
            patch("app.tasks.supervisor.os.replace", side_effect=locked_twice),
            patch("app.tasks.supervisor.time.sleep") as sleep,
        ):
            _write_supervisor_state(self.app, {"status": "running"})
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")), {"status": "running"}
        )
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(list(path.parent.glob(".*.tmp")))

    def test_persistent_state_lock_preserves_previous_snapshot_and_cleans_temp(self):
        path = supervisor_state_path(self.app)
        path.write_text('{"status":"previous"}', encoding="utf-8")
        with (
            patch(
                "app.tasks.supervisor.os.replace",
                side_effect=PermissionError(13, "file locked"),
            ) as replace,
            patch("app.tasks.supervisor.time.sleep"),
        ):
            with self.assertRaises(PermissionError):
                _write_supervisor_state(self.app, {"status": "running"})
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")), {"status": "previous"}
        )
        self.assertFalse(list(path.parent.glob(".*.tmp")))

    def test_shutdown_terminates_process_even_when_state_writes_fail(self):
        supervisor, task_id, entry = self._launch_one()
        with (
            patch(
                "app.tasks.supervisor._write_supervisor_state",
                side_effect=PermissionError("file locked"),
            ),
            patch("app.tasks.supervisor.SUPERVISOR_SHUTDOWN_GRACE_SECONDS", 0),
            patch(
                "app.tasks.supervisor.psutil.Process",
                side_effect=psutil.NoSuchProcess(entry.process.pid),
            ),
        ):
            supervisor._shutdown_active_processes()
        self.assertFalse(entry.process.is_alive())
        self.assertFalse(supervisor._active)
        self.assertEqual(self._task(task_id)["status"], "queued")

    def test_failed_launch_is_terminated_on_following_loops(self):
        task_id = self._insert_task("ip:1")
        supervisor = TaskSupervisor(self.app, process_factory=_FakeProcess)
        with (
            patch(
                "app.tasks.supervisor._write_supervisor_state",
                side_effect=PermissionError("file locked"),
            ),
            patch(
                "app.tasks.supervisor.psutil.Process",
                side_effect=psutil.NoSuchProcess(1),
            ),
            patch.object(_FakeProcess, "terminate"),
            patch.object(_FakeProcess, "kill") as kill,
            patch("app.tasks.supervisor.TASK_TERMINATE_GRACE_SECONDS", 0),
        ):
            supervisor.run_once()
            entry = supervisor._active[task_id]
            self.assertFalse(entry.process.permitted)
            supervisor.run_once()
            self.assertTrue(kill.called)
            self.assertEqual(self._task(task_id)["status"], "running")
        entry.process.kill()
        supervisor._reap_finished_processes()
        self.assertEqual(self._task(task_id)["status"], "failed")

    def test_cancellation_wins_over_startup_timeout(self):
        supervisor, task_id, entry = self._launch_one()
        entry.process.ready.clear()
        entry.startup_error = "任务进程启动超时"
        get_db().execute(
            "UPDATE tasks SET status = 'canceling', cancel_requested = 1 WHERE id = ?",
            (task_id,),
        )
        get_db().commit()
        entry.process.terminate()
        supervisor._reap_finished_processes()
        self.assertEqual(self._task(task_id)["status"], "canceled")

    def test_supervisor_launches_one_process_for_each_claimed_task(self):
        supervisor = TaskSupervisor(self.app, process_factory=_FakeProcess)
        claims = [(11, "claim-11"), (12, "claim-12")]

        with patch.object(supervisor, "_claim_available_tasks", return_value=claims):
            supervisor._launch_available_tasks()

        self.assertEqual(set(supervisor._active), {11, 12})
        self.assertEqual(
            [entry.process.args for entry in supervisor._active.values()],
            [(11, "claim-11"), (12, "claim-12")],
        )

    def _task(self, task_id):
        return (
            get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )

    def _launch_one(self, *, capacity=4):
        self.app.config["MAX_TASK_PROCESSES"] = capacity
        task_id = self._insert_task("ip:1")
        supervisor = TaskSupervisor(self.app, process_factory=_FakeProcess)
        supervisor._launch_available_tasks()
        return supervisor, task_id, supervisor._active[task_id]

    def test_live_expired_task_is_never_reclaimed_or_overwritten(self):
        supervisor, task_id, entry = self._launch_one()
        for _ in range(6):
            get_db().execute(
                "UPDATE tasks SET lease_expires_at = '2000-01-01' WHERE id = ?",
                (task_id,),
            )
            get_db().commit()
            supervisor._launch_available_tasks()
            self.assertIs(supervisor._active[task_id], entry)
            self.assertEqual(self._task(task_id)["claim_token"], entry.claim_token)
        self.assertTrue(entry.process.is_alive())
        self.assertEqual(len(supervisor._active), 1)

    def test_supervisor_renews_at_full_capacity_without_worker_heartbeat(self):
        supervisor, task_id, entry = self._launch_one(capacity=1)
        get_db().execute(
            "UPDATE tasks SET lease_expires_at = '2000-01-01' WHERE id = ?", (task_id,)
        )
        get_db().commit()
        supervisor.run_once()
        self.assertGreater(self._task(task_id)["lease_expires_at"], now_text())
        self.assertIs(supervisor._active[task_id], entry)

    def test_completed_but_alive_process_keeps_global_and_user_capacity(self):
        supervisor, task_id, entry = self._launch_one()
        queued_id = self._insert_task("ip:1")
        get_db().execute(
            "UPDATE tasks SET status = 'completed', claim_token = NULL WHERE id = ?",
            (task_id,),
        )
        get_db().commit()
        set_setting("global_concurrency", 1)
        self.assertEqual(supervisor._claim_available_tasks(), [])
        set_setting("global_concurrency", 4)
        self.assertEqual(supervisor._claim_available_tasks(), [])
        entry.process.terminate()
        supervisor._reap_finished_processes()
        self.assertEqual(
            [task for task, _ in supervisor._claim_available_tasks()], [queued_id]
        )

    def test_queued_task_with_an_old_live_process_cannot_launch_again(self):
        supervisor, task_id, entry = self._launch_one()
        get_db().execute(
            "UPDATE tasks SET status = 'queued', claim_token = NULL WHERE id = ?",
            (task_id,),
        )
        get_db().commit()
        set_setting("user_concurrency", 4)
        self.assertEqual(supervisor._claim_available_tasks(), [])
        self.assertIs(supervisor._active[task_id], entry)

    def test_canceling_expired_live_task_waits_for_confirmed_exit(self):
        supervisor, task_id, entry = self._launch_one()
        get_db().execute(
            "UPDATE tasks SET status = 'canceling', cancel_requested = 1, lease_expires_at = '2000-01-01' WHERE id = ?",
            (task_id,),
        )
        get_db().commit()
        self.assertEqual(supervisor._claim_available_tasks(), [])
        self.assertEqual(self._task(task_id)["status"], "canceling")
        entry.process.terminate()
        supervisor._reap_finished_processes()
        self.assertEqual(self._task(task_id)["status"], "canceled")
        self.assertFalse(supervisor._active)

    def test_abnormal_exit_recovers_only_after_process_exits(self):
        supervisor, task_id, entry = self._launch_one()
        entry.process.terminate()
        supervisor._reap_finished_processes()
        self.assertEqual(self._task(task_id)["status"], "queued")
        self.assertFalse(supervisor._active)

    def test_failed_recovery_retains_the_process_record(self):
        supervisor, task_id, entry = self._launch_one()
        entry.process.terminate()
        with patch(
            "app.tasks.supervisor._recover_owned_task",
            side_effect=RuntimeError("database busy"),
        ):
            with self.assertRaises(RuntimeError):
                supervisor._reap_finished_processes()
        self.assertIs(supervisor._active[task_id], entry)
        supervisor._reap_finished_processes()
        self.assertEqual(self._task(task_id)["status"], "queued")

    def test_defensive_launch_guard_preserves_old_process(self):
        supervisor, task_id, entry = self._launch_one()
        with patch.object(
            supervisor,
            "_claim_available_tasks",
            return_value=[(task_id, "unexpected-claim")],
        ):
            supervisor._launch_available_tasks()
        self.assertIs(supervisor._active[task_id], entry)

    def test_unconfirmed_termination_preserves_task_and_process_capacity(self):
        supervisor, task_id, entry = self._launch_one(capacity=1)
        get_db().execute(
            "UPDATE tasks SET status = 'canceling', cancel_requested = 1 WHERE id = ?",
            (task_id,),
        )
        get_db().commit()
        with (
            patch(
                "app.tasks.supervisor.psutil.Process",
                side_effect=psutil.NoSuchProcess(entry.process.pid),
            ),
            patch("app.tasks.supervisor.TASK_CANCEL_GRACE_SECONDS", 0),
            patch("app.tasks.supervisor.TASK_TERMINATE_GRACE_SECONDS", 0),
            patch.object(entry.process, "terminate"),
            patch.object(entry.process, "kill") as kill,
        ):
            for _ in range(3):
                supervisor.run_once()
        self.assertTrue(kill.called)
        self.assertIs(supervisor._active[task_id], entry)
        self.assertEqual(self._task(task_id)["status"], "canceling")
        self.assertTrue(entry.process.is_alive())

    def test_reused_pid_is_not_terminated_on_recovery(self):
        task_id = self._insert_task("ip:1")
        record = {
            "pid": os.getpid(),
            "create_time": psutil.Process().create_time() - 1,
            "task_id": task_id,
            "claim_token": "old",
        }
        supervisor_state_path(self.app).write_text(
            json.dumps({"task_processes": [record]})
        )
        with patch("app.tasks.supervisor.signal_processes") as signal:
            TaskSupervisor(self.app)._recover_previous_processes()
        self.assertTrue(all(not call.args[0] for call in signal.call_args_list))

    def _insert_task(self, owner: str) -> int:
        now = now_text()
        inserted = get_db().execute(
            """
            INSERT INTO tasks(
                ip, owner_subject, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', ?, 'task.txt', 'task.txt', 'txt', 1,
                '[]', 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'queued', 0, ?, ?
            )
            """,
            (owner, now, now),
        )
        get_db().commit()
        return inserted.lastrowid


if __name__ == "__main__":
    unittest.main()
