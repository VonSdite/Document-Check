import json
import unittest

from app.long_documents import (
    align_language_chunks,
    build_chunk_search_index,
    build_consistency_candidates,
    build_document_outline,
    consistency_candidate_batches,
    extract_consistency_facts,
    long_document_limits,
    merge_chunk_reports,
    parse_consistency_documents,
    parse_language_consistency_documents,
    parse_structured_report,
    rank_relevant_chunks,
    split_grouped_document,
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

    def test_multi_document_parser_preserves_file_boundaries(self):
        text = (
            "# 素材文档\n\n"
            "## 素材文档1：master-a.txt\n素材A\n\n"
            "## 素材文档2：master-b.txt\n素材B\n\n"
            "# 资料\n\n"
            "## 资料1：related.txt\n资料正文"
        )

        grouped = parse_consistency_documents(text)

        self.assertEqual([item.filename for item in grouped["master"]], ["master-a.txt", "master-b.txt"])
        self.assertEqual(grouped["related"][0].text, "资料正文")

    def test_multi_document_retrieval_prefers_matching_parameter_chunk(self):
        text = (
            "# 素材文档\n\n## 素材文档1：master.txt\n"
            f"[第1页]\n{'通用安装说明。' * 180}\n\n"
            f"[第2页]\n型号 ZX-900 的额定电流为 32A。{'技术参数。' * 160}\n\n"
            "# 资料\n\n## 资料1：related.txt\n"
            f"[第1页]\nZX-900 额定电流标注为 30A。{'产品说明。' * 160}"
        )
        grouped = parse_consistency_documents(text)
        master_chunks = split_grouped_document(grouped["master"][0], max_chars=1_500)
        related_chunk = split_grouped_document(grouped["related"][0], max_chars=1_500)[0]

        ranked = rank_relevant_chunks(related_chunk, build_chunk_search_index(master_chunks), top_k=1)

        self.assertIn("ZX-900", ranked[0].text)
        self.assertIn("32A", ranked[0].text)

    def test_language_alignment_uses_section_numbers_and_leaves_missing_section(self):
        text = (
            "# 静态预检摘要\n数字线索\n\n"
            "# 文档A：zh.txt\n"
            f"[第1页]\n1. 安装\n{'安装说明。' * 180}\n\n"
            f"[第2页]\n2. 接线\n{'接线说明。' * 180}\n\n"
            f"[第3页]\n3. 启动\n{'启动说明。' * 180}\n\n"
            "# 文档B：en.txt\n"
            f"[第1页]\n1. Installation\n{'Installation details. ' * 50}\n\n"
            f"[第2页]\n3. Startup\n{'Startup details. ' * 50}"
        )
        _, document_a, document_b = parse_language_consistency_documents(text)
        self.assertIsNotNone(document_a)
        self.assertIsNotNone(document_b)
        left_chunks = split_grouped_document(document_a, max_chars=1_500)
        right_chunks = split_grouped_document(document_b, max_chars=1_500)

        alignments, unmatched_left, unmatched_right = align_language_chunks(left_chunks, right_chunks)

        aligned_text = [(item.left.text, item.right.text) for item in alignments]
        self.assertTrue(any("1. 安装" in left and "1. Installation" in right for left, right in aligned_text))
        self.assertTrue(any("3. 启动" in left and "3. Startup" in right for left, right in aligned_text))
        self.assertTrue(any("2. 接线" in chunk.text for chunk in unmatched_left))
        self.assertEqual(unmatched_right, [])


if __name__ == "__main__":
    unittest.main()
