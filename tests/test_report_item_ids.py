import copy
import io
import json
import unittest

from bs4 import BeautifulSoup
from openpyxl import load_workbook

from app.persistence.connection import get_db
from app.reporting.constants import (
    REPORT_ACCEPTANCE_LABEL,
    REPORT_EXPORT_ITEM_ID_HEADER,
    REPORT_EXPORT_SHEET_NAME,
)
from app.reporting.reviews import review_context
from app.reporting.rounds import build_round_result
from app.reporting.service import (
    _prepare_task_results,
    _result_report_items,
    _structured_report_items,
)
from app.tasks.runtime.report_validation import parse_check_report
from tests import test_document_rounds as round_fixtures
from tests import test_final_review as review_fixtures


def colliding_rounds():
    common = {"category": "符号规范", "location": "第2章", "excerpt": "尺寸为2*3。"}
    findings = [
        round_fixtures.finding(
            description,
            **common,
            suggestion=suggestion,
        )
        for description, suggestion in (
            ("尺寸乘号不规范（如2×3）", "将“2*3”修改为“2×3”。"),
            ("尺寸乘号不规范（如4×5）", "将“2*3”修改为“2×3”。"),
            ("尺寸乘号不规范（如2×3）", "核对并统一尺寸符号。"),
        )
    ]
    rounds = [
        round_fixtures.completed(number, finding)
        for number, finding in enumerate(findings, 1)
    ]
    for entry in rounds:
        parse_check_report(entry["result"])
    return rounds


def legacy_collision_result():
    result = build_round_result("consistency", 3, colliding_rounds())
    items = result["structured_report"]["items"]
    items[1]["id"] = items[0]["id"]
    result["result"] = json.dumps(result["structured_report"], ensure_ascii=False)
    return {**result, "code": "consistency", "name": "全文一致性检查"}


