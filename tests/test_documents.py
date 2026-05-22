import unittest

from app.documents import format_document_text


class DocumentFormattingTest(unittest.TestCase):
    def test_formats_document_text_with_filename(self):
        self.assertEqual(
            format_document_text("../报告.txt", " 内容 "),
            "file: 报告.txt\n\n内容",
        )

    def test_empty_text_stays_empty(self):
        self.assertEqual(format_document_text("报告.txt", "  "), "")


if __name__ == "__main__":
    unittest.main()
