import unittest

from app.translation_coverage import run_translation_coverage_check


class TranslationCoverageTest(unittest.TestCase):
    def test_reports_missing_structural_entry(self):
        report = run_translation_coverage_check(
            """# 素材文档
## 素材文档1：cn.txt
### 1 Overview
1. 支持断电记忆功能。
2. 最大输入电流 10A。

# 资料
## 资料1：en.txt
### 1 Overview
1. Memory retention is supported.
"""
        )

        self.assertIn("疑似漏翻译或条目缺失", report)
        self.assertIn("最大输入电流 10A", report)

    def test_accepts_matching_number_units_with_space(self):
        report = run_translation_coverage_check(
            """# 素材文档
## 素材文档1：cn.txt
### 1 Overview
1. 最大输入电流 10A。

# 资料
## 资料1：en.txt
### 1 Overview
1. Maximum input current 10 A.
"""
        )

        self.assertIn("未发现素材文档与资料", report)
        self.assertNotIn("数字、单位、日期或型号疑似不一致", report)


if __name__ == "__main__":
    unittest.main()