class ReportItemIdentityTest(unittest.TestCase):
    def test_duplicate_ids_are_stable_in_every_structured_storage(self):
        raw_items = [
            {key: "same", "status": "issue", "description": f"参数 {index} 不一致"}
            for index, key in enumerate(("id", "item_id", "report_item_id"))
        ]
        expected = None
        for key, value in (
            ("structured_report", {"items": raw_items}),
            ("report_json", {"items": raw_items}),
            ("result", json.dumps({"items": raw_items})),
            ("structured_items", raw_items),
        ):
            with self.subTest(storage=key):
                result = {"code": "check", key: value}
                original = copy.deepcopy(result)
                ids = [item["id"] for item in _result_report_items(result)]
                self.assertEqual(len(set(ids)), 3)
                self.assertEqual(ids[0], "same")
                self.assertEqual(result, original)
                self.assertEqual(
                    ids,
                    [
                        item["id"]
                        for item in _result_report_items(json.loads(json.dumps(result)))
                    ],
                )
                expected = expected or ids
                self.assertEqual(ids, expected)

    def test_generated_collision_ids_leave_other_explicit_ids_unchanged(self):
        for original_id in ("same", "x" * 128):
            with self.subTest(original_id=original_id):
                report = {
                    "items": [
                        {"id": original_id, "description": f"参数 {index} 不一致"}
                        for index in range(3)
                    ]
                }
                first_ids = [
                    item["id"] for item in _structured_report_items("check", report)
                ]
                report["items"].append({"id": first_ids[1], "description": "独立问题"})
                ids = [item["id"] for item in _structured_report_items("check", report)]
                self.assertEqual(len(set(ids)), 4)
                self.assertEqual(ids[0], original_id)
                self.assertEqual(ids[3], first_ids[1])
                for item_id in ids:
                    self.assertLessEqual(len(item_id), 128)
                    self.assertRegex(item_id, r"^[A-Za-z0-9._:-]+$")

    def test_existing_acceptance_belongs_only_to_first_duplicate(self):
        result = legacy_collision_result()
        original_id = result["structured_report"]["items"][0]["id"]
        result["item_acceptances"] = {original_id: {"status": "accepted"}}
        prepared = _prepare_task_results([result])[0]
        self.assertEqual(
            [item["acceptance_status"] for item in prepared["report_items"]],
            ["accepted", "pending"],
        )
        self.assertEqual(prepared["report_counts"]["reviewed"], 1)
        self.assertEqual(prepared["report_counts"]["pending_review"], 1)

    def test_round_groups_keep_unique_ids_across_snapshots_and_rebuilds(self):
        rounds = colliding_rounds()
        previous = []
        first_id = None
        for count in range(1, 4):
            result = build_round_result(
                "check", 3, rounds[:count], previous_items=previous
            )
            previous = result["structured_report"]["items"]
            first_id = first_id or previous[0]["id"]
            self.assertEqual(previous[0]["id"], first_id)
        self.assertEqual(len(previous), 2)
        self.assertEqual(len({item["id"] for item in previous}), 2)
        rebuilt = build_round_result("check", 3, rounds, previous_items=previous)
        self.assertEqual(rebuilt["structured_report"]["items"], previous)

    def test_retry_reuses_the_ids_exposed_for_duplicate_stored_items(self):
        result = legacy_collision_result()
        expected = _result_report_items(result)
        rebuilt = build_round_result(
            "consistency",
            3,
            result["round_results"],
            previous_items=result["structured_report"]["items"],
        )
        self.assertEqual(
            [item["id"] for item in rebuilt["structured_report"]["items"]],
            [item["id"] for item in expected],
        )
        self.assertEqual(len({item["id"] for item in expected}), 2)

    def test_new_groups_respect_retained_and_removed_previous_ids(self):
        rounds = [
            round_fixtures.completed(1, round_fixtures.finding("输入电压应为5V")),
            round_fixtures.completed(2, round_fixtures.finding("输出电流应为12mA")),
        ]
        items = build_round_result("check", 2, rounds)["structured_report"]["items"]
        for retain in (True, False):
            with self.subTest(retain=retain):
                previous = copy.deepcopy(items[1])
                previous["id"] = items[0]["id"]
                if not retain:
                    previous["source_items"] = [{"round": 3, "item": 1}]
                rebuilt = build_round_result(
                    "check", 3, rounds, previous_items=[previous]
                )
                updated = rebuilt["structured_report"]["items"]
                self.assertNotEqual(updated[0]["id"], previous["id"])
                self.assertEqual(len({item["id"] for item in updated}), 2)
                if retain:
                    self.assertEqual(updated[1]["id"], previous["id"])
                else:
                    self.assertNotIn(previous["id"], {item["id"] for item in updated})


