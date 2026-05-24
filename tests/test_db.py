import tempfile
import unittest
from pathlib import Path

from flask import Flask

from app.db import (
    DEFAULT_CHECK_ITEMS_BY_CODE,
    default_check_item_codes,
    get_bool_setting,
    get_db,
    get_setting,
    init_db,
    reset_default_check_item_prompt,
    seed_defaults,
    set_setting,
)
from app.task_types import CONSISTENCY_TASK_TYPE, DOCUMENT_TASK_TYPE
from app.routes import _next_check_item_sort_order, _reorder_check_items


class CheckItemDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(Path(self.temp_dir.name) / "test.sqlite3")
        self.context = self.app.app_context()
        self.context.push()
        init_db()
        seed_defaults()

    def tearDown(self):
        self.context.pop()
        self.temp_dir.cleanup()

    def test_resets_builtin_check_item_prompt(self):
        db = get_db()
        item = db.execute("SELECT id FROM check_items WHERE code = 'typo'").fetchone()
        db.execute("UPDATE check_items SET prompt = ? WHERE id = ?", ("已修改提示词", item["id"]))
        db.commit()

        self.assertTrue(reset_default_check_item_prompt(item["id"]))

        updated = db.execute("SELECT prompt FROM check_items WHERE id = ?", (item["id"],)).fetchone()
        self.assertEqual(updated["prompt"], DEFAULT_CHECK_ITEMS_BY_CODE["typo"]["prompt"])

    def test_does_not_reset_custom_check_item(self):
        db = get_db()
        db.execute(
            """
            INSERT INTO check_items(code, name, description, prompt, enabled, sort_order, created_at, updated_at)
            VALUES ('custom', '自定义检查', '', '自定义提示词', 1, 40, '2026-05-12 00:00:00', '2026-05-12 00:00:00')
            """
        )
        db.commit()
        item = db.execute("SELECT id, prompt FROM check_items WHERE code = 'custom'").fetchone()

        self.assertFalse(reset_default_check_item_prompt(item["id"]))

        updated = db.execute("SELECT prompt FROM check_items WHERE id = ?", (item["id"],)).fetchone()
        self.assertEqual(updated["prompt"], item["prompt"])

    def test_tasks_table_does_not_store_network_config(self):
        db = get_db()
        columns = {
            row["name"]: row
            for row in db.execute("PRAGMA table_info(tasks)").fetchall()
        }

        self.assertNotIn("proxy_mode", columns)
        self.assertNotIn("proxy", columns)
        self.assertNotIn("ssl_verify", columns)

    def test_tasks_table_has_task_type_and_document_meta(self):
        db = get_db()
        columns = {
            row["name"]: row
            for row in db.execute("PRAGMA table_info(tasks)").fetchall()
        }

        self.assertIn("task_type", columns)
        self.assertEqual(columns["task_type"]["dflt_value"], "'document_check'")
        self.assertIn("document_text", columns)
        self.assertIn("document_meta_json", columns)
        self.assertIn("checks_snapshot_json", columns)
        self.assertIn("owner_subject", columns)
        self.assertIn("owner_name_snapshot", columns)
        self.assertIn("owner_source", columns)

    def test_user_model_tables_exist(self):
        db = get_db()
        provider_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(user_model_providers)").fetchall()
        }
        model_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(user_model_configs)").fetchall()
        }

        self.assertIn("owner_subject", provider_columns)
        self.assertIn("api_base", provider_columns)
        self.assertNotIn("proxy_mode", provider_columns)
        self.assertNotIn("proxy", provider_columns)
        self.assertNotIn("ssl_verify", provider_columns)
        self.assertIn("model_name", model_columns)
        self.assertIn("force_disable_thinking", model_columns)

    def test_default_check_item_concurrency_is_seeded(self):
        self.assertEqual(get_setting("check_item_concurrency"), 1)

    def test_llm_stream_trace_is_disabled_by_default(self):
        self.assertFalse(get_setting("llm_stream_trace_enabled"))

    def test_bool_setting_treats_text_false_as_disabled(self):
        set_setting("llm_stream_trace_enabled", "false")

        self.assertFalse(get_bool_setting("llm_stream_trace_enabled", False))

    def test_default_check_items_are_grouped_by_task_type(self):
        db = get_db()
        document_codes = [
            row["code"]
            for row in db.execute(
                "SELECT code FROM check_items WHERE task_type = ? ORDER BY sort_order ASC, id ASC",
                (DOCUMENT_TASK_TYPE,),
            ).fetchall()
        ]
        consistency_codes = [
            row["code"]
            for row in db.execute(
                "SELECT code FROM check_items WHERE task_type = ? ORDER BY sort_order ASC, id ASC",
                (CONSISTENCY_TASK_TYPE,),
            ).fetchall()
        ]

        self.assertIn("typo", document_codes)
        self.assertEqual(consistency_codes, ["consistency-cross-document"])
        self.assertIn("consistency-cross-document", default_check_item_codes(CONSISTENCY_TASK_TYPE))

    def test_next_custom_check_item_sort_order_goes_before_first_item(self):
        self.assertEqual(_next_check_item_sort_order(get_db()), 0)

    def test_reorders_check_items(self):
        db = get_db()
        original_ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM check_items WHERE task_type = ? ORDER BY sort_order ASC, id ASC",
                (DOCUMENT_TASK_TYPE,),
            ).fetchall()
        ]
        requested_ids = list(reversed(original_ids))

        self.assertEqual(_reorder_check_items(db, requested_ids), requested_ids)
        db.commit()

        updated_ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM check_items WHERE task_type = ? ORDER BY sort_order ASC, id ASC",
                (DOCUMENT_TASK_TYPE,),
            ).fetchall()
        ]
        self.assertEqual(updated_ids, requested_ids)


if __name__ == "__main__":
    unittest.main()
