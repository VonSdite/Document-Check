import json
import unittest
from copy import deepcopy

from app.reporting.rounds import build_round_result, group_cross_check_report_items
from app.reporting.service import _prepare_task_results
from tests.test_document_rounds import completed, finding


class RoundPresentationTest(unittest.TestCase):
    def result(self, *rounds):
        return {"code": "check", **build_round_result("check", 3, list(rounds))}

    def prepare(self, result, **kwargs):
        return _prepare_task_results(
            [result], task_type="document_check", suppression_rules={}, **kwargs
        )[0]

    def test_saved_concatenation_displays_one_complete_source_without_mutation(self):
        long = finding(
            "文中电压单位不一致，同一参数的单位写法不同，需按参数定义统一。",
            impact="单位写法不同会影响操作准确性，读者可能误解参数含义。",
            suggestion="请按参数定义核对单位，并将单位写法统一。",
        )
        short = finding()
        result = self.result(completed(1, long), completed(2, short))
        before = deepcopy(result)
        stored = result["structured_report"]["items"][0]
        result["item_acceptances"] = {stored["id"]: {"status": "accepted"}}
        before["item_acceptances"] = deepcopy(result["item_acceptances"])

        prepared = self.prepare(result)
        self.assertEqual(len(prepared["report_items"]), 1)
        item = prepared["report_items"][0]
        self.assertEqual(
            item["summary_fields"],
            {field: short[field] for field in ("description", "impact", "suggestion")},
        )
        self.assertEqual(item["description"], stored["description"])
        self.assertEqual(item["id"], stored["id"])
        self.assertEqual(item["acceptance_status"], "accepted")
        self.assertEqual(prepared["report_counts"]["accepted"], 1)
        self.assertEqual(result, before)
        variants = item["round_field_variants"]["suggestion"]
        self.assertEqual(
            [v["text"] for v in variants], [long["suggestion"], short["suggestion"]]
        )

    def test_source_indices_follow_normalized_items_and_skip_other_findings(self):
        result = self.result(
            completed(
                1,
                None,
                finding("此项符合要求", status="non_issue"),
                finding("操作步骤缺少退出说明", excerpt="完成配置后退出"),
                finding("文中电压单位不一致"),
            ),
            completed(2, finding()),
        )
        item = next(
            item
            for item in self.prepare(result)["report_items"]
            if item.get("summary_fields")
        )
        self.assertEqual(
            item["source_items"], [{"round": 1, "item": 3}, {"round": 2, "item": 1}]
        )
        self.assertEqual(item["summary_fields"]["description"], "电压单位不一致")
        self.assertNotIn(
            "退出", json.dumps(item["round_field_variants"], ensure_ascii=False)
        )

    def test_tentative_source_keeps_its_condition_even_when_longer(self):
        tentative = finding(
            "电压单位疑似不一致，若两处指同一参数则需统一，需先核对实际参数。",
            status="suggestion",
            suggestion="若两处指同一参数，核对实际单位后统一；若参数不同，请补充说明。",
            impact="若为同一参数，可能造成误读。",
        )
        result = self.result(
            completed(1, finding(tentative["description"])), completed(2, tentative)
        )
        item = self.prepare(result)["report_items"][0]
        self.assertEqual(item["type"], "suggestion")
        self.assertEqual(
            item["summary_fields"]["description"], "待确认：" + tentative["description"]
        )
        self.assertEqual(item["summary_fields"]["suggestion"], tentative["suggestion"])
        self.assertEqual(item["summary_fields"]["impact"], tentative["impact"])

    def test_conflicting_targets_do_not_become_an_arbitrary_recommendation(self):
        for left, right in (("5V", "12V"), ("≤5V", "≥5V"), ("5mV", "5MV")):
            with self.subTest(left=left, right=right):
                result = self.result(
                    completed(1, finding(suggestion=f"将电压改为{left}。")),
                    completed(2, finding(suggestion=f"将电压改为{right}。")),
                )
                item = self.prepare(result)["report_items"][0]
                summary = item["summary_fields"]["suggestion"]
                self.assertIn("各轮修改方案存在分歧", summary)
                self.assertNotIn(left, summary)
                self.assertNotIn(right, summary)
                variants = item["round_field_variants"]["suggestion"]
                self.assertEqual(len(variants), 2)
                self.assertIn(left, variants[0]["text"])
                self.assertIn(right, variants[1]["text"])

    def test_explicit_fix_is_preferred_to_generic_short_suggestion(self):
        specific = finding(suggestion="将“5mV”改为“5V”，并按实际参数核对其他位置。")
        result = self.result(completed(1, specific), completed(2, finding()))
        item = self.prepare(result)["report_items"][0]
        self.assertEqual(item["summary_fields"]["suggestion"], specific["suggestion"])

    def test_missing_or_invalid_sources_preserve_full_saved_text(self):
        original = self.result(
            completed(1, finding()), completed(2, finding("文中电压单位不一致"))
        )
        for fault in (
            "missing_round",
            "missing_ref",
            "invalid_ref",
            "wrong_index",
            "wrong_rounds",
        ):
            with self.subTest(fault=fault):
                result = deepcopy(original)
                stored = result["structured_report"]["items"][0]
                if fault == "missing_round":
                    result["round_results"].pop()
                elif fault == "missing_ref":
                    stored.pop("source_items")
                elif fault == "invalid_ref":
                    stored["source_items"][0]["item"] = True
                elif fault == "wrong_index":
                    stored["source_items"][0]["item"] = 99
                else:
                    stored["source_rounds"] = [1, 2, 3]
                item = self.prepare(result)["report_items"][0]
                self.assertNotIn("summary_fields", item)
                self.assertEqual(item["description"], stored["description"])

    def test_saved_additional_content_and_missing_source_fields_are_kept(self):
        result = self.result(
            completed(1, finding(impact="")),
            completed(2, finding("文中电压单位不一致", suggestion="")),
        )
        stored = result["structured_report"]["items"][0]
        stored["description"] += "；还需核对设备实际版本。"
        item = self.prepare(result)["report_items"][0]
        self.assertNotIn("description", item["summary_fields"])
        self.assertNotIn("impact", item["summary_fields"])
        self.assertEqual(item["description"], stored["description"])
        self.assertEqual(item["impact"], finding()["impact"])

    def test_identical_versions_are_combined_and_single_source_keeps_original(self):
        result = self.result(
            completed(1, finding()),
            completed(2, finding()),
            completed(3, finding("文中电压单位不一致")),
        )
        item = self.prepare(result)["report_items"][0]
        variants = item["round_field_variants"]["description"]
        self.assertEqual(len(variants), 2)
        self.assertEqual(
            variants[0]["sources"], [{"round": 1, "item": 1}, {"round": 2, "item": 1}]
        )
        single = self.prepare(self.result(completed(1, finding())))["report_items"][0]
        self.assertNotIn("summary_fields", single)

    def test_suppression_uses_full_description_and_keeps_expandable_sources(self):
        result = self.result(
            completed(1, finding()), completed(2, finding("文中电压单位不一致"))
        )
        stored = result["structured_report"]["items"][0]
        prepared = _prepare_task_results(
            [result],
            task_type="document_check",
            suppression_rules={
                "check": [
                    {"id": 7, "reason": "已确认", "description": stored["description"]}
                ]
            },
        )[0]
        self.assertEqual(prepared["report_items"], [])
        item = prepared["suppressed_report_items"][0]
        self.assertEqual(item["suppression_similarity"], 1)
        self.assertEqual(item["id"], stored["id"])
        self.assertEqual(
            item["summary_fields"]["description"], finding()["description"]
        )
        self.assertEqual(len(item["round_field_variants"]["description"]), 2)

    def test_summary_metadata_preserves_cross_check_groups(self):
        first = self.result(
            completed(1, finding()), completed(2, finding("文中电压单位不一致"))
        )
        second = deepcopy(first)
        second["code"] = "other"
        prepared = _prepare_task_results([first, second], suppression_rules={})
        groups = group_cross_check_report_items(prepared)
        without_summaries = deepcopy(prepared)
        for result in without_summaries:
            for item in result["report_items"]:
                item.pop("summary_fields", None)
                item.pop("round_field_variants", None)

        def identities(groups):
            return [
                [(member["code"], member["item"]["id"]) for member in group["members"]]
                for group in groups
            ]

        self.assertEqual(len(groups), 1)
        self.assertEqual(
            identities(groups),
            identities(group_cross_check_report_items(without_summaries)),
        )


if __name__ == "__main__":
    unittest.main()
