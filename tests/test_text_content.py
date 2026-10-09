import unittest

from app.checks.text_content import document_body_view


class DocumentBodyViewTest(unittest.TestCase):
    def test_maps_decoded_entities_and_inline_markup_to_source(self):
        source = "file: doc.pdf\n[第3页]\n<p>at&amp;t open<b>ai</b> 与中文。</p>"
        body = document_body_view(source)

        for observed, original in [("at&t", "at&amp;t"), ("openai", "open<b>ai")]:
            with self.subTest(observed=observed):
                start = body.text.index(observed)
                source_start, source_end = body.source_span(
                    start, start + len(observed)
                )
                self.assertEqual(source[source_start:source_end], original)
        self.assertNotIn("file:", body.text)
        self.assertNotIn("第3页", body.text)
        self.assertIn("与中文。", body.text)

    def test_keeps_table_anchors_and_excludes_inherited_and_nontext_cells(self):
        source = (
            "[PDF结构化表格 page001-table001 开始；1行×5列；置信度=high]\n"
            '<table id="page001-table001" data-view="normalized"><tr>'
            '<td data-cell="A1" data-original-colspan="2">Settings</td>'
            '<td data-cell="B1" data-inherited-from="A1"><b>Settings</b></td>'
            '<td data-cell="C1" data-empty="true">[空单元格]</td>'
            '<td data-cell="D1" data-non-text="true">[非文本图形或图标]</td>'
            '<td data-cell="E1" data-unresolved="true">[未能可靠还原的单元格]</td>'
            "</tr></table>\n[PDF结构化表格 page001-table001 结束]"
        )
        body = document_body_view(source)

        self.assertEqual(body.text.split(), ["Settings"])
        start = body.text.index("Settings")
        source_start, source_end = body.source_span(start, start + len("Settings"))
        self.assertEqual(source_start, source.index("Settings"))
        self.assertEqual(source[source_start:source_end], "Settings")

    def test_excludes_multiline_link_annotations_and_keeps_next_page(self):
        source = (
            "[第1页]\nFirst page\n[超链接] Repeated\n"
            "link label（超链接：https://example.test）\n"
            "[第2页]\nSecond page\n[超链接] Another label"
        )
        body = document_body_view(source)

        self.assertEqual(body.text.split(), ["First", "page", "Second", "page"])
        start = body.text.index("Second")
        self.assertEqual(body.source_span(start, start + 6)[0], source.index("Second"))

    def test_excludes_markdown_targets_and_code_but_keeps_link_labels(self):
        source = (
            "[Visible label](guide.html)\n[Reference]: https://example.test\n"
            '`command --flag`\n```python\nprint("example")\n```\n'
            "Actual body\n<script>hidden text</script><!-- hidden comment -->"
        )

        body = document_body_view(source)
        self.assertIn("Visible label", body.text)
        self.assertIn("Actual body", body.text)
        for ignored in ("guide.html", "Reference", "command", "print", "hidden"):
            self.assertNotIn(ignored, body.text)

    def test_empty_or_symbol_only_body_has_no_prose(self):
        for source in ["", "file: guide.pdf\n[第1页]\n[超链接] https://example.test"]:
            with self.subTest(source=source):
                self.assertFalse(document_body_view(source).text.strip())

    def test_html_declarations_and_comments_preserve_visible_words(self):
        source = '<!DOCTYPE html><?xml version="1.0"?><p>open<!-- note -->ai</p>'
        body = document_body_view(source)

        self.assertEqual(body.text.strip(), "openai")
        start = body.text.index("openai")
        source_start, source_end = body.source_span(start, start + 6)
        self.assertEqual(source[source_start:source_end], "open<!-- note -->ai")

    def test_encoded_network_targets_are_filtered_after_entity_decoding(self):
        source = (
            "<p>https&#58;&#47;&#47;example.test/smartlogger "
            "smartlogger&#64;example.test</p>\n[第8页]\nActual smartlogger spelling."
        )
        body = document_body_view(source)

        self.assertEqual(body.text.split(), ["Actual", "smartlogger", "spelling."])
        start = body.text.index("smartlogger")
        self.assertEqual(
            body.source_span(start, start + 11)[0], source.rindex("smartlogger")
        )


if __name__ == "__main__":
    unittest.main()
