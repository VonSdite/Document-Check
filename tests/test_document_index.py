import tempfile
import threading
import unittest
from pathlib import Path

import fitz
from docx import Document
from pydantic import ValidationError

from app.checks.evidence_tools import execute_tool
from app.contracts.agent import AgentOptions, Candidate, strict_schema
from app.documents.extraction import extract_document
from app.documents.extraction.common import DocumentReadCanceled
from app.documents.index import DocumentIndex, text_blocks


class DocumentIndexTest(unittest.TestCase):
    def test_docx_preserves_paragraph_table_context_and_empty_columns(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "guide.docx"
            doc = Document()
            doc.add_heading("A 型号", 1)
            doc.add_paragraph("下表只适用于 A 型号。")
            table = doc.add_table(rows=1, cols=3)
            table.cell(0, 0).text = "电压"
            table.cell(0, 2).text = "220 V"
            doc.add_heading("B 型号", 1)
            doc.add_paragraph("110 V")
            doc.save(path)
            blocks = []
            text, _ = extract_document(path, "docx", blocks=blocks)
            self.assertLess(text.index("电压"), text.index("B 型号"))
            self.assertIn("电压 |  | 220 V", text)
            source = next(b for b in blocks if b["kind"] == "table")
            self.assertEqual(source["section"], "A 型号")
            self.assertIn("表格1", source["location"])

    def test_docx_cancellation_checks_content_boundaries(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "guide.docx"
            Document().save(path)
            canceled = threading.Event()
            canceled.set()
            with self.assertRaises(DocumentReadCanceled):
                extract_document(path, "docx", cancel_event=canceled)

    def test_docx_headers_footers_and_their_tables_keep_source_locations(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "guide.docx"
            doc = Document()
            doc.add_paragraph("安装指南正文")
            section = doc.sections[0]
            section.header.paragraphs[0].text = "A型号 / 版本2"
            table = section.footer.add_table(rows=1, cols=2, width=section.page_width)
            table.cell(0, 0).text = "发布日期"
            table.cell(0, 1).text = "2026-01-01"
            section.different_first_page_header_footer = True
            section.first_page_header.paragraphs[0].text = "首页说明"
            doc.save(path)
            blocks = []
            text, _ = extract_document(path, "docx", blocks=blocks)
            self.assertIn("首页说明", text)
            self.assertIn("发布日期 | 2026-01-01", text)
            footer = next(b for b in blocks if b["kind"] == "table")
            self.assertIn("页脚 / 表格", footer["location"])
            self.assertEqual(footer["section"], "第1节 / 页脚")

    def test_pdf_records_unread_pages(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "guide.pdf"
            doc = fitz.open()
            doc.new_page().insert_text((72, 72), "Voltage 220 V")
            doc.new_page()
            doc.save(path)
            doc.close()
            blocks = []
            extract_document(path, "pdf", blocks=blocks)
            self.assertEqual(len(blocks), 2)
            self.assertEqual(blocks[1]["kind"], "unresolved")

    def test_reference_requires_exact_quote_and_allowed_source(self):
        index = DocumentIndex(
            [{"filename": "a.txt", "text": "A: 220 V\nB: 110 V", "text_complete": True}]
        )
        first, second = index.blocks
        self.assertTrue(index.verify({"source_id": first.id, "quote": "220 V"}))
        self.assertFalse(index.verify({"source_id": first.id, "quote": "110 V"}))
        self.assertFalse(
            index.verify({"source_id": first.id, "quote": "220 V"}, allowed={second.id})
        )
        self.assertFalse(index.verify({"source_id": first.id, "quote": ""}))

    def test_source_ids_stable_and_documents_separated(self):
        documents = [
            {"filename": "a", "text": "same"},
            {"filename": "b", "text": "same"},
        ]
        index = DocumentIndex(documents)
        self.assertEqual(index.signature, DocumentIndex(documents).signature)
        self.assertNotEqual(index.blocks[0].id, index.blocks[1].id)
        self.assertEqual(len(index.context(index.blocks[0].id, 3)), 1)

    def test_duplicate_filenames_do_not_mix_context_or_table_rows(self):
        index = DocumentIndex(
            [
                {
                    "filename": "guide.docx",
                    "role": "master",
                    "blocks": [
                        {"kind": "table", "location": "表格1 > 第1行", "text": text}
                    ],
                    "text_complete": True,
                }
                for text in ("A型号 | 220 V", "B型号 | 110 V")
            ]
        )
        source = index.blocks[0]
        self.assertEqual(
            [b["text"] for b in index.context(source.id, 3)], [source.text]
        )
        result = execute_tool("read_table", {"source_id": source.id}, index, {})
        self.assertEqual([b["text"] for b in result["sources"]], [source.text])

    def test_table_read_keeps_requested_distant_row_and_headers(self):
        index = DocumentIndex(
            [
                {
                    "filename": "guide.docx",
                    "text_complete": True,
                    "blocks": [
                        {
                            "kind": "table",
                            "location": f"表格1 > 第{i + 1}行",
                            "text": f"型号{i} | 参数{i}",
                        }
                        for i in range(80)
                    ],
                }
            ]
        )
        target = index.blocks[65]
        result = execute_tool("read_table", {"source_id": target.id}, index, {})
        self.assertEqual(result["sources"][0]["id"], target.id)
        self.assertIn(index.blocks[0].id, {b["id"] for b in result["sources"]})
        self.assertEqual(len(result["sources"]), 20)
        self.assertTrue(result["truncated"])

    def test_missing_hits_do_not_establish_complete_coverage(self):
        index = DocumentIndex(
            [{"filename": "a", "text": "first\nsecond", "text_complete": False}]
        )
        self.assertEqual(index.search("missing"), [])
        coverage = index.coverage([b.id for b in index.blocks])
        self.assertTrue(coverage["all_checked"])
        self.assertFalse(coverage["text_complete"])

    def test_text_tables_stay_with_page_context(self):
        blocks = text_blocks(
            "[第2页]\n# Scope\n[PDF结构化表格 T1 开始；x]\n<table>220</table>\n[PDF结构化表格 T1 结束]\nEnd"
        )
        table = next(b for b in blocks if b["kind"] == "table")
        self.assertIn("第2页", table["location"])
        self.assertEqual(table["section"], "Scope")
        self.assertEqual(blocks[-1]["text"], "End")

    def test_candidate_schema_rejects_unrecognized_fields_and_types(self):
        with self.assertRaises(ValidationError):
            Candidate.model_validate(
                {"description": "无证据", "severity": "fatal", "evidence": "none"}
            )
        schema = strict_schema(Candidate)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        for invalid_price in (float("nan"), float("inf"), -1.0):
            with self.assertRaises(ValidationError):
                AgentOptions(input_price=invalid_price)


if __name__ == "__main__":
    unittest.main()
