import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.persistence.connection import get_db, now_text
from app.persistence.schema import init_db
from app.persistence.settings import set_setting
from app.tasks.supervisor import (
    SUPERVISOR_HEARTBEAT_STALE_SECONDS,
    TaskSupervisor,
    supervisor_is_ready,
    supervisor_state_path,
)


class _FakeProcess:
    next_pid = 1000

    def __init__(self, *, target, args, name):
        self.target = target
        self.args = args
        self.name = name
        self.pid = None
        self.exitcode = None
        self._alive = False

    def start(self):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self._alive = True

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        return None


class _FakeProcessContext:
    def Process(self, *, target, args, name):
        return _FakeProcess(target=target, args=args, name=name)


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
        supervisor = TaskSupervisor(self.app, process_context=_FakeProcessContext())

        claimed = supervisor._claim_available_tasks(max_claims=2)

        self.assertEqual(len(claimed), 2)
        running = (
            get_db()
            .execute("SELECT COUNT(*) AS total FROM tasks WHERE status = 'running'")
            .fetchone()["total"]
        )
        self.assertEqual(running, 2)

    def test_supervisor_launches_one_process_for_each_claimed_task(self):
        supervisor = TaskSupervisor(self.app, process_context=_FakeProcessContext())
        claims = [(11, "claim-11"), (12, "claim-12")]

        with patch.object(supervisor, "_claim_available_tasks", return_value=claims):
            supervisor._launch_available_tasks()

        self.assertEqual(set(supervisor._active), {11, 12})
        self.assertEqual(
            [entry[0].args for entry in supervisor._active.values()],
            [(11, "claim-11"), (12, "claim-12")],
        )

    def _insert_task(self, owner: str) -> None:
        now = now_text()
        get_db().execute(
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


if __name__ == "__main__":
    unittest.main()
