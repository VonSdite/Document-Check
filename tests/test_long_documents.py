import json
import unittest

from app.long_documents import (
    build_consistency_candidates,
    build_document_outline,
    consistency_candidate_batches,
    extract_consistency_facts,
    long_document_limits,
    merge_chunk_reports,
    parse_structured_report,
    split_long_document,
)


def _report(items: list[dict], summary: str = "") -> str:
    return json.dumps({"summary": summary, "items": items}, ensure_ascii=False)


class LongDocumentTest(unittest.TestCase):
    def test_limits_reserve_context_for_prompt_and_output(self):
        direct, chunk = long_document_limits(100_000)

        self.assertEqual(direct, 60_000)
        self.assertEqual(chunk, 40_000)
        self.assertLess(chunk, 100_000)

    def test_split_preserves_page_markers_and_overlap(self):
        text = "file: guide.pdf\n\n" + "\n\n".join(
            f"[第{page}页]\n{('第' + str(page) + '页内容。') * 80}"
            for page in range(1, 7)
        )

        chunks = split_long_document(text, max_chars=1_500, overlap_chars=120)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.text.startswith("file: guide.pdf") for chunk in chunks))
        self.assertIn("[第1页]", chunks[0].text)
        self.assertIn("分段 1/", chunks[0].label)

    def test_outline_keeps_page_and_heading_locations(self):
        text = "file: guide.pdf\n\n[第1页]\n1 Safety\n正文\n[第2页]\n1.1 Warning\n正文"

        outline = build_document_outline(text)

        self.assertIn("file: guide.pdf", outline)
        self.assertIn("[第1页]", outline)
        self.assertIn("1 Safety", outline)
        self.assertIn("1.1 Warning", outline)

    def test_merge_reports_deduplicates_overlap_and_merges_locations(self):
        item = {
            "status": "issue",
            "severity": "medium",
            "confidence": "high",
            "category": "拼写错误",
            "location": "第1页",
            "excerpt": "错字",
            "description": "存在错字",
            "impact": "影响阅读",
            "suggestion": "修改",
        }
        duplicate = {**item, "location": "第2页"}

        merged = parse_structured_report(
            merge_chunk_reports(
                [("分段1", _report([item])), ("分段2", _report([duplicate]))],
                issue_output_limit=20,
            )
        )

        self.assertIsNotNone(merged)
        self.assertEqual(len(merged["items"]), 1)
        self.assertEqual(merged["items"][0]["location"], "第1页；第2页")

    def test_consistency_facts_generate_cross_chunk_candidate(self):
        fact_a = {
            "status": "non_issue",
            "severity": "low",
            "confidence": "high",
            "category": "设备输入电压",
            "location": "第12页",
            "excerpt": "额定输入电压为220 V",
            "description": "额定值",
            "impact": "220 V",
            "suggestion": "交流输入模式",
        }
        fact_b = {**fact_a, "location": "第386页", "excerpt": "额定输入电压为110 V", "impact": "110 V"}
        facts = extract_consistency_facts(_report([fact_a]), "分段A")
        facts += extract_consistency_facts(_report([fact_b]), "分段B")

        candidates = build_consistency_candidates(facts)
        batches = consistency_candidate_batches(candidates, max_chars=2_000)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(batches), 1)
        self.assertIn("220 V", batches[0])
        self.assertIn("110 V", batches[0])


if __name__ == "__main__":
    unittest.main()
