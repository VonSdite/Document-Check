import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.documents import allowed_file, extract_text, format_document_text


class DocumentFormattingTest(unittest.TestCase):
    def test_formats_document_text_with_filename(self):
        self.assertEqual(
            format_document_text("../报告.txt", " 内容 "),
            "file: 报告.txt\n\n内容",
        )

    def test_empty_text_stays_empty(self):
        self.assertEqual(format_document_text("报告.txt", "  "), "")

    def test_allows_excel_files(self):
        self.assertTrue(allowed_file("素材.xlsx"))
        self.assertTrue(allowed_file("素材.xlsm"))
        self.assertTrue(allowed_file("素材.xls"))

    def test_extracts_xlsx_workbook_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "素材.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "参数表"
            sheet.append(["项目", "参数", "单位"])
            sheet.append(["额定电流", 10, "A"])
            workbook.save(path)
            workbook.close()

            text = extract_text(path, "xlsx")

        self.assertIn("# 工作表：参数表", text)
        self.assertIn("项目 | 参数 | 单位", text)
        self.assertIn("额定电流 | 10 | A", text)


if __name__ == "__main__":
    unittest.main()
