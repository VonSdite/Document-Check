import unittest
from itertools import permutations

from app.reporting.rounds import build_round_result
from tests.test_document_rounds import completed, finding


class RoundMatchingRegressionTest(unittest.TestCase):
    def aggregate(self, *sources):
        return build_round_result(
            "check",
            len(sources),
            [completed(n, item) for n, item in enumerate(sources, 1)],
        )["structured_report"]["items"]

    def assert_merged(self, *sources):
        items = self.aggregate(*sources)
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["source_items"],
            [{"round": n, "item": 1} for n in range(1, len(sources) + 1)],
        )
        self.assertFalse(items[0]["round_conflict"])
        self.assertEqual(items[0]["status"], "issue")

    def test_same_typo_with_different_chapter_and_table_references(self):
        sources = [
            finding(
                description,
                location="3.2节 表3-4；4.1节 表4-2",
                excerpt=excerpt,
                suggestion=suggestion,
            )
            for description, excerpt, suggestion in (
                (
                    "表3-4和表4-2标题将“阈值”误写为“阀值”，正文用字正确。",
                    "设置告警阀值",
                    "将表3-4、表4-2标题中的“阀值”更正为“阈值”。",
                ),
                (
                    "两处表题将“阈值”误写为“阀值”，而3.2、4.1节标题使用“阈值”。",
                    "表3-4 设置告警阀值",
                    "将两处标题中的“阀值”改为“阈值”。",
                ),
                (
                    "“阀值”属于错别字。",
                    "设置告警阀值",
                    "将“阀值”改为“阈值”。",
                ),
            )
        ]
        for order in permutations(sources):
            self.assert_merged(*order)

    def test_same_specific_fix_with_different_explanations(self):
        self.assert_merged(
            finding(
                "API是接口的标准缩写，文档中的“Api”大小写不规范。",
                excerpt="导出Api文件，并导入接口配置。",
                suggestion="将“Api文件”统一改为“API文件”。",
            ),
            finding(
                "Application Programming Interface的缩写应全大写。",
                excerpt="导出Api文件，并导入接口配置。",
                suggestion="将“Api文件”统一改为“API文件”。",
            ),
        )

    def test_shared_reference_context_handles_different_location_detail(self):
        sources = [
            finding(
                "界面名称写法不统一。",
                location="第3章 > 3.1 登录Web UI",
                excerpt="2.4.1 使用WebUI管理；3.1 登录Web UI",
            ),
            finding(
                "界面名称写法不统一。",
                location="第2章 > 2.4.1节；第3章 > 3.1节",
                excerpt="3.1 登录Web UI",
            ),
        ]
        self.assert_merged(*sources)
        self.assert_merged(*reversed(sources))

    def test_same_insertion_in_short_instruction_and_rewritten_sentence(self):
        excerpt = "将信号线的一端，安装控制模块MOD02C的接口。"
        sources = [
            finding(description, excerpt=excerpt, suggestion=suggestion)
            for description, suggestion in (
                (
                    "步骤1中“安装控制模块”缺少介词“到”；3.1.2同类表述完整。",
                    "在“安装”后补充“到”，即“安装到控制模块”。",
                ),
                (
                    "“安装”后缺少介词“到”，句子结构不完整；同章3.1.2步骤2表述完整。",
                    "在“安装”后补充“到”，改为“安装到……接口”。",
                ),
                (
                    "该句缺少“到/上”，应将“一端”安装到接口上。",
                    "改为“将信号线的一端，安装到控制模块MOD02C的接口上。”",
                ),
                (
                    "“安装……其中一个接口”缺少介词“到”，动宾搭配不完整。",
                    "在“安装”后补充“到”。",
                ),
                (
                    "“安装控制模块MOD02C的接口”需要补充介词“到”。",
                    "在“安装”后补充“到”。",
                ),
            )
        ]
        self.assert_merged(*sources)
        self.assert_merged(*reversed(sources))

    def test_same_symbol_fix_can_use_different_numeric_examples(self):
        sources = [
            finding(
                description,
                excerpt="额定容量=2x单组容量",
                suggestion="将“2x”改为“2×”。",
            )
            for description in (
                "公式以字母“x”代替乘号“×”，表格使用“×”（如120×2、360×2）。",
                "用字母“x”表示乘号，与表格中“120×2”“360×3”等“×”写法不一致。",
                "计算式使用字母“x”，同类计算（如“360×2”“800×2”）使用乘号“×”。",
            )
        ]
        self.assert_merged(*sources)

    def test_equivalent_unit_fixes_do_not_conflict_with_original_values_or_references(
        self,
    ):
        sources = [
            finding(
                description,
                location="第6章 > 6.4节 步骤6",
                excerpt=excerpt,
                suggestion=suggestion,
            )
            for description, excerpt, suggestion in (
                (
                    "“输出–24母排”缺少电压单位“V”，应为“输出–24V母排”；全文写作“–24V母排”。",
                    "步骤6 测量输出–24母排电压",
                    "将“输出–24母排”改为“输出–24V母排”。",
                ),
                (
                    "“输出−24母排”缺少电压单位“V”，应为“输出−24V母排”；6.1节写为“−24V母排”。",
                    "测量输出−24母排电压，应为21V~29V。",
                    "将“输出−24母排”改为“输出−24V母排”。",
                ),
                (
                    "“输出–24母排”漏写电压单位“V”。",
                    "测量输出–24母排电压",
                    "改为“测量输出–24V母排电压”，与6.1节步骤7保持一致。",
                ),
            )
        ]
        for order in permutations(sources):
            self.assert_merged(*order)

    def test_reference_variations_merge_but_judgment_conflict_is_retained(self):
        excerpt = "并柜时只安装一根保护地线。上电前确认两根保护地线已接好。"
        sources = [
            finding(
                description,
                location="3.2 安装保护地线；6.1 交流上电 步骤1",
                excerpt=excerpt,
                suggestion="核对并统一保护地线数量。",
                status=status,
            )
            for description, status in (
                (
                    "3.2规定并柜时安装1根保护地线，6.1交流上电前要求确认2根，数量不一致。",
                    "issue",
                ),
                ("并柜时安装一根保护地线，上电前要求确认两根，数量不一致。", "issue"),
                (
                    "3.2规定并柜时安装1根保护地线，6.1步骤1要求确认2根，数量不一致，需核实适用条件。",
                    "suggestion",
                ),
            )
        ]
        items = self.aggregate(*sources)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source_rounds"], [1, 2, 3])
        self.assertTrue(items[0]["round_conflict"])
        self.assertEqual(items[0]["status"], "suggestion")
        self.assertEqual(items[0]["confidence"], "low")

    def test_same_correction_does_not_hide_different_parameter_claims(self):
        for left, right in (
            ("3.2V", "3.3V"),
            ("3.2", "3.3"),
            ("3.2的设定值", "3.3的设定值"),
            ("−5V", "5V"),
            ("5mV", "5MV"),
            ("一根", "两根"),
            ("MOD02C", "MOD03C"),
        ):
            with self.subTest(left=left, right=right):
                items = self.aggregate(
                    *[
                        finding(
                            f"参数应为{value}；“阀值”存在错字。",
                            location="第3.2节、第3.3节",
                            excerpt="设置告警阀值",
                            suggestion="将“阀值”改为“阈值”。",
                        )
                        for value in (left, right)
                    ]
                )
                self.assertEqual(len(items), 2)

    def test_generic_fix_or_unrelated_correction_does_not_merge_different_issues(self):
        excerpt = "将线缆安装模块的的接口。"
        for suggestions in (
            ("核对并修订本句。", "核对并修订本句。"),
            ("在“安装”后补充“到”。", "将“的的”改为“的”。"),
            ("将“阀值”改为“阈值”。", "将“阀值”改为“阈值”。"),
        ):
            with self.subTest(suggestions=suggestions):
                items = self.aggregate(
                    finding(
                        "“安装模块”缺少介词“到”。",
                        excerpt=excerpt,
                        suggestion=suggestions[0],
                    ),
                    finding(
                        "“的的”中出现重复字。",
                        excerpt=excerpt,
                        suggestion=suggestions[1],
                    ),
                )
                self.assertEqual(len(items), 2)

    def test_opposite_numeric_fixes_and_unit_case_require_confirmation(self):
        for left, right in (
            ("将5V改为12V。", "将12V改为5V。"),
            ("将“5V”改为“12V”。", "将“12V”改为“5V”。"),
            ("将阈值改为>5V。", "将阈值改为<5V。"),
            ("将单位改为mV。", "将单位改为MV。"),
            ("将电压改为5V，将电流改为2A。", "将电压改为12V，将电流改为2A。"),
            ("将电压改为“5V”，将电流改为2A。", "将电压改为“5V”，将电流改为3A。"),
        ):
            with self.subTest(left=left, right=right):
                items = self.aggregate(
                    finding(suggestion=left), finding(suggestion=right)
                )
                self.assertEqual(len(items), 1)
                self.assertTrue(items[0]["round_conflict"])
                self.assertEqual(items[0]["status"], "suggestion")
                self.assertEqual(items[0]["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
