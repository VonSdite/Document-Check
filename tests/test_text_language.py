import unittest

from app.checks.text_language import (
    TEXT_LANGUAGE_CHINESE,
    TEXT_LANGUAGE_LATIN,
    TEXT_LANGUAGE_MIXED,
    TEXT_LANGUAGE_OTHER,
    TEXT_LANGUAGE_UNCERTAIN,
    TEXT_LANGUAGE_UNKNOWN,
    estimate_text_language,
    text_language_label,
)


class TextLanguageTest(unittest.TestCase):
    def test_estimates_supported_document_language_groups(self):
        chinese = "这是中文客户资料，用于说明产品安装、配置、操作、验证和维护方法。" * 3
        english = (
            "This customer guide explains installation, configuration, operation, verification, "
            "maintenance, restrictions, and troubleshooting procedures."
        )
        mixed = "这是中文说明文字，用于介绍安装配置操作和结果验证。" * 5 + english * 2
        japanese = (
            "この文書では、製品のインストール、設定、操作、確認、および保守手順について説明します。"
            * 3
        )
        korean = (
            "이 문서는 제품 설치 구성 작동 확인 유지 관리 및 문제 해결 절차를 설명합니다."
            * 3
        )

        self.assertEqual(estimate_text_language(chinese), TEXT_LANGUAGE_CHINESE)
        self.assertEqual(estimate_text_language(english), TEXT_LANGUAGE_LATIN)
        self.assertEqual(estimate_text_language(mixed), TEXT_LANGUAGE_MIXED)
        self.assertEqual(estimate_text_language(japanese), TEXT_LANGUAGE_OTHER)
        self.assertEqual(estimate_text_language(korean), TEXT_LANGUAGE_OTHER)
        self.assertEqual(estimate_text_language("打开 APP。"), TEXT_LANGUAGE_UNCERTAIN)

    def test_ignores_alphanumeric_model_identifiers_in_chinese_document(self):
        chinese = "这是中文产品资料，用于说明设备安装、配置、操作、验证和维护方法。" * 3
        model_identifiers = (
            "SUN2000-50KTL-M3 S5735-L48T4X-A1 NetCol5000-A UPS5000-E V100R001C00 " * 4
        )

        self.assertEqual(
            estimate_text_language(chinese + model_identifiers),
            TEXT_LANGUAGE_CHINESE,
        )

    def test_keeps_counting_english_prose_around_model_identifiers(self):
        english = (
            "This customer guide explains installation, configuration, operation, verification, "
            "maintenance, restrictions, and troubleshooting procedures for SUN2000-50KTL-M3."
        )

        self.assertEqual(estimate_text_language(english), TEXT_LANGUAGE_LATIN)

    def test_ignores_abbreviations_urls_and_code_in_chinese_document(self):
        chinese = "这是中文产品资料，用于说明设备安装、配置、操作、验证和维护方法。" * 3
        technical_content = (
            " TCP HTTP HTTPS API SDK CPU GPU VLAN WLAN SSH SNMP " * 16
            + " https://support.example.com/documentation/configuration/troubleshooting "
            * 14
            + " `curl --config /opt/guide/settings.yaml` " * 10
        )

        self.assertEqual(
            estimate_text_language(chinese + technical_content), TEXT_LANGUAGE_CHINESE
        )

    def test_estimates_pdf_body_without_generated_chinese_annotations(self):
        english = (
            "This document describes cable connections, power-on and commissioning procedures. "
            "Connect the computer to the network port and verify the operating status."
        )
        generated_content = (
            "[PDF结构化表格 page001-table001 开始；1行×1列；置信度=high；视图=归一化]\n"
            '<table data-view="normalized"><tr><td data-cell="A1">'
            + english
            + "</td></tr></table>\n[PDF结构化表格 page001-table001 结束]\n"
        ) * 10

        self.assertEqual(estimate_text_language(generated_content), TEXT_LANGUAGE_LATIN)
        self.assertEqual(
            text_language_label(TEXT_LANGUAGE_LATIN), "拉丁字母为主（含英文）"
        )

    def test_inherited_table_values_do_not_outweigh_chinese_body(self):
        chinese = "这是中文产品资料，用于说明设备安装、配置、操作、验证和维护方法。" * 3
        table = '<table><tr><td data-cell="A1">Settings</td>'
        table += '<td data-inherited-from="A1">Settings</td>' * 100
        table += "</tr></table>"

        self.assertEqual(estimate_text_language(chinese + table), TEXT_LANGUAGE_CHINESE)

    def test_keeps_uppercase_english_prose(self):
        english = "THIS CUSTOMER GUIDE EXPLAINS THE INSTALLATION AND CONFIGURATION PROCEDURES."
        chinese = "这是中文说明文字，用于介绍安装配置操作和结果验证。" * 3

        self.assertEqual(estimate_text_language(english), TEXT_LANGUAGE_LATIN)
        self.assertEqual(
            estimate_text_language(chinese + "\n" + english * 3), TEXT_LANGUAGE_MIXED
        )

    def test_system_markers_and_link_targets_provide_no_language_evidence(self):
        self.assertEqual(
            estimate_text_language(
                "file: EnglishCustomerGuide.pdf\n[第1页]\n"
                "[超链接] https://support.example.com/documentation"
            ),
            TEXT_LANGUAGE_UNKNOWN,
        )


if __name__ == "__main__":
    unittest.main()
