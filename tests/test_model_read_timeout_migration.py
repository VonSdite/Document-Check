import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask

from app.persistence.connection import get_db
from app.persistence.schema import MODEL_READ_TIMEOUT_MIGRATION_KEY, init_db


class ModelReadTimeoutMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(
            Path(self.temporary.name) / "timeout-test.sqlite3"
        )
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        init_db()

    def prepare_existing_providers(self):
        db = get_db()
        db.execute(
            "DELETE FROM settings WHERE key = ?", (MODEL_READ_TIMEOUT_MIGRATION_KEY,)
        )
        db.executemany(
            """
            INSERT INTO user_model_providers (
                owner_subject, name, api_base, api_key, request_timeout, is_active,
                created_at, updated_at
            ) VALUES (?, ?, 'https://example.test/v1/chat/completions', 'test-key',
                      ?, ?, '2026-01-01', '2026-01-01')
            """,
            [
                ("ip:127.0.0.1", "provider-a", 3600, 1),
                ("cookie_session:user-a", "provider-b", 120, 0),
                ("cookie_session:user-b", "provider-c", 7200, 1),
                ("ip:127.0.0.2", "provider-d", 600, 0),
            ],
        )
        db.commit()

    def providers(self):
        return [
            dict(row)
            for row in get_db().execute(
                "SELECT * FROM user_model_providers ORDER BY id"
            )
        ]

    def marker(self):
        return (
            get_db()
            .execute(
                "SELECT value FROM settings WHERE key = ?",
                (MODEL_READ_TIMEOUT_MIGRATION_KEY,),
            )
            .fetchone()
        )

    def test_updates_all_providers_once_and_preserves_task_snapshots(self):
        self.prepare_existing_providers()
        db = get_db()
        db.executemany(
            """
            INSERT INTO tasks (
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, request_timeout, status,
                created_at, updated_at
            ) VALUES ('127.0.0.1', 'test.txt', 'test.txt', 'txt', 1, '[]', 'model-a',
                      'https://example.test/v1/chat/completions', 3600, ?,
                      '2026-01-01', '2026-01-01')
            """,
            [("queued",), ("running",), ("completed",)],
        )
        db.commit()
        before = self.providers()

        init_db()

        for previous, current in zip(before, self.providers(), strict=True):
            self.assertEqual(current["request_timeout"], 600)
            expected = {**previous, "request_timeout": 600}
            if previous["request_timeout"] != 600:
                self.assertNotEqual(current["updated_at"], previous["updated_at"])
                expected["updated_at"] = current["updated_at"]
            self.assertEqual(current, expected)
        self.assertEqual(self.marker()["value"], "true")
        self.assertEqual(
            [row["request_timeout"] for row in db.execute("SELECT * FROM tasks")],
            [3600, 3600, 3600],
        )

        db.execute(
            "UPDATE user_model_providers SET request_timeout = 300 WHERE name = 'provider-a'"
        )
        db.commit()
        customized = self.providers()
        init_db()
        self.assertEqual(self.providers(), customized)

    def test_fresh_database_preserves_later_custom_configuration(self):
        self.assertEqual(self.marker()["value"], "true")
        db = get_db()
        db.execute(
            """
            INSERT INTO user_model_providers (
                owner_subject, name, api_base, request_timeout, created_at, updated_at
            ) VALUES ('ip:127.0.0.1', 'custom', 'https://example.test', 900,
                      '2026-01-01', '2026-01-01')
            """
        )
        db.commit()

        init_db()

        self.assertEqual(self.providers()[0]["request_timeout"], 900)

    def test_failure_rolls_back_values_and_marker_and_allows_retry(self):
        self.prepare_existing_providers()
        db = get_db()
        before = self.providers()
        db.execute(
            """
            CREATE TRIGGER fail_read_timeout_update
            BEFORE UPDATE OF request_timeout ON user_model_providers
            WHEN OLD.name = 'provider-b'
            BEGIN
                SELECT RAISE(ABORT, 'forced timeout update failure');
            END
            """
        )
        db.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced timeout update"):
            init_db()
        db.commit()

        self.assertEqual(self.providers(), before)
        self.assertIsNone(self.marker())

        db.execute("DROP TRIGGER fail_read_timeout_update")
        db.commit()
        init_db()
        self.assertEqual(
            [row["request_timeout"] for row in self.providers()], [600] * 4
        )
        self.assertEqual(self.marker()["value"], "true")

    def test_concurrent_initialization_applies_update_once(self):
        self.prepare_existing_providers()
        db = get_db()
        db.executescript(
            """
            CREATE TABLE timeout_updates (provider_id INTEGER);
            CREATE TRIGGER record_timeout_update
            AFTER UPDATE OF request_timeout ON user_model_providers
            BEGIN
                INSERT INTO timeout_updates(provider_id) VALUES (NEW.id);
            END;
            """
        )

        def initialize():
            with self.app.app_context():
                init_db()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(initialize) for _ in range(2)]
            for future in futures:
                future.result(timeout=30)

        self.assertEqual(
            [row["request_timeout"] for row in self.providers()], [600] * 4
        )
        updates = db.execute(
            "SELECT provider_id, COUNT(*) AS count FROM timeout_updates GROUP BY provider_id"
        ).fetchall()
        self.assertEqual(len(updates), 3)
        self.assertTrue(all(row["count"] == 1 for row in updates))
        self.assertEqual(self.marker()["value"], "true")


if __name__ == "__main__":
    unittest.main()
