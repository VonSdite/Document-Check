import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from app.documents.extraction import extract_document, extract_text, pdf
from app.documents.extraction.common import DocumentReadCanceled
from app.documents.extraction.pdf_margins import find_repeated_pdf_margins


class PdfMarginsTest(unittest.TestCase):
    def test_repeated_margins_keep_first_occurrence_and_identical_body_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "margins.pdf"
            with fitz.open() as document:
                for index in range(4):
                    page = document.new_page()
                    page.insert_text((50, 180), "Alarm attributes", fontsize=10)
                    page.insert_text((50, 210), "Release 2026", fontsize=10)
                    page.insert_text((50, 40), "Release 2026", fontsize=10)
                    page.insert_text((50, 800), "Copyright example", fontsize=10)
                    page.insert_text((520, 800), str(index + 1), fontsize=10)
                document.save(path)
            for include_tables in (False, True):
                with self.subTest(include_tables=include_tables):
                    text = extract_text(path, "pdf", include_tables=include_tables)
                    self.assertEqual(text.count("Alarm attributes"), 4)
                    self.assertEqual(text.count("Release 2026"), 5)
                    self.assertEqual(text.count("Copyright example"), 1)
                    for number in range(1, 5):
                        self.assertIn(f"[第{number}页]", text)
                        self.assertIn(f"\n{number}", text)

    def test_chapter_and_alternating_margins_keep_one_of_each(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chapters.pdf"
            with fitz.open() as document:
                for index in range(10):
                    page = document.new_page()
                    page.insert_text((50, 180), f"Body {index}", fontsize=10)
                    if index < 6:
                        title = "Odd header" if index % 2 else "Even header"
                    elif index < 9:
                        title = "Next chapter"
                    else:
                        title = "Unique caution"
                    page.insert_text((50, 40), title, fontsize=10)
                document.save(path)
            text = extract_text(path, "pdf", include_tables=False)
            for title in (
                "Odd header",
                "Even header",
                "Next chapter",
                "Unique caution",
            ):
                self.assertEqual(text.count(title), 1)
            for index in range(10):
                self.assertIn(f"Body {index}", text)

    def test_unique_versions_sparse_notes_and_roman_page_numbers_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unique.pdf"
            numbers = ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x")
            with fitz.open() as document:
                for index, number in enumerate(numbers):
                    page = document.new_page()
                    page.insert_text((50, 180), "Body", fontsize=10)
                    page.insert_text((50, 40), f"Version {index + 1}", fontsize=10)
                    page.insert_text((520, 800), number, fontsize=10)
                    if index in (0, 4, 9):
                        page.insert_text((50, 800), "Occasional caution", fontsize=10)
                document.save(path)
            text = extract_text(path, "pdf", include_tables=False)
            self.assertEqual(text.count("Occasional caution"), 3)
            for index, number in enumerate(numbers):
                self.assertIn(f"Version {index + 1}", text)
                self.assertIn(f"\n{number}", text)

    def test_margin_table_cells_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge-table.pdf"
            with fitz.open() as document:
                for _ in range(3):
                    page = document.new_page()
                    for x in (40, 250, 450):
                        page.draw_line((x, 10), (x, 70))
                    for y in (10, 40, 70):
                        page.draw_line((40, y), (450, y))
                    page.insert_text((50, 30), "Required field", fontsize=8)
                    page.insert_text((260, 30), "Value", fontsize=8)
                    page.insert_text((50, 60), "Required step", fontsize=8)
                    page.insert_text((260, 60), "Check", fontsize=8)
                    page.insert_text((50, 180), "Body", fontsize=10)
                document.save(path)
            for include_tables in (False, True):
                with self.subTest(include_tables=include_tables):
                    text = extract_text(path, "pdf", include_tables=include_tables)
                    self.assertEqual(text.count("Required field"), 3)
                    self.assertEqual(text.count("Required step"), 3)

    def test_repeated_margin_links_keep_all_targets_and_source_locations(self):
        target = "https://example.test/manual"
        body_target = "https://example.test/body"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.pdf"
            with fitz.open() as document:
                for index in range(3):
                    page = document.new_page()
                    page.insert_text((50, 40), "Manual portal", fontsize=10)
                    page.insert_text((50, 180), f"Body {index}", fontsize=10)
                    page.insert_link(
                        {
                            "kind": fitz.LINK_URI,
                            "from": fitz.Rect(48, 27, 120, 43),
                            "uri": target,
                        }
                    )
                    if index == 1:
                        page.insert_text((50, 220), "Manual portal", fontsize=10)
                        page.insert_link(
                            {
                                "kind": fitz.LINK_URI,
                                "from": fitz.Rect(48, 207, 120, 223),
                                "uri": body_target,
                            }
                        )
                document.save(path)
            text, hyperlinks = extract_document(path, "pdf")
            self.assertEqual(text.count("[超链接]"), 2)
            self.assertEqual(text.count(target), 1)
            self.assertEqual(text.count(body_target), 1)
            self.assertEqual(
                [item["target"] for item in hyperlinks],
                [target, target, body_target, target],
            )
            self.assertEqual(
                [item["location"] for item in hyperlinks],
                ["第1页", "第2页", "第2页", "第3页"],
            )

    def test_page_fallback_keeps_ambiguous_identical_body_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fallback.pdf"
            with fitz.open() as document:
                for _ in range(3):
                    page = document.new_page()
                    page.insert_text((50, 180), "Release", fontsize=10)
                    page.insert_text((50, 40), "Release", fontsize=10)
                    page.insert_text((50, 800), "Copyright example", fontsize=10)
                document.save(path)
            extract_page = pdf._extract_pymupdf_page_text

            def empty_primary(page):
                _text, raw_page = extract_page(page)
                return "", raw_page

            with patch.object(
                pdf, "_extract_pymupdf_page_text", side_effect=empty_primary
            ):
                text = extract_text(path, "pdf")
            self.assertEqual(text.count("Release"), 6)
            self.assertEqual(text.count("Copyright example"), 1)

    def test_two_page_document_keeps_unconfirmed_margins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.pdf"
            with fitz.open() as document:
                for _ in range(2):
                    document.new_page().insert_text((50, 40), "Repeated", fontsize=10)
                document.save(path)
            self.assertEqual(extract_text(path, "pdf").count("Repeated"), 2)

    def test_missing_page_coordinates_keep_repeated_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "no-layout.pdf"
            with fitz.open() as document:
                for _ in range(3):
                    document.new_page().insert_text((50, 40), "Repeated", fontsize=10)
                document.save(path)
            with patch.object(pdf.fitz, "open", side_effect=RuntimeError("no layout")):
                self.assertEqual(extract_text(path, "pdf").count("Repeated"), 3)

    def test_margin_scan_observes_cancellation_before_reading_next_page(self):
        with fitz.open() as document:
            for _ in range(3):
                document.new_page().insert_text((50, 40), "Header")
            cancel = threading.Event()
            get_text = fitz.Page.get_text

            def first_page(page, *args, **kwargs):
                result = get_text(page, *args, **kwargs)
                cancel.set()
                return result

            with patch.object(
                fitz.Page, "get_text", autospec=True, side_effect=first_page
            ) as read:
                with self.assertRaises(DocumentReadCanceled):
                    find_repeated_pdf_margins(document, cancel_event=cancel)
            self.assertEqual(read.call_count, 1)
