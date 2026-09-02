import json
import unittest

from app.report_guardrails import (
    filter_unsupported_visual_missing_items,
    is_unsupported_visual_missing_item,
    sanitize_text_check_result,
)


class ReportGuardrailTest(unittest.TestCase):
    def test_filters_visual_object_missing_claim_without_direct_placeholder(self):
        item = {
            "status": "issue",
            "category": "图片缺失",
            "excerpt": "操作步骤如图 3 所示。",
            "description": "文档中未提供与操作步骤对应的截图。",
        }

        self.assertTrue(is_unsupported_visual_missing_item(item))
        filtered, removed_count = filter_unsupported_visual_missing_items([item])
        self.assertEqual(filtered, [])
        self.assertEqual(removed_count, 1)

    def test_keeps_missing_table_field_and_image_title_claims(self):
        items = [
            {
                "status": "issue",
                "category": "单位缺失",
                "excerpt": "电流 | 10",
                "description": "表格中的电流参数未提供单位。",
            },
            {
                "status": "issue",
                "category": "图片标题缺失",
                "excerpt": "图 2 网络结构",
                "description": "图片标题缺少编号。",
            },
            {
                "status": "suggestion",
                "category": "补充说明",
                "excerpt": "告警图标",
                "description": "缺少图标的说明。",
            },
            {
                "status": "issue",
                "category": "表题缺失",
                "excerpt": "接口参数",
                "description": "缺少表格中的标题。",
            },
            {
                "status": "issue",
                "category": "单位缺失",
                "excerpt": "电流 | 10",
                "description": "表格未提供单位。",
            },
        ]

        filtered, removed_count = filter_unsupported_visual_missing_items(items)

        self.assertEqual(filtered, items)
        self.assertEqual(removed_count, 0)

    def test_keeps_visual_missing_claim_with_explicit_placeholder_excerpt(self):
        item = {
            "status": "issue",
            "category": "插图缺失",
            "excerpt": "TODO：此处插入接线图",
            "description": "接线图未插入。",
        }

        self.assertFalse(is_unsupported_visual_missing_item(item))

    def test_sanitizes_json_and_updates_summary(self):
        payload = {
            "summary": "发现 2 个问题",
            "items": [
                {
                    "status": "issue",
                    "category": "表格缺失",
                    "excerpt": "参数如下表所示。",
                    "description": "没有找到对应表格。",
                },
                {
                    "status": "suggestion",
                    "category": "参数说明缺失",
                    "excerpt": "超时时间：30",
                    "description": "没有说明超时时间的单位。",
                },
            ],
        }

        content, removed_count = sanitize_text_check_result(
            "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        )
        sanitized = json.loads(content)

        self.assertEqual(removed_count, 1)
        self.assertEqual(len(sanitized["items"]), 1)
        self.assertEqual(sanitized["items"][0]["category"], "参数说明缺失")
        self.assertEqual(sanitized["summary"], "检查完成，保留 1 条待确认建议。")

    def test_leaves_non_json_legacy_result_unchanged(self):
        content = "检查完成，未发现明显问题。"

        sanitized, removed_count = sanitize_text_check_result(content)

        self.assertEqual(sanitized, content)
        self.assertEqual(removed_count, 0)


if __name__ == "__main__":
    unittest.main()
