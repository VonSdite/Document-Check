import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import app.checks.common_terms as common_terms
import app.checks.sensitive_terms as sensitive_terms
from app.checks.common_terms import (
    CommonTermRule,
    build_common_terms_report,
    load_common_terms,
)
from app.checks.sensitive_terms import load_sensitive_terms
from app.checks.term_cache import clear_term_file_cache


class CommonTermsTest(unittest.TestCase):
    def setUp(self):
        clear_term_file_cache()

    def tearDown(self):
        clear_term_file_cache()

    def test_term_files_are_cached_until_file_signature_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            common_path = Path(temp_dir) / "common_terms.csv"
            common_path.write_text(
                "常用词,常见错误/不推荐用法\n登录,登陆\n",
                encoding="utf-8",
            )
            sensitive_path = Path(temp_dir) / "sensitive_terms.csv"
            sensitive_path.write_text(
                "不规范用语,规范用语\n黑名单,阻止名单\n",
                encoding="utf-8",
            )

            with (
                patch.object(
                    common_terms,
                    "_load_csv_terms",
                    wraps=common_terms._load_csv_terms,
                ) as common_loader,
                patch.object(
                    sensitive_terms,
                    "_load_csv_terms",
                    wraps=sensitive_terms._load_csv_terms,
                ) as sensitive_loader,
            ):
                first_common = load_common_terms(common_path)
                first_common.clear()
                second_common = load_common_terms(common_path)
                load_sensitive_terms(sensitive_path)
                load_sensitive_terms(sensitive_path)

                self.assertEqual(common_loader.call_count, 1)
                self.assertEqual(sensitive_loader.call_count, 1)
                self.assertEqual(len(second_common), 1)

                common_path.write_text(
                    "常用词,常见错误/不推荐用法\n登录,登陆\n账户,帐号\n",
                    encoding="utf-8",
                )
                changed_common = load_common_terms(common_path)

                self.assertEqual(common_loader.call_count, 2)
                self.assertEqual(len(changed_common), 2)

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
        self.assertEqual(rules[0].language_scope, "all")
        self.assertEqual(rules[1].discouraged, ("登陆",))

    def test_loads_optional_language_scope_and_keeps_scopes_separate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "common_terms.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            assert sheet is not None
            sheet.append(["常用词", "常见错误/不推荐用法", "适用语种"])
            sheet.append(["App", "APP；app", "中文"])
            sheet.append(["App", "APP EN", "全部"])
            workbook.save(path)
            workbook.close()

            rules = load_common_terms(path)

        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0].language_scope, "zh")
        self.assertEqual(rules[1].language_scope, "all")

    def test_rejects_unsupported_language_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "common_terms.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            assert sheet is not None
            sheet.append(["常用词", "常见错误/不推荐用法", "适用语种"])
            sheet.append(["App", "APP；app", "英文"])
            workbook.save(path)
            workbook.close()

            with self.assertRaisesRegex(ValueError, "目前支持留空/全部/all，或中文/zh"):
                load_common_terms(path)

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
        self.assertTrue(
            all("文件：doc.txt" in item["location"] for item in report["items"])
        )
        self.assertTrue(
            all("页码：第3页" in item["location"] for item in report["items"])
        )

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

    def test_chinese_only_rule_checks_discouraged_terms_and_case_in_chinese_document(
        self,
    ):
        rules = [CommonTermRule("App", ("APP", "app"), language_scope="zh")]
        document_text = (
            "这是面向客户发布的中文操作指南，介绍移动应用的安装、登录、配置、使用和维护方法。"
            "用户应按照以下步骤完成操作，并在操作完成后检查系统状态和业务结果。"
            "请打开 APP，然后在 app 中选择设置。"
        )

        report = build_common_terms_report(
            document_text,
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(len(report["items"]), 2)
        descriptions = "\n".join(item["description"] for item in report["items"])
        self.assertIn("不推荐用法“APP”", descriptions)
        self.assertIn("不推荐用法“app”", descriptions)
        self.assertIn("文档语种估计：中文为主", report["summary"])
        self.assertNotIn("已跳过", report["summary"])

    def test_model_identifiers_do_not_skip_chinese_only_rules(self):
        rules = [CommonTermRule("App", ("APP",), language_scope="zh")]
        document_text = (
            "这是中文产品资料，用于说明设备安装、配置、操作、验证和维护方法。" * 3
            + "SUN2000-50KTL-M3 S5735-L48T4X-A1 NetCol5000-A UPS5000-E V100R001C00 " * 4
            + "请打开 APP 并检查设备状态。"
        )

        report = build_common_terms_report(
            document_text,
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(len(report["items"]), 1)
        self.assertIn("不推荐用法“APP”", report["items"][0]["description"])
        self.assertIn("文档语种估计：中文为主", report["summary"])
        self.assertNotIn("已跳过", report["summary"])

    def test_chinese_only_rule_is_skipped_for_english_document(self):
        rules = [CommonTermRule("App", ("APP", "app"), language_scope="zh")]
        document_text = (
            "This English customer guide explains how to install, configure, operate, and maintain "
            "the mobile APP. Open the app, select Settings, save the changes, and verify the result."
        )

        report = build_common_terms_report(
            document_text,
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(report["items"], [])
        self.assertIn("文档语种估计：拉丁字母为主（含英文）", report["summary"])
        self.assertIn("已跳过 1 条不适用于当前文档语种", report["summary"])

    def test_chinese_only_scope_also_controls_automatic_case_check(self):
        rules = [CommonTermRule("App", language_scope="zh")]
        chinese_text = (
            "这是中文客户操作指南，用于说明移动应用的安装、配置、使用、验证和维护方法。"
            "用户完成准备工作后，请打开 APP 并检查运行状态。"
        )
        english_text = (
            "This English customer guide explains installation, configuration, operation, verification, "
            "and maintenance. Open the APP and check the operating status."
        )

        chinese_report = build_common_terms_report(
            chinese_text,
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )
        english_report = build_common_terms_report(
            english_text,
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(len(chinese_report["items"]), 1)
        self.assertIn("大小写“App”不一致", chinese_report["items"][0]["description"])
        self.assertEqual(english_report["items"], [])
        self.assertIn("已跳过 1 条不适用于当前文档语种", english_report["summary"])

    def test_chinese_only_rule_is_skipped_for_mixed_or_short_document(self):
        rules = [CommonTermRule("App", ("APP", "app"), language_scope="zh")]
        mixed_text = (
            "这是中文说明文字，用于介绍安装配置操作、使用限制、结果验证和维护注意事项。"
            * 2
            + " This English section explains installation configuration operation verification maintenance "
            * 3
            + "APP app"
        )

        mixed_report = build_common_terms_report(
            mixed_text,
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )
        short_report = build_common_terms_report(
            "打开 APP 或 app。",
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(mixed_report["items"], [])
        self.assertIn("文档语种估计：中英混合", mixed_report["summary"])
        self.assertEqual(short_report["items"], [])
        self.assertIn("语种特征较少", short_report["summary"])

    def test_chinese_only_rule_is_skipped_for_japanese_document(self):
        rules = [CommonTermRule("App", ("APP", "app"), language_scope="zh")]
        document_text = (
            "この文書では、製品のインストール、設定、操作、確認、および保守手順について説明します。"
            "APP を開き、app の設定画面で必要な項目を選択して、処理結果を確認してください。"
        )

        report = build_common_terms_report(
            document_text,
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(report["items"], [])
        self.assertIn("文档语种估计：其他语种为主", report["summary"])
        self.assertIn("已跳过 1 条不适用于当前文档语种", report["summary"])

    def test_all_language_rule_still_checks_english_document(self):
        rules = [CommonTermRule("OpenAI", ("Open AI",), language_scope="all")]
        document_text = (
            "This English document describes the Open AI service configuration, request parameters, "
            "response fields, error handling, and operational verification for customer deployments."
        )

        report = build_common_terms_report(
            document_text,
            rules,
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(len(report["items"]), 1)
        self.assertIn("不推荐用法“Open AI”", report["items"][0]["description"])

    def test_smartlogger_url_paths_do_not_trigger_case_issues(self):
        document_text = (
            "file: User Manual.pdf\n[第59页]\n"
            "This section describes how to log in to the SmartLogger and perform initial operations.\n"
            "https://support.example.com/enterprise/en/smartlogger-pid-21294677/software\n"
            "[超链接] https://support.example.com/enterprise/en/smartlogger-\n"
            "pid-21294677/software（超链接：https://support.example.com/en/smartlogger/software）\n"
            "[第60页]\nDownload the required software and verify the device configuration."
        )

        report = build_common_terms_report(
            document_text,
            [CommonTermRule("SmartLogger")],
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(report["items"], [])
        self.assertIn("文档语种估计：拉丁字母为主（含英文）", report["summary"])
        self.assertNotIn("已跳过", report["summary"])

    def test_keeps_visible_link_errors_and_original_locations(self):
        document_text = (
            "file: openai.pdf\n[第3页]\n"
            "[openai](https://example.test/openai)\n"
            "openai（超链接：#openai）\n"
            "[超链接] openai（超链接：https://example.test/openai）\n"
            "[第4页]\nOpenAI is the standard spelling."
        )

        report = build_common_terms_report(
            document_text,
            [CommonTermRule("OpenAI")],
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(len(report["items"]), 1)
        item = report["items"][0]
        self.assertIn("共 2 处", item["description"])
        self.assertIn("文件：openai.pdf，页码：第3页，行：3", item["location"])
        self.assertIn("页码：第3页，行：4", item["location"])
        self.assertIn("[openai](https://example.test/openai)", item["excerpt"])

    def test_checks_table_anchors_and_decoded_entities_once(self):
        document_text = (
            "file: table.pdf\n[第5页]\n"
            "[PDF结构化表格 page005-table001 开始；1行×2列；置信度=high]\n"
            '<table id="page005-table001"><tr>\n'
            '<td data-cell="A1" data-original-range="A1:B1">at&amp;t</td>\n'
            '<td data-cell="B1" data-inherited-from="A1">at&amp;t</td>\n'
            "</tr></table>\n[PDF结构化表格 page005-table001 结束]\n"
            "[第6页]\nopen<b>ai</b> is mentioned in the next section."
        )

        report = build_common_terms_report(
            document_text,
            [CommonTermRule("AT&T"), CommonTermRule("OpenAI")],
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(len(report["items"]), 2)
        first, second = report["items"]
        self.assertIn("“at&t”", first["description"])
        self.assertIn("共 1 处", first["description"])
        self.assertIn("页码：第5页，行：5", first["location"])
        self.assertIn("at&amp;t", first["excerpt"])
        self.assertIn("“openai”", second["description"])
        self.assertIn("页码：第6页，行：10", second["location"])

    def test_excludes_discouraged_terms_in_network_targets_and_code(self):
        document_text = (
            "[第7页]\n"
            "https://example.test/Open-AI ftp://example.test/Open-AI www.example.test/Open-AI\n"
            "Open-AI@example.test D:\\docs\\Open-AI\\guide /opt/Open-AI/config\n"
            "`Open-AI --help`\n```text\nOpen-AI\n```\n"
            "The Open-AI service is available."
        )

        report = build_common_terms_report(
            document_text,
            [CommonTermRule("OpenAI", ("Open-AI",))],
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(len(report["items"]), 1)
        self.assertIn("共 1 处", report["items"][0]["description"])
        self.assertIn(
            "The Open-AI service is available.", report["items"][0]["excerpt"]
        )

    def test_abbreviations_do_not_skip_chinese_rules(self):
        document_text = (
            "这是中文产品资料，用于说明设备安装、配置、操作、验证和维护方法。" * 3
            + " TCP HTTP HTTPS API SDK CPU GPU VLAN WLAN SSH SNMP " * 16
            + " https://support.example.com/documentation/configuration/troubleshooting "
            * 14
            + "请打开 APP，并完成设备配置。"
        )

        report = build_common_terms_report(
            document_text,
            [CommonTermRule("App", ("APP",), language_scope="zh")],
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(len(report["items"]), 1)
        self.assertIn("文档语种估计：中文为主", report["summary"])
        self.assertNotIn("已跳过", report["summary"])

    def test_explicit_alphanumeric_term_rule_checks_visible_product_name(self):
        report = build_common_terms_report(
            "This product supports wifi6 connections.",
            [CommonTermRule("WiFi6")],
            source_path=Path("common_terms.xlsx"),
            issue_limit=20,
        )

        self.assertEqual(len(report["items"]), 1)
        self.assertIn("“wifi6”", report["items"][0]["description"])


if __name__ == "__main__":
    unittest.main()