class ReportItemReviewIdentityTest(unittest.TestCase):
    def setUp(self):
        self.case = review_fixtures.FinalReviewTest()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def save_result(self, result):
        with self.case.app.app_context():
            get_db().execute(
                "UPDATE tasks SET result_json=? WHERE id=?",
                (json.dumps([result], ensure_ascii=False), self.case.task_id),
            )
            get_db().commit()
        self.case.items = _result_report_items(result)

    def report_rows(self, prefix=""):
        response = self.case.client.get(f"{prefix}/tasks/{self.case.task_id}")
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.data, "html.parser").select("[data-report-item]")

    def test_stored_duplicate_items_save_independently_after_page_reload(self):
        self.save_result(legacy_collision_result())
        for prefix in ("", "/admin"):
            rows = self.report_rows(prefix)
            self.assertEqual(len({row["data-item-id"] for row in rows}), 2)
            for row in rows:
                response = self.case.client.post(
                    f"{prefix}/tasks/{self.case.task_id}/report-items",
                    json={
                        "result_code": row["data-result-code"],
                        "item_id": row["data-item-id"],
                        "review_token": row.select_one("[data-review-token]")["value"],
                        "acceptance_status": "accepted",
                    },
                )
                self.assertEqual(response.status_code, 200, response.get_json())
            for row in self.report_rows(prefix):
                self.assertEqual(row["data-acceptance-status"], "accepted")
        self.assertEqual(len(self.case.state(0)["history"]), 2)
        self.assertEqual(len(self.case.state(1)["history"]), 2)

    def test_existing_initial_and_final_reviews_stay_with_original_item(self):
        result = legacy_collision_result()
        duplicate = result["structured_report"]["items"].pop()
        self.save_result(result)
        self.assertEqual(self.case.initial().status_code, 200)
        self.assertEqual(self.case.final().status_code, 200)
        original_state = self.case.state()
        with self.case.app.app_context():
            result = json.loads(self.case.task()["result_json"])[0]
        result["structured_report"]["items"].append(duplicate)
        self.save_result(result)
        self.assertEqual(self.case.state(), original_state)
        self.assertEqual(self.case.state(1)["history"], [])
        self.assertEqual(self.case.state(1)["status"], "not_required")
        self.assertEqual(self.case.initial(1).status_code, 200)
        self.assertEqual(self.case.final(1).status_code, 200)
        self.assertEqual(self.case.state(), original_state)
        self.assertNotEqual(
            self.case.state()["initial"]["id"], self.case.state(1)["initial"]["id"]
        )
        stale_token = self.case.state(1)["token"]
        self.assertEqual(self.case.initial(1, status="accepted").status_code, 200)
        self.assertEqual(self.case.initial(1, token=stale_token).status_code, 409)
        with self.case.app.app_context():
            progress = review_context(self.case.task())["progress"]
        self.assertEqual(progress["initial_count"], 2)
        self.assertEqual(progress["required_count"], 1)
        self.assertEqual(progress["confirmed"], 1)

    def test_retry_keeps_feedback_and_audit_for_each_resolved_item(self):
        self.save_result(legacy_collision_result())
        self.assertEqual(self.case.initial(0, status="accepted").status_code, 200)
        self.assertEqual(self.case.initial(1).status_code, 200)
        self.assertEqual(self.case.final(1).status_code, 200)
        states = [self.case.state(index) for index in range(2)]
        with self.case.app.app_context():
            result = json.loads(self.case.task()["result_json"])[0]
        rebuilt = build_round_result(
            "consistency",
            3,
            result["round_results"],
            previous_items=result["structured_report"]["items"],
        )
        self.save_result({**result, **rebuilt})
        self.assertEqual([self.case.state(index) for index in range(2)], states)
        self.assertEqual(
            [row["data-acceptance-status"] for row in self.report_rows()],
            ["accepted", "rejected"],
        )

    def test_excel_round_trip_keeps_duplicate_items_independent(self):
        self.save_result(legacy_collision_result())
        response = self.case.client.get(f"/tasks/{self.case.task_id}/export.xlsx")
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(io.BytesIO(response.data))
        sheet = workbook[REPORT_EXPORT_SHEET_NAME]
        columns = {cell.value: cell.column for cell in sheet[1]}
        ids = [
            sheet.cell(row, columns[REPORT_EXPORT_ITEM_ID_HEADER]).value
            for row in (2, 3)
        ]
        self.assertEqual(len(set(ids)), 2)
        sheet.cell(2, columns[REPORT_ACCEPTANCE_LABEL], "认可")
        sheet.cell(3, columns[REPORT_ACCEPTANCE_LABEL], "不认可")
        sheet.cell(3, columns["不认可原因"], "模型误报")
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        output.seek(0)
        response = self.case.client.post(
            f"/tasks/{self.case.task_id}/import.xlsx",
            data={"report_excel": (output, "report.xlsx")},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.case.state()["initial"]["decision"], "accepted")
        self.assertEqual(self.case.state(1)["initial"]["decision"], "rejected")
        self.assertEqual(self.case.state()["initial"]["channel"], "excel")
        self.assertEqual(self.case.state(1)["initial"]["channel"], "excel")
