import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import patch

from app.reporting import rounds
from app.reporting.rounds import group_cross_check_report_items
from tests.test_document_rounds import finding


def report_item(item_id, **overrides):
    item = finding(**overrides)
    return {
        **item,
        "id": item_id,
        "index": 1,
        "type": item.get("status", "issue"),
        "type_label": item.get("status", "issue"),
    }


def check_result(code, name, *items):
    return {"code": code, "name": name, "report_items": list(items)}


class CrossCheckGroupingTest(unittest.TestCase):
    def test_shared_webui_spacing_issue_is_grouped_across_checks(self):
        excerpt = "使用WebUI管理，登录Web UI。"
        normative = report_item(
            "normative-1",
            category="术语书写",
            excerpt=excerpt,
            description="文档中“WebUI”和“Web UI”两种写法并存，需统一写法。",
            suggestion="按企业规范统一为“WebUI”或“Web UI”。",
        )
        consistency = report_item(
            "consistency-1",
            category="名称一致性",
            excerpt=excerpt,
            description="同一界面名称在文中同时存在“WebUI”和“Web UI”两种写法，写法不统一。",
            suggestion="核实界面正式名称后统一写法。",
        )
        results = [
            check_result("normative", "规范性检查", normative),
            check_result("consistency", "一致性检查", consistency),
        ]

        groups = group_cross_check_report_items(results)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["item_count"], 2)
        self.assertEqual(
            [member["code"] for member in groups[0]["members"]],
            ["normative", "consistency"],
        )
        self.assertEqual(normative["id"], "normative-1")
        self.assertEqual(consistency["id"], "consistency-1")
        self.assertEqual(normative["cross_check_group_id"], 1)
        self.assertEqual(consistency["cross_check_group_id"], 1)
        self.assertEqual(len(results[0]["report_items"]), 1)
        self.assertEqual(len(results[1]["report_items"]), 1)

    def test_matching_description_without_shared_original_evidence_is_not_grouped(self):
        first = report_item("first", excerpt="", description="同一术语写法不统一。")
        second = report_item("second", excerpt="", description="同一术语写法不统一。")

        groups = group_cross_check_report_items(
            [
                check_result("first-check", "规范性检查", first),
                check_result("second-check", "一致性检查", second),
            ]
        )

        self.assertEqual(groups, [])
        self.assertNotIn("cross_check_group_id", first)
        self.assertNotIn("cross_check_group_id", second)

    def test_shared_evidence_and_same_target_link_differently_worded_findings(self):
        excerpt = "TNV Telecommunication Network Voltage 通讯网络电压；正文使用通信网络电压（TNV）。"
        normative = report_item(
            "normative-tnv",
            excerpt=excerpt,
            description="缩略语表将TNV释义写作“通讯网络电压”，正文使用“通信网络电压”，两处全称不一致。",
            suggestion="全文统一使用“通信网络电压”。",
        )
        consistency = report_item(
            "consistency-tnv",
            excerpt=excerpt,
            description="证据A将TNV中文全称写为“通讯网络电压”，证据B写为“通信网络电压”，同一全称不能同时成立。",
            suggestion="统一为“通信网络电压”。",
        )

        groups = group_cross_check_report_items(
            [
                check_result("normative", "规范性检查", normative),
                check_result("consistency", "一致性检查", consistency),
            ]
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["item_count"], 2)

    def test_same_named_cabinet_conflict_groups_with_shared_sections(self):
        normative = report_item(
            "normative-cabinet",
            category="术语或命名书写不规范",
            location="第3章 3.2 多柜管理原则；第5章 5.1.2；第8章 8.2.5",
            excerpt=(
                "需设置一个整流柜的SMU02C为主机 / 将CAN通信线缆分别安装到组合柜的监控模块SMU02C的CAN口"
            ),
            description=(
                "同一“主机”对象在3.2中写作“整流柜”，在5.1.2、8.2.5中写作“组合柜”，"
                "全文未对“组合柜”进行定义，存在命名不一致。"
            ),
            suggestion="若为同一对象，应将“组合柜”统一改为“整流柜”。",
        )
        consistency = report_item(
            "consistency-cabinet",
            category="名称或术语不一致",
            location="第3章 > 3.2 多柜管理原则；第5章 > 5.1.2；第8章 > 8.2.5 设置主从机参数",
            excerpt=(
                "A：多个整流柜时，需设置一个整流柜的SMU02C为主机；"
                "B：将CAN通信线缆安装到组合柜的监控模块SMU02C的CAN口，需设置一个组合柜为主机。"
            ),
            description=(
                "A将具有SMU02C的主机柜称为“整流柜”，B在相同场景下称为“组合柜”；"
                "文档未对“组合柜”进行定义，两种叫法指向同一对象，导致对象混淆。"
            ),
            suggestion=(
                "全文统一使用“整流柜”或“主整流柜”表述，删除未定义的“组合柜”，"
                "或首次出现时明确其与“整流柜”的对应关系。"
            ),
        )
        consistency_uncertain = report_item(
            "consistency-cabinet-uncertain",
            category="名称或术语不一致",
            location="5.1.1 > 步骤1；5.1.2 > 步骤2",
            excerpt=(
                "A将CAN通信线缆安装到整流柜的监控模块SMU02C；"
                "B将CAN通信线缆安装到组合柜的监控模块SMU02C。"
            ),
            description=(
                "A将SMU02C所在机柜称为“整流柜”，B称为“组合柜”；全文未定义“组合柜”。"
                "待确认“组合柜”是否即指整流柜或整流柜与直流柜的组合。"
            ),
            suggestion=(
                "统一使用已定义的机柜名称；如“组合柜”确为特定对象，"
                "应明确其与“整流柜”的关系。"
            ),
        )

        groups = group_cross_check_report_items(
            [
                check_result("normative", "规范性检查", normative),
                check_result(
                    "consistency",
                    "一致性检查",
                    consistency,
                    consistency_uncertain,
                ),
            ]
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["item_count"], 3)
        self.assertEqual(
            [member["code"] for member in groups[0]["members"]],
            ["normative", "consistency", "consistency"],
        )

    def test_different_cabinet_roles_are_not_grouped_by_shared_terms(self):
        first = report_item(
            "first-role",
            location="5.1.2",
            excerpt="整流柜与组合柜分别接入不同的CAN接口。",
            description="“整流柜”和“组合柜”分别作为不同机柜类型接入CAN网络。",
            suggestion="在操作步骤中区分“整流柜”和“组合柜”的连接方式。",
        )
        second = report_item(
            "second-role",
            location="5.1.2",
            excerpt="整流柜与组合柜需要配置不同的主从机参数。",
            description="“整流柜”和“组合柜”是两个不同的机柜对象，应分别设置参数。",
            suggestion="在参数说明中区分“整流柜”和“组合柜”的设置要求。",
        )

        groups = group_cross_check_report_items(
            [
                check_result("first-check", "检查一", first),
                check_result("second-check", "检查二", second),
            ]
        )

        self.assertEqual(groups, [])

    def test_uncertain_same_object_name_links_with_shared_names_and_sections(self):
        for location, name, expected_count in (
            ("3.2；8.2.5；8.3.4；5.1.2", "整流柜", 1),
            ("3.2；8.2.6；8.3.5；5.1.3", "整流柜", 0),
            ("3.2；8.2.5；8.3.4；5.1.2", "动力柜", 0),
        ):
            with self.subTest(location=location, name=name):
                normative = report_item(
                    "normative-uncertain",
                    status="suggestion",
                    location="第5章 > 5.1.2 > 步骤2；第8章 > 8.2.5 > 须知",
                    excerpt="将CAN通信线缆分别安装到组合柜的监控模块SMU02C的CAN口",
                    description=(
                        "已确认线索：5.1.1中相同操作写作“整流柜的监控模块SMU02C”，"
                        "而5.1.2与8.2.5使用“组合柜”，3.1节说明系统由交流柜、整流柜、直流柜组成"
                        "且未定义“组合柜”；待确认条件：需核对企业术语规范中"
                        "“组合柜”是否与“整流柜”为同一对象的正式名称。"
                    ),
                    suggestion="依据术语规范统一使用“整流柜”或“组合柜”，明确其对应关系。",
                )
                consistency = report_item(
                    "consistency-names",
                    location=location,
                    excerpt=(
                        f"证据A：“多个{name}时，需设置一个{name}的SMU02C为主机”；"
                        "证据B：“需设置一个组合柜为主机，其他机柜为从机”。"
                    ),
                    description=(
                        f"同一对象（装有SMU02C的主机柜）在前文称为“{name}”，"
                        "在后文称为“组合柜”，文档未定义“组合柜”，二者指向同一对象，"
                        "导致名称/术语前后不一致。"
                    ),
                    suggestion="核对名称定义后全文统一使用。",
                )

                groups = group_cross_check_report_items(
                    [
                        check_result("normative", "规范性检查", normative),
                        check_result("consistency", "一致性检查", consistency),
                    ]
                )

                self.assertEqual(len(groups), expected_count)
                self.assertEqual(normative["status"], "suggestion")
                self.assertEqual(consistency["status"], "issue")

    def test_different_check_items_are_required(self):
        excerpt = "额定电压为24 V。"
        first = report_item("first", excerpt=excerpt)
        second = report_item("second", excerpt=excerpt)

        groups = group_cross_check_report_items(
            [check_result("same-check", "规范性检查", first, second)]
        )

        self.assertEqual(groups, [])

    def test_different_correction_targets_are_not_grouped(self):
        excerpt = "额定电压为24 V。"
        first = report_item(
            "first",
            excerpt=excerpt,
            description="额定电压24 V应更正为48 V。",
            suggestion="将“24 V”更正为“48 V”。",
        )
        second = report_item(
            "second",
            excerpt=excerpt,
            description="额定电压24 V应更正为12 V。",
            suggestion="将“24 V”更正为“12 V”。",
        )

        groups = group_cross_check_report_items(
            [
                check_result("first-check", "规范性检查", first),
                check_result("second-check", "一致性检查", second),
            ]
        )

        self.assertEqual(groups, [])

    def test_quoted_part_name_inside_sentence_rewrite_is_not_a_shared_target(self):
        excerpt = (
            "将CAN通信线缆一端安装整流柜的监控模块和交流柜的交流柜信号检测板接口。"
        )
        component = report_item(
            "component",
            excerpt=excerpt,
            description="部件名称“交流柜的交流柜信号检测板”重复了“交流柜”，应删除一个“柜”字。",
            suggestion="将“交流柜的交流柜信号检测板”改为“交流柜的交流信号检测板”。",
        )
        grammar = report_item(
            "grammar",
            excerpt=excerpt,
            description="该句缺少介词“到”，安装接口的表达不完整。",
            suggestion="改为“将CAN通信线缆一端安装到整流柜的监控模块和交流柜的交流柜信号检测板接口上。”",
        )

        groups = group_cross_check_report_items(
            [
                check_result("normative", "规范性检查", component),
                check_result("grammar", "语法检查", grammar),
            ]
        )

        self.assertEqual(groups, [])

    def test_incompatible_transitive_links_are_split_into_complete_groups(self):
        excerpt = "报告中列出了同一个完整的设备参数修改上下文。"
        first = report_item(
            "first",
            excerpt=excerpt,
            description="设备名称建议采用“目标甲方案”。",
            suggestion="改为“目标甲方案”。",
        )
        bridge = report_item(
            "bridge",
            excerpt=excerpt,
            description="设备名称需要在“目标甲方案”和“目标乙方案”之间统一。",
            suggestion="改为“目标甲方案”，并调整为“目标乙方案”。",
        )
        last = report_item(
            "last",
            excerpt=excerpt,
            description="设备名称建议采用“目标乙方案”。",
            suggestion="改为“目标乙方案”。",
        )

        groups = group_cross_check_report_items(
            [
                check_result("first-check", "检查一", first),
                check_result("bridge-check", "检查二", bridge),
                check_result("last-check", "检查三", last),
            ]
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            [member["item"]["id"] for member in groups[0]["members"]],
            ["first", "bridge"],
        )
        self.assertNotIn("cross_check_group_id", last)

    def test_group_anchors_distinguish_same_item_ids_from_different_checks(self):
        excerpt = "设备名称在正文中写为WebUI，在索引中写为Web UI。"
        items = [
            report_item(
                "same-id",
                excerpt=excerpt,
                description="界面名称的两种写法需要统一。",
                suggestion="统一使用“WebUI”。",
            ),
            report_item(
                "same-id",
                excerpt=excerpt,
                description="同一界面名称存在WebUI和Web UI两种写法。",
                suggestion="全文统一使用“WebUI”。",
            ),
        ]

        groups = group_cross_check_report_items(
            [
                check_result("first-check", "检查一", items[0]),
                check_result("second-check", "检查二", items[1]),
            ]
        )

        self.assertEqual(len(groups), 1)
        self.assertNotEqual(
            groups[0]["members"][0]["anchor_id"], groups[0]["members"][1]["anchor_id"]
        )
        self.assertEqual([item["id"] for item in items], ["same-id", "same-id"])


