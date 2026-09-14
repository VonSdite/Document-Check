import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Thread
from unittest.mock import patch

from flask import Flask
from openpyxl import Workbook

from app.checks.common_terms import COMMON_TERMS_CHECK_CODE
from app.checks.hyperlinks import HYPERLINK_CHECK_CODE
from app.checks.sensitive_terms import SENSITIVE_TERMS_CHECK_CODE
from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.models.client import LLMError
from app.persistence.connection import get_db, now_text
from app.persistence.schema import init_db
from app.persistence.settings import set_setting
from app.tasks.runner import (
    TaskRunner,
    _document_check_items,
    _run_check_items_concurrently,
)
from app.tasks.runtime.artifacts import (
    cleanup_expired_task_files,
    cleanup_task_file_cache,
    task_file_cache_snapshot,
)
from app.tasks.runtime.common import _merge_check_results
from app.tasks.runtime.image_checks import _image_check_target
from app.tasks.runtime.multimodal_common import (
    _check_item_groups,
    _document_text_for_image_batch,
    _format_image_check_issue_summary,
)
from app.tasks.runtime.multimodal_protocol import (
    _run_combined_multimodal_check_with_repair,
    _split_combined_check_output,
    _split_combined_structured_output,
)
from app.tasks.runtime.state import _mark_failed, _save_intermediate_results
from app.tasks.runtime.video_checks import _merge_video_batch_reports
from app.tasks.supervisor import TaskSupervisor


class TaskExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(Path(self.temp_dir.name) / "test.sqlite3")
        self.app.config["UPLOAD_FOLDER"] = str(Path(self.temp_dir.name) / "uploads")
        self.app.config["IMAGE_FOLDER"] = str(Path(self.temp_dir.name) / "images")
        self.app.config["NETWORK"] = {
            "proxy_mode": "direct",
            "proxy": "",
            "ssl_verify": False,
        }
        Path(self.app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
        Path(self.app.config["IMAGE_FOLDER"]).mkdir(parents=True, exist_ok=True)
        self.context = self.app.app_context()
        self.context.push()
        init_db()

    def tearDown(self):
        self.context.pop()
        self.temp_dir.cleanup()

    def test_single_check_cancel_keeps_other_check_running_and_retains_results(self):
        from app.tasks.activity import request_check_cancellation, task_activities

        checks = [
            {"id": 101, "code": "first", "name": "第一项", "prompt": "检查"},
            {"id": 102, "code": "second", "name": "第二项", "prompt": "检查"},
        ]
        task_id = self._insert_running_document_task(checks)
        set_setting("check_item_concurrency", 2)
        started = Barrier(3)
        canceled = Event()
        release_second = Event()
        failures = []
        events = {}

        def model(**kwargs):
            name = kwargs["check_name"]
            events[name] = kwargs["cancel_event"]
            kwargs["on_activity"]("thinking", 1)
            started.wait(timeout=5)
            if name == "第一项":
                self.assertTrue(kwargs["cancel_event"].wait(5))
                canceled.set()
                kwargs["check_canceled"]()
                self.fail("canceled check resumed")
            self.assertTrue(release_second.wait(5))
            self.assertFalse(kwargs["cancel_event"].is_set())
            return "第二项检查完成"

        def run():
            try:
                TaskRunner(self.app).run(task_id)
            except BaseException as exc:
                failures.append(exc)

        with patch("app.tasks.runner.run_check", side_effect=model):
            thread = Thread(target=run)
            thread.start()
            try:
                started.wait(timeout=5)
                self.assertTrue(request_check_cancellation(task_id, None, "first"))
                self.assertTrue(canceled.wait(5))
                self.assertFalse(events["第二项"].is_set())
            finally:
                release_second.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        task = (
            get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )
        self.assertEqual(task["status"], "partial")
        results = json.loads(task["result_json"])
        self.assertTrue(results[0]["canceled"])
        self.assertEqual(results[1]["result"], "第二项检查完成")
        self.assertIsNone(task["api_key"])
        self.assertEqual(task_activities([task_id]), {})
        self.assertIsNone(
            get_db()
            .execute(
                "SELECT 1 FROM settings WHERE key = ?", (f"task_activity:{task_id}",)
            )
            .fetchone()
        )

    def test_cancel_all_checks_finishes_canceled_with_result_explanations(self):
        from app.tasks.activity import request_check_cancellation

        task_id = self._insert_running_document_task(
            [{"id": 101, "code": "only", "name": "唯一项", "prompt": "检查"}]
        )

        def model(**kwargs):
            kwargs["on_activity"]("output", 1)
            self.assertTrue(request_check_cancellation(task_id, None, "only"))
            return "已接收的部分结果"

        with patch("app.tasks.runner.run_check", side_effect=model):
            TaskRunner(self.app).run(task_id)
        task = (
            get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )
        self.assertEqual(task["status"], "canceled")
        self.assertTrue(json.loads(task["result_json"])[0]["canceled"])
        self.assertIsNone(task["api_key"])

    def test_check_cancellation_is_fenced_and_survives_late_stage_updates(self):
        from app.tasks.activity import (
            finish_check_activity,
            initialize_activity,
            request_check_cancellation,
            task_activities,
            update_check_activity,
        )

        task_id = self._insert_scheduler_task(status="running", claim_token="current")
        checks = [{"code": "demo", "name": "演示"}]
        initialize_activity(task_id, "current", phase="checking", checks=checks)
        update_check_activity(task_id, "current", ["demo"], "thinking", 1)
        self.assertFalse(request_check_cancellation(task_id, "old", "demo"))
        self.assertFalse(request_check_cancellation(task_id, None, "demo"))
        self.assertTrue(request_check_cancellation(task_id, "current", "demo"))
        update_check_activity(task_id, "current", ["demo"], "output", 1)
        self.assertEqual(
            task_activities([task_id])[task_id]["checks"]["demo"]["phase"], "canceling"
        )
        self.assertTrue(finish_check_activity(task_id, "current", "demo"))
        self.assertFalse(request_check_cancellation(task_id, "current", "demo"))
        self.assertEqual(
            task_activities([task_id])[task_id]["checks"]["demo"]["phase"], "canceled"
        )
        get_db().execute("UPDATE tasks SET claim_token='new' WHERE id=?", (task_id,))
        get_db().commit()
        self.assertEqual(task_activities([task_id]), {})
        initialize_activity(task_id, "new", phase="checking", checks=checks)
        self.assertNotIn(
            "cancel_requested", task_activities([task_id])[task_id]["checks"]["demo"]
        )
        finish_check_activity(task_id, "new", "demo")
        self.assertFalse(request_check_cancellation(task_id, "new", "demo"))

    def test_queued_check_cancellation_survives_claim_and_skips_model(self):
        from app.tasks.activity import request_check_cancellation, task_activities

        checks = [
            {"id": 101, "code": "first", "name": "第一项", "prompt": "检查"},
            {"id": 102, "code": "second", "name": "第二项", "prompt": "检查"},
        ]
        task_id = self._insert_running_document_task(checks)
        get_db().execute("UPDATE tasks SET status='queued' WHERE id=?", (task_id,))
        get_db().commit()
        self.assertTrue(request_check_cancellation(task_id, None, "first"))
        supervisor = TaskSupervisor(self.app)
        self.assertEqual(supervisor._claim_available_tasks(max_claims=0), [])
        self.assertTrue(
            task_activities([task_id])[task_id]["checks"]["first"]["cancel_requested"]
        )
        [(claimed_id, token)] = supervisor._claim_available_tasks()
        self.assertEqual(claimed_id, task_id)
        self.assertEqual(task_activities([task_id])[task_id]["claim_token"], token)
        self.assertFalse(request_check_cancellation(task_id, None, "second"))
        with patch("app.tasks.runner.run_check", return_value="第二项完成") as model:
            TaskRunner(self.app).run(task_id, token)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(model.call_args.kwargs["check_name"], "第二项")
        task = get_db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "partial")
        results = json.loads(task["result_json"])
        self.assertTrue(results[0]["canceled"])
        self.assertEqual(results[1]["result"], "第二项完成")

    def test_cancellation_during_preprocessing_skips_check(self):
        from app.tasks.activity import request_check_cancellation, task_activities

        checks = [{"id": 101, "code": "only", "name": "唯一项", "prompt": "检查"}]
        task_id = self._insert_running_document_task(checks)

        def prepare(*args, **kwargs):
            activity = task_activities([task_id])[task_id]
            self.assertEqual(activity["phase"], "preparing")
            self.assertEqual(activity["checks"]["only"]["phase"], "pending")
            self.assertTrue(request_check_cancellation(task_id, None, "only"))
            return "正文", None

        with (
            patch("app.tasks.runner._prepare_task_inputs", side_effect=prepare),
            patch("app.tasks.runner.run_check") as model,
        ):
            TaskRunner(self.app).run(task_id)
        model.assert_not_called()
        task = get_db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "canceled")
        self.assertTrue(json.loads(task["result_json"])[0]["canceled"])

    def test_pending_checks_skip_execution_before_watcher_poll(self):
        from app.tasks.activity import request_check_cancellation

        checks = [
            {"id": 101, "code": "first", "name": "第一项", "prompt": "检查"},
            {"id": 102, "code": "second", "name": "第二项", "prompt": "检查"},
            {
                "id": 103,
                "code": COMMON_TERMS_CHECK_CODE,
                "name": "常用语",
                "prompt": "检查",
            },
        ]
        task_id = self._insert_running_document_task(checks)
        set_setting("check_item_concurrency", 1)

        def model(**kwargs):
            self.assertEqual(kwargs["check_name"], "第一项")
            self.assertTrue(request_check_cancellation(task_id, None, "second"))
            self.assertTrue(
                request_check_cancellation(task_id, None, COMMON_TERMS_CHECK_CODE)
            )
            return "第一项完成"

        with (
            patch(
                "app.tasks.runner._start_task_cancel_watcher", return_value=(None, None)
            ),
            patch("app.tasks.runner.run_check", side_effect=model) as run_model,
            patch("app.tasks.runner._run_common_terms_check") as local_check,
        ):
            TaskRunner(self.app).run(task_id)
        self.assertEqual(run_model.call_count, 1)
        local_check.assert_not_called()
        task = get_db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(task["status"], "partial")
        results = json.loads(task["result_json"])
        self.assertEqual(results[0]["result"], "第一项完成")
        self.assertTrue(all(result["canceled"] for result in results[1:]))

    def test_recovered_lease_does_not_inherit_old_check_cancellation(self):
        from app.tasks.activity import (
            initialize_activity,
            request_check_cancellation,
            task_activities,
        )

        task_id = self._insert_scheduler_task(status="running", claim_token="expired")
        initialize_activity(
            task_id, "expired", checks=[{"code": "demo", "name": "演示"}]
        )
        self.assertTrue(request_check_cancellation(task_id, "expired", "demo"))
        [(claimed_id, token)] = TaskSupervisor(self.app)._claim_available_tasks()
        self.assertEqual(claimed_id, task_id)
        initialize_activity(task_id, token, checks=[{"code": "demo", "name": "演示"}])
        item = task_activities([task_id])[task_id]["checks"]["demo"]
        self.assertEqual(item["phase"], "pending")
        self.assertNotIn("cancel_requested", item)

    def test_external_task_cancel_event_reaches_individual_model_request(self):
        from app.tasks.runtime.state import TaskCanceled

        checks = [{"id": 101, "code": "only", "name": "唯一项", "prompt": "检查"}]
        task_id = self._insert_running_document_task(checks)
        task = dict(
            get_db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        )
        cancel_event = Event()
        started = Event()
        errors = []

        def model(**kwargs):
            started.set()
            self.assertTrue(kwargs["cancel_event"].wait(4))
            kwargs["check_canceled"]()

        def run():
            with self.app.app_context():
                try:
                    _run_check_items_concurrently(
                        self.app,
                        task,
                        checks,
                        "文档",
                        max_workers=1,
                        stream_trace_enabled=False,
                        cancel_event=cancel_event,
                    )
                except Exception as exc:
                    errors.append(exc)

        with patch("app.tasks.runner.run_check", side_effect=model):
            thread = Thread(target=run)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                cancel_event.set()
            finally:
                thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TaskCanceled)

    def _insert_scheduler_task(
        self,
        *,
        status: str = "queued",
        owner_subject: str = "ip:127.0.0.1",
        provider_id: int | None = None,
        api_key: str | None = None,
        claim_token: str | None = None,
        lease_expires_at: str | None = None,
    ) -> int:
        now = now_text()
        cursor = get_db().execute(
            """
            INSERT INTO tasks(
                ip, owner_subject, original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, provider_id, model_name, api_base, api_key, request_timeout,
                max_input_chars, status, progress, claim_token, lease_expires_at,
                created_at, updated_at
            )
            VALUES (
                '127.0.0.1', ?, 'scheduler.txt', 'scheduler.txt', 'txt', 1,
                'file: scheduler.txt\n\n测试', '[]', ?, 'test-model',
                'http://example.test/v1/chat/completions', ?, 30, 5000,
                ?, 0, ?, ?, ?, ?
            )
            """,
            (
                owner_subject,
                provider_id,
                api_key,
                status,
                claim_token,
                lease_expires_at,
                now,
                now,
            ),
        )
        get_db().commit()
        return int(cursor.lastrowid)

    def _insert_running_document_task(
        self, check_items: list[dict], *, api_key: str = "task-secret"
    ) -> int:
        now = now_text()
        cursor = get_db().execute(
            """
            INSERT INTO tasks(
                ip, owner_subject, original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, checks_snapshot_json, model_name, api_base, api_key,
                request_timeout, max_input_chars, status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'ip:127.0.0.1', 'partial.txt', 'partial.txt', 'txt', 1,
                'file: partial.txt\n\n测试正文', ?, ?, 'test-model',
                'http://example.test/v1/chat/completions', ?, 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                json.dumps([item["id"] for item in check_items]),
                json.dumps(check_items, ensure_ascii=False),
                api_key,
                now,
                now,
            ),
        )
        get_db().commit()
        return int(cursor.lastrowid)

    def _insert_running_preprocessing_task(
        self,
        *,
        task_type: str,
        original_filename: str,
        stored_filename: str,
        file_type: str,
        check_item: dict,
        document_meta: dict | None = None,
        max_input_chars: int = 5000,
    ) -> int:
        now = now_text()
        cursor = get_db().execute(
            """
            INSERT INTO tasks(
                task_type, ip, owner_subject, original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json,
                model_name, api_base, api_key, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                ?, '127.0.0.1', 'ip:127.0.0.1', ?, ?, ?, 1,
                NULL, ?, ?, ?, 'test-model', 'http://example.test/v1/chat/completions',
                'task-secret', 30, ?, 'running', 1, ?, ?
            )
            """,
            (
                task_type,
                original_filename,
                stored_filename,
                file_type,
                json.dumps(
                    document_meta or {"preprocessing": {"status": "pending"}},
                    ensure_ascii=False,
                ),
                json.dumps([check_item["id"]]),
                json.dumps([check_item], ensure_ascii=False),
                max_input_chars,
                now,
                now,
            ),
        )
        get_db().commit()
        return int(cursor.lastrowid)

    def test_multiple_schedulers_atomically_claim_task_once(self):
        task_id = self._insert_scheduler_task()
        barrier = Barrier(2)

        def claim_task():
            with self.app.app_context():
                barrier.wait()
                return TaskSupervisor(self.app)._claim_available_tasks()

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: claim_task(), range(2)))

        claimed = [claim for scheduler_claims in claims for claim in scheduler_claims]
        self.assertEqual([claim[0] for claim in claimed], [task_id])
        self.assertEqual(len(claimed[0][1]), 32)
        task = (
            get_db()
            .execute(
                "SELECT status, claim_token, lease_expires_at FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["claim_token"], claimed[0][1])
        self.assertGreater(task["lease_expires_at"], now_text())

    def test_multiple_schedulers_keep_global_limit_while_claiming(self):
        first_task_id = self._insert_scheduler_task(owner_subject="ip:10.0.0.1")
        second_task_id = self._insert_scheduler_task(owner_subject="ip:10.0.0.2")
        set_setting("global_concurrency", 1)
        barrier = Barrier(2)

        def claim_tasks():
            with self.app.app_context():
                barrier.wait()
                return TaskSupervisor(self.app)._claim_available_tasks()

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(lambda _: claim_tasks(), range(2)))

        claimed_ids = [
            claim[0] for scheduler_claims in claims for claim in scheduler_claims
        ]
        self.assertEqual(claimed_ids, [first_task_id])
        tasks = get_db().execute("SELECT id, status FROM tasks ORDER BY id").fetchall()
        self.assertEqual(
            [(task["id"], task["status"]) for task in tasks],
            [(first_task_id, "running"), (second_task_id, "queued")],
        )

    def test_scheduler_batches_owner_counts_without_per_task_queries(self):
        first_owner_task = self._insert_scheduler_task(owner_subject="ip:10.0.0.1")
        blocked_same_owner_task = self._insert_scheduler_task(
            owner_subject="ip:10.0.0.1"
        )
        other_owner_task = self._insert_scheduler_task(owner_subject="ip:10.0.0.2")
        set_setting("global_concurrency", 3)
        set_setting("user_concurrency", 1)
        statements = []
        db = get_db()
        db.set_trace_callback(statements.append)
        try:
            claimed = TaskSupervisor(self.app)._claim_available_tasks()
        finally:
            db.set_trace_callback(None)

        self.assertEqual(
            [task_id for task_id, _claim in claimed],
            [first_owner_task, other_owner_task],
        )
        blocked = db.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (blocked_same_owner_task,),
        ).fetchone()
        self.assertEqual(blocked["status"], "queued")
        task_selects = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith(("SELECT", "WITH"))
            and "FROM TASKS" in statement.upper()
        ]
        self.assertEqual(len(task_selects), 2)

    def test_scheduler_skips_large_blocked_owner_queue(self):
        running_task_id = self._insert_scheduler_task(
            status="running",
            owner_subject="ip:10.0.0.1",
            claim_token="active-claim",
            lease_expires_at="2999-01-01 00:00:00",
        )
        blocked_task_ids = [
            self._insert_scheduler_task(owner_subject="ip:10.0.0.1") for _ in range(50)
        ]
        first_runnable_id = self._insert_scheduler_task(owner_subject="ip:10.0.0.2")
        second_runnable_id = self._insert_scheduler_task(owner_subject="ip:10.0.0.3")
        set_setting("global_concurrency", 3)
        set_setting("user_concurrency", 1)

        claimed = TaskSupervisor(self.app)._claim_available_tasks()

        self.assertEqual(
            [task_id for task_id, _claim in claimed],
            [first_runnable_id, second_runnable_id],
        )
        blocked_statuses = (
            get_db()
            .execute(
                f"SELECT DISTINCT status FROM tasks WHERE id IN ({','.join('?' for _ in blocked_task_ids)})",
                tuple(blocked_task_ids),
            )
            .fetchall()
        )
        self.assertEqual([row["status"] for row in blocked_statuses], ["queued"])
        running_status = (
            get_db()
            .execute(
                "SELECT status FROM tasks WHERE id = ?",
                (running_task_id,),
            )
            .fetchone()
        )
        self.assertEqual(running_status["status"], "running")

    def test_scheduler_limits_each_owner_before_global_candidate_limit(self):
        same_owner_task_ids = [
            self._insert_scheduler_task(owner_subject="ip:10.0.0.1") for _ in range(50)
        ]
        other_owner_task_id = self._insert_scheduler_task(owner_subject="ip:10.0.0.2")
        set_setting("global_concurrency", 2)
        set_setting("user_concurrency", 1)

        claimed = TaskSupervisor(self.app)._claim_available_tasks()

        self.assertEqual(
            [task_id for task_id, _claim in claimed],
            [same_owner_task_ids[0], other_owner_task_id],
        )
        queued_same_owner = (
            get_db()
            .execute(
                """
            SELECT COUNT(*) AS total
            FROM tasks
            WHERE owner_subject = 'ip:10.0.0.1' AND status = 'queued'
            """
            )
            .fetchone()["total"]
        )
        self.assertEqual(queued_same_owner, 49)

    def test_scheduler_does_not_recover_active_lease(self):
        task_id = self._insert_scheduler_task(
            status="running",
            claim_token="active-claim",
            lease_expires_at="2999-01-01 00:00:00",
        )

        claimed = TaskSupervisor(self.app)._claim_available_tasks()

        self.assertEqual(claimed, [])
        task = (
            get_db()
            .execute(
                "SELECT status, claim_token, lease_expires_at FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["claim_token"], "active-claim")
        self.assertEqual(task["lease_expires_at"], "2999-01-01 00:00:00")

    def test_scheduler_recovers_expired_lease_with_new_claim(self):
        task_id = self._insert_scheduler_task(
            status="running",
            claim_token="expired-claim",
            lease_expires_at="2000-01-01 00:00:00",
        )

        claimed = TaskSupervisor(self.app)._claim_available_tasks()

        self.assertEqual([claim[0] for claim in claimed], [task_id])
        self.assertNotEqual(claimed[0][1], "expired-claim")
        task = (
            get_db()
            .execute(
                "SELECT status, progress, claim_token, lease_expires_at FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["progress"], 1)
        self.assertEqual(task["claim_token"], claimed[0][1])
        self.assertGreater(task["lease_expires_at"], now_text())

    def test_scheduler_recovery_preserves_retry_base_results(self):
        task_id = self._insert_scheduler_task(
            status="running",
            claim_token="expired-retry-claim",
            lease_expires_at="2000-01-01 00:00:00",
        )
        base_results = [{"code": "success", "name": "成功项", "result": "保留结果"}]
        get_db().execute(
            "UPDATE tasks SET result_json = ?, retry_check_codes_json = ? WHERE id = ?",
            (
                json.dumps(base_results, ensure_ascii=False),
                json.dumps(["failed"]),
                task_id,
            ),
        )
        get_db().commit()

        claimed = TaskSupervisor(self.app)._claim_available_tasks()

        self.assertEqual([claim[0] for claim in claimed], [task_id])
        task = (
            get_db()
            .execute(
                "SELECT status, result_json, retry_check_codes_json FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        self.assertEqual(task["status"], "running")
        self.assertEqual(json.loads(task["result_json"]), base_results)
        self.assertEqual(json.loads(task["retry_check_codes_json"]), ["failed"])

    def test_canceling_task_keeps_concurrency_slot_until_worker_exits(self):
        canceling_task_id = self._insert_scheduler_task(
            status="canceling",
            owner_subject="ip:10.0.0.1",
            claim_token="active-claim",
            lease_expires_at="2999-01-01 00:00:00",
        )
        queued_task_id = self._insert_scheduler_task(owner_subject="ip:10.0.0.2")
        set_setting("global_concurrency", 1)

        claimed = TaskSupervisor(self.app)._claim_available_tasks()

        self.assertEqual(claimed, [])
        tasks = {
            row["id"]: row["status"]
            for row in get_db()
            .execute(
                "SELECT id, status FROM tasks WHERE id IN (?, ?)",
                (canceling_task_id, queued_task_id),
            )
            .fetchall()
        }
        self.assertEqual(tasks[canceling_task_id], "canceling")
        self.assertEqual(tasks[queued_task_id], "queued")

    def test_scheduler_finalizes_expired_canceling_task(self):
        task_id = self._insert_scheduler_task(
            status="canceling",
            api_key="task-secret",
            claim_token="expired-claim",
            lease_expires_at="2000-01-01 00:00:00",
        )
        get_db().execute(
            "UPDATE tasks SET cancel_requested = 1 WHERE id = ?",
            (task_id,),
        )
        get_db().commit()

        claimed = TaskSupervisor(self.app)._claim_available_tasks()

        self.assertEqual(claimed, [])
        task = (
            get_db()
            .execute(
                """
            SELECT status, api_key, claim_token, lease_expires_at, finished_at
            FROM tasks
            WHERE id = ?
            """,
                (task_id,),
            )
            .fetchone()
        )
        self.assertEqual(task["status"], "canceled")
        self.assertIsNone(task["api_key"])
        self.assertIsNone(task["claim_token"])
        self.assertIsNone(task["lease_expires_at"])
        self.assertIsNotNone(task["finished_at"])

    def test_worker_with_stale_claim_does_not_run_task(self):
        task_id = self._insert_scheduler_task(
            status="running",
            claim_token="current-claim",
            lease_expires_at="2999-01-01 00:00:00",
        )

        with patch("app.tasks.runner.run_check") as mocked_run_check:
            TaskRunner(self.app).run(task_id, "stale-claim")

        mocked_run_check.assert_not_called()
        task = (
            get_db()
            .execute(
                "SELECT status, claim_token FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["claim_token"], "current-claim")

    def test_failed_and_worker_canceled_tasks_clear_api_key_snapshots(self):
        failed_task_id = self._insert_scheduler_task(
            status="running",
            api_key="failed-secret",
        )
        canceled_task_id = self._insert_scheduler_task(
            status="running",
            api_key="canceled-secret",
        )
        get_db().execute(
            "UPDATE tasks SET cancel_requested = 1 WHERE id = ?",
            (canceled_task_id,),
        )
        get_db().commit()

        runner = TaskRunner(self.app)
        runner.run(failed_task_id)
        runner.run(canceled_task_id)

        tasks = {
            row["id"]: row
            for row in get_db()
            .execute(
                "SELECT id, status, api_key FROM tasks WHERE id IN (?, ?)",
                (failed_task_id, canceled_task_id),
            )
            .fetchall()
        }
        self.assertEqual(tasks[failed_task_id]["status"], "failed")
        self.assertIsNone(tasks[failed_task_id]["api_key"])
        self.assertEqual(tasks[canceled_task_id]["status"], "canceled")
        self.assertIsNone(tasks[canceled_task_id]["api_key"])

    def test_running_worker_stops_and_finalizes_after_cancel_signal(self):
        check_item = {
            "id": 1,
            "code": "typo",
            "name": "错别字检查",
            "prompt": "检查错别字",
        }
        task_id = self._insert_running_document_task([check_item])
        db = get_db()
        db.execute(
            """
            UPDATE tasks
            SET claim_token = 'worker-claim',
                lease_expires_at = '2999-01-01 00:00:00'
            WHERE id = ?
            """,
            (task_id,),
        )
        db.commit()
        runner = TaskRunner(self.app)
        request_started = Event()

        def wait_for_cancel(**kwargs):
            request_started.set()
            if not kwargs["cancel_event"].wait(3):
                raise RuntimeError("测试等待取消信号超时")
            kwargs["check_canceled"]()
            return "不应完成"

        with patch(
            "app.tasks.runner.run_check", side_effect=wait_for_cancel
        ) as mocked_run_check:
            worker = Thread(
                target=runner.run,
                args=(task_id, "worker-claim"),
                daemon=True,
            )
            worker.start()
            self.assertTrue(request_started.wait(3))
            db.execute(
                """
                UPDATE tasks
                SET status = 'canceling', cancel_requested = 1
                WHERE id = ? AND status = 'running'
                """,
                (task_id,),
            )
            db.commit()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        mocked_run_check.assert_called_once()
        task = db.execute(
            """
            SELECT status, api_key, claim_token, lease_expires_at, finished_at
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()
        self.assertEqual(task["status"], "canceled")
        self.assertIsNone(task["api_key"])
        self.assertIsNone(task["claim_token"])
        self.assertIsNone(task["lease_expires_at"])
        self.assertIsNotNone(task["finished_at"])

    def test_intermediate_results_use_live_table_without_invalidating_report_cache(
        self,
    ):
        task_id = self._insert_scheduler_task(
            status="running", claim_token="live-claim"
        )
        db = get_db()
        db.execute(
            """
            INSERT INTO task_report_stats(task_id, source_updated_at, suppression_version, updated_at)
            VALUES (?, '2026-08-29 10:00:00', '0:0:', '2026-08-29 10:00:00')
            """,
            (task_id,),
        )
        db.commit()
        snapshot = [{"code": "typo", "name": "错别字检查", "result": "实时结果"}]

        _save_intermediate_results(db, task_id, snapshot, "正在检查", 35, "live-claim")

        task = db.execute(
            "SELECT result_json, summary, progress FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        live = db.execute(
            "SELECT result_json, summary, progress FROM task_live_results WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        cached = db.execute(
            "SELECT task_id FROM task_report_stats WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        self.assertIsNone(task["result_json"])
        self.assertEqual(task["summary"], "正在检查")
        self.assertEqual(task["progress"], 35)
        self.assertEqual(json.loads(live["result_json"]), snapshot)
        self.assertEqual(live["summary"], "正在检查")
        self.assertEqual(live["progress"], 35)
        self.assertIsNotNone(cached)

        _mark_failed(db, task_id, "模型服务中断", claim_token="live-claim")

        failed = db.execute(
            "SELECT status, result_json, summary FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        remaining_live = db.execute(
            "SELECT task_id FROM task_live_results WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(json.loads(failed["result_json"]), snapshot)
        self.assertEqual(failed["summary"], "正在检查")
        self.assertIsNone(remaining_live)

    def test_task_uses_submission_config_snapshot_after_provider_changes(self):
        db = get_db()
        now = now_text()
        provider_id = db.execute(
            """
            INSERT INTO user_model_providers(
                owner_subject, name, api_base, api_key, request_timeout,
                max_input_chars, is_active, created_at, updated_at
            )
            VALUES ('ip:127.0.0.1', '当前提供商',
                    'https://current.example.test/v1/chat/completions',
                    'current-secret', 45, 9000, 1, ?, ?)
            """,
            (now, now),
        ).lastrowid
        db.execute(
            """
            INSERT INTO user_model_configs(
                provider_id, model_name, force_disable_thinking,
                sort_order, created_at, updated_at
            )
            VALUES (?, 'test-model', 0, 10, ?, ?)
            """,
            (provider_id, now, now),
        )
        task_id = db.execute(
            """
            INSERT INTO tasks(
                ip, owner_subject, original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, checks_snapshot_json, provider_id,
                provider_name, model_name, api_base, api_key, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'ip:127.0.0.1', 'current.txt', 'current.txt', 'txt', 1,
                'file: current.txt\n\n测试', '[1]', ?, ?, '提交时名称', 'test-model',
                'https://snapshot.example.test/v1/chat/completions', 'snapshot-secret',
                37, 7000, 'running', 0, ?, ?
            )
            """,
            (
                json.dumps(
                    [
                        {
                            "id": 1,
                            "code": "typo",
                            "name": "错别字检查",
                            "prompt": "检查错别字",
                        }
                    ],
                    ensure_ascii=False,
                ),
                provider_id,
                now,
                now,
            ),
        ).lastrowid
        db.commit()
        db.execute(
            """
            UPDATE user_model_providers
            SET name = '修改后的提供商',
                api_base = 'https://changed.example.test/v1/chat/completions',
                api_key = 'changed-secret',
                request_timeout = 99,
                max_input_chars = 1000,
                is_active = 0
            WHERE id = ?
            """,
            (provider_id,),
        )
        db.execute(
            "DELETE FROM user_model_configs WHERE provider_id = ?", (provider_id,)
        )
        db.commit()
        calls = []

        with patch(
            "app.tasks.runner.run_check",
            side_effect=lambda **kwargs: calls.append(kwargs) or "完成",
        ):
            TaskRunner(self.app).run(task_id)

        task = db.execute(
            "SELECT status, api_key FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        self.assertEqual(task["status"], "completed")
        self.assertIsNone(task["api_key"])
        self.assertEqual(
            calls[0]["api_base"], "https://snapshot.example.test/v1/chat/completions"
        )
        self.assertEqual(calls[0]["api_key"], "snapshot-secret")
        self.assertEqual(calls[0]["request_timeout"], 37)

    def test_cleanup_expired_task_files_preserves_history_and_removes_runtime_data(
        self,
    ):
        upload_dir = Path(self.app.config["UPLOAD_FOLDER"])
        image_root = Path(self.app.config["IMAGE_FOLDER"])
        old_upload = upload_dir / "old.pdf"
        recent_upload = upload_dir / "recent.pdf"
        running_upload = upload_dir / "running.pdf"
        old_image_dir = image_root / "old-images"
        old_image = old_image_dir / "old.png"
        old_frame_dir = image_root / "old-video"
        old_frame = old_frame_dir / "frame.jpg"
        old_upload.write_text("old", encoding="utf-8")
        recent_upload.write_text("recent", encoding="utf-8")
        running_upload.write_text("running", encoding="utf-8")
        old_image_dir.mkdir(parents=True, exist_ok=True)
        old_image.write_bytes(b"png")
        old_frame_dir.mkdir(parents=True, exist_ok=True)
        old_frame.write_bytes(b"jpg")
        set_setting("task_file_retention_days", 1)
        db = get_db()
        old_meta = {
            "images": [
                {
                    "filename": "old.png",
                    "relative_path": "old-images/old.png",
                    "mime_type": "image/png",
                    "position": "page001-image001",
                }
            ],
            "page_images": [],
            "frames": [
                {
                    "filename": "frame.jpg",
                    "relative_path": "old-video/frame.jpg",
                    "mime_type": "image/jpeg",
                    "position": "00:01.000",
                }
            ],
        }
        rows = [
            (
                "old.pdf",
                "old.pdf",
                IMAGE_TASK_TYPE,
                json.dumps(old_meta, ensure_ascii=False),
                "completed",
                "2000-01-01 00:00:00",
            ),
            (
                "recent.pdf",
                "recent.pdf",
                IMAGE_TASK_TYPE,
                "{}",
                "completed",
                now_text(),
            ),
            (
                "running.pdf",
                "running.pdf",
                IMAGE_TASK_TYPE,
                "{}",
                "running",
                "2000-01-01 00:00:00",
            ),
        ]
        task_ids = {}
        for original, stored, task_type, meta, status, finished_at in rows:
            cursor = db.execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type, file_size,
                    document_text, document_meta_json, result_json, checks_json, model_name, api_base, api_key, request_timeout,
                    max_input_chars, status, progress, created_at, updated_at, finished_at
                )
                VALUES (?, '127.0.0.1', ?, ?, 'pdf', 1, '保留前的文档正文', ?, '[{"result":"历史报告"}]', '[]', 'model-a',
                        'http://example.test/v1/chat/completions', 'task-secret', 30, 5000,
                        ?, 100, ?, ?, ?)
                """,
                (
                    task_type,
                    original,
                    stored,
                    meta,
                    status,
                    finished_at,
                    finished_at,
                    finished_at,
                ),
            )
            task_ids[stored] = int(cursor.lastrowid)
        rule_id = db.execute(
            """
            INSERT INTO report_suppression_rules(
                task_type, check_code, fingerprint, item_json, created_at, updated_at
            )
            VALUES (?, 'image-test', 'fingerprint', '{}', ?, ?)
            """,
            (IMAGE_TASK_TYPE, now_text(), now_text()),
        ).lastrowid
        db.executemany(
            """
            INSERT INTO report_suppression_hits(
                rule_id, task_id, result_code, item_id, item_json, created_at
            )
            VALUES (?, ?, 'image-test', ?, '{}', ?)
            """,
            [
                (rule_id, task_ids["old.pdf"], "old-item", now_text()),
                (rule_id, task_ids["recent.pdf"], "recent-item", now_text()),
            ],
        )
        db.commit()

        self.assertEqual(cleanup_expired_task_files(self.app), 1)

        remaining = {
            row["stored_filename"]: row
            for row in db.execute(
                """
                SELECT stored_filename, status, document_text, document_meta_json,
                       result_json, api_key, source_files_cleaned_at
                FROM tasks
                """
            ).fetchall()
        }
        self.assertEqual(set(remaining), {"old.pdf", "recent.pdf", "running.pdf"})
        self.assertEqual(remaining["old.pdf"]["status"], "completed")
        self.assertIsNone(remaining["old.pdf"]["document_text"])
        self.assertEqual(remaining["old.pdf"]["api_key"], "task-secret")
        self.assertIsNotNone(remaining["old.pdf"]["source_files_cleaned_at"])
        self.assertEqual(
            remaining["old.pdf"]["document_meta_json"],
            json.dumps(old_meta, ensure_ascii=False),
        )
        self.assertEqual(remaining["old.pdf"]["result_json"], '[{"result":"历史报告"}]')
        self.assertEqual(remaining["recent.pdf"]["document_text"], "保留前的文档正文")
        self.assertEqual(remaining["running.pdf"]["document_text"], "保留前的文档正文")
        self.assertFalse(old_upload.exists())
        self.assertFalse(old_image.exists())
        self.assertFalse(old_frame.exists())
        self.assertFalse(old_image_dir.exists())
        self.assertFalse(old_frame_dir.exists())
        self.assertTrue(recent_upload.exists())
        self.assertTrue(running_upload.exists())
        remaining_hit_task_ids = {
            row["task_id"]
            for row in db.execute(
                "SELECT task_id FROM report_suppression_hits"
            ).fetchall()
        }
        self.assertEqual(
            remaining_hit_task_ids,
            {task_ids["old.pdf"], task_ids["recent.pdf"]},
        )

    def test_cleanup_expired_task_files_skips_locked_files(self):
        upload_dir = Path(self.app.config["UPLOAD_FOLDER"])
        old_upload = upload_dir / "old.pdf"
        old_upload.write_text("old", encoding="utf-8")
        set_setting("task_file_retention_days", 1)
        db = get_db()
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_meta_json, checks_json, model_name, api_base, request_timeout,
                max_input_chars, status, progress, created_at, updated_at, finished_at
            )
            VALUES (?, '127.0.0.1', 'old.pdf', 'old.pdf', 'pdf', 1, '{}', '[]', 'model-a',
                    'http://example.test/v1/chat/completions', 30, 5000,
                    'completed', 100, '2000-01-01 00:00:00', '2000-01-01 00:00:00', '2000-01-01 00:00:00')
            """,
            (IMAGE_TASK_TYPE,),
        )
        db.commit()

        with patch(
            "app.tasks.runtime.artifacts.remove_file",
            return_value=(False, "[WinError 32] 文件正被占用"),
        ):
            self.assertEqual(cleanup_expired_task_files(self.app), 0)

        task = db.execute(
            "SELECT * FROM tasks WHERE stored_filename = 'old.pdf'"
        ).fetchone()
        self.assertIsNotNone(task)
        self.assertIsNone(task["source_files_cleaned_at"])
        self.assertTrue(old_upload.exists())

    def test_task_file_cache_snapshot_counts_actual_files_and_sorts_oldest_smallest_first(
        self,
    ):
        upload_dir = Path(self.app.config["UPLOAD_FOLDER"])
        image_root = Path(self.app.config["IMAGE_FOLDER"])
        (upload_dir / "large.pdf").write_bytes(b"1234")
        (upload_dir / "small.txt").write_bytes(b"12")
        (upload_dir / "running.mp4").write_bytes(b"123456789")
        image_dir = image_root / "large-images"
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "page.png").write_bytes(b"png")
        image_meta = json.dumps(
            {
                "page_images": [
                    {
                        "filename": "page.png",
                        "relative_path": "large-images/page.png",
                    }
                ]
            },
            ensure_ascii=False,
        )
        db = get_db()
        rows = [
            (IMAGE_TASK_TYPE, "large.pdf", "large.pdf", image_meta, "completed"),
            ("document_check", "small.txt", "small.txt", "{}", "failed"),
            (VIDEO_TASK_TYPE, "running.mp4", "running.mp4", "{}", "running"),
        ]
        for task_type, original, stored, meta, status in rows:
            db.execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type, file_size,
                    document_meta_json, checks_json, model_name, api_base, status, progress,
                    created_at, updated_at, finished_at
                )
                VALUES (?, '127.0.0.1', ?, ?, 'pdf', 1, ?, '[]', 'model-a',
                        'https://example.test/v1/chat/completions', ?, 100,
                        '2026-05-01 10:00:00', '2026-05-01 10:00:00', '2026-05-01 10:00:00')
                """,
                (task_type, original, stored, meta, status),
            )
        db.commit()

        snapshot = task_file_cache_snapshot(self.app)

        self.assertEqual(snapshot["total_size_bytes"], 18)
        self.assertEqual(snapshot["upload_size_bytes"], 15)
        self.assertEqual(snapshot["generated_size_bytes"], 3)
        self.assertEqual(snapshot["cleanable_size_bytes"], 9)
        self.assertEqual(snapshot["cleanable_count"], 2)
        self.assertEqual(
            [item["original_filename"] for item in snapshot["items"]],
            ["small.txt", "large.pdf"],
        )
        self.assertEqual([item["file_count"] for item in snapshot["items"]], [1, 2])

    def test_cleanup_task_file_cache_preserves_report_and_removes_cleaned_task_from_snapshot(
        self,
    ):
        upload_dir = Path(self.app.config["UPLOAD_FOLDER"])
        completed_file = upload_dir / "completed.txt"
        running_file = upload_dir / "running.txt"
        completed_file.write_bytes(b"complete")
        running_file.write_bytes(b"running")
        db = get_db()
        completed_id = db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                document_text, result_json, checks_json, model_name, api_base,
                status, progress, created_at, updated_at, finished_at
            )
            VALUES (
                '127.0.0.1', 'completed.txt', 'completed.txt', 'txt', 8,
                '正文', '[{"result":"保留报告"}]', '[]', 'model-a',
                'https://example.test/v1/chat/completions', 'completed', 100,
                '2026-05-01 10:00:00', '2026-05-01 10:00:00', '2026-05-01 10:00:00'
            )
            """
        ).lastrowid
        running_id = db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, model_name, api_base,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'running.txt', 'running.txt', 'txt', 7,
                '运行正文', '[]', 'model-a', 'https://example.test/v1/chat/completions',
                'running', 50, '2026-05-01 10:00:00', '2026-05-01 10:00:00'
            )
            """
        ).lastrowid
        db.commit()

        result = cleanup_task_file_cache(self.app, [completed_id, running_id])

        self.assertEqual(result["cleaned_ids"], [completed_id])
        self.assertEqual(result["freed_size_bytes"], 8)
        self.assertEqual(result["skipped_ids"], [running_id])
        self.assertFalse(completed_file.exists())
        self.assertTrue(running_file.exists())
        completed = db.execute(
            "SELECT * FROM tasks WHERE id = ?", (completed_id,)
        ).fetchone()
        self.assertIsNone(completed["document_text"])
        self.assertIsNotNone(completed["source_files_cleaned_at"])
        self.assertEqual(completed["result_json"], '[{"result":"保留报告"}]')
        self.assertEqual(task_file_cache_snapshot(self.app)["items"], [])

    def test_document_check_sends_full_text_once(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, max_input_chars,
                reasoning_effort, status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'long.txt', 'long.txt', 'txt', 1,
                ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 200000, 'high',
                'running', 0, ?, ?
            )
            """,
            (json.dumps([1]), created_at, created_at),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()
        check_items = [{"code": "typo", "name": "错别字检查", "prompt": "检查错别字"}]
        document_text = "\n\n".join(
            f"第{i}段 " + ("内容" * 15_000) for i in range(1, 6)
        )
        set_setting("issue_output_limit", 45)
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            kwargs["on_content"]("流式结果")
            return "最终结果"

        with patch("app.tasks.runner.run_check", side_effect=fake_run_check):
            results = _run_check_items_concurrently(
                self.app,
                task,
                check_items,
                document_text,
                max_workers=1,
                stream_trace_enabled=False,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["check_name"], "错别字检查")
        self.assertEqual(calls[0]["prompt"], "检查错别字")
        self.assertEqual(calls[0]["document_text"], document_text)
        self.assertEqual(calls[0]["issue_output_limit"], 30)
        self.assertEqual(calls[0]["reasoning_effort"], "high")
        self.assertIsNone(calls[0]["max_completion_tokens"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["result"], "最终结果")
        self.assertEqual(results[0]["issue_output_limit"], 30)

    def test_document_check_filters_pdf_table_missing_claim_against_cell_structure(
        self,
    ):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'table.pdf', 'table.pdf', 'pdf', 1,
                ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (json.dumps([1]), created_at, created_at),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()
        document_text = """
<table id="page001-table001" data-confidence="high" data-view="normalized">
  <tr><td data-cell="A1" data-original-range="A1:B1" data-original-colspan="2">合并表头</td><td data-cell="B1" data-original-range="A1:B1" data-inherited-from="A1">合并表头</td></tr>
  <tr><td data-cell="A2" data-empty="true">[空单元格]</td><td data-cell="B2">10 A</td></tr>
</table>
"""
        model_result = {
            "summary": "发现 2 个问题",
            "items": [
                {
                    "status": "issue",
                    "category": "表格数据缺失",
                    "location": "page001-table001 > B1",
                    "description": "B1 单元格数据为空。",
                },
                {
                    "status": "issue",
                    "category": "表格数据缺失",
                    "location": "page001-table001 > A2",
                    "description": "A2 单元格数据为空。",
                },
            ],
        }

        with patch(
            "app.tasks.runner.run_check",
            return_value=json.dumps(model_result, ensure_ascii=False),
        ):
            results = _run_check_items_concurrently(
                self.app,
                task,
                [
                    {
                        "code": "completeness",
                        "name": "内容完整性检查",
                        "prompt": "检查完整性",
                    }
                ],
                document_text,
                max_workers=1,
                stream_trace_enabled=False,
            )

        sanitized = json.loads(results[0]["result"])
        self.assertEqual(len(sanitized["items"]), 1)
        self.assertEqual(sanitized["items"][0]["location"], "page001-table001 > A2")

    def test_comparison_tasks_keep_full_text_instead_of_single_document_chunks(self):
        db = get_db()
        created_at = now_text()
        document_text = (
            "# 文档A：中文手册.pdf\n"
            + ("中文内容。" * 350)
            + "\n\n# 文档B：English Manual.pdf\n"
            + ("English content. " * 140)
        )
        self.assertGreater(len(document_text), 3000)
        self.assertLess(len(document_text), 5000)

        for task_type, check_code, check_name in (
            (CONSISTENCY_TASK_TYPE, "consistency-compare", "多文档内容对照"),
            (
                LANGUAGE_CONSISTENCY_TASK_TYPE,
                "language-consistency-cross-lingual",
                "跨语种内容一致性对比",
            ),
        ):
            with self.subTest(task_type=task_type):
                cursor = db.execute(
                    """
                    INSERT INTO tasks(
                        task_type, ip, original_filename, stored_filename, file_type, file_size,
                        checks_json, model_name, api_base, request_timeout, max_input_chars,
                        status, progress, created_at, updated_at
                    )
                    VALUES (
                        ?, '127.0.0.1', 'comparison.txt', 'comparison.txt', 'txt', 1,
                        ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                        'running', 0, ?, ?
                    )
                    """,
                    (task_type, json.dumps([1]), created_at, created_at),
                )
                db.commit()
                task = db.execute(
                    "SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                calls = []

                def fake_run_check(**kwargs):
                    calls.append(kwargs)
                    return "完整对照结果"

                with patch("app.tasks.runner.run_check", side_effect=fake_run_check):
                    results = _run_check_items_concurrently(
                        self.app,
                        task,
                        [
                            {
                                "code": check_code,
                                "name": check_name,
                                "prompt": "执行完整对照",
                            }
                        ],
                        document_text,
                        max_workers=1,
                        stream_trace_enabled=False,
                    )

                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["document_text"], document_text)
                self.assertIn("# 文档A：", calls[0]["document_text"])
                self.assertIn("# 文档B：", calls[0]["document_text"])
                self.assertNotIn("max_completion_tokens", calls[0])
                self.assertEqual(results[0]["result"], "完整对照结果")

    def test_sensitive_terms_check_uses_local_dictionary_without_llm(self):
        terms_path = Path(self.temp_dir.name) / "sensitive_terms.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        sheet.append(["不规范用语", "规范用语"])
        sheet.append(["旧称", "新称"])
        sheet.append(["坏词", "好词"])
        workbook.save(terms_path)
        workbook.close()
        self.app.config["SENSITIVE_TERMS_PATH"] = str(terms_path)

        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'doc.txt', 'doc.txt', 'txt', 1,
                ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (json.dumps([1]), created_at, created_at),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()
        check_items = [
            {
                "code": SENSITIVE_TERMS_CHECK_CODE,
                "name": "敏感词检查",
                "prompt": "本地检查",
            }
        ]
        document_text = (
            "file: doc.txt\n\n[第2页]\n这里出现旧称，坏词也出现了。旧称需要统一。"
        )

        with patch(
            "app.tasks.runner.run_check",
            side_effect=AssertionError("should not call llm"),
        ):
            results = _run_check_items_concurrently(
                self.app,
                task,
                check_items,
                document_text,
                max_workers=1,
                stream_trace_enabled=False,
            )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIn("structured_report", result)
        self.assertIn("发现 2 类不规范用语", result["structured_report"]["summary"])
        self.assertEqual(len(result["structured_report"]["items"]), 2)
        first_item = result["structured_report"]["items"][0]
        self.assertIn("旧称", first_item["description"])
        self.assertIn("新称", first_item["suggestion"])
        self.assertIn("文件：doc.txt", first_item["location"])
        self.assertIn("页码：第2页", first_item["location"])

    def test_common_terms_check_uses_local_rules_without_llm(self):
        terms_path = Path(self.temp_dir.name) / "common_terms.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        sheet.append(["常用词", "常见错误/不推荐用法"])
        sheet.append(["OpenAI", "Open AI"])
        sheet.append(["登录", "登陆"])
        workbook.save(terms_path)
        workbook.close()
        self.app.config["COMMON_TERMS_PATH"] = str(terms_path)

        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'doc.txt', 'doc.txt', 'txt', 1,
                ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (json.dumps([1]), created_at, created_at),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()
        check_items = [
            {
                "code": COMMON_TERMS_CHECK_CODE,
                "name": "常用词检查",
                "prompt": "本地检查",
            }
        ]
        document_text = "file: doc.txt\n\n[第2页]\nOpenAI 正确，openai 错误，Open AI 不推荐，请勿写成登陆。"

        with patch(
            "app.tasks.runner.run_check",
            side_effect=AssertionError("should not call llm"),
        ):
            results = _run_check_items_concurrently(
                self.app,
                task,
                check_items,
                document_text,
                max_workers=1,
                stream_trace_enabled=False,
            )

        self.assertEqual(len(results), 1)
        report = results[0]["structured_report"]
        self.assertIn("发现 3 类常用词写法问题", report["summary"])
        self.assertEqual(len(report["items"]), 3)
        self.assertTrue(all(item["type"] == "issue" for item in report["items"]))

    def test_hyperlink_check_uses_local_rules_without_llm(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, max_input_chars,
                document_meta_json, status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'doc.txt', 'doc.txt', 'txt', 1,
                ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                ?, 'running', 0, ?, ?
            )
            """,
            (
                json.dumps([1]),
                json.dumps(
                    {
                        "hyperlinks": [
                            {
                                "display_text": "安装指南",
                                "target": "javascript:alert(1)",
                                "location": "第1段",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()
        check_items = [
            {
                "code": HYPERLINK_CHECK_CODE,
                "name": "超链接有效性检查",
                "prompt": "本地规则",
            }
        ]

        with patch(
            "app.tasks.runner.run_check",
            side_effect=AssertionError("should not call llm"),
        ):
            results = _run_check_items_concurrently(
                self.app,
                task,
                check_items,
                "file: doc.txt\n\n请参见安装指南",
                document_meta=json.loads(task["document_meta_json"]),
                max_workers=1,
                stream_trace_enabled=False,
            )

        report = results[0]["structured_report"]
        self.assertEqual(report["items"][0]["category"], "超链接格式错误")
        self.assertIn("javascript", report["items"][0]["excerpt"])

    def test_passes_force_disable_thinking_to_llm(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, max_input_chars,
                force_disable_thinking, status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'doc.txt', 'doc.txt', 'txt', 1,
                ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                1, 'running', 0, ?, ?
            )
            """,
            (json.dumps([1]), created_at, created_at),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()
        check_items = [{"code": "typo", "name": "错别字检查", "prompt": "检查错别字"}]
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            return "完成"

        with patch("app.tasks.runner.run_check", side_effect=fake_run_check):
            _run_check_items_concurrently(
                self.app,
                task,
                check_items,
                "文档",
                max_workers=1,
                stream_trace_enabled=False,
            )

        self.assertTrue(calls[0]["force_disable_thinking"])

    def test_task_execution_uses_system_network_config(self):
        self.app.config["NETWORK"] = {
            "proxy_mode": "custom",
            "proxy": "http://127.0.0.1:7890",
            "ssl_verify": True,
        }
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'doc.txt', 'doc.txt', 'txt', 1,
                ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (json.dumps([1]), created_at, created_at),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()
        check_items = [{"code": "typo", "name": "错别字检查", "prompt": "检查错别字"}]
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            return "完成"

        with patch("app.tasks.runner.run_check", side_effect=fake_run_check):
            _run_check_items_concurrently(
                self.app,
                task,
                check_items,
                "文档",
                max_workers=1,
                stream_trace_enabled=False,
            )

        self.assertEqual(calls[0]["proxy_mode"], "custom")
        self.assertEqual(calls[0]["proxy"], "http://127.0.0.1:7890")
        self.assertTrue(calls[0]["ssl_verify"])

    def test_document_check_items_prefers_snapshot_over_current_database(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO check_items(code, name, description, prompt, enabled, sort_order, created_at, updated_at)
            VALUES ('typo', '当前名称', '', '当前提示词', 0, 10, ?, ?)
            """,
            (created_at, created_at),
        )
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, checks_snapshot_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'doc.txt', 'doc.txt', 'txt', 1,
                ?, ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                json.dumps([1]),
                json.dumps(
                    [
                        {
                            "id": 1,
                            "code": "typo",
                            "name": "提交时名称",
                            "prompt": "提交时提示词",
                        }
                    ],
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task = db.execute("SELECT * FROM tasks").fetchone()

        self.assertEqual(
            _document_check_items(db, task),
            [{"code": "typo", "name": "提交时名称", "prompt": "提交时提示词"}],
        )

    def test_run_task_uses_cached_document_text_when_original_file_is_missing(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                ip, original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, checks_snapshot_json, model_name, api_base, api_key, request_timeout, max_input_chars,
                status, progress, claim_token, lease_expires_at, created_at, updated_at
            )
            VALUES (
                '127.0.0.1', 'missing.txt', 'missing.txt', 'txt', 1,
                'file: missing.txt\n\n缓存文本', ?, ?, 'test-model', 'http://example.test/v1/chat/completions', 'task-secret', 30, 5000,
                'running', 0, 'test-claim', '2999-01-01 00:00:00', ?, ?
            )
            """,
            (
                json.dumps([1]),
                json.dumps(
                    [
                        {
                            "id": 1,
                            "code": "typo",
                            "name": "错别字检查",
                            "prompt": "检查错别字",
                        }
                    ],
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task_id = db.execute("SELECT id FROM tasks").fetchone()["id"]
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            return "完成"

        with patch("app.tasks.runner.run_check", side_effect=fake_run_check):
            TaskRunner(self.app).run(task_id, "test-claim")

        updated = db.execute(
            "SELECT status, result_json, api_key, claim_token, lease_expires_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        self.assertEqual(updated["status"], "completed")
        self.assertIsNone(updated["api_key"])
        self.assertIsNone(updated["claim_token"])
        self.assertIsNone(updated["lease_expires_at"])
        self.assertEqual(calls[0]["document_text"], "file: missing.txt\n\n缓存文本")
        self.assertEqual(calls[0]["api_key"], "task-secret")

    def test_run_task_extracts_and_persists_document_text_in_worker(self):
        upload_path = Path(self.app.config["UPLOAD_FOLDER"]) / "queued.txt"
        upload_path.write_text("后台解析正文", encoding="utf-8")
        check_item = {
            "id": 1,
            "code": "typo",
            "name": "错别字检查",
            "prompt": "检查错别字",
        }
        task_id = self._insert_running_preprocessing_task(
            task_type=DOCUMENT_TASK_TYPE,
            original_filename="queued.txt",
            stored_filename="queued.txt",
            file_type="txt",
            check_item=check_item,
        )
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            return "完成"

        with patch("app.tasks.runner.run_check", side_effect=fake_run_check):
            TaskRunner(self.app).run(task_id)

        updated = (
            get_db()
            .execute(
                "SELECT status, document_text, document_meta_json FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        meta = json.loads(updated["document_meta_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["document_text"], "file: queued.txt\n\n后台解析正文")
        self.assertEqual(meta["preprocessing"]["status"], "completed")
        self.assertEqual(calls[0]["document_text"], updated["document_text"])

    def test_run_task_persists_hyperlinks_and_executes_rule_check(self):
        upload_path = Path(self.app.config["UPLOAD_FOLDER"]) / "links.html"
        upload_path.write_text(
            '<p>请参见<a href="javascript:alert(1)">安装指南</a></p>',
            encoding="utf-8",
        )
        check_item = {
            "id": 1,
            "code": HYPERLINK_CHECK_CODE,
            "name": "超链接有效性检查",
            "prompt": "本地规则",
        }
        task_id = self._insert_running_preprocessing_task(
            task_type=DOCUMENT_TASK_TYPE,
            original_filename="links.html",
            stored_filename="links.html",
            file_type="html",
            check_item=check_item,
        )

        with patch(
            "app.tasks.runner.run_check",
            side_effect=AssertionError("should not call llm"),
        ):
            TaskRunner(self.app).run(task_id)

        updated = (
            get_db()
            .execute(
                "SELECT status, document_meta_json, result_json FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        meta = json.loads(updated["document_meta_json"])
        results = json.loads(updated["result_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(meta["hyperlinks"][0]["target"], "javascript:alert(1)")
        self.assertEqual(meta["hyperlinks"][0]["source"], "links.html")
        self.assertEqual(
            results[0]["structured_report"]["items"][0]["category"],
            "超链接格式错误",
        )

    def test_run_task_marks_oversized_document_failed_after_worker_extraction(self):
        upload_path = Path(self.app.config["UPLOAD_FOLDER"]) / "long.txt"
        upload_path.write_text("超长正文" * 20, encoding="utf-8")
        check_item = {
            "id": 1,
            "code": "typo",
            "name": "错别字检查",
            "prompt": "检查错别字",
        }
        task_id = self._insert_running_preprocessing_task(
            task_type=DOCUMENT_TASK_TYPE,
            original_filename="long.txt",
            stored_filename="long.txt",
            file_type="txt",
            check_item=check_item,
            max_input_chars=20,
        )

        with patch("app.tasks.runner.run_check") as run_check_mock:
            TaskRunner(self.app).run(task_id)

        updated = (
            get_db()
            .execute(
                "SELECT status, error, document_text FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        run_check_mock.assert_not_called()
        self.assertEqual(updated["status"], "failed")
        self.assertIn("超过当前模型文本上限", updated["error"])
        self.assertIsNone(updated["document_text"])
        self.assertTrue(upload_path.is_file())

    def test_run_image_task_extracts_images_and_persists_metadata_in_worker(self):
        upload_path = Path(self.app.config["UPLOAD_FOLDER"]) / "diagram.pdf"
        upload_path.write_bytes(b"pdf-bytes")
        check_item = {
            "id": 9,
            "code": "image-small-language-text",
            "name": "图片语种匹配检查",
            "prompt": "检查图片文字语种是否和文档一致",
        }
        task_id = self._insert_running_preprocessing_task(
            task_type=IMAGE_TASK_TYPE,
            original_filename="diagram.pdf",
            stored_filename="diagram.pdf",
            file_type="pdf",
            check_item=check_item,
            document_meta={
                "source_document": {
                    "original_filename": "diagram.pdf",
                    "stored_filename": "diagram.pdf",
                    "file_type": "pdf",
                    "file_size": len(b"pdf-bytes"),
                },
                "preprocessing": {"status": "pending"},
            },
        )

        def fake_extract_images(
            _document_path, _file_type, output_dir, *, source_filename=""
        ):
            output_dir.mkdir(parents=True, exist_ok=True)
            image_path = output_dir / "0001_page001-image001.png"
            image_path.write_bytes(b"png-bytes")
            return [
                {
                    "id": "image-0001",
                    "filename": image_path.name,
                    "stored_filename": image_path.name,
                    "mime_type": "image/png",
                    "position": "page001-image001",
                    "source": source_filename,
                    "size_bytes": image_path.stat().st_size,
                    "kind": "resource",
                    "page_number": 1,
                }
            ]

        with (
            patch(
                "app.tasks.runtime.preprocessing.extract_text",
                return_value="[第1页]\n图 1 是接线图。",
            ),
            patch(
                "app.tasks.runtime.preprocessing.extract_images",
                side_effect=fake_extract_images,
            ),
            patch(
                "app.tasks.runtime.preprocessing.render_pdf_page_images",
                return_value=(
                    [],
                    {
                        "total_pages": 1,
                        "selected_pages": [],
                        "omitted_pages": 1,
                        "max_pages": 120,
                    },
                ),
            ),
            patch(
                "app.tasks.runtime.multimodal_protocol.run_multimodal_document_check",
                return_value="未发现问题",
            ),
        ):
            TaskRunner(self.app).run(task_id)

        updated = (
            get_db()
            .execute(
                "SELECT status, document_text, document_meta_json FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        meta = json.loads(updated["document_meta_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertIn("document_text:", updated["document_text"])
        self.assertEqual(meta["preprocessing"]["status"], "completed")
        self.assertEqual(len(meta["images"]), 1)
        self.assertTrue(
            (
                Path(self.app.config["IMAGE_FOLDER"])
                / meta["images"][0]["relative_path"]
            ).is_file()
        )

    def test_run_video_task_extracts_frames_and_persists_metadata_in_worker(self):
        upload_path = Path(self.app.config["UPLOAD_FOLDER"]) / "install.mp4"
        upload_path.write_bytes(b"video-bytes")
        check_item = {
            "id": 10,
            "code": "video-installation-sequence",
            "name": "安装顺序检查",
            "prompt": "检查安装顺序",
        }
        task_id = self._insert_running_preprocessing_task(
            task_type=VIDEO_TASK_TYPE,
            original_filename="install.mp4",
            stored_filename="install.mp4",
            file_type="mp4",
            check_item=check_item,
            document_meta={
                "source_video": {
                    "original_filename": "install.mp4",
                    "stored_filename": "install.mp4",
                    "file_type": "mp4",
                    "file_size": len(b"video-bytes"),
                },
                "preprocessing": {"status": "pending"},
            },
        )

        def fake_extract_video_frames(
            _video_path, output_dir, *, source_filename="", max_frames=16
        ):
            output_dir.mkdir(parents=True, exist_ok=True)
            frame_path = output_dir / "0001_t000001000.jpg"
            frame_path.write_bytes(b"jpeg-bytes")
            return (
                [
                    {
                        "id": "frame-0001",
                        "filename": frame_path.name,
                        "stored_filename": frame_path.name,
                        "mime_type": "image/jpeg",
                        "position": "00:01.000",
                        "source": source_filename,
                        "size_bytes": frame_path.stat().st_size,
                        "kind": "video_frame",
                        "timestamp_seconds": 1.0,
                    }
                ],
                {
                    "duration_seconds": 8.0,
                    "selected_timestamps": [1.0],
                    "frame_count": 1,
                    "max_frames": max_frames,
                    "strategy": "uniform-sampling",
                },
            )

        with (
            patch(
                "app.tasks.runtime.preprocessing.extract_video_frames",
                side_effect=fake_extract_video_frames,
            ),
            patch(
                "app.tasks.runtime.multimodal_protocol.run_multimodal_document_check",
                return_value="未发现问题",
            ),
        ):
            TaskRunner(self.app).run(task_id)

        updated = (
            get_db()
            .execute(
                "SELECT status, document_text, document_meta_json FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        meta = json.loads(updated["document_meta_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertIn("video_frames:", updated["document_text"])
        self.assertEqual(meta["preprocessing"]["status"], "completed")
        self.assertEqual(meta["frames"][0]["position"], "00:01.000")
        self.assertTrue(
            (
                Path(self.app.config["IMAGE_FOLDER"])
                / meta["frames"][0]["relative_path"]
            ).is_file()
        )

    def test_run_language_consistency_task_builds_static_precheck_in_worker(self):
        upload_folder = Path(self.app.config["UPLOAD_FOLDER"])
        (upload_folder / "zh.txt").write_text(
            "1. 安装要求\n设备电流为 10A。", encoding="utf-8"
        )
        (upload_folder / "en.txt").write_text(
            "1. Installation requirements\nThe device current is 12A.",
            encoding="utf-8",
        )
        check_item = {
            "id": 11,
            "code": "language-consistency-cross-lingual",
            "name": "跨语种一致性检查",
            "prompt": "检查两份文档是否一致",
        }
        task_id = self._insert_running_preprocessing_task(
            task_type=LANGUAGE_CONSISTENCY_TASK_TYPE,
            original_filename="跨语种检查：zh.txt / en.txt",
            stored_filename="zh.txt",
            file_type="双文档",
            check_item=check_item,
            document_meta={
                "groups": [
                    {
                        "role": "document_a",
                        "label": "文档A",
                        "files": [
                            {
                                "original_filename": "zh.txt",
                                "stored_filename": "zh.txt",
                                "file_type": "txt",
                                "file_size": 1,
                            }
                        ],
                    },
                    {
                        "role": "document_b",
                        "label": "文档B",
                        "files": [
                            {
                                "original_filename": "en.txt",
                                "stored_filename": "en.txt",
                                "file_type": "txt",
                                "file_size": 1,
                            }
                        ],
                    },
                ],
                "preprocessing": {"status": "pending"},
            },
        )

        with patch("app.tasks.runner.run_check", return_value="完成"):
            TaskRunner(self.app).run(task_id)

        updated = (
            get_db()
            .execute(
                "SELECT status, document_text, document_meta_json FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        meta = json.loads(updated["document_meta_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertIn("# 静态预检摘要", updated["document_text"])
        self.assertIn("10a", meta["static_precheck"])
        self.assertIn("12a", meta["static_precheck"])
        self.assertEqual(meta["preprocessing"]["status"], "completed")

    def test_run_task_continues_other_checks_and_marks_partial(self):
        check_items = [
            {
                "id": 1,
                "code": "compliance",
                "name": "文档规范性检查",
                "prompt": "检查规范性",
            },
            {
                "id": 2,
                "code": "clarity",
                "name": "易理解性检查",
                "prompt": "检查易理解性",
            },
            {
                "id": 3,
                "code": COMMON_TERMS_CHECK_CODE,
                "name": "常用词检查",
                "prompt": "本地检查",
            },
        ]
        task_id = self._insert_running_document_task(check_items)
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs["check_name"])
            if kwargs["check_name"] == "文档规范性检查":
                raise LLMError("模型流式正文疑似重复输出")
            return "易理解性检查完成"

        with patch("app.tasks.runner.run_check", side_effect=fake_run_check):
            TaskRunner(self.app).run(task_id)

        updated = (
            get_db()
            .execute(
                "SELECT status, progress, result_json, summary, error, api_key FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        results = json.loads(updated["result_json"])
        self.assertEqual(calls, ["文档规范性检查", "易理解性检查"])
        self.assertEqual(updated["status"], "partial")
        self.assertEqual(updated["progress"], 100)
        self.assertIsNone(updated["api_key"])
        self.assertIn("已完成 2/3 个检查项", updated["summary"])
        self.assertIn("文档规范性检查", updated["error"])
        self.assertEqual(
            [result["code"] for result in results],
            ["compliance", "clarity", COMMON_TERMS_CHECK_CODE],
        )
        self.assertEqual(results[0]["error"], "模型流式正文疑似重复输出")
        self.assertEqual(results[1]["result"], "易理解性检查完成")
        self.assertIn("structured_report", results[2])

    def test_run_task_marks_failed_when_all_checks_fail(self):
        check_items = [
            {
                "id": 1,
                "code": "compliance",
                "name": "文档规范性检查",
                "prompt": "检查规范性",
            },
            {
                "id": 2,
                "code": "clarity",
                "name": "易理解性检查",
                "prompt": "检查易理解性",
            },
        ]
        task_id = self._insert_running_document_task(check_items)

        with patch(
            "app.tasks.runner.run_check", side_effect=LLMError("模型服务不可用")
        ):
            TaskRunner(self.app).run(task_id)

        updated = (
            get_db()
            .execute(
                "SELECT status, progress, result_json, summary, error, api_key FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        results = json.loads(updated["result_json"])
        self.assertEqual(updated["status"], "failed")
        self.assertIsNone(updated["api_key"])
        self.assertIn("2 个检查项全部失败", updated["summary"])
        self.assertIn("2 个检查项失败", updated["error"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["error"] == "模型服务不可用" for result in results))

    def test_retry_run_only_executes_failed_item_and_replaces_result_by_code(self):
        check_items = [
            {
                "id": 1,
                "code": "compliance",
                "name": "文档规范性检查",
                "prompt": "检查规范性",
            },
            {
                "id": 2,
                "code": "clarity",
                "name": "易理解性检查",
                "prompt": "检查易理解性",
            },
        ]
        task_id = self._insert_running_document_task(check_items)
        old_results = [
            {
                "code": "compliance",
                "name": "文档规范性检查",
                "result": "旧的失败输出",
                "error": "模型服务不可用",
            },
            {
                "code": "clarity",
                "name": "易理解性检查",
                "result": "原成功结果",
                "item_classifications": {"item-1": "issue"},
            },
        ]
        get_db().execute(
            """
            UPDATE tasks
            SET result_json = ?, retry_check_codes_json = ?
            WHERE id = ?
            """,
            (
                json.dumps(old_results, ensure_ascii=False),
                json.dumps(["compliance"]),
                task_id,
            ),
        )
        get_db().commit()
        calls = []
        live_snapshots = []

        def run_retry(**kwargs):
            calls.append(kwargs["check_name"])
            live = (
                get_db()
                .execute(
                    "SELECT result_json FROM task_live_results WHERE task_id = ?",
                    (task_id,),
                )
                .fetchone()
            )
            live_snapshots.append(json.loads(live["result_json"]))
            return "重试成功结果"

        with patch("app.tasks.runner.run_check", side_effect=run_retry):
            TaskRunner(self.app).run(task_id)

        updated = (
            get_db()
            .execute(
                """
            SELECT status, result_json, retry_check_codes_json, api_key
            FROM tasks
            WHERE id = ?
            """,
                (task_id,),
            )
            .fetchone()
        )
        results = json.loads(updated["result_json"])
        self.assertEqual(calls, ["文档规范性检查"])
        self.assertEqual(live_snapshots, [[old_results[1]]])
        self.assertEqual(updated["status"], "completed")
        self.assertIsNone(updated["retry_check_codes_json"])
        self.assertIsNone(updated["api_key"])
        self.assertEqual(
            [result["code"] for result in results], ["compliance", "clarity"]
        )
        self.assertEqual(results[0]["result"], "重试成功结果")
        self.assertNotIn("error", results[0])
        self.assertEqual(results[1], old_results[1])

    def test_retry_run_keeps_successful_result_when_failed_item_fails_again(self):
        check_items = [
            {
                "id": 1,
                "code": "compliance",
                "name": "文档规范性检查",
                "prompt": "检查规范性",
            },
            {
                "id": 2,
                "code": "clarity",
                "name": "易理解性检查",
                "prompt": "检查易理解性",
            },
        ]
        task_id = self._insert_running_document_task(check_items)
        old_results = [
            {
                "code": "compliance",
                "name": "文档规范性检查",
                "result": "",
                "error": "首次失败",
            },
            {"code": "clarity", "name": "易理解性检查", "result": "原成功结果"},
        ]
        get_db().execute(
            "UPDATE tasks SET result_json = ?, retry_check_codes_json = ? WHERE id = ?",
            (
                json.dumps(old_results, ensure_ascii=False),
                json.dumps(["compliance"]),
                task_id,
            ),
        )
        get_db().commit()

        with patch(
            "app.tasks.runner.run_check", side_effect=LLMError("重试仍失败")
        ) as run_check_mock:
            TaskRunner(self.app).run(task_id)

        updated = (
            get_db()
            .execute(
                "SELECT status, result_json, retry_check_codes_json FROM tasks WHERE id = ?",
                (task_id,),
            )
            .fetchone()
        )
        results = json.loads(updated["result_json"])
        self.assertEqual(run_check_mock.call_count, 1)
        self.assertEqual(updated["status"], "partial")
        self.assertIsNone(updated["retry_check_codes_json"])
        self.assertEqual(results[0]["error"], "重试仍失败")
        self.assertEqual(results[1], old_results[1])

    def test_merge_check_results_replaces_in_original_order_and_appends_new_codes(self):
        merged = _merge_check_results(
            [
                {"code": "a", "result": "旧 A"},
                {"code": "b", "result": "保留 B"},
            ],
            [
                {"code": "a", "result": "新 A"},
                {"code": "c", "result": "新增 C"},
            ],
        )

        self.assertEqual([item["code"] for item in merged], ["a", "b", "c"])
        self.assertEqual(merged[0]["result"], "新 A")

    def test_consistency_task_uses_selected_check_snapshot(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, checks_snapshot_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                ?, '127.0.0.1', '多文档对照检查：素材1个 / 资料1个', 'master.txt', '多文档', 1,
                '# 素材文档\n素材参数 10A\n\n# 资料\n资料参数 12A', ?, ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                CONSISTENCY_TASK_TYPE,
                json.dumps([7]),
                json.dumps(
                    [
                        {
                            "id": 7,
                            "code": "custom-consistency",
                            "name": "参数一致性检查",
                            "prompt": "只检查参数是否一致",
                        }
                    ],
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task_id = db.execute("SELECT id FROM tasks").fetchone()["id"]
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            return "发现参数不一致"

        with patch("app.tasks.runner.run_check", side_effect=fake_run_check):
            TaskRunner(self.app).run(task_id)

        updated = db.execute(
            "SELECT status, result_json FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(calls[0]["check_name"], "参数一致性检查")
        self.assertEqual(calls[0]["prompt"], "只检查参数是否一致")

    def test_consistency_task_supports_legacy_code_checks_json(self):
        db = get_db()
        created_at = now_text()
        db.execute(
            """
            INSERT INTO check_items(task_type, code, name, description, prompt, enabled, sort_order, created_at, updated_at)
            VALUES (?, 'consistency-cross-document', '多文档对照检查', '', '默认多文档对照提示词', 1, 10, ?, ?)
            """,
            (CONSISTENCY_TASK_TYPE, created_at, created_at),
        )
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                ?, '127.0.0.1', '多文档对照检查：素材1个 / 资料1个', 'master.txt', '多文档', 1,
                '# 素材文档\n素材参数 10A\n\n# 资料\n资料参数 12A', ?, 'test-model', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                CONSISTENCY_TASK_TYPE,
                json.dumps(["consistency-cross-document"], ensure_ascii=False),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task_id = db.execute("SELECT id FROM tasks").fetchone()["id"]
        calls = []

        def fake_run_check(**kwargs):
            calls.append(kwargs)
            return "完成"

        with patch("app.tasks.runner.run_check", side_effect=fake_run_check):
            TaskRunner(self.app).run(task_id)

        self.assertEqual(calls[0]["check_name"], "多文档对照检查")
        self.assertEqual(calls[0]["prompt"], "默认多文档对照提示词")

    def test_image_task_runs_multimodal_check_for_extracted_images(self):
        image_dir = Path(self.app.config["IMAGE_FOLDER"]) / "task-images"
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "0120_page094-image001.bin").write_bytes(b"unknown-bytes")
        (image_dir / "0001_page001-image001.png").write_bytes(b"png-bytes")
        db = get_db()
        created_at = now_text()
        image_meta = {
            "images": [
                {
                    "id": "image-0120",
                    "filename": "0120_page094-image001.bin",
                    "relative_path": "task-images/0120_page094-image001.bin",
                    "mime_type": "application/octet-stream",
                    "position": "page094-image001",
                    "source": "图纸.pdf",
                    "size_bytes": 13,
                },
                {
                    "id": "image-0001",
                    "filename": "0001_page001-image001.png",
                    "relative_path": "task-images/0001_page001-image001.png",
                    "mime_type": "image/png",
                    "position": "page001-image001",
                    "source": "图纸.pdf",
                    "size_bytes": 9,
                },
            ]
        }
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json, model_name, api_base, request_timeout, max_input_chars,
                reasoning_effort, status, progress, created_at, updated_at
            )
            VALUES (
                ?, '127.0.0.1', '图纸.pdf', '图纸.pdf', 'pdf', 1,
                ?, ?, ?, ?, 'qwen-vl', 'http://example.test/v1/chat/completions', 30, 5000, 'xhigh',
                'running', 0, ?, ?
            )
            """,
            (
                IMAGE_TASK_TYPE,
                "file: 图纸.pdf\n\ndocument_text:\n图 1 是电源接线图。",
                json.dumps(image_meta, ensure_ascii=False),
                json.dumps([9]),
                json.dumps(
                    [
                        {
                            "id": 9,
                            "code": "image-small-language-text",
                            "name": "图片语种匹配检查",
                            "prompt": "检查图片文字语种是否和文档一致",
                        }
                    ],
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task_id = db.execute("SELECT id FROM tasks").fetchone()["id"]
        set_setting("issue_output_limit", 0)
        calls = []

        def fake_run_multimodal_document_check(**kwargs):
            calls.append(kwargs)
            kwargs["on_content"]("流式图文结果")
            return "图文最终结果\n发现问题：图片中中文说明与英文文档语种不一致。\n需人工确认：截图底部文字较小。"

        with patch(
            "app.tasks.runtime.multimodal_protocol.run_multimodal_document_check",
            side_effect=fake_run_multimodal_document_check,
        ):
            TaskRunner(self.app).run(task_id)

        updated = db.execute(
            "SELECT status, result_json FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        results = json.loads(updated["result_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["check_name"], "图片资源检查合并检查（1项）")
        self.assertIn("image-small-language-text", calls[0]["prompt"])
        self.assertIn("图片语种匹配检查", calls[0]["prompt"])
        self.assertIn("图 1 是电源接线图", calls[0]["document_text"])
        self.assertEqual(calls[0]["batch_index"], 1)
        self.assertEqual(calls[0]["batch_count"], 1)
        self.assertEqual(calls[0]["reasoning_effort"], "xhigh")
        self.assertEqual(calls[0]["issue_output_limit"], 30)
        self.assertEqual(calls[0]["image_items"][0]["index"], 2)
        self.assertEqual(
            calls[0]["image_items"][0]["name"], "0001_page001-image001.png"
        )
        self.assertEqual(
            calls[0]["image_items"][0]["position"], "PDF第1页（page001-image001）"
        )
        self.assertTrue(
            calls[0]["image_items"][0]["data_url"].startswith("data:image/png;base64,")
        )
        self.assertIn(
            "PDF第1页（page001-image001）：0001_page001-image001.png",
            results[0]["result"],
        )
        self.assertIn("0001_page001-image001.png", results[0]["result"])
        self.assertIn("0120_page094-image001.bin", results[0]["result"])
        self.assertIn("已跳过的图片", results[0]["result"])
        self.assertIn("图文最终结果", results[0]["result"])
        self.assertIn("检查汇总", results[0]["result"])
        self.assertIn("明确问题", results[0]["result"])
        self.assertIn("图片中中文说明与英文文档语种不一致", results[0]["result"])
        self.assertIn("需人工确认", results[0]["result"])

    def test_image_task_merges_page_level_checks_for_page_screenshots(self):
        image_dir = Path(self.app.config["IMAGE_FOLDER"]) / "task-pages"
        image_dir.mkdir(parents=True, exist_ok=True)
        (image_dir / "0001_page001-screenshot.png").write_bytes(b"page-png-bytes")
        db = get_db()
        created_at = now_text()
        image_meta = {
            "page_selection": {
                "total_pages": 150,
                "selected_pages": [1],
                "omitted_pages": 149,
                "max_pages": 1,
                "strategy": "candidate-and-segment-sampling",
            },
            "images": [],
            "page_images": [
                {
                    "id": "page-0001",
                    "filename": "0001_page001-screenshot.png",
                    "relative_path": "task-pages/0001_page001-screenshot.png",
                    "mime_type": "image/png",
                    "position": "page001-screenshot",
                    "source": "图纸.pdf",
                    "size_bytes": 14,
                    "kind": "page",
                    "page_number": 1,
                }
            ],
        }
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json, model_name, api_base, request_timeout, max_input_chars,
                status, progress, created_at, updated_at
            )
            VALUES (
                ?, '127.0.0.1', '图纸.pdf', '图纸.pdf', 'pdf', 1,
                ?, ?, ?, ?, 'qwen-vl', 'http://example.test/v1/chat/completions', 30, 5000,
                'running', 0, ?, ?
            )
            """,
            (
                IMAGE_TASK_TYPE,
                "file: 图纸.pdf\n\ndocument_text:\n[第1页]\n3.1 参数\n项目 参数 单位",
                json.dumps(image_meta, ensure_ascii=False),
                json.dumps([35, 38]),
                json.dumps(
                    [
                        {
                            "id": 35,
                            "code": "image-figure-table-title-standard",
                            "name": "图表标题规范检查",
                            "prompt": "检查图标题和表标题是否缺失",
                        },
                        {
                            "id": 38,
                            "code": "image-integrity-clarity",
                            "name": "图片完整性和清晰度检查",
                            "prompt": "检查图片完整性和清晰度",
                        },
                    ],
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task_id = db.execute("SELECT id FROM tasks").fetchone()["id"]
        calls = []

        def fake_run_multimodal_document_check(**kwargs):
            calls.append(kwargs)
            return json.dumps(
                {
                    "results": [
                        {
                            "code": "image-figure-table-title-standard",
                            "summary": "发现表标题缺失。",
                            "items": [
                                {
                                    "status": "issue",
                                    "severity": "medium",
                                    "confidence": "high",
                                    "category": "图表标题",
                                    "location": "page001",
                                    "excerpt": "表格上方未见标题",
                                    "description": "表格缺少表标题。",
                                    "impact": "不利于引用表格",
                                    "suggestion": "补充表编号和标题",
                                }
                            ],
                        },
                        {
                            "code": "image-integrity-clarity",
                            "summary": "未发现明确清晰度问题。",
                            "items": [
                                {
                                    "status": "suggestion",
                                    "severity": "low",
                                    "confidence": "low",
                                    "category": "清晰度",
                                    "location": "page001",
                                    "excerpt": "页面截图较小",
                                    "description": "页面截图较小，需人工确认清晰度。",
                                    "impact": "",
                                    "suggestion": "查看原始 PDF",
                                }
                            ],
                        },
                    ]
                },
                ensure_ascii=False,
            )

        with patch(
            "app.tasks.runtime.multimodal_protocol.run_multimodal_document_check",
            side_effect=fake_run_multimodal_document_check,
        ):
            TaskRunner(self.app).run(task_id)

        updated = db.execute(
            "SELECT status, result_json FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        results = json.loads(updated["result_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["check_name"], "页面级检查合并检查（2项）")
        self.assertIn("image-figure-table-title-standard", calls[0]["prompt"])
        self.assertIn("image-integrity-clarity", calls[0]["prompt"])
        self.assertIn("PDF 页码", calls[0]["prompt"])
        self.assertEqual(calls[0]["output_contract"], "multi_check_json")
        self.assertEqual(
            calls[0]["image_items"][0]["position"], "PDF第1页（page001-screenshot）"
        )
        self.assertEqual(
            [item["code"] for item in results],
            ["image-figure-table-title-standard", "image-integrity-clarity"],
        )
        self.assertIn(
            "PDF第1页（page001-screenshot）：0001_page001-screenshot.png",
            results[0]["result"],
        )
        self.assertIn("page001：表格缺少表标题", results[0]["result"])
        self.assertIn(
            "page001：表格缺少表标题", results[0]["result"].split("### 检查汇总", 1)[-1]
        )
        self.assertIn("页面截图较小，需人工确认清晰度", results[1]["result"])
        self.assertIn(
            "页面截图较小，需人工确认清晰度",
            results[1]["result"].split("### 检查汇总", 1)[-1],
        )
        self.assertIn("未覆盖 149 页", results[0]["result"])

    def test_combined_json_output_maps_results_by_exact_code(self):
        check_items = [
            {"code": "video-a", "name": "检查A"},
            {"code": "video-b", "name": "检查B"},
        ]
        content = json.dumps(
            {
                "results": [
                    {
                        "code": "video-a",
                        "summary": "发现问题",
                        "items": [
                            {
                                "status": "issue",
                                "location": "00:01.000",
                                "description": "安装顺序错误",
                                "impact": "可能损坏设备",
                                "suggestion": "调整安装顺序",
                            }
                        ],
                    },
                    {"code": "unknown", "summary": "忽略", "items": []},
                ]
            },
            ensure_ascii=False,
        )

        recognized = _split_combined_check_output(
            content, check_items, fill_missing=False
        )
        completed = _split_combined_check_output(content, check_items)

        self.assertEqual(list(recognized), ["video-a"])
        self.assertIn("00:01.000：安装顺序错误", recognized["video-a"])
        self.assertIn("建议：调整安装顺序", recognized["video-a"])
        self.assertIn("模型未按要求返回该检查项的独立结果", completed["video-b"])

    def test_combined_structured_output_preserves_video_report_fields(self):
        check_items = [{"code": "video-a", "name": "检查A"}]
        content = json.dumps(
            {
                "results": [
                    {
                        "code": "video-a",
                        "summary": "发现两个问题",
                        "items": [
                            {
                                "status": "issue",
                                "severity": "high",
                                "confidence": "high",
                                "category": "安装顺序",
                                "location": "00:01.000",
                                "excerpt": "设备已上电",
                                "description": "未确认接地线后直接上电",
                                "impact": "存在安全风险",
                                "suggestion": "先确认接地",
                            },
                            {
                                "status": "suggestion",
                                "severity": "low",
                                "confidence": "low",
                                "category": "画面完整性",
                                "location": "00:03.000",
                                "excerpt": "端子被遮挡",
                                "description": "无法确认接线关系",
                                "impact": "可能漏检",
                                "suggestion": "人工回看原视频",
                            },
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        )

        sections = _split_combined_structured_output(content, check_items)

        self.assertEqual(sections["video-a"]["summary"], "发现两个问题")
        self.assertEqual(len(sections["video-a"]["items"]), 2)
        self.assertEqual(sections["video-a"]["items"][0]["severity"], "high")
        self.assertEqual(sections["video-a"]["items"][1]["status"], "suggestion")

    def test_video_batch_reports_merge_duplicate_issue_and_evidence_frames(self):
        frames = [
            {
                "id": "frame-0001",
                "filename": "0001_t000001000.jpg",
                "relative_path": "task-video/0001_t000001000.jpg",
                "mime_type": "image/jpeg",
                "position": "00:01.000",
                "timestamp_seconds": 1.0,
            },
            {
                "id": "frame-0002",
                "filename": "0002_t000003000.jpg",
                "relative_path": "task-video/0002_t000003000.jpg",
                "mime_type": "image/jpeg",
                "position": "00:03.000",
                "timestamp_seconds": 3.0,
            },
        ]
        batch_results = []
        for index, frame in enumerate(frames, start=1):
            batch_results.append(
                {
                    "batch_index": index,
                    "batch_count": 2,
                    "images": [frame],
                    "structured_report": {
                        "summary": "发现问题",
                        "items": [
                            {
                                "status": "issue",
                                "severity": "high",
                                "confidence": "medium" if index == 1 else "high",
                                "category": "安装顺序",
                                "location": frame["position"],
                                "excerpt": "设备已上电",
                                "description": "未确认接地线后直接上电",
                                "impact": "存在安全风险",
                                "suggestion": "先确认接地",
                            }
                        ],
                    },
                }
            )

        report = _merge_video_batch_reports("video-a", batch_results)
        reversed_report = _merge_video_batch_reports(
            "video-a", list(reversed(batch_results))
        )

        self.assertEqual(len(report["items"]), 1)
        item = report["items"][0]
        self.assertEqual(item["confidence"], "high")
        self.assertEqual(len(item["evidence_refs"]), 2)
        self.assertEqual(item["location"], "视频时间 00:01.000、00:03.000")
        self.assertEqual(item["id"], reversed_report["items"][0]["id"])

    def test_combined_output_keeps_legacy_markdown_compatibility(self):
        check_items = [
            {"code": "video-a", "name": "检查A"},
            {"code": "video-b", "name": "检查B"},
        ]
        content = """### 检查项：video-a｜检查A
#### 总体判断
未发现明确问题。

### 检查项：video-b｜检查B
#### 总体判断
需人工确认。"""

        sections = _split_combined_check_output(
            content, check_items, fill_missing=False
        )

        self.assertEqual(set(sections), {"video-a", "video-b"})

    def test_multimodal_check_repairs_only_missing_codes(self):
        check_items = [
            {"code": "video-a", "name": "检查A"},
            {"code": "video-b", "name": "检查B"},
        ]
        responses = [
            json.dumps(
                {"results": [{"code": "video-a", "summary": "检查A完成", "items": []}]},
                ensure_ascii=False,
            ),
            json.dumps(
                {"results": [{"code": "video-b", "summary": "检查B完成", "items": []}]},
                ensure_ascii=False,
            ),
        ]

        with patch(
            "app.tasks.runtime.multimodal_protocol.run_multimodal_document_check",
            side_effect=responses,
        ) as runner:
            sections = _run_combined_multimodal_check_with_repair(
                self.app,
                check_items=check_items,
                prompt_builder=lambda items: ",".join(item["code"] for item in items),
                check_name="视频检查",
                error_label="视频",
                run_kwargs={"task_id": 7, "on_content": lambda content: None},
            )

        self.assertEqual(set(sections), {"video-a", "video-b"})
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].kwargs["prompt"], "video-a,video-b")
        self.assertIn("video-b", runner.call_args_list[1].kwargs["prompt"])
        self.assertNotIn("video-a,video-b", runner.call_args_list[1].kwargs["prompt"])
        self.assertNotIn("on_content", runner.call_args_list[1].kwargs)

    def test_multimodal_check_fails_after_two_unrecognized_responses(self):
        check_items = [
            {"code": "video-a", "name": "检查A"},
            {"code": "video-b", "name": "检查B"},
        ]

        with (
            patch(
                "app.tasks.runtime.multimodal_protocol.run_multimodal_document_check",
                side_effect=["无法解析", "仍然无法解析"],
            ) as runner,
            self.assertRaisesRegex(RuntimeError, "连续两次未返回可识别"),
        ):
            _run_combined_multimodal_check_with_repair(
                self.app,
                check_items=check_items,
                prompt_builder=lambda items: ",".join(item["code"] for item in items),
                check_name="视频检查",
                error_label="视频",
                run_kwargs={"task_id": 8},
            )

        self.assertEqual(runner.call_count, 2)

    def test_multimodal_check_marks_still_missing_code_after_repair(self):
        check_items = [
            {"code": "video-a", "name": "检查A"},
            {"code": "video-b", "name": "检查B"},
        ]
        first_response = json.dumps(
            {"results": [{"code": "video-a", "summary": "检查A完成", "items": []}]},
            ensure_ascii=False,
        )

        with patch(
            "app.tasks.runtime.multimodal_protocol.run_multimodal_document_check",
            side_effect=[first_response, "无法解析"],
        ):
            sections = _run_combined_multimodal_check_with_repair(
                self.app,
                check_items=check_items,
                prompt_builder=lambda items: ",".join(item["code"] for item in items),
                check_name="视频检查",
                error_label="视频",
                run_kwargs={"task_id": 9},
            )

        self.assertIn("检查A完成", sections["video-a"])
        self.assertIn("模型未按要求返回该检查项的独立结果", sections["video-b"])

    def test_multimodal_check_items_are_grouped_by_three(self):
        groups = _check_item_groups([{"code": f"check-{index}"} for index in range(7)])

        self.assertEqual([len(group) for group in groups], [3, 3, 1])

    def test_video_task_runs_multimodal_check_for_extracted_frames(self):
        frame_dir = Path(self.app.config["IMAGE_FOLDER"]) / "task-video"
        frame_dir.mkdir(parents=True, exist_ok=True)
        (frame_dir / "0001_t000001000.jpg").write_bytes(b"jpeg-bytes")
        db = get_db()
        created_at = now_text()
        video_meta = {
            "frame_selection": {
                "duration_seconds": 8.0,
                "selected_timestamps": [1.0],
                "max_frames": 16,
                "frame_count": 1,
                "strategy": "uniform-sampling",
            },
            "frames": [
                {
                    "id": "frame-0001",
                    "filename": "0001_t000001000.jpg",
                    "relative_path": "task-video/0001_t000001000.jpg",
                    "mime_type": "image/jpeg",
                    "position": "00:01.000",
                    "source": "安装.mp4",
                    "size_bytes": 10,
                    "kind": "video_frame",
                    "timestamp_seconds": 1.0,
                }
            ],
        }
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json, model_name, api_base, request_timeout, max_input_chars,
                reasoning_effort, status, progress, created_at, updated_at
            )
            VALUES (
                ?, '127.0.0.1', '安装.mp4', '安装.mp4', 'mp4', 10,
                ?, ?, ?, ?, 'qwen-vl', 'http://example.test/v1/chat/completions', 30, 5000, 'max',
                'running', 0, ?, ?
            )
            """,
            (
                VIDEO_TASK_TYPE,
                "file: 安装.mp4\n\nvideo_context:\n- 视频时长：00:08.000（8.0 秒）\nvideo_frames:\n- 0001_t000001000.jpg: 时间点 00:01.000",
                json.dumps(video_meta, ensure_ascii=False),
                json.dumps([91]),
                json.dumps(
                    [
                        {
                            "id": 91,
                            "code": "video-installation-sequence",
                            "name": "安装步骤顺序检查",
                            "prompt": "检查硬件安装视频中的步骤顺序",
                        }
                    ],
                    ensure_ascii=False,
                ),
                created_at,
                created_at,
            ),
        )
        db.commit()
        task_id = db.execute("SELECT id FROM tasks").fetchone()["id"]
        calls = []

        def fake_run_multimodal_document_check(**kwargs):
            calls.append(kwargs)
            return """### 检查项：video-installation-sequence｜安装步骤顺序检查
#### 总体判断
发现明确问题。
#### 明确问题
- 00:01.000：未确认接地线后直接上电，存在安全风险。
#### 需人工确认
- 未发现需人工确认项。
#### 未发现问题
- 未发现其他安装顺序问题。"""

        with patch(
            "app.tasks.runtime.multimodal_protocol.run_multimodal_document_check",
            side_effect=fake_run_multimodal_document_check,
        ):
            TaskRunner(self.app).run(task_id)

        updated = db.execute(
            "SELECT status, result_json FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        results = json.loads(updated["result_json"])
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["check_name"], "视频帧检查合并检查（1项）")
        self.assertIn("硬件产品安装调测视频质检", calls[0]["prompt"])
        self.assertIn("视频时间点", calls[0]["prompt"])
        self.assertIn("current_batch_video_frames", calls[0]["document_text"])
        self.assertEqual(calls[0]["reasoning_effort"], "max")
        self.assertEqual(calls[0]["image_items"][0]["position"], "00:01.000")
        self.assertTrue(
            calls[0]["image_items"][0]["data_url"].startswith("data:image/jpeg;base64,")
        )
        self.assertEqual(
            [item["code"] for item in results], ["video-installation-sequence"]
        )
        self.assertEqual(len(results[0]["structured_report"]["items"]), 1)
        self.assertEqual(results[0]["structured_report"]["items"][0]["status"], "issue")
        self.assertTrue(results[0]["structured_report"]["items"][0]["id"])
        self.assertIn("覆盖视频帧", results[0]["result"])
        self.assertIn("视频时间 00:01.000", results[0]["result"])
        self.assertIn("未确认接地线后直接上电", results[0]["result"])
        self.assertIn(
            "未确认接地线后直接上电", results[0]["result"].split("### 检查汇总", 1)[-1]
        )

    def test_qwen_vl_optimized_image_checks_use_expected_targets(self):
        self.assertEqual(
            _image_check_target({"code": "image-text-correspondence"}), "page"
        )
        self.assertEqual(_image_check_target({"code": "image-wiring"}), "resource")
        self.assertEqual(
            _image_check_target({"code": "image-ui-step-consistency"}), "page"
        )
        self.assertEqual(
            _image_check_target({"code": "image-device-installation"}), "resource"
        )

    def test_image_issue_summary_reads_bare_page_level_sections(self):
        summary = _format_image_check_issue_summary(
            [
                {
                    "batch_index": 23,
                    "batch_count": 30,
                    "images": [
                        {
                            "filename": "0089_page089-screenshot.png",
                            "position": "page089-screenshot",
                            "page_number": 89,
                        }
                    ],
                    "content": """### 检查项：image-figure-table-title-standard｜图表标题规范检查
总体判断
发现明确问题：图片 89 页面顶部的表格缺失规范的表编号和标题。

明确问题
- 图片 89（page089-screenshot）：页面顶部的表格缺失表编号。
  - 线索：表格上方仅有“设置时间”文字，未见“表X-X”格式标题。

需人工确认
- 未发现需人工确认项。""",
                }
            ],
            [],
        )

        self.assertIn("批次 23/30", summary)
        self.assertIn("PDF第89页", summary)
        self.assertIn(
            "图片 89（page089-screenshot）：页面顶部的表格缺失表编号", summary
        )
        self.assertNotIn("线索：表格上方仅有", summary)
        self.assertNotIn("未汇总到明确问题", summary)

    def test_image_issue_summary_filters_normal_items_from_clear_issues(self):
        summary = _format_image_check_issue_summary(
            [
                {
                    "batch_index": 1,
                    "batch_count": 1,
                    "images": [
                        {
                            "filename": "0003_page003-screenshot.png",
                            "position": "page003-screenshot",
                            "page_number": 3,
                        }
                    ],
                    "content": """### 检查项：image-integrity-clarity｜图片完整性和清晰度检查
#### 明确问题
- PDF第3页：页面显示正常，文字清晰。
- PDF第3页：未发现明确问题。
- PDF第3页：页面截图较小，需人工确认清晰度。
- PDF第3页：右下角表格标题缺失。
#### 需人工确认
- 未发现需人工确认项。""",
                }
            ],
            [],
        )

        issue_section = summary.split("#### 明确问题", 1)[-1].split(
            "#### 需人工确认", 1
        )[0]
        manual_section = summary.split("#### 需人工确认", 1)[-1]
        self.assertIn("PDF第3页", summary)
        self.assertIn("右下角表格标题缺失", issue_section)
        self.assertNotIn("页面显示正常", issue_section)
        self.assertNotIn("未发现明确问题", issue_section)
        self.assertNotIn("页面截图较小", issue_section)
        self.assertIn("页面截图较小，需人工确认清晰度", manual_section)

    def test_image_batch_uses_nearby_page_text_context(self):
        document_text = "\n\n".join(
            [
                "file: 图纸.pdf",
                "document_text:",
                "[第68页]\n前一页说明",
                "[第69页]\n图 7-5 叠光控制器 ESN码位置",
                "[第70页]\n图 7-6 光伏优化器 ESN码位置",
                "[第71页]\n后一页说明",
                "[第10页]\n无关安装步骤",
                "extracted_images: 1\n- 0105_page069-image001.png: page069-image001",
            ]
        )

        scoped = _document_text_for_image_batch(
            document_text,
            [
                {
                    "filename": "0105_page069-image001.png",
                    "position": "page069-image001",
                    "mime_type": "image/png",
                }
            ],
        )

        self.assertIn("document_text_scope", scoped)
        self.assertIn("[第68页]", scoped)
        self.assertIn("[第69页]", scoped)
        self.assertIn("[第70页]", scoped)
        self.assertIn("0105_page069-image001.png", scoped)
        self.assertNotIn("[第10页]", scoped)
        self.assertNotIn("extracted_images: 1", scoped)


if __name__ == "__main__":
    unittest.main()
