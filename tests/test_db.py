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

    def test_default_image_page_check_max_pages_is_seeded(self):
        self.assertEqual(get_setting("image_page_check_max_pages"), 120)

    def test_default_issue_output_limit_is_seeded(self):
        self.assertEqual(get_setting("issue_output_limit"), 20)

    def test_default_report_retention_is_disabled(self):
        self.assertEqual(get_setting("report_retention_days"), 0)

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
        compliance_item = db.execute(
            "SELECT prompt FROM check_items WHERE task_type = ? AND code = ?",
            (DOCUMENT_TASK_TYPE, "compliance"),
        ).fetchone()
        typo_item = db.execute(
            "SELECT prompt FROM check_items WHERE task_type = ? AND code = ?",
            (DOCUMENT_TASK_TYPE, "typo"),
        ).fetchone()
        self.assertIn("不要把解析换行/分页造成的空白判为“多余空格”", compliance_item["prompt"])
        self.assertIn("不要把解析换行/分页造成的空白当作多余空格", typo_item["prompt"])
        self.assertIn("页码：未提取", typo_item["prompt"])
        self.assertIn("章节：未识别", typo_item["prompt"])
        self.assertIn("位置（文件/页码/章节或工作表/附近线索）", typo_item["prompt"])
        self.assertNotIn("image-ui-step-consistency", default_check_item_codes(IMAGE_TASK_TYPE))
        self.assertNotIn("image-device-installation", default_check_item_codes(IMAGE_TASK_TYPE))
        image_text_item = db.execute(
            "SELECT name, description, prompt FROM check_items WHERE task_type = ? AND code = ?",
            (IMAGE_TASK_TYPE, "image-text-correspondence"),
        ).fetchone()
        self.assertIsNotNone(image_text_item)
        self.assertEqual(image_text_item["name"], "图文与界面步骤一致性检查")
        self.assertIn("界面截图", image_text_item["description"])
        self.assertIn("旧版界面", image_text_item["prompt"])
        wiring_item = db.execute(
            "SELECT name, description, prompt FROM check_items WHERE task_type = ? AND code = ?",
            (IMAGE_TASK_TYPE, "image-wiring"),
        ).fetchone()
        self.assertIsNotNone(wiring_item)
        self.assertEqual(wiring_item["name"], "设备安装与接线检查")
        self.assertIn("设备外观", wiring_item["description"])
        self.assertIn("壁挂/导轨/机柜/桌面", wiring_item["prompt"])
        image_title_item = db.execute(
            "SELECT name, description, prompt FROM check_items WHERE task_type = ? AND code = ?",
            (IMAGE_TASK_TYPE, "image-figure-table-title-standard"),
        ).fetchone()
        self.assertIsNotNone(image_title_item)
        self.assertEqual(image_title_item["name"], "图表标题可见性复核")
        self.assertIn("图x-x", image_title_item["description"])
        self.assertIn("表3-1", image_title_item["prompt"])
        self.assertIn("章节标题不能替代图表标题", image_title_item["prompt"])
        self.assertIn("逐项识别当前页面", image_title_item["prompt"])
        self.assertIn("页眉或文档名不能替代表标题", image_title_item["prompt"])
        self.assertIn("续表x-x", image_title_item["prompt"])
        image_quality_item = db.execute(
            "SELECT name, description, prompt FROM check_items WHERE task_type = ? AND code = ?",
            (IMAGE_TASK_TYPE, "image-integrity-clarity"),
        ).fetchone()
        self.assertIsNotNone(image_quality_item)
        self.assertEqual(image_quality_item["name"], "图片完整性和清晰度检查")
        self.assertIn("关键文字", image_quality_item["description"])
        self.assertIn("端子号", image_quality_item["prompt"])

    def test_seed_defaults_removes_retired_translation_coverage_check(self):
        db = get_db()
        now = "2026-05-12 00:00:00"
        db.execute(
            """
            INSERT INTO check_items(task_type, code, name, description, prompt, enabled, sort_order, created_at, updated_at)
            VALUES (?, 'consistency-translation-coverage', '跨语言条目完整性检查', '', '本地规则', 1, 20, ?, ?)
            """,
            (CONSISTENCY_TASK_TYPE, now, now),
        )
        db.commit()

        seed_defaults()

        row = db.execute(
            "SELECT 1 FROM check_items WHERE code = 'consistency-translation-coverage'"
        ).fetchone()
        self.assertIsNone(row)

    def test_seed_defaults_migrates_stock_typo_prompt_to_location_version(self):
        db = get_db()
        legacy_prompt = """你是一名中文校对专家。请检查文档中的错别字、漏字、多字、标点误用、重复表达、常见语病和明显不通顺句子。
注意：文档文本由解析器抽取得到，换行、分页、表格分隔符、行首行尾空白可能与原版版式不同；不要把解析换行/分页造成的空白当作多余空格或标点问题。
输出要求：
1. 按条列出：原文片段、疑似问题、建议修改、理由。
2. 对专业术语、人名、地名、品牌名保持谨慎，不确定时标注“疑似”。
3. 如果未发现明显问题，明确说明“未发现明显错别字或语病”。"""
        db.execute(
            "UPDATE check_items SET prompt = ? WHERE code = 'typo'",
            (legacy_prompt,),
        )
        db.commit()

        seed_defaults()

        row = db.execute("SELECT prompt FROM check_items WHERE code = 'typo'").fetchone()
        self.assertIn("页码：未提取", row["prompt"])
        self.assertIn("章节：未识别", row["prompt"])
        self.assertIn("位置（文件/页码/章节或工作表/附近线索）", row["prompt"])

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

    def test_seed_defaults_migrates_stock_image_prompts_to_qwen_vl_optimized_versions(self):
        db = get_db()
        db.execute(
            """
            UPDATE check_items
            SET prompt = ?
            WHERE code = ?
            """,
            (
                "旧版提示词：必须判为表标题缺失；同一张图片中可能同时出现多个表格。",
                "image-figure-table-title-standard",
            ),
        )
        db.commit()

        seed_defaults()

        row = db.execute(
            "SELECT name, description, prompt FROM check_items WHERE code = ?",
            ("image-figure-table-title-standard",),
        ).fetchone()
        self.assertEqual(row["name"], "图表标题可见性复核")
        self.assertIn("疑似缺失项", row["description"])
        self.assertIn("逐项识别当前页面", row["prompt"])
        self.assertIn("疑似标题缺失", row["prompt"])

    def test_seed_defaults_disables_merged_stock_image_check_items(self):
        db = get_db()
        created_at = "2026-01-01 00:00:00"
        db.execute(
            """
            INSERT INTO check_items(task_type, code, name, description, prompt, enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                IMAGE_TASK_TYPE,
                "image-ui-step-consistency",
                "界面截图与步骤一致性检查",
                "旧拆分项",
                "你是一名产品界面截图与操作步骤审查专家。旧提示词。",
                15,
                created_at,
                created_at,
            ),
        )
        db.execute(
            """
            INSERT INTO check_items(task_type, code, name, description, prompt, enabled, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                IMAGE_TASK_TYPE,
                "image-device-installation",
                "设备外观与安装图检查",
                "旧拆分项",
                "你是一名产品设备外观与安装图审查专家。旧提示词。",
                25,
                created_at,
                created_at,
            ),
        )
        db.commit()

        seed_defaults()

        rows = {
            row["code"]: bool(row["enabled"])
            for row in db.execute(
                "SELECT code, enabled FROM check_items WHERE code IN (?, ?)",
                ("image-ui-step-consistency", "image-device-installation"),
            ).fetchall()
        }
        self.assertEqual(rows, {"image-ui-step-consistency": False, "image-device-installation": False})

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
