import unittest

from app.documents import split_text_chunks


class DocumentChunkingTest(unittest.TestCase):
    def test_short_text_returns_single_chunk(self):
        chunks = split_text_chunks("第一章\n内容", 100)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["index"], 1)
        self.assertEqual(chunks[0]["total"], 1)
        self.assertEqual(chunks[0]["label"], "第一章")
        self.assertEqual(chunks[0]["text"], "第一章\n内容")

    def test_splits_on_paragraph_boundaries(self):
        text = "\n\n".join([f"第{i}段 " + ("内容" * 20) for i in range(1, 6)])

        chunks = split_text_chunks(text, 80)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk["text"]) <= 80 for chunk in chunks))
        self.assertEqual([chunk["index"] for chunk in chunks], list(range(1, len(chunks) + 1)))
        self.assertTrue(all(chunk["total"] == len(chunks) for chunk in chunks))

    def test_splits_oversized_paragraph(self):
        text = "无标点长段落" * 80

        chunks = split_text_chunks(text, 90)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk["text"]) <= 90 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
