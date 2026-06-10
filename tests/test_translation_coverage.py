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

    def test_ignores_repeated_pdf_header_footer_metadata(self):
        report = run_translation_coverage_check(
            """# 素材文档
## 素材文档1：cn.pdf
[第1页]
iSitePower-A 站点叠光（华为电源，PVPU-86N2） 安装指南
文档版本 02 (2026-05-20)
版权所有 © 华为技术有限公司
18
最大输入电流 10A。

[第2页]
iSitePower-A 站点叠光（华为电源，PVPU-86N2） 安装指南
文档版本 02 (2026-05-20)
版权所有 © 华为技术有限公司
19
支持断电记忆功能。

# 资料
## 资料1：en.pdf
[第1页]
iSitePower-A Site PV Solution Installation Guide
Document version 02 (2026-05-20)
Copyright Huawei Technologies Co., Ltd.
18
Maximum input current 10 A.

[第2页]
iSitePower-A Site PV Solution Installation Guide
Document version 02 (2026-05-20)
Copyright Huawei Technologies Co., Ltd.
19
Power-off memory is supported.
"""
        )

        self.assertIn("未发现素材文档与资料", report)
        self.assertNotIn("2026-05-20", report)
        self.assertNotIn("PVPU-86N2", report)
        self.assertNotIn("版权所有", report)

    def test_embedded_footer_metadata_does_not_create_identifier_noise(self):
        report = run_translation_coverage_check(
            """# 素材文档
## 素材文档1：cn.pdf
最大输入电流 10A。 文档版本 02 (2026-05-20) 版权所有 © 华为技术有限公司 18

# 资料
## 资料1：en.pdf
Maximum input current 10 A.
"""
        )

        self.assertIn("未发现素材文档与资料", report)
        self.assertNotIn("2026-05-20", report)
        self.assertNotIn("版权所有", report)


if __name__ == "__main__":
    unittest.main()
