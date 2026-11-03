import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from app.documents.extraction import extract_document, extract_text
from app.documents.extraction.pdf import _normalize_pdf_toc_leaders


class PdfTextCleanupTest(unittest.TestCase):
    def test_toc_leaders_keep_titles_page_numbers_and_line_breaks(self):
        cases = [
            ("1.2 模块丢失" + "." * 182 + "2\n", "1.2 模块丢失\t2\n"),
            ("  Introduction ........  iv\n", "  Introduction\tiv\n"),
            ("Appendix . . . . . . . . A-12", "Appendix\tA-12"),
            ("目录标题…………12", "目录标题\t12"),
            ("目录标题．．．．．．．．12", "目录标题\t12"),
            ("目录标题········12", "目录标题\t12"),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(_normalize_pdf_toc_leaders(source), expected)

    def test_prose_technical_content_and_unconfirmed_leaders_are_preserved(self):
        sources = [
            "继续……下一步。",
            "省略......12",
            "等待........稍后重试",
            "占位符 ........",
            "........12",
            "型号 v1.2.3，地址 192.168.1.12",
            "https://example.com/........12",
            r"C:\logs\........12",
            "../logs/........12",
            "logs/........12",
            'print("........12")',
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(_normalize_pdf_toc_leaders(source), source)

    def test_pdf_body_and_link_label_share_cleanup_and_keep_target(self):
        title = "1.2 Module lost" + "." * 80 + "2"
        target = "https://example.com/" + "." * 12 + "12"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "toc.pdf"
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((40, 60), title, fontsize=8)
                page.insert_text((40, 90), "Version 1.2.3 ... retry", fontsize=8)
                page.insert_link(
                    {
                        "kind": fitz.LINK_URI,
                        "from": fitz.Rect(35, 45, 560, 65),
                        "uri": target,
                    }
                )
                document.save(path)
            for include_tables in (False, True):
                with self.subTest(include_tables=include_tables):
                    text, hyperlinks = extract_document(
                        path, "pdf", include_tables=include_tables
                    )
                    self.assertIn("[第1页]\n1.2 Module lost\t2", text)
                    self.assertIn("Version 1.2.3 ... retry", text)
                    self.assertIn(target, text)
                    self.assertNotIn("." * 80, text)
                    self.assertEqual(
                        hyperlinks[0]["display_text"], "1.2 Module lost\t2"
                    )
                    self.assertEqual(hyperlinks[0]["target"], target)

    def test_pypdf_fallback_uses_same_toc_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fallback.pdf"
            with fitz.open() as document:
                page = document.new_page()
                page.insert_text((40, 60), "Setup" + "." * 80 + "12", fontsize=8)
                document.save(path)
            with patch(
                "app.documents.extraction.pdf.fitz.open",
                side_effect=RuntimeError("layout unavailable"),
            ):
                text = extract_text(path, "pdf")
            self.assertIn("[第1页]\nSetup\t12", text)
            self.assertNotIn("." * 80, text)

    def test_pdf_table_cleanup_preserves_cells_and_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "toc-table.pdf"
            with fitz.open() as document:
                page = document.new_page()
                for x in (40, 400, 550):
                    page.draw_line((x, 40), (x, 120))
                for y in (40, 80, 120):
                    page.draw_line((40, y), (550, y))
                page.insert_text((50, 60), "Setup" + "." * 60 + "12", fontsize=8)
                page.insert_text((410, 60), "12", fontsize=8)
                page.insert_text((50, 100), "Version 1.2.3 ... retry", fontsize=8)
                page.insert_text((410, 100), "13", fontsize=8)
                document.save(path)
            text = extract_text(path, "pdf")
            self.assertIn('<td data-cell="A1">Setup\t12</td>', text)
            self.assertIn('<td data-cell="B1">12</td>', text)
            self.assertIn('<td data-cell="A2">Version 1.2.3 ... retry</td>', text)
            self.assertNotIn("." * 60, text)
