import json
import unittest

from app.report_guardrails import (
    build_pdf_table_evidence_index,
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

    def test_pdf_table_evidence_filters_merged_nontext_and_unlocated_missing_claims(self):
        document_text = """
<table id="page001-table001" data-confidence="high">
  <tr>
    <td data-cell="A1" data-original-range="A1:B1" data-original-colspan="2">合并表头</td>
    <td data-cell="B1" data-original-range="A1:B1" data-inherited-from="A1">合并表头</td>
    <td data-cell="C1">说明</td>
  </tr>
  <tr>
    <td data-cell="A2" data-empty="true">[空单元格]</td>
    <td data-cell="B2">10 A</td>
    <td data-cell="C2" data-non-text="true">[非文本图形或图标]</td>
  </tr>
  <tr>
    <td data-cell="A3" data-original-range="A3:B3" data-original-colspan="2" data-empty="true">[空单元格]</td>
    <td data-cell="B3" data-original-range="A3:B3" data-inherited-from="A3">[合并覆盖，继承自A3]</td>
    <td data-cell="C3">备注</td>
  </tr>
</table>
"""
        evidence = build_pdf_table_evidence_index(document_text)
        items = [
            {
                "status": "issue",
                "category": "表格数据缺失",
                "location": "page001-table001 > B1",
                "description": "B1 单元格数据为空。",
            },
            {
                "status": "issue",
                "category": "表格数据缺失",
                "location": "page001-table001 > A2",
                "description": "A2 单元格数据为空。",
            },
            {
                "status": "issue",
                "category": "表格数据缺失",
                "location": "page001-table001 > C2",
                "description": "C2 单元格数据为空。",
            },
            {
                "status": "issue",
                "category": "表格数据缺失",
                "location": "page001-table001",
                "description": "表格中存在数据缺失。",
            },
            {
                "status": "issue",
                "category": "表格数据缺失",
                "location": "page001-table001 > B3",
                "description": "B3 单元格数据为空。",
            },
            {
                "status": "issue",
                "category": "表格数据缺失",
                "location": "page001-table001 > A3:B3",
                "description": "A3:B3 原始合并单元格为空。",
            },
            {
                "status": "issue",
                "category": "参数值缺失",
                "location": "第3章",
                "description": "正文中的参数值缺失。",
            },
        ]

        filtered, removed_count = filter_unsupported_visual_missing_items(
            items,
            pdf_table_evidence=evidence,
        )

        self.assertEqual(evidence["page001-table001"]["cells"]["B1"]["kind"], "merged")
        self.assertEqual(evidence["page001-table001"]["cells"]["C2"]["kind"], "nontext")
        self.assertEqual(evidence["page001-table001"]["cells"]["B3"]["anchor"], "A3")
        self.assertEqual(removed_count, 4)
        self.assertEqual(
            [item["location"] for item in filtered],
            ["page001-table001 > A2", "page001-table001 > A3:B3", "第3章"],
        )


if __name__ == "__main__":
    unittest.main()
