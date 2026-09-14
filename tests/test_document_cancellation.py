import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from openpyxl import Workbook

from app.documents.extraction import extract_document, pdf, spreadsheets
from app.documents.extraction.common import DocumentReadCanceled


class DocumentCancellationTest(unittest.TestCase):
    def test_pdf_cancellation_stops_before_next_page(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pages.pdf"
            with fitz.open() as document:
                for _ in range(3):
                    document.new_page().insert_text((72, 72), "Cancellation test")
                document.save(path)
            cancel = threading.Event()
            extract_page = pdf._extract_pymupdf_page_text

            def first_page(page):
                result = extract_page(page)
                cancel.set()
                return result

            with patch.object(
                pdf, "_extract_pymupdf_page_text", side_effect=first_page
            ) as read:
                with self.assertRaises(DocumentReadCanceled):
                    extract_document(path, "pdf", cancel_event=cancel)
            self.assertEqual(read.call_count, 1)

    def test_pdf_table_cancellation_propagates_through_fallback_handler(self):
        with fitz.open() as document:
            page = document.new_page()
            for coordinate in (50, 100, 150):
                page.draw_line((50, coordinate), (150, coordinate))
                page.draw_line((coordinate, 50), (coordinate, 150))
            for x, y in ((60, 80), (110, 80), (60, 130), (110, 130)):
                page.insert_text((x, y), "Cell")
            with patch.object(
                pdf, "_pdf_table_model", side_effect=DocumentReadCanceled("canceled")
            ) as model:
                with self.assertRaises(DocumentReadCanceled):
                    pdf._extract_pymupdf_page_with_tables(page, 1, {}, "Cell")
            model.assert_called_once()

    def test_512_column_excel_cancellation_stops_before_next_row(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wide.xlsx"
            book = Workbook()
            for _ in range(5):
                book.active.append(["测试值"] * 512)
            book.save(path)
            book.close()
            cancel = threading.Event()
            format_row = spreadsheets._spreadsheet_row_text

            def first_row(values):
                result = format_row(values)
                cancel.set()
                return result

            with patch.object(
                spreadsheets, "_spreadsheet_row_text", side_effect=first_row
            ) as read:
                with self.assertRaises(DocumentReadCanceled):
                    extract_document(path, "xlsx", cancel_event=cancel)
            self.assertEqual(read.call_count, 1)

    def test_canceled_extraction_does_not_open_source_file(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(DocumentReadCanceled):
            extract_document(Path("missing.pdf"), "pdf", cancel_event=cancel)


if __name__ == "__main__":
    unittest.main()
