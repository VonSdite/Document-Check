import json
import unittest

from app.documents.extraction.pdf import _format_pdf_table_html
from app.models.document_input import compact_model_document_text, validate_model_input
from tests.fixtures.pdf_tables import TABLE_ID, normalized_pdf_table, pdf_table_model


class ModelDocumentInputTest(unittest.TestCase):
    def test_compact_table_preserves_values_coordinates_and_merged_ranges(self):
        table = normalized_pdf_table()
        prefix = "file: guide.pdf\n\n[第75页]\n告警属性\n"
        suffix = "\n处理步骤\n[超链接]\n安装指南：https://example.test/guide"
        result = compact_model_document_text(prefix + table + suffix)
        expected_table = "\n".join(
            [
                f"[表 {TABLE_ID} 3×3 H",
                'A1:C1="组合柜"',
                'A2:A3="告警ID"',
                'B2="级别"',
                "C2=[空单元格]",
                "B3=[非文本图形或图标]",
                'C3="温度 ≥ 30℃\\n路径 C:\\\\alarm\\t\\"确认\\" & <标签>"',
                "]",
            ]
        )
        self.assertEqual(result, prefix + expected_table + suffix)
        self.assertLess(len(result), len(prefix + table + suffix))
        self.assertEqual(result.count("组合柜"), 1)
        self.assertEqual(result.count("告警ID"), 1)
        value = expected_table.splitlines()[-2].split("=", 1)[1]
        self.assertEqual(json.loads(value), pdf_table_model()["cells"][-1]["text"])

    def test_empty_and_nontext_merged_cells_keep_anchor_state(self):
        for nontext, expected in (
            (False, "[空单元格]"),
            (True, "[非文本图形或图标]"),
        ):
            with self.subTest(nontext=nontext):
                model = pdf_table_model()
                model["cells"][0].update(text="", has_nontext_content=nontext)
                result = compact_model_document_text(_format_pdf_table_html(model))
                self.assertIn(f"A1:C1={expected}\n", result)
                self.assertNotIn("继承自", result)
                self.assertNotIn("B1=", result)

    def test_unresolved_cells_and_literal_placeholder_text_are_distinct(self):
        model = pdf_table_model()
        model["cells"] = model["cells"][:-1]
        model["unresolved_positions"] = [(2, 2)]
        model["confidence"] = "low"
        model["cells"][2]["text"] = "[空单元格]"
        result = compact_model_document_text(_format_pdf_table_html(model))
        self.assertIn('B2="[空单元格]"\n', result)
        self.assertIn("C2=[空单元格]\n", result)
        self.assertIn("C3=[未能可靠还原的单元格]\n", result)
        self.assertIn(f"[表 {TABLE_ID} 3×3 L\n", result)

    def test_two_dimensional_merge_and_columns_after_z(self):
        model = pdf_table_model()
        model.update(row_count=2, column_count=27)
        model["cells"] = [
            dict(
                row=row,
                column=column,
                rowspan=2 if column == 0 else 1,
                colspan=2 if column == 0 else 1,
                text="合并" if column == 0 else f"值{row}-{column}",
                has_nontext_content=False,
            )
            for row in range(2)
            for column in range(27)
            if column >= 2 or (row, column) == (0, 0)
        ]
        result = compact_model_document_text(_format_pdf_table_html(model))
        self.assertIn('A1:B2="合并"\n', result)
        self.assertIn('AA2="值1-26"\n', result)
        self.assertEqual(result.count('="合并"'), 1)

    def test_confidence_repeated_values_and_multiple_file_scopes_are_preserved(self):
        for confidence in ("high", "medium", "low"):
            with self.subTest(confidence=confidence):
                model = pdf_table_model()
                model["confidence"] = confidence
                model["cells"][-1]["text"] = model["cells"][2]["text"]
                table = _format_pdf_table_html(model)
                document = f"file: first.pdf\n{table}\nfile: second.pdf\n{table}"
                result = compact_model_document_text(document)
                code = {"high": "H", "medium": "M", "low": "L"}[confidence]
                self.assertEqual(result.count(f"[表 {TABLE_ID} 3×3 {code}\n"), 2)
                self.assertEqual(result.count('="级别"'), 4)
                self.assertEqual(compact_model_document_text(result), result)

    def test_unrecognized_or_inconsistent_tables_preserve_full_source(self):
        table = normalized_pdf_table()
        mutations = [
            ("3行×3列", "4行×3列"),
            ('data-cell="C2"', 'data-cell="C9"'),
            ('data-original-range="A1:C1"', 'data-original-range="A1:D1"'),
            ('data-original-colspan="3"', 'data-original-colspan="2"'),
            ('data-inherited-from="A1"', 'data-inherited-from="B2"'),
            ('data-inherited-from="A1">组合柜', 'data-inherited-from="A1">另一个值'),
            ('data-empty="true"', 'data-empty="false"'),
            ('data-empty="true">[空单元格]', 'data-empty="true">正文内容'),
            ('data-cell="B2"', 'data-cell="B2" data-extra="保留说明"'),
            ('data-cell="B2"', 'data-cell="B2" data-cell="C2"'),
            ('<td data-cell="B2">', '<th data-cell="B2">'),
            ("  <tr>", "额外说明\n  <tr>"),
            (f"[PDF结构化表格 {TABLE_ID} 结束]", "缺少结束标记"),
        ]
        for old, new in mutations:
            with self.subTest(mutation=new):
                source = table.replace(old, new, 1)
                self.assertNotEqual(source, table)
                self.assertEqual(compact_model_document_text(source), source)

    def test_unrelated_text_and_html_are_preserved(self):
        for source in (
            "file: example.docx\n普通正文\nA1:C1=原文",
            '<table id="user-table"><tr><td>原文</td></tr></table>',
            "[PDF结构化表格 注释] <table>原文</table>",
            "",
        ):
            with self.subTest(source=source):
                self.assertEqual(compact_model_document_text(source), source)

    def test_input_limit_counts_exact_model_view_without_truncating_text(self):
        source = "file: guide.pdf\n\n" + normalized_pdf_table()
        compact = compact_model_document_text(source)
        validate_model_input(source, len(compact))
        with self.assertRaisesRegex(
            RuntimeError, f"文档文本 {len(compact)} 字，超过当前模型文本上限"
        ):
            validate_model_input(source, len(compact) - 1)
        with self.assertRaisesRegex(RuntimeError, "文档文本 200 字"):
            validate_model_input("正文" * 100, 199)


if __name__ == "__main__":
    unittest.main()
