import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.audit_ip_owners import (
    delete_ip_owner_data,
    format_table,
    remaining_ip_owners,
)


class AuditIpOwnersTest(unittest.TestCase):
    def test_remaining_ip_owners_reports_only_ip_owned_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "document_check.sqlite3"
            self._create_database(database)
            rows = remaining_ip_owners(database)

        self.assertEqual(
            rows,
            [
                {
                    "ip": "10.0.0.8",
                    "username": "灰度用户",
                    "task_count": 1,
                    "provider_count": 1,
                    "model_count": 2,
                }
            ],
        )

    def test_format_table_keeps_columns_aligned_without_tabs(self):
        table = format_table(
            [
                {
                    "ip": "127.0.0.1",
                    "username": "",
                    "task_count": 3,
                    "provider_count": 1,
                    "model_count": 6,
                },
                {
                    "ip": "10.0.0.8",
                    "username": "灰度用户",
                    "task_count": 12,
                    "provider_count": 2,
                    "model_count": 16,
                },
            ]
        )

        self.assertNotIn("\t", table)
        self.assertIn("未迁移任务数", table)
        self.assertEqual(
            {self._display_width(line) for line in table.splitlines()},
            {self._display_width(table.splitlines()[0])},
        )

    def test_delete_ip_owner_data_previews_without_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "document_check.sqlite3"
            self._create_database(database)

            result = delete_ip_owner_data(database, "10.0.0.8", confirmed=False)

            self.assertFalse(result["confirmed"])
            self.assertEqual(result["matched"]["task_count"], 1)
            self.assertEqual(result["matched"]["provider_count"], 1)
            self.assertEqual(result["matched"]["model_count"], 2)
            self.assertEqual(len(self._table_rows(database, "tasks")), 2)
            self.assertEqual(len(self._table_rows(database, "user_model_providers")), 2)

    def test_delete_ip_owner_data_removes_only_ip_owned_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "document_check.sqlite3"
            self._create_database(database)

            result = delete_ip_owner_data(database, "10.0.0.8", confirmed=True)

            self.assertTrue(result["confirmed"])
            self.assertEqual(
                result["deleted"],
                {
                    "tasks": 1,
                    "task_activity_settings": 1,
                    "report_suppression_hits": 1,
                    "task_live_results": 1,
                    "task_report_stats": 1,
                    "providers": 1,
                    "models": 2,
                },
            )
            self.assertEqual(remaining_ip_owners(database), [])
            self.assertEqual(
                [
                    (row["ip"], row["owner_subject"])
                    for row in self._table_rows(database, "tasks")
                ],
                [("10.0.0.9", "cookie_session:user-1")],
            )
            self.assertEqual(
                [
                    row["owner_subject"]
                    for row in self._table_rows(database, "user_model_providers")
                ],
                ["cookie_session:user-1"],
            )
            self.assertEqual(len(self._table_rows(database, "user_model_configs")), 1)

    def _create_database(self, database: Path):
        with sqlite3.connect(database) as db:
            db.executescript(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY,
                    ip TEXT,
                    owner_subject TEXT,
                    provider_id INTEGER,
                    owner_name_snapshot TEXT,
                    username_snapshot TEXT
                );
                CREATE TABLE user_model_providers (
                    id INTEGER PRIMARY KEY,
                    owner_subject TEXT
                );
                CREATE TABLE user_model_configs (
                    id INTEGER PRIMARY KEY,
                    provider_id INTEGER
                );
                CREATE TABLE settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                );
                CREATE TABLE report_suppression_hits (
                    id INTEGER PRIMARY KEY,
                    task_id INTEGER
                );
                CREATE TABLE task_live_results (
                    task_id INTEGER PRIMARY KEY,
                    result_json TEXT
                );
                CREATE TABLE task_report_stats (
                    task_id INTEGER PRIMARY KEY,
                    issue_count INTEGER
                );
                CREATE TABLE ip_usernames (
                    ip TEXT PRIMARY KEY,
                    username TEXT
                );
                """
            )
            db.execute(
                """
                INSERT INTO tasks (
                    ip, owner_subject, provider_id, owner_name_snapshot, username_snapshot
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("10.0.0.8", "ip:10.0.0.8", 101, "任务快照", ""),
            )
            ip_task_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                """
                INSERT INTO tasks (
                    ip, owner_subject, provider_id, owner_name_snapshot, username_snapshot
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("10.0.0.9", "cookie_session:user-1", 102, "已迁移用户", ""),
            )
            cookie_task_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (f"task_activity:{ip_task_id}", "{}", "2026-09-17 00:00:00"),
            )
            db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (f"task_activity:{cookie_task_id}", "{}", "2026-09-17 00:00:00"),
            )
            for table, column in (
                ("report_suppression_hits", "task_id"),
                ("task_live_results", "task_id"),
                ("task_report_stats", "task_id"),
            ):
                db.execute(f"INSERT INTO {table} ({column}) VALUES (?)", (ip_task_id,))
                db.execute(
                    f"INSERT INTO {table} ({column}) VALUES (?)", (cookie_task_id,)
                )
            db.execute(
                "INSERT INTO ip_usernames (ip, username) VALUES (?, ?)",
                ("10.0.0.8", "灰度用户"),
            )
            db.execute(
                "INSERT INTO user_model_providers (id, owner_subject) VALUES (?, ?)",
                (101, "ip:10.0.0.8"),
            )
            db.executemany(
                "INSERT INTO user_model_configs (provider_id) VALUES (?)",
                [(101,), (101,)],
            )
            db.execute(
                "INSERT INTO user_model_providers (id, owner_subject) VALUES (?, ?)",
                (102, "cookie_session:user-1"),
            )
            db.execute(
                "INSERT INTO user_model_configs (provider_id) VALUES (?)",
                (102,),
            )

    def _table_rows(self, database: Path, table: str):
        with sqlite3.connect(database) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def test_delete_ip_owner_data_blocks_shared_provider_reference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "document_check.sqlite3"
            self._create_database(database)
            with sqlite3.connect(database) as db:
                db.execute(
                    """
                    INSERT INTO tasks (
                        ip, owner_subject, provider_id, owner_name_snapshot,
                        username_snapshot
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    ("10.0.0.9", "cookie_session:user-1", 101, "异常引用", ""),
                )

            with self.assertRaisesRegex(ValueError, "非目标归属任务引用"):
                delete_ip_owner_data(database, "10.0.0.8", confirmed=True)

            self.assertEqual(len(self._table_rows(database, "user_model_providers")), 2)

    def _display_width(self, value: str) -> int:
        import unicodedata

        return sum(
            2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
            for char in value
        )
