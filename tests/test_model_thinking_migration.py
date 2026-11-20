import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask

from app.models.service import get_enabled_models
from app.persistence.connection import MODEL_THINKING_DEFAULT_MIGRATION_KEY, get_db
from app.persistence.schema import init_db


class ModelThinkingMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(Path(self.temporary.name) / "test.sqlite3")
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        init_db()
        db = get_db()
        db.execute(
            "DELETE FROM settings WHERE key = ?",
            (MODEL_THINKING_DEFAULT_MIGRATION_KEY,),
        )
        db.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES ('model_force_disable_thinking_default_v2', 'true', '2026-01-01')
            """
        )
        for owner, enabled in (("ip:127.0.0.1", 1), ("cookie_session:user-a", 0)):
            provider_id = db.execute(
                """
                INSERT INTO user_model_providers (
                    owner_subject, name, api_base, is_active, created_at, updated_at
                ) VALUES (?, 'provider', 'https://example.test/v1/chat/completions',
                          ?, '2026-01-01', '2026-01-01')
                """,
                (owner, enabled),
            ).lastrowid
            db.executemany(
                """
                INSERT INTO user_model_configs (
                    provider_id, model_name, force_disable_thinking, reasoning_effort,
                    sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, '2026-01-01', '2026-01-01')
                """,
                [
                    (provider_id, "single-model", 1, "high", 10),
                    (provider_id, "duplicate-model", 1, "low", 20),
                    (provider_id, "duplicate-model", 0, "xhigh", 30),
                ],
            )
        db.commit()

    def models(self):
        return [
            dict(row)
            for row in get_db().execute("SELECT * FROM user_model_configs ORDER BY id")
        ]

    def marker(self):
        return (
            get_db()
            .execute(
                "SELECT value FROM settings WHERE key = ?",
                (MODEL_THINKING_DEFAULT_MIGRATION_KEY,),
            )
            .fetchone()
        )

    def test_resets_all_existing_models_once_with_prior_completion_marker(self):
        db = get_db()
        db.executemany(
            """
            INSERT INTO tasks (
                ip, original_filename, stored_filename, file_type, file_size,
                checks_json, model_name, api_base, force_disable_thinking,
                reasoning_effort, status, created_at, updated_at
            ) VALUES ('127.0.0.1', 'test.txt', 'test.txt', 'txt', 1, '[]', 'single-model',
                      'https://example.test/v1/chat/completions', 1, 'high', ?,
                      '2026-01-01', '2026-01-01')
            """,
            [("queued",), ("running",), ("completed",)],
        )
        db.commit()
        before = self.models()

        init_db()

        current = self.models()
        self.assertEqual(len(current), 4)
        by_id = {row["id"]: row for row in current}
        for previous in before:
            if (
                previous["model_name"] == "duplicate-model"
                and previous["force_disable_thinking"]
            ):
                self.assertNotIn(previous["id"], by_id)
                continue
            expected = {**previous, "force_disable_thinking": 0}
            if previous["force_disable_thinking"]:
                self.assertNotEqual(
                    by_id[previous["id"]]["updated_at"], previous["updated_at"]
                )
                expected["updated_at"] = by_id[previous["id"]]["updated_at"]
            self.assertEqual(by_id[previous["id"]], expected)
        self.assertEqual(self.marker()["value"], "true")
        options = get_enabled_models("ip:127.0.0.1")
        self.assertEqual(len(options), 2)
        self.assertTrue(all(not option["force_disable_thinking"] for option in options))
        self.assertEqual(
            [
                tuple(row)
                for row in db.execute(
                    "SELECT force_disable_thinking, reasoning_effort FROM tasks"
                )
            ],
            [(1, "high")] * 3,
        )

        db.execute(
            "UPDATE user_model_configs SET force_disable_thinking = 1 WHERE model_name = 'single-model'"
        )
        db.commit()
        customized = self.models()
        init_db()
        self.assertEqual(self.models(), customized)

    def test_failure_restores_deleted_duplicates_updates_and_marker(self):
        db = get_db()
        before = self.models()
        db.execute(
            """
            CREATE TRIGGER fail_thinking_update
            BEFORE UPDATE OF force_disable_thinking ON user_model_configs
            WHEN OLD.provider_id IN (
                SELECT id FROM user_model_providers WHERE is_active = 0
            )
            BEGIN
                SELECT RAISE(ABORT, 'forced thinking update failure');
            END
            """
        )
        db.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced thinking update"):
            init_db()
        db.commit()
        self.assertEqual(self.models(), before)
        self.assertIsNone(self.marker())

        db.execute("DROP TRIGGER fail_thinking_update")
        db.commit()
        init_db()
        self.assertEqual(len(self.models()), 4)
        self.assertTrue(
            all(row["force_disable_thinking"] == 0 for row in self.models())
        )
        self.assertEqual(self.marker()["value"], "true")

    def test_concurrent_startup_applies_reset_once(self):
        db = get_db()
        db.executescript(
            """
            CREATE TABLE thinking_updates (model_id INTEGER);
            CREATE TRIGGER record_thinking_update
            AFTER UPDATE OF force_disable_thinking ON user_model_configs
            BEGIN
                INSERT INTO thinking_updates(model_id) VALUES (NEW.id);
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

        self.assertEqual(len(self.models()), 4)
        self.assertTrue(
            all(row["force_disable_thinking"] == 0 for row in self.models())
        )
        counts = db.execute(
            "SELECT COUNT(*) FROM thinking_updates GROUP BY model_id"
        ).fetchall()
        self.assertEqual([row[0] for row in counts], [1, 1])
        self.assertEqual(self.marker()["value"], "true")


if __name__ == "__main__":
    unittest.main()
