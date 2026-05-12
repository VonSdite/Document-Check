import tempfile
import unittest
from pathlib import Path

from flask import Flask

from app.db import (
    DEFAULT_CHECK_ITEMS_BY_CODE,
    get_db,
    init_db,
    reset_default_check_item_prompt,
    seed_defaults,
)


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


if __name__ == "__main__":
    unittest.main()
