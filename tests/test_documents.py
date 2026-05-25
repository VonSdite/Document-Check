import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook

from app.documents import allowed_file, extract_text, format_document_text
from app.images import extract_images, format_image_document_text, image_items_from_meta


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
        self.assertEqual(image["position"], "document-block001-p001")
        self.assertTrue(image["filename"].startswith("0001_document-block001-p001"))
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
