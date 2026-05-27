import tempfile
import unittest
from pathlib import Path

from flask import Flask

from app.db import (
    DEFAULT_CHECK_ITEMS_BY_CODE,
    default_check_item_codes,
    get_bool_setting,
    get_db,
    get_ip_username,
    get_setting,
    init_db,
    reset_default_check_item_prompt,
    seed_defaults,
    set_ip_username,
    set_setting,
)
from app.task_types import CONSISTENCY_TASK_TYPE, DOCUMENT_TASK_TYPE, IMAGE_TASK_TYPE
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

    def test_ip_username_table_exists(self):
        db = get_db()
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(ip_usernames)").fetchall()
        }

        self.assertEqual(columns, {"ip", "username", "created_at", "updated_at"})

    def test_ip_username_mapping_can_be_saved_and_cleared(self):
        set_ip_username("10.0.0.8", "张三")

        self.assertEqual(get_ip_username("10.0.0.8"), "张三")

        set_ip_username("10.0.0.8", "")

        self.assertEqual(get_ip_username("10.0.0.8"), "")

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
        image_title_item = db.execute(
            "SELECT name, description, prompt FROM check_items WHERE task_type = ? AND code = ?",
            (IMAGE_TASK_TYPE, "image-figure-table-title-standard"),
        ).fetchone()
        self.assertIsNotNone(image_title_item)
        self.assertEqual(image_title_item["name"], "图表标题规范检查")
        self.assertIn("图x-x", image_title_item["description"])
        self.assertIn("表3-1", image_title_item["prompt"])
        image_quality_item = db.execute(
            "SELECT name, description, prompt FROM check_items WHERE task_type = ? AND code = ?",
            (IMAGE_TASK_TYPE, "image-integrity-clarity"),
        ).fetchone()
        self.assertIsNotNone(image_quality_item)
        self.assertEqual(image_quality_item["name"], "图片完整性和清晰度检查")
        self.assertIn("异常色块", image_quality_item["description"])
        self.assertIn("过度拉伸", image_quality_item["prompt"])

    def test_seed_defaults_migrates_image_language_check_item(self):
        db = get_db()
        db.execute(
            """
            UPDATE check_items
            SET name = ?,
                description = ?,
                prompt = ?,
                sort_order = ?
            WHERE code = ?
            """,
            (
                "图片小语种文字检查",
                "检查图片中的文字是否包含小语种文本。",
                "旧提示词：检查非中文、非英文的小语种文字。",
                99,
                "image-small-language-text",
            ),
        )
        db.commit()

        seed_defaults()

        row = db.execute(
            "SELECT task_type, name, description, prompt, sort_order FROM check_items WHERE code = ?",
            ("image-small-language-text",),
        ).fetchone()
        self.assertEqual(row["task_type"], IMAGE_TASK_TYPE)
        self.assertEqual(row["name"], "图片语种匹配检查")
        self.assertIn("文档主要语种", row["description"])
        self.assertIn("文档主要语种", row["prompt"])
        self.assertEqual(row["sort_order"], 20)

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
