import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook

from app.documents import allowed_file, extract_text, format_document_text
from app.images import (
    candidate_pdf_pages_for_image_check,
    extract_images,
    format_image_document_text,
    image_items_from_meta,
    select_pdf_page_numbers,
)
from app.videos import (
    allowed_video_file,
    format_video_document_text,
    video_extension_of,
    _decode_process_output,
    _sample_video_timestamps,
)


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


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
            assert sheet is not None
            sheet.title = "参数表"
            sheet.append(["项目", "参数", "单位"])
            sheet.append(["额定电流", 10, "A"])
            sheet.append(["空列示例", None, "保留位置"])
            sheet["D5"] = "=SUM(1,2)"
            workbook.save(path)
            workbook.close()

            text = extract_text(path, "xlsx")

        self.assertIn("# 工作表：参数表", text)
        self.assertIn("项目 | 参数 | 单位", text)
        self.assertIn("额定电流 | 10 | A", text)
        self.assertIn("空列示例 |  | 保留位置", text)
        self.assertIn(" |  |  | =SUM(1,2)", text)

    def test_extracts_docx_images_with_position_based_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            document_path = root / "图纸.docx"
            image_dir = root / "images"
            _write_docx_with_inline_image(document_path)

            images = extract_images(document_path, "docx", image_dir, source_filename="图纸.docx")

        self.assertEqual(len(images), 1)
        image = images[0]
        self.assertEqual(image["position"], "document-1-安装步骤-block002-p002")
        self.assertTrue(image["filename"].startswith("0001_document-1-安装步骤-block002-p002"))
        self.assertEqual(image["mime_type"], "image/png")
        self.assertIn("图纸.docx", format_image_document_text("图纸.docx", images))

    def test_image_items_from_meta_normalizes_image_list(self):
        raw = """
        {
          "images": [
            {
              "id": "image-0001",
              "filename": "0001_page001-image001.png",
              "relative_path": "task/0001_page001-image001.png",
              "mime_type": "image/png",
              "position": "page001-image001",
              "source": "报告.pdf",
              "size_bytes": 128
            }
          ]
        }
        """

        images = image_items_from_meta(raw)

        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["filename"], "0001_page001-image001.png")
        self.assertEqual(images[0]["relative_path"], "task/0001_page001-image001.png")

    def test_formats_page_screenshots_in_image_document_text(self):
        text = format_image_document_text(
            "报告.pdf",
            [],
            document_text="[第1页]\n表格内容",
            page_images=[
                {
                    "filename": "0001_page001-screenshot.png",
                    "position": "page001-screenshot",
                    "mime_type": "image/png",
                    "size_bytes": 2048,
                }
            ],
            page_selection={
                "total_pages": 1,
                "omitted_pages": 0,
                "strategy": "full",
            },
        )

        self.assertIn("page_screenshots: 1", text)
        self.assertIn("page_screenshot_selection", text)
        self.assertIn("0001_page001-screenshot.png", text)

    def test_select_pdf_pages_uses_candidates_and_segments_for_long_documents(self):
        selection = select_pdf_page_numbers(200, max_pages=10, candidate_pages=[50, 120])

        self.assertEqual(selection["total_pages"], 200)
        self.assertEqual(len(selection["selected_pages"]), 10)
        self.assertIn(1, selection["selected_pages"])
        self.assertIn(50, selection["selected_pages"])
        self.assertIn(120, selection["selected_pages"])
        self.assertEqual(selection["omitted_pages"], 190)

    def test_candidate_pdf_pages_include_embedded_image_and_text_signals(self):
        pages = candidate_pdf_pages_for_image_check(
            "[第1页]\n普通正文\n\n[第2页]\n表格内容\n项目 参数 单位",
            [{"position": "page005-image001", "page_number": 5}],
        )

        self.assertEqual(pages, [2, 5])

    def test_video_helpers_format_sampling_context(self):
        self.assertTrue(allowed_video_file("安装调测.MP4"))
        self.assertFalse(allowed_video_file("安装调测.pdf"))
        self.assertEqual(video_extension_of("demo.MOV"), "mov")
        timestamps = _sample_video_timestamps(5.2, 3)

        text = format_video_document_text(
            "安装调测.mp4",
            [
                {
                    "filename": "0001_t000000000.jpg",
                    "position": "00:00.000",
                    "mime_type": "image/jpeg",
                    "size_bytes": 2048,
                },
                {
                    "filename": "0002_t000002000.jpg",
                    "position": "00:02.000",
                    "mime_type": "image/jpeg",
                    "size_bytes": 4096,
                },
            ],
            {"duration_seconds": 5.2},
        )

        self.assertEqual(len(timestamps), 3)
        self.assertEqual(timestamps[0], 0.0)
        self.assertLessEqual(timestamps[-1], 5.1)
        self.assertIn("file: 安装调测.mp4", text)
        self.assertIn("视频时长：00:05.200", text)
        self.assertIn("抽取帧数：2", text)
        self.assertIn("时间点 00:02.000", text)

    def test_video_process_output_decodes_utf8_bytes(self):
        text = _decode_process_output(b"ffmpeg: \xe2\x80\x9cinput\xe2\x80\x9d")

        self.assertIn("\u201cinput\u201d", text)


def _write_docx_with_inline_image(path: Path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdImage1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>""",
        )
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>1 安装步骤</w:t></w:r>
    </w:p>
    <w:p>
      <w:r>
        <w:drawing>
          <a:graphic>
            <a:graphicData>
              <a:blip r:embed="rIdImage1"/>
            </a:graphicData>
          </a:graphic>
        </w:drawing>
      </w:r>
    </w:p>
  </w:body>
</w:document>""",
        )
        archive.writestr("word/media/image1.png", _TINY_PNG)


if __name__ == "__main__":
    unittest.main()
