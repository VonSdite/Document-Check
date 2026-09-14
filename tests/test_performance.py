import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.checks.term_locations import DocumentLocationIndex
from app.documents.extraction.pdf import _pdf_line_text_variants
from app.identity.models import UserIdentity
from app.models.service import _find_enabled_model, _load_user_model_providers
from app.persistence.connection import get_db
from app.persistence.schema import init_db
from app.persistence.settings import set_setting
from app.reporting import statistics
from app.reporting.service import _empty_report_suppression_version
from app.tasks import files
from app.tasks.processes import TaskProcess
from app.tasks.submission import TaskSubmission, submit_document_task
from app.tasks.supervisor import TaskSupervisor
from app.web.task_lists import (
    _task_stats_for_where,
    _task_status_payload,
    _user_task_list_data,
)


class InternalPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(
            DATABASE=str(Path(self.temporary.name) / "test.sqlite3"),
            MAX_TASK_PROCESSES=4,
        )
        self.context = self.app.app_context()
        self.context.push()
        init_db()

    def tearDown(self):
        self.context.pop()
        self.temporary.cleanup()

    def insert_tasks(self, count, *, owner="ip:user", status="completed", result=None):
        get_db().executemany(
            """
            INSERT INTO tasks (
                ip, owner_subject, original_filename, stored_filename, file_type,
                file_size, checks_json, model_name, api_base, status, result_json,
                created_at, updated_at
            ) VALUES ('127.0.0.1', ?, 'sample.txt', 'sample.txt', 'txt', 1, '[]',
                      'sample', 'http://example.invalid', ?, ?, '2026-09-01', '2026-09-01')
            """,
            ((owner, status, result) for _ in range(count)),
        )
        get_db().commit()

    def vm_steps(self, callback):
        steps = 0

        def tick():
            nonlocal steps
            steps += 100
            return 0

        get_db().set_progress_handler(tick, 100)
        try:
            value = callback()
        finally:
            get_db().set_progress_handler(None, 0)
        return steps, value

    def test_user_counts_use_covering_index_for_the_requested_owner(self):
        self.insert_tasks(20)
        self.insert_tasks(3000, owner="ip:other")
        statements = []
        db = get_db()
        db.set_trace_callback(statements.append)
        try:
            stats = _task_stats_for_where(
                "owner_subject = ? AND task_type = ?", ("ip:user", "document_check")
            )
            with self.app.test_request_context("/?ids=1"):
                payload = _task_status_payload(
                    "document_check",
                    owner_clause="t.owner_subject = ?",
                    owner_params=("ip:user",),
                )
        finally:
            db.set_trace_callback(None)
        self.assertEqual(stats["total"], 20)
        self.assertEqual(payload["counts"]["tasks"], 20)
        counts = [
            sql
            for sql in statements
            if "COUNT(*) AS total FROM tasks" in sql or "COUNT(*) AS tasks" in sql
        ]
        self.assertEqual(len(counts), 2)
        for sql in counts:
            plan = " ".join(
                row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + sql)
            )
            self.assertIn("COVERING INDEX idx_tasks_type_owner_status", plan)
            self.assertNotIn("USE TEMP B-TREE", plan)

    def test_expired_file_selection_skips_cleaned_history(self):
        from app.tasks.runtime.artifacts import cleanup_expired_task_files

        self.insert_tasks(10000)
        db = get_db()
        db.execute("UPDATE tasks SET source_files_cleaned_at = '2026-09-01'")
        db.execute(
            "UPDATE tasks SET source_files_cleaned_at = NULL, finished_at = '2000-01-01' WHERE id IN (1, 2)"
        )
        db.commit()
        set_setting("task_file_retention_days", 1)
        statements = []
        db.set_trace_callback(statements.append)
        try:
            with patch("app.tasks.runtime.artifacts._remove_task_artifacts") as remove:
                steps, count = self.vm_steps(
                    lambda: cleanup_expired_task_files(self.app)
                )
        finally:
            db.set_trace_callback(None)
        self.assertEqual(count, 2)
        self.assertEqual({call.args[1]["id"] for call in remove.call_args_list}, {1, 2})
        self.assertLess(steps, 2000)
        selection = next(
            sql for sql in statements if "SELECT id, stored_filename" in sql
        )
        plan = " ".join(
            row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + selection)
        )
        self.assertIn("idx_tasks_pending_file_cleanup", plan)
        self.assertNotIn("USE TEMP B-TREE", plan)
        self.assertNotIn("SCAN tasks", plan)

    def poll_tasks(self, ids):
        with self.app.test_request_context("/?ids=" + ",".join(map(str, ids))):
            return _task_status_payload(
                "document_check",
                owner_clause="t.owner_subject = ?",
                owner_params=("ip:user",),
            )

    def test_polling_cached_and_active_tasks_does_not_read_report_bodies(self):
        self.insert_tasks(1, result="[]")
        self.insert_tasks(1, status="running", result="[]")
        db = get_db()
        db.execute(
            "INSERT INTO task_report_stats(task_id, source_updated_at, suppression_version, "
            "issue_count, reviewed_item_count, pending_review_item_count, updated_at) "
            "VALUES (1, '2026-09-01', ?, 3, 1, 2, '2026-09-01')",
            (_empty_report_suppression_version(),),
        )
        db.commit()
        statements = []
        db.set_trace_callback(statements.append)
        try:
            tasks = {task["id"]: task for task in self.poll_tasks([1, 2])["tasks"]}
        finally:
            db.set_trace_callback(None)
        self.assertEqual(tasks[1]["review_key"], "in_progress:1:3")
        self.assertEqual(tasks[2]["review_key"], "unavailable:0:0")
        self.assertFalse(any("result_json" in sql for sql in statements))

    def test_polling_tracks_report_invalidation_and_scopes_fallback_to_owner(self):
        self.insert_tasks(1, result="[]")
        self.insert_tasks(1, owner="ip:other", result="[]")
        db = get_db()
        statistics._task_report_stat_rows_for_where("t.id = ?", (1,))
        self.assertEqual(self.poll_tasks([1])["tasks"][0]["review_key"], "empty:0:0")
        # 同一时间戳内写入报告仍由数据库触发器使缓存失效。
        db.execute("UPDATE tasks SET result_json = '[]' WHERE id = 1")
        db.commit()
        statements = []
        db.set_trace_callback(statements.append)
        try:
            tasks = self.poll_tasks([1, 2])["tasks"]
        finally:
            db.set_trace_callback(None)
        self.assertEqual([task["id"] for task in tasks], [1])
        self.assertEqual(tasks[0]["review_key"], "stale")
        fallback = [sql for sql in statements if "AS has_result" in sql]
        self.assertEqual(len(fallback), 1)
        self.assertIn("WHERE id IN (1)", fallback[0])
        plan = " ".join(
            row["detail"] for row in db.execute("EXPLAIN QUERY PLAN " + fallback[0])
        )
        self.assertIn("USING INTEGER PRIMARY KEY", plan)
        statistics._task_report_stat_rows_for_where("t.id = ?", (1,))
        db.execute("UPDATE tasks SET result_json = NULL WHERE id = 1")
        db.commit()
        self.assertEqual(self.poll_tasks([1])["tasks"][0]["review_key"], "empty:0:0")
        self.assertEqual(statistics._select_task_report_stat_rows("t.id = ?", (1,)), [])

    def test_list_metadata_is_loaded_only_for_multi_document_tasks(self):
        self.insert_tasks(1)
        db = get_db()
        metadata = json.dumps(
            {"groups": [{"files": [{"stored_filename": "sample.txt"}]}]}
        )
        db.execute("UPDATE tasks SET document_meta_json = ?", (metadata,))
        identity = UserIdentity("ip:user", "user", "ip", "127.0.0.1")
        for task_type in (
            "document_check",
            "image_check",
            "video_check",
            "consistency_check",
            "language_consistency_check",
        ):
            with self.subTest(task_type=task_type):
                db.execute("UPDATE tasks SET task_type = ?", (task_type,))
                db.commit()
                with self.app.test_request_context("/"):
                    _, _, total, rows, _ = _user_task_list_data(identity, task_type)
                self.assertEqual(total, 1)
                self.assertEqual(
                    rows[0]["document_meta_json"],
                    metadata if "consistency" in task_type else None,
                )

    def test_file_availability_parses_groups_once_and_checks_current_files(self):
        self.app.config["UPLOAD_FOLDER"] = self.temporary.name
        source = Path(self.temporary.name) / "source.txt"
        source.write_text("内容", encoding="utf-8")
        task = {
            "task_type": "consistency_check",
            "stored_filename": "combined.txt",
            "document_meta_json": json.dumps(
                {"groups": [{"files": [{"stored_filename": source.name}]}]}
            ),
        }
        with patch.object(
            files, "_task_document_groups", wraps=files._task_document_groups
        ) as groups:
            self.assertTrue(files._task_source_files_available(task))
            self.assertEqual(groups.call_count, 1)
        source.unlink()
        self.assertFalse(files._task_source_files_available(task))
        source.write_text("恢复", encoding="utf-8")
        self.assertTrue(files._task_source_files_available(task))

    def test_scheduler_cost_is_bounded_by_owners_instead_of_queue_length(self):
        self.insert_tasks(50, status="queued")
        self.insert_tasks(1, owner="ip:other", status="queued")
        set_setting("user_concurrency", 1)
        set_setting("global_concurrency", 2)
        baseline_steps, claims = self.vm_steps(
            lambda: TaskSupervisor(self.app)._claim_available_tasks()
        )
        self.assertEqual(len(claims), 2)
        get_db().execute("UPDATE tasks SET status = 'queued', claim_token = NULL")
        get_db().commit()
        self.insert_tasks(10000, status="queued")
        larger_steps, claims = self.vm_steps(
            lambda: TaskSupervisor(self.app)._claim_available_tasks()
        )
        self.assertEqual(len(claims), 2)
        self.assertLess(larger_steps, max(3000, baseline_steps * 3))

    def test_scheduler_managed_processes_skip_history_and_blocked_queue(self):
        self.insert_tasks(1, owner="ip:busy")
        self.insert_tasks(50, owner="ip:busy", status="queued")
        self.insert_tasks(1, owner="ip:eligible", status="queued")
        set_setting("global_concurrency", 3)
        set_setting("user_concurrency", 1)
        supervisor = TaskSupervisor(self.app)
        supervisor._active[1] = TaskProcess(object(), "finishing")
        baseline, claims = self.vm_steps(supervisor._claim_available_tasks)
        self.assertEqual(len(claims), 1)
        get_db().execute(
            "UPDATE tasks SET status = 'queued' WHERE id = ?", (claims[0][0],)
        )
        get_db().commit()
        self.insert_tasks(10000, owner="ip:busy", status="queued")
        self.insert_tasks(10000, owner="ip:history")
        larger, claims = self.vm_steps(supervisor._claim_available_tasks)
        self.assertEqual(len(claims), 1)
        self.assertLess(larger, max(3000, baseline * 3))

    def test_background_stats_scan_is_bounded_and_eventually_revisits_old_tasks(self):
        self.insert_tasks(1200, result="[]")
        version = _empty_report_suppression_version()
        get_db().execute(
            "INSERT INTO task_report_stats(task_id, source_updated_at, suppression_version, updated_at) "
            "SELECT id, updated_at, ?, updated_at FROM tasks",
            (version,),
        )
        get_db().commit()
        steps, refreshed = self.vm_steps(statistics.refresh_stale_report_stats_batch)
        self.assertEqual(refreshed, 0)
        self.assertEqual(
            self.app.extensions["report_stats_scan_cursor"],
            statistics.REPORT_STATS_SCAN_BATCH_SIZE,
        )
        self.insert_tasks(10000)
        self.app.extensions["report_stats_scan_cursor"] = 0
        larger_steps, _ = self.vm_steps(statistics.refresh_stale_report_stats_batch)
        self.assertLess(larger_steps, steps * 2)
        get_db().execute("UPDATE tasks SET result_json = '[]' WHERE id IN (1, 1150)")
        get_db().commit()
        for _ in range(30):
            statistics.refresh_stale_report_stats_batch()
        count = (
            get_db()
            .execute(
                "SELECT COUNT(*) FROM task_report_stats WHERE task_id IN (1, 1150)"
            )
            .fetchone()[0]
        )
        self.assertEqual(count, 2)

    def test_aggregate_prepares_only_a_bounded_batch_and_uses_sql_sum(self):
        self.insert_tasks(2000, result="[]")
        statements = []
        get_db().set_trace_callback(statements.append)
        try:
            with patch.object(
                statistics, "_parse_result_json", wraps=json.loads
            ) as parser:
                totals = statistics._admin_report_item_totals_for_where(
                    "t.task_type = ?", ("document_check",)
                )
        finally:
            get_db().set_trace_callback(None)
        self.assertEqual(totals["issue"], 0)
        self.assertLessEqual(
            parser.call_count, statistics.REPORT_STATS_INLINE_REBUILD_LIMIT
        )
        self.assertTrue(
            any("SUM(s.issue_count)" in statement for statement in statements)
        )

    def test_models_are_loaded_in_two_queries_and_ownership_is_enforced(self):
        for owner, count in [("ip:user", 25), ("ip:other", 1)]:
            for _ in range(count):
                provider_id = (
                    get_db()
                    .execute(
                        "INSERT INTO user_model_providers(owner_subject, name, api_base, created_at, updated_at) "
                        "VALUES (?, 'example', 'http://example.invalid/chat/completions', '2026-09-01', '2026-09-01')",
                        (owner,),
                    )
                    .lastrowid
                )
                get_db().execute(
                    "INSERT INTO user_model_configs(provider_id, model_name, created_at, updated_at) "
                    "VALUES (?, 'model', '2026-09-01', '2026-09-01')",
                    (provider_id,),
                )
        get_db().commit()
        statements = []
        get_db().set_trace_callback(statements.append)
        try:
            providers = _load_user_model_providers("ip:user")
        finally:
            get_db().set_trace_callback(None)
        self.assertEqual(len(providers), 25)
        self.assertTrue(all(len(provider["models"]) == 1 for provider in providers))
        self.assertEqual(
            len([q for q in statements if q.lstrip().startswith("SELECT")]), 2
        )
        self.assertIsNone(_find_enabled_model("26:0:model", "ip:user"))
        self.assertIsNone(_find_enabled_model("01:0:model", "ip:user"))

    def test_file_cleanup_releases_database_during_file_operations(self):
        from app.tasks.runtime.artifacts import cleanup_task_file_cache

        self.insert_tasks(3)
        transaction_states = []
        with (
            patch(
                "app.tasks.runtime.artifacts._task_artifact_usage", return_value=(1, 1)
            ),
            patch(
                "app.tasks.runtime.artifacts._remove_task_artifacts",
                side_effect=lambda app, task: transaction_states.append(
                    get_db().in_transaction
                ),
            ),
        ):
            result = cleanup_task_file_cache(self.app, [1, 2, 3])
        self.assertEqual(result["cleaned_ids"], [1, 2, 3])
        self.assertEqual(transaction_states, [False, False, False])
        self.assertFalse(get_db().in_transaction)

    def test_hot_queries_search_existing_indexes(self):
        self.insert_tasks(100)
        queries = [
            (
                "SELECT id FROM tasks WHERE task_type = ? AND owner_subject = ? ORDER BY created_at DESC, id DESC LIMIT 20",
                ("document_check", "ip:user"),
                "idx_tasks_type_owner_created",
            ),
            (
                "SELECT id FROM tasks WHERE task_type IN ('document_check', 'image_check') AND created_at >= ? AND created_at < ?",
                ("2026-09-01", "2026-09-02"),
                "idx_tasks_type_created",
            ),
            (
                "SELECT id FROM tasks WHERE id > ? ORDER BY id LIMIT 512",
                (100,),
                "INTEGER PRIMARY KEY",
            ),
        ]
        for query, params, index in queries:
            plan = "\n".join(
                row[3]
                for row in get_db().execute("EXPLAIN QUERY PLAN " + query, params)
            )
            self.assertIn(index, plan)
            self.assertNotIn("SCAN tasks", plan)

    def test_submission_service_accepts_values_without_an_http_request(self):
        result = submit_document_task(
            UserIdentity("ip:user", "", "ip", "127.0.0.1"), TaskSubmission({}, [], "")
        )
        self.assertEqual(result.task_type, "document_check")
        self.assertEqual(result.messages, [("请选择要上传的文档。", "error")])


class DocumentPerformanceTest(unittest.TestCase):
    def test_pdf_space_normalization_preserves_visible_and_overlapping_spaces(self):
        chars = [
            {"c": value, "origin": (position, 0)}
            for value, position in [
                (" ", 0),
                ("A", 4),
                (" ", 8),
                ("B", 8),
                (" ", 12),
                ("C", 17),
                (" ", 21),
            ]
        ]
        line = {"dir": (1, 0), "spans": [{"size": 10, "chars": chars}]}
        self.assertEqual(_pdf_line_text_variants(line), (" A B C ", " AB C "))

    def test_location_lookup_searches_markers_without_copying_the_page_index(self):
        class SearchOnlyMarkers(list):
            def __iter__(self):
                raise AssertionError("定位时应直接二分查找页面索引")

        index = DocumentLocationIndex("[第1页]\n示例\n[第2页]\n更多示例")
        index.pages = SearchOnlyMarkers(index.pages)
        self.assertIn("第2页", index.location_for(20, "示例"))
