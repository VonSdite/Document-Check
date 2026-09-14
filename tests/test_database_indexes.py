import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from app.persistence.connection import get_db
from app.persistence.schema import init_db

INDEX_TABLES = {
    "idx_tasks_type_owner_status": "tasks",
    "idx_tasks_pending_file_cleanup": "tasks",
    "idx_report_suppression_hits_task": "report_suppression_hits",
    "idx_report_suppression_rules_enabled_type_updated": "report_suppression_rules",
}


class DatabaseIndexInitializationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(Path(self.temporary.name) / "test.sqlite3")
        self.context = self.app.app_context()
        self.context.push()

    def tearDown(self):
        self.context.pop()
        self.temporary.cleanup()

    def assert_query_indexes(self):
        db = get_db()
        for name, table in INDEX_TABLES.items():
            indexes = {
                row["name"]: row for row in db.execute(f"PRAGMA index_list({table})")
            }
            self.assertIn(name, indexes)
            self.assertEqual(indexes[name]["unique"], 0)

    def prepare_existing_database(self):
        init_db()
        db = get_db()
        db.executemany(
            """
            INSERT INTO tasks (
                ip, owner_subject, owner_source, original_filename, stored_filename,
                file_type, file_size, checks_json, model_name, api_base,
                document_text, result_json, status, created_at, updated_at
            ) VALUES ('127.0.0.1', 'ip:127.0.0.1', 'ip', 'sample.txt', 'sample.txt',
                      'txt', 100, '[]', 'example', '', ?, '[]', 'completed',
                      '2026-09-01', '2026-09-01')
            """,
            [("保留完整文档和已有数据",), ("保留完整文档和已有数据",)],
        )
        db.executemany(
            """
            INSERT INTO report_suppression_rules (
                task_type, check_code, fingerprint, item_json, enabled, created_at, updated_at
            ) VALUES ('document_check', 'typo', ?, '{}', 1, '2026-09-01', '2026-09-01')
            """,
            [("first",), ("second",)],
        )
        db.executemany(
            """
            INSERT INTO report_suppression_hits (
                rule_id, task_id, result_code, item_id, item_json, created_at
            ) VALUES (1, 1, 'typo', ?, '{}', '2026-09-01')
            """,
            [("first",), ("second",)],
        )
        db.commit()

    def drop_indexes(self, names):
        db = get_db()
        for name in names:
            db.execute(f"DROP INDEX {name}")
        db.commit()

    def snapshot_records_and_tables(self):
        db = get_db()
        tables = [
            tuple(row)
            for row in db.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        rows = {
            name: [
                tuple(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")
            ]
            for name, _ in tables
        }
        return tables, rows

    def trace_initialization(self):
        statements = []
        db = get_db()
        db.set_trace_callback(statements.append)
        try:
            init_db()
        finally:
            db.set_trace_callback(None)
        return statements

    def test_fresh_database_creates_indexes_without_compatibility_helper(self):
        with patch("app.persistence.schema._ensure_existing_query_indexes"):
            statements = self.trace_initialization()
        self.assert_query_indexes()
        self.assertFalse(any(sql.startswith("ANALYZE") for sql in statements))

    def test_existing_database_adds_indexes_and_preserves_schema_and_records(self):
        self.prepare_existing_database()
        self.drop_indexes(INDEX_TABLES)
        before = self.snapshot_records_and_tables()

        statements = self.trace_initialization()

        self.assert_query_indexes()
        self.assertEqual(self.snapshot_records_and_tables(), before)
        self.assertEqual(
            [sql for sql in statements if sql.startswith("ANALYZE")],
            [
                "ANALYZE report_suppression_hits",
                "ANALYZE report_suppression_rules",
                "ANALYZE tasks",
            ],
        )
        stats = {row["idx"] for row in get_db().execute("SELECT idx FROM sqlite_stat1")}
        self.assertTrue(set(INDEX_TABLES).issubset(stats))
        self.assertEqual(get_db().execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(get_db().execute("PRAGMA foreign_key_check").fetchall(), [])

        repeated = self.trace_initialization()
        self.assertFalse(any(sql.startswith("ANALYZE") for sql in repeated))
        self.assertFalse(
            any(
                sql.startswith("CREATE INDEX")
                and any(name in sql for name in INDEX_TABLES)
                for sql in repeated
            )
        )
        self.assertEqual(self.snapshot_records_and_tables(), before)

    def test_partial_indexes_analyze_only_the_affected_table(self):
        self.prepare_existing_database()
        self.drop_indexes(["idx_report_suppression_hits_task"])

        statements = self.trace_initialization()

        self.assert_query_indexes()
        self.assertEqual(
            [sql for sql in statements if sql.startswith("ANALYZE")],
            ["ANALYZE report_suppression_hits"],
        )

    def test_index_creation_follows_column_compatibility(self):
        self.prepare_existing_database()
        self.drop_indexes(["idx_tasks_pending_file_cleanup"])
        get_db().execute("ALTER TABLE tasks DROP COLUMN source_files_cleaned_at")
        get_db().commit()

        init_db()

        self.assert_query_indexes()
        self.assertEqual(
            get_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2
        )

    def test_concurrent_initialization_completes_the_same_indexes(self):
        self.prepare_existing_database()
        self.drop_indexes(INDEX_TABLES)
        before = self.snapshot_records_and_tables()
        ready = threading.Barrier(2)

        def initialize():
            app = Flask(__name__)
            app.config["DATABASE"] = self.app.config["DATABASE"]
            with app.app_context():
                ready.wait(timeout=10)
                init_db()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(initialize) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)

        self.assert_query_indexes()
        self.assertEqual(self.snapshot_records_and_tables(), before)

    def test_failed_index_transaction_can_be_retried(self):
        self.prepare_existing_database()
        self.drop_indexes(INDEX_TABLES)
        db = get_db()

        def deny_cleanup_index(action, name, *_args):
            if (
                action == sqlite3.SQLITE_CREATE_INDEX
                and name == "idx_tasks_pending_file_cleanup"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(deny_cleanup_index)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                init_db()
        finally:
            db.set_authorizer(None)
            db.rollback()
        indexes = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        self.assertTrue(set(INDEX_TABLES).isdisjoint(indexes))

        init_db()
        self.assert_query_indexes()


if __name__ == "__main__":
    unittest.main()
