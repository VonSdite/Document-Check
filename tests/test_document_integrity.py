import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

from docx import Document
from openpyxl import Workbook

from app.documents.extraction import docx, extract_document
from app.documents.extraction.common import DocumentReadCanceled
from tests.test_documents import _add_docx_hyperlink

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class DocumentIntegrityTest(unittest.TestCase):
    def test_docx_merged_cells_keep_text_once_and_preserve_independent_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "merged.docx"
            book = Document()
            table = book.add_table(rows=3, cols=4)
            table.cell(0, 0).merge(table.cell(0, 3)).text = "横向合并正文" * 100
            table.cell(1, 0).merge(table.cell(2, 0)).text = "纵向合并正文" * 100
            table.cell(1, 1).text = table.cell(1, 3).text = "独立重复内容"
            book.save(path)

            text, _links = extract_document(path, "docx")

        self.assertEqual(text.count("横向合并正文"), 100)
        self.assertEqual(text.count("纵向合并正文"), 100)
        self.assertEqual(text.count("独立重复内容"), 2)
        self.assertIn("独立重复内容 |  | 独立重复内容", text)

    def test_docx_keeps_nested_tables_and_links_in_reading_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested.docx"
            book = Document()
            book.add_paragraph("表格前正文")
            cell = book.add_table(rows=1, cols=1).cell(0, 0)
            cell.paragraphs[0].text = "嵌套表格前"
            nested = cell.add_table(rows=1, cols=1).cell(0, 0)
            _add_docx_hyperlink(nested.paragraphs[0], "嵌套链接", "javascript:alert(1)")
            cell.add_paragraph("嵌套表格后")
            book.add_paragraph("表格后正文")
            book.save(path)

            text, links = extract_document(path, "docx")

        ordered = ["表格前正文", "嵌套表格前", "嵌套链接", "嵌套表格后", "表格后正文"]
        self.assertEqual(sorted(ordered, key=text.index), ordered)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["target"], "javascript:alert(1)")
        self.assertIn("表格1 > 第1行 > 第1列 > 表格1", links[0]["location"])

    def test_docx_cancellation_stops_before_next_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cancel.docx"
            book = Document()
            book.add_paragraph("第一段")
            book.add_table(rows=2, cols=2).cell(0, 0).text = "表格正文"
            book.save(path)
            cancel = threading.Event()
            paragraph_text = docx._docx_paragraph_text

            def first_paragraph(*args, **kwargs):
                result = paragraph_text(*args, **kwargs)
                cancel.set()
                return result

            with patch.object(
                docx, "_docx_paragraph_text", side_effect=first_paragraph
            ):
                with self.assertRaises(DocumentReadCanceled):
                    extract_document(path, "docx", cancel_event=cancel)

    def test_xlsx_reads_real_cells_and_trailing_links_despite_stale_dimensions(self):
        for dimension in ("A1", "A1:B3", "A1:XFD1048576", None):
            with (
                self.subTest(dimension=dimension),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "dimensions.xlsx"
                book = Workbook()
                sheet = book.active
                sheet["A1"] = "开头正文"
                sheet["D3"] = "末尾实际正文"
                sheet["C4"] = "=SUM(1,2)"
                sheet["E7"] = "末尾链接"
                sheet["E7"].hyperlink = "javascript:alert(1)"
                book.save(path)
                book.close()
                with zipfile.ZipFile(path) as archive:
                    members = {name: archive.read(name) for name in archive.namelist()}
                tree = ET.fromstring(members["xl/worksheets/sheet1.xml"])
                declared = tree.find(NS + "dimension")
                if dimension is None:
                    tree.remove(declared)
                else:
                    declared.set("ref", dimension)
                data = tree.find(NS + "sheetData")
                data.remove(next(row for row in data if row.get("r") == "7"))
                tree.find(NS + "hyperlinks")[0].set("display", "末尾链接")
                members["xl/worksheets/sheet1.xml"] = ET.tostring(tree)
                with zipfile.ZipFile(path, "w") as archive:
                    for name, content in members.items():
                        archive.writestr(name, content)

                text, links = extract_document(path, "xlsx")

                self.assertIn("末尾实际正文", text)
                self.assertIn("=SUM(1,2)", text)
                self.assertIn("末尾链接（超链接：javascript:alert(1)）", text)
                self.assertEqual(links[0]["location"], "工作表“Sheet”!E7")


if __name__ == "__main__":
    unittest.main()
