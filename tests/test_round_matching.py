import unittest
from itertools import permutations
from unittest.mock import patch

from app.reporting.rounds import (
    _correction_edits,
    _same_spacing_consistency,
    build_round_result,
)
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

    def test_missing_preposition_matches_ellipsis_rewrite_by_anchor(self):
        excerpt = "将信号线一端安装控制器接口。"
        description = "“安装”后缺少介词“到”，谓语不完整。"
        sources = [
            finding(
                description,
                excerpt=excerpt,
                suggestion=("在“安装”后增加介词“到”，改为“将信号线一端安装到……接口”。"),
            ),
            finding(
                description,
                excerpt=excerpt,
                suggestion="改为“将信号线一端安装到……接口”。",
            ),
        ]
        items = self.aggregate(*sources)
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["source_items"], [{"round": 1, "item": 1}, {"round": 2, "item": 1}]
        )
        self.assertTrue(
            all(
                "……" not in part
                for edit in _correction_edits(sources[1])
                for part in edit
            )
        )

    def test_insertions_at_different_anchors_remain_separate(self):
        excerpt = "将线缆安装控制器接口。"
        items = self.aggregate(
            finding(
                "“安装”后缺少介词“到”。",
                excerpt=excerpt,
                suggestion="在“安装”后增加介词“到”。",
            ),
            finding(
                "“线缆”后缺少介词“到”。",
                excerpt=excerpt,
                suggestion="在“线缆”后增加介词“到”。",
            ),
        )
        self.assertEqual(len(items), 2)

    def test_later_bridge_reconciles_compatible_groups_across_orderings(self):
        sources = [
            finding(
                label,
                status=status,
                severity=severity,
                excerpt="同一处足够明确的原文证据。",
                suggestion=f"采用修订方案{label}。",
                evidence_refs=[{"id": label}],
            )
            for label, status, severity in (
                ("A", "issue", "medium"),
                ("B", "issue", "low"),
                ("C", "suggestion", "high"),
            )
        ]
        connected = {frozenset(("A", "C")), frozenset(("B", "C"))}

        def same_finding(left, right):
            pair = frozenset((left["description"], right["description"]))
            return left["description"] == right["description"] or pair in connected

        for order in permutations(sources):
            round_of_a = order.index(sources[0]) + 1
            previous = [
                {"id": "saved-A", "source_items": [{"round": round_of_a, "item": 1}]}
            ]
            with (
                patch("app.reporting.rounds._same_finding", side_effect=same_finding),
                patch("app.reporting.rounds._compatible_claims", return_value=True),
            ):
                items = build_round_result(
                    "check",
                    len(order),
                    [completed(number, item) for number, item in enumerate(order, 1)],
                    previous_items=previous,
                )["structured_report"]["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["id"], "saved-A")
            self.assertEqual(
                items[0]["source_items"], [{"round": n, "item": 1} for n in range(1, 4)]
            )
            self.assertEqual(
                {ref["id"] for ref in items[0]["evidence_refs"]}, {"A", "B", "C"}
            )
            self.assertEqual(items[0]["severity"], "high")
            self.assertTrue(items[0]["round_conflict"])
            self.assertEqual(items[0]["status"], "suggestion")
            self.assertEqual(items[0]["confidence"], "low")

    def test_later_bridge_does_not_merge_conflicting_claim_groups(self):
        sources = [
            finding(label, excerpt="同一处足够明确的原文证据。")
            for label in ("A", "B", "C")
        ]
        connected = {frozenset(("A", "C")), frozenset(("B", "C"))}

        def same_finding(left, right):
            pair = frozenset((left["description"], right["description"]))
            return left["description"] == right["description"] or pair in connected

        def compatible_claims(left, right):
            return frozenset((left["description"], right["description"])) != frozenset(
                ("A", "B")
            )

        for order in permutations(sources):
            with (
                patch("app.reporting.rounds._same_finding", side_effect=same_finding),
                patch(
                    "app.reporting.rounds._compatible_claims",
                    side_effect=compatible_claims,
                ),
            ):
                items = self.aggregate(*order)
            self.assertEqual(len(items), 2)
            memberships = [
                {source["round"] for source in item["source_items"]} for item in items
            ]
            self.assertEqual(sum(map(len, memberships)), 3)
            self.assertFalse(any(len(group) == 3 for group in memberships))

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

    def test_webui_spacing_variants_merge_with_status_disagreement(self):
        sources = [
            finding(
                description,
                status=status,
                category="术语或命名书写不规范",
                location=location,
                excerpt=excerpt,
                suggestion=suggestion,
            )
            for description, status, location, excerpt, suggestion in (
                (
                    "文档中“WebUI”与“Web UI”两种写法并存，需核对后统一写法。",
                    "suggestion",
                    "5.1 使用WebUI；8.3 登录Web UI",
                    "当使用Web UI管理时需参照本章节设置",
                    "核对术语规范后统一为“WebUI”或“Web UI”。",
                ),
                (
                    "同一对象在文中同时存在“WebUI”和“Web UI”两种写法。",
                    "issue",
                    "第5章 > 5.1；第8章 > 8.3",
                    "使用WebUI管理；登录Web UI",
                    "统一为“Web UI”或按企业规范统一为“WebUI”。",
                ),
                (
                    "管理界面名称在标题和正文混用“WebUI”与“Web UI”，应统一写法。",
                    "issue",
                    "5.1 使用WebUI；8.3 登录Web UI",
                    "使用WebUI管理",
                    "全文统一为“WebUI”或“Web UI”，并与界面实际名称一致。",
                ),
                (
                    "同一界面/功能在文档中同时写作“WebUI”和“Web UI”，标题与正文即不一致。",
                    "issue",
                    "5.1 使用WebUI；8.3 登录Web UI",
                    "使用WebUI管理前的设置；当使用Web UI管理时参照本章节",
                    "统一采用一种写法，如“WebUI”或“Web UI”。",
                ),
            )
        ]
        items = self.aggregate(*sources)
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0]["source_items"], [{"round": n, "item": 1} for n in range(1, 5)]
        )
        self.assertTrue(items[0]["round_conflict"])
        self.assertEqual(items[0]["status"], "suggestion")

    def test_different_spacing_variant_pairs_do_not_match(self):
        items = self.aggregate(
            finding(
                "“WebUI”与“Web UI”在文档中混用，界面术语写法不统一。",
                excerpt="关于管理界面的说明。",
                suggestion="统一为“WebUI”或“Web UI”。",
            ),
            finding(
                "参数区域需要确认“CANBus”与“CAN Bus”的适用范围。",
                excerpt="关于管理界面的说明。",
                suggestion="核实CANBus支持的通信方式。",
            ),
        )
        self.assertEqual(len(items), 2)

    def test_spacing_variant_requires_term_in_original_evidence(self):
        left = finding(
            "界面名称“WebUI”和“Web UI”两种写法不一致。",
            excerpt="电源模块连接状态异常。",
        )
        right = finding(
            "“WebUI”与“Web UI”混用，需核实操作界面名称。",
            excerpt="电源模块连接状态异常。",
        )
        self.assertFalse(_same_spacing_consistency(left, right))

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
