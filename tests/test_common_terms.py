import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.common_terms import CommonTermRule, build_common_terms_report, load_common_terms


class CommonTermsTest(unittest.TestCase):
    def test_loads_workbook_and_splits_multiple_discouraged_terms(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "common_terms.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            assert sheet is not None
            sheet.append(["常用词", "常见错误/不推荐用法"])
            sheet.append(["OpenAI", "Open AI；open-ai、OPEN AI"])
            sheet.append(["登录", "登陆"])
            workbook.save(path)
            workbook.close()

            rules = load_common_terms(path)

        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0].standard, "OpenAI")
        self.assertEqual(rules[0].discouraged, ("Open AI", "open-ai", "OPEN AI"))
        self.assertEqual(rules[1].discouraged, ("登陆",))

    def test_reports_discouraged_terms_and_incorrect_case(self):
        rules = [
            CommonTermRule("OpenAI", ("Open AI", "open-ai")),
            CommonTermRule("登录", ("登陆",)),
        ]
        document_text = (
            "file: doc.txt\n\n[第3页]\n"
            "OpenAI 是正确写法，openai 和 OPENAI 大小写错误，Open AI 不推荐。\n"
            "openaiService 是更长的标识符，不应按子串命中。请先登陆。"
        )

        report = build_common_terms_report(
            document_text,
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertIn("发现 4 类常用词写法问题", report["summary"])
        self.assertEqual(len(report["items"]), 4)
        descriptions = "\n".join(item["description"] for item in report["items"])
        self.assertIn("“openai”与常用词表规定的大小写“OpenAI”不一致", descriptions)
        self.assertIn("“OPENAI”与常用词表规定的大小写“OpenAI”不一致", descriptions)
        self.assertIn("不推荐用法“Open AI”", descriptions)
        self.assertIn("不推荐用法“登陆”", descriptions)
        self.assertNotIn("openaiService", descriptions)
        self.assertTrue(all("文件：doc.txt" in item["location"] for item in report["items"]))
        self.assertTrue(all("页码：第3页" in item["location"] for item in report["items"]))

    def test_accepts_exact_case_and_does_not_match_inside_longer_identifier(self):
        rules = [CommonTermRule("OpenAI", ("Open AI",))]
        document_text = "OpenAI OpenAIService preOpenAI openaiService"

        report = build_common_terms_report(
            document_text,
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(report["items"], [])
        self.assertIn("未发现错误、不推荐用法或大小写不一致问题", report["summary"])

    def test_exact_standard_is_not_rejected_when_repeated_in_discouraged_cell(self):
        rules = [CommonTermRule("OpenAI", ("OpenAI", "Open AI"))]

        report = build_common_terms_report(
            "OpenAI",
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(report["items"], [])


if __name__ == "__main__":
    unittest.main()