class CrossCheckGroupingPerformanceTest(unittest.TestCase):
    def setUp(self):
        rounds._cached_cross_check_group_indexes.cache_clear()
        self.addCleanup(rounds._cached_cross_check_group_indexes.cache_clear)

    def results(self):
        return [
            check_result(
                code,
                code,
                report_item(code, suggestion="将“5mV”改为“5MV”。"),
            )
            for code in ("compliance", "consistency")
        ]

    def test_repeated_report_reuses_groups_with_current_items_and_metadata(self):
        first = self.results()
        current = deepcopy(first)
        with patch(
            "app.reporting.rounds._cross_check_group_indexes",
            wraps=rounds._cross_check_group_indexes,
        ) as compute:
            old_group = group_cross_check_report_items(first)[0]
            current[0]["name"] = "当前检查名称"
            current_item = current[0]["report_items"][0]
            current_item.update(
                id="current-id",
                acceptance_status="rejected",
                severity="high",
                summary_fields={"description": "当前展示表述"},
            )
            new_group = group_cross_check_report_items(current)[0]
            self.assertEqual(compute.call_count, 1)

        self.assertIs(new_group["members"][0]["item"], current_item)
        self.assertEqual(new_group["members"][0]["name"], "当前检查名称")
        self.assertEqual(current_item["acceptance_status"], "rejected")
        self.assertNotEqual(
            old_group["members"][0]["anchor_id"],
            new_group["members"][0]["anchor_id"],
        )
        old_group["members"].clear()
        self.assertEqual(len(group_cross_check_report_items(current)[0]["members"]), 2)

    def test_matching_fields_invalidate_cached_comparison(self):
        for field in ("category", "location", "excerpt", "description", "suggestion"):
            with self.subTest(field=field):
                rounds._cached_cross_check_group_indexes.cache_clear()
                results = self.results()
                with patch(
                    "app.reporting.rounds._cross_check_group_indexes",
                    wraps=rounds._cross_check_group_indexes,
                ) as compute:
                    group_cross_check_report_items(results)
                    results[0]["report_items"][0][field] += "新增内容"
                    group_cross_check_report_items(results)
                    self.assertEqual(compute.call_count, 2)

    def test_changed_correction_target_removes_stale_group_markers(self):
        results = self.results()
        self.assertEqual(len(group_cross_check_report_items(results)), 1)
        results[1]["report_items"][0]["suggestion"] = "将“5mV”改为“5mA”。"

        self.assertEqual(group_cross_check_report_items(results), [])
        for result in results:
            self.assertNotIn("cross_check_group_id", result["report_items"][0])
            self.assertNotIn("cross_check_anchor_id", result["report_items"][0])

    def test_order_and_visible_members_follow_current_report(self):
        results = self.results()
        group_cross_check_report_items(results)
        results.reverse()
        members = group_cross_check_report_items(results)[0]["members"]
        self.assertEqual(
            [member["code"] for member in members], ["consistency", "compliance"]
        )
        results.append(
            check_result("third", "检查三", deepcopy(results[0]["report_items"][0]))
        )
        self.assertEqual(group_cross_check_report_items(results)[0]["item_count"], 3)
        results[1]["report_items"].clear()
        members = group_cross_check_report_items(results)[0]["members"]
        self.assertEqual(
            [member["code"] for member in members], ["consistency", "third"]
        )
        results[2]["code"] = "consistency"
        with patch("app.reporting.rounds._cross_check_group_indexes") as compute:
            self.assertEqual(group_cross_check_report_items(results), [])
            compute.assert_not_called()

    def test_unrelated_evidence_skips_correction_parsing(self):
        results = [
            check_result(
                code,
                code,
                *(
                    report_item(str(index), excerpt=chr(0x4E00 + index) * 20)
                    for index in indexes
                ),
            )
            for code, indexes in (("first", range(20)), ("second", range(20, 40)))
        ]
        with patch("app.reporting.rounds._correction_edits") as parse:
            self.assertEqual(group_cross_check_report_items(results), [])
            parse.assert_not_called()

    def test_corrections_are_parsed_at_most_once_per_item(self):
        results = self.results()
        for result in results:
            result["report_items"] = [
                {**result["report_items"][0], "id": f"item-{index}"}
                for index in range(10)
            ]
        with patch(
            "app.reporting.rounds._correction_edits", wraps=rounds._correction_edits
        ) as parse:
            groups = group_cross_check_report_items(results)
        self.assertEqual(groups[0]["item_count"], 20)
        self.assertLessEqual(parse.call_count, 20)

    def test_large_reports_bypass_cache_and_still_group(self):
        for limit in ("_CROSS_CHECK_CACHE_MAX_ITEMS", "_CROSS_CHECK_CACHE_MAX_CHARS"):
            with self.subTest(limit=limit), patch(f"app.reporting.rounds.{limit}", 1):
                with patch(
                    "app.reporting.rounds._cross_check_group_indexes",
                    wraps=rounds._cross_check_group_indexes,
                ) as compute:
                    for _ in range(2):
                        self.assertEqual(
                            len(group_cross_check_report_items(self.results())), 1
                        )
                    self.assertEqual(compute.call_count, 2)
                self.assertEqual(
                    rounds._cached_cross_check_group_indexes.cache_info().currsize, 0
                )

    def test_concurrent_reports_keep_their_own_items(self):
        group_cross_check_report_items(self.results())

        def group_report(index):
            results = self.results()
            for result in results:
                result["report_items"][0]["id"] = f"{index}-{result['code']}"
            groups = group_cross_check_report_items(results)
            for result, member in zip(results, groups[0]["members"]):
                self.assertIs(member["item"], result["report_items"][0])
                self.assertEqual(member["item"]["id"], f"{index}-{result['code']}")

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(group_report, range(20)))


if __name__ == "__main__":
    unittest.main()
