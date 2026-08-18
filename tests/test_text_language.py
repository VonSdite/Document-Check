import unittest

from app.text_language import (
    TEXT_LANGUAGE_CHINESE,
    TEXT_LANGUAGE_LATIN,
    TEXT_LANGUAGE_MIXED,
    TEXT_LANGUAGE_OTHER,
    TEXT_LANGUAGE_UNCERTAIN,
    estimate_text_language,
)


class TextLanguageTest(unittest.TestCase):
    def test_estimates_supported_document_language_groups(self):
        chinese = "这是中文客户资料，用于说明产品安装、配置、操作、验证和维护方法。" * 3
        english = (
            "This customer guide explains installation, configuration, operation, verification, "
            "maintenance, restrictions, and troubleshooting procedures."
        )
        mixed = "这是中文说明文字，用于介绍安装配置操作和结果验证。" * 5 + english * 2
        japanese = "この文書では、製品のインストール、設定、操作、確認、および保守手順について説明します。" * 3
        korean = "이 문서는 제품 설치 구성 작동 확인 유지 관리 및 문제 해결 절차를 설명합니다." * 3

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


if __name__ == "__main__":
    unittest.main()
