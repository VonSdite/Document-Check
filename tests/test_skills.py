import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from app.checks.skills import (
    SKILL_FIELDS,
    check_snapshot,
    create_skill,
    delete_case,
    get_skill,
    save_case,
    save_skill,
    skill_cases,
)
from app.persistence.connection import get_db
from app.persistence.permissions import register_subject, replace_subject_permissions
from app.persistence.schema import init_db
from app.persistence.settings import set_setting
from app.reporting.excel import _excel_report_item_field
from app.reporting.service import _prepare_task_results, _report_item_totals
from app.tasks.runner import TaskRunner
from app.tasks.selection import selected_check_items
from app.tasks.submission import _enabled_check_item_snapshots
from tests import test_routes


def rule_data(**extra):
    return {
        "name": "默认值冲突",
        "description": "比较同一版本的默认值",
        "target": "检查同一参数的默认值冲突",
        "applicability": "相同设备、版本、运行模式",
        "steps": "核对对象与单位，引用两处参数描述",
        "evidence_requirements": "两处默认值原文及位置",
        "exclusions": "不同运行模式或单位换算后等价",
        "suggestion_principles": "报告冲突，正确值由产品人员确认",
        **extra,
    }


def case_data(**extra):
    return {
        "title": "同一版本超时参数冲突",
        "reference": "设备 A，V1，相同运行模式",
        "location": "参数说明；附录 A",
        "evidence": "默认超时为 30 s。默认超时为 60 s。",
        "rationale": "两处描述同一对象和相同条件，数值不等价",
        "expected_result": "报告超时默认值冲突，引用两处原文",
        "corrected_text": "",
        "document_text": "参数说明：默认超时为 30 s。\n附录 A：默认超时为 60 s。",
        "case_kind": "positive",
        "reviewed": "on",
        **extra,
    }


def report(code, **extra):
    return {
        "code": code,
        "name": code,
        "structured_report": {
            "items": [
                {
                    "id": f"{code}-1",
                    "status": "issue",
                    "category": code,
                    "location": "参数说明",
                    "excerpt": "默认超时为 30 s。默认超时为 60 s。",
                    "description": "同一参数默认值冲突",
                    "severity": "high",
                    "confidence": "high",
                    "suggestion": "核对正确值并统一两处",
                }
            ]
        },
        **extra,
    }


class SkillManagementTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_routes.AdminSettingsRouteTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.app
        self.client = self.fixture.client

    def skill(self, *, enabled=False):
        with self.app.app_context():
            db = get_db()
            skill_id = create_skill(db, "默认值冲突")
            save_skill(db, get_skill(db, skill_id), rule_data())
            case_id = save_case(db, get_skill(db, skill_id), case_data())
            if enabled:
                save_skill(db, get_skill(db, skill_id), rule_data(enabled="on"))
            db.commit()
        return skill_id, case_id

    def test_old_database_keeps_prompt_and_enabled_state(self):
        with self.app.app_context():
            db = get_db()
            original = dict(
                db.execute(
                    "SELECT * FROM check_items WHERE code='consistency'"
                ).fetchone()
            )
            for column in ("kind", "definition_json", "version"):
                db.execute(f"ALTER TABLE check_items DROP COLUMN {column}")
            db.commit()
            init_db()
            row = db.execute(
                "SELECT * FROM check_items WHERE code='consistency'"
            ).fetchone()
            self.assertEqual(row["prompt"], original["prompt"])
            self.assertEqual(row["enabled"], original["enabled"])
            self.assertEqual(row["kind"], "general")
            self.assertEqual(row["version"], 1)

    def test_draft_creation_rule_editor_and_catalog(self):
        response = self.client.post(
            "/admin/rules", data={"action": "create_skill", "name": "默认值冲突"}
        )
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.location)
        self.assertEqual(page.status_code, 200)
        soup = BeautifulSoup(page.get_data(as_text=True), "html.parser")
        for field, _ in SKILL_FIELDS:
            self.assertIsNotNone(soup.select_one(f'textarea[name="{field}"]'))
        with self.app.app_context():
            row = (
                get_db()
                .execute("SELECT * FROM check_items WHERE kind='specialist'")
                .fetchone()
            )
            self.assertFalse(row["enabled"])
            self.assertEqual(
                _enabled_check_item_snapshots(get_db(), [row["id"]], "document_check"),
                [],
            )
        self.assertIn(
            "默认值冲突", self.client.get("/admin/rules").get_data(as_text=True)
        )

    def test_enabling_requires_confirmed_positive_and_case_removal_disables(self):
        with self.app.app_context():
            db = get_db()
            skill_id = create_skill(db, "默认值冲突")
            save_skill(db, get_skill(db, skill_id), rule_data())
            for case in (case_data(reviewed=""), case_data(case_kind="negative")):
                save_case(db, get_skill(db, skill_id), case)
            with self.assertRaisesRegex(ValueError, "真实问题"):
                save_skill(db, get_skill(db, skill_id), rule_data(enabled="on"))
            positive = save_case(db, get_skill(db, skill_id), case_data())
            save_skill(db, get_skill(db, skill_id), rule_data(enabled="on"))
            self.assertTrue(get_skill(db, skill_id)["enabled"])
            delete_case(db, get_skill(db, skill_id), positive)
            self.assertFalse(get_skill(db, skill_id)["enabled"])

    def test_example_budget_excludes_unconfirmed_and_full_context(self):
        skill_id, _ = self.skill()
        with self.app.app_context():
            db = get_db()
            for index in range(4):
                save_case(db, get_skill(db, skill_id), case_data(title=f"正例 {index}"))
                save_case(
                    db,
                    get_skill(db, skill_id),
                    case_data(title=f"反例 {index}", case_kind="negative"),
                )
            save_case(
                db, get_skill(db, skill_id), case_data(title="未核对案例", reviewed="")
            )
            snapshot = check_snapshot(db, get_skill(db, skill_id))
            self.assertEqual(len(snapshot["example_case_ids"]), 4)
            self.assertNotIn("未核对案例", snapshot["prompt"])
            self.assertNotIn("附录 A：默认超时", snapshot["prompt"])

    def test_snapshot_and_retry_preserve_rule_after_edit_and_delete(self):
        skill_id, case_id = self.skill(enabled=True)
        with self.app.app_context():
            db = get_db()
            snapshot = _enabled_check_item_snapshots(db, [skill_id], "document_check")[
                0
            ]
            general = db.execute(
                "SELECT id FROM check_items WHERE code='compliance'"
            ).fetchone()[0]
            self.assertEqual(
                set(_enabled_check_item_snapshots(db, [general], "document_check")[0]),
                {"id", "code", "name", "prompt"},
            )
            save_case(
                db, get_skill(db, skill_id), case_data(evidence="更新后的证据"), case_id
            )
            save_skill(
                db,
                get_skill(db, skill_id),
                rule_data(target="更新后的目标", enabled="on"),
            )
            db.execute("DELETE FROM check_items WHERE id=?", (skill_id,))
            self.assertEqual(skill_cases(db, skill_id), [])
            task = {
                "task_type": "document_check",
                "checks_snapshot_json": json.dumps([snapshot]),
                "retry_check_codes_json": json.dumps([snapshot["code"]]),
            }
            selected = selected_check_items(db, task)[0]
            self.assertEqual(selected["prompt"], snapshot["prompt"])
            self.assertEqual(selected["version"], snapshot["version"])

    def test_invalid_rule_and_case_keep_inputs_without_database_changes(self):
        skill_id, _ = self.skill()
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={
                "action": "save_skill",
                **rule_data(steps="", target="尚待补齐的输入"),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("尚待补齐的输入", response.get_data(as_text=True))
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={"action": "add_case", **case_data(evidence="", title="未保存的案例")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("未保存的案例", response.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(len(skill_cases(get_db(), skill_id)), 1)

    def test_stale_editor_and_raw_prompt_edit_are_rejected(self):
        skill_id, _ = self.skill()
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={"action": "save_skill", "version": "1", **rule_data()},
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/admin/rules",
            data={
                "action": "prompt",
                "item_id": skill_id,
                "name": "绕过结构化规则",
                "prompt": "直接覆盖",
                "enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_task_report_limit_is_snapshotted(self):
        skill_id, _ = self.skill(enabled=True)
        with self.app.app_context():
            db = get_db()
            set_setting("task_report_limit", 5)
            snapshot = _enabled_check_item_snapshots(db, [skill_id], "document_check")[
                0
            ]
            set_setting("task_report_limit", 12)
            task = {
                "task_type": "document_check",
                "checks_snapshot_json": json.dumps([snapshot]),
            }
            self.assertEqual(selected_check_items(db, task)[0]["task_report_limit"], 5)

    def test_stale_service_rule_edit_keeps_current_version(self):
        skill_id, _ = self.skill()
        with self.app.app_context():
            db = get_db()
            stale = get_skill(db, skill_id)
            save_skill(db, stale, rule_data(target="最新判断目标"))
            with self.assertRaisesRegex(ValueError, "已更新"):
                save_skill(db, stale, rule_data(target="过期判断目标"))
            self.assertIn("最新判断目标", get_skill(db, skill_id)["prompt"])

    def test_validation_uses_full_case_text_and_rule_snapshot(self):
        skill_id, case_id = self.skill()
        model_id = self.fixture._configure_provider()
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={"action": "validate_case", "case_id": case_id, "model_id": model_id},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            db = get_db()
            case = skill_cases(db, skill_id)[0]
            task_id = case["validation_task_id"]
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            snapshot = json.loads(task["checks_snapshot_json"])[0]
            self.assertEqual(snapshot["example_case_ids"], [])
            self.assertEqual(snapshot["validation_case_id"], case_id)
            self.assertEqual(snapshot["version"], case["validation_version"])
            path = Path(self.app.config["UPLOAD_FOLDER"]) / task["stored_filename"]
            self.assertEqual(
                path.read_text(encoding="utf-8"), case_data()["document_text"]
            )
            save_skill(db, get_skill(db, skill_id), rule_data(target="更新后的规则"))
            db.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
            db.commit()
        with patch(
            "app.tasks.runner.run_check",
            return_value=json.dumps(
                report("result")["structured_report"], ensure_ascii=False
            ),
        ) as model:
            TaskRunner(self.app).run(task_id)
        self.assertEqual(model.call_args.kwargs["prompt"], snapshot["prompt"])
        self.assertIn("默认超时为 60 s", model.call_args.kwargs["document_text"])
        with self.app.app_context():
            task = (
                get_db()
                .execute("SELECT * FROM tasks WHERE id=?", (task_id,))
                .fetchone()
            )
            self.assertEqual(task["status"], "completed")
            result = json.loads(task["result_json"])[0]
            self.assertEqual(result["kind"], "specialist")
            self.assertEqual(result["version"], snapshot["version"])

    def test_validation_cannot_use_another_users_model(self):
        skill_id, case_id = self.skill()
        model_id = self.fixture._configure_provider(owner_subject="ip:10.0.0.8")
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={"action": "validate_case", "case_id": case_id, "model_id": model_id},
        )
        self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            self.assertIsNone(skill_cases(get_db(), skill_id)[0]["validation_task_id"])

    def test_report_import_prefills_evidence_and_is_pending(self):
        skill_id, _ = self.skill()
        task_id = self.fixture._insert_task()
        with self.app.app_context():
            get_db().execute(
                "UPDATE tasks SET result_json=?, document_text=? WHERE id=?",
                (
                    json.dumps([report("consistency")]),
                    case_data()["document_text"],
                    task_id,
                ),
            )
            get_db().commit()
        page = self.client.get(f"/admin/tasks/{task_id}")
        soup = BeautifulSoup(page.get_data(as_text=True), "html.parser")
        link = soup.find("a", string="固化为案例")
        self.assertIsNotNone(link)
        rules_page = self.client.get(link["href"])
        soup = BeautifulSoup(rules_page.get_data(as_text=True), "html.parser")
        target = soup.find("a", string="录入报告案例")
        self.assertIsNotNone(target)
        imported = self.client.get(target["href"])
        self.assertEqual(imported.status_code, 200)
        soup = BeautifulSoup(imported.get_data(as_text=True), "html.parser")
        form = soup.select_one("#new-case form")
        self.assertIn(
            "默认超时为 30 s", form.select_one('textarea[name="evidence"]').text
        )
        self.assertFalse(form.select_one('input[name="reviewed"]').has_attr("checked"))
        self.assertEqual(
            form.select_one('input[name="source_task_id"]')["value"], str(task_id)
        )

    def test_rule_permission_and_report_source_owner_scope(self):
        skill_id, _ = self.skill()
        user = self.app.test_client()
        self.assertEqual(
            user.post(
                f"/rules/skills/{skill_id}", data={"action": "delete_skill"}
            ).status_code,
            403,
        )
        with self.app.app_context():
            register_subject("ip:127.0.0.1")
            replace_subject_permissions("ip:127.0.0.1", {"rules.manage"})
        self.assertEqual(user.get(f"/rules/skills/{skill_id}").status_code, 200)
        other_task = self.fixture._insert_task(ip="10.0.0.8")
        response = user.get(
            f"/rules/skills/{skill_id}",
            query_string={
                "source_task_id": other_task,
                "source_result_code": "consistency",
                "source_item_id": "consistency-1",
            },
        )
        self.assertEqual(response.status_code, 404)


class SkillDuplicateReportTest(unittest.TestCase):
    def prepare(self, results):
        return _prepare_task_results(
            results, task_type="document_check", suppression_rules={}
        )

    def test_same_evidence_and_description_merge_and_keep_sources(self):
        original = [
            report("general"),
            report("specialist", kind="specialist", version=3),
        ]
        unchanged = copy.deepcopy(original)
        prepared = self.prepare(original)
        self.assertEqual(original, unchanged)
        self.assertEqual(_report_item_totals(prepared)["issue"], 1)
        self.assertEqual(len(prepared[0]["merged_report_items"]), 1)
        item = prepared[1]["report_items"][0]
        self.assertEqual(
            {source["code"] for source in item["sources"]}, {"general", "specialist"}
        )
        self.assertIn(
            "共同发现", _excel_report_item_field(item, "description", "document_check")
        )

    def test_distinct_findings_missing_evidence_and_general_only_stay_separate(self):
        for mode in (
            "different_description",
            "different_evidence",
            "empty_evidence",
            "general_only",
            "different_status",
        ):
            with self.subTest(mode=mode):
                results = [report("a"), report("b", kind="specialist", version=1)]
                item = results[1]["structured_report"]["items"][0]
                if mode == "different_description":
                    item["description"] = "同一原文中的另一个独立问题"
                elif mode == "different_evidence":
                    item["excerpt"] = "另一个对象的默认值"
                elif mode == "empty_evidence":
                    item["excerpt"] = ""
                elif mode == "different_status":
                    item["status"] = "suggestion"
                else:
                    results[1].pop("kind")
                self.assertEqual(_report_item_totals(self.prepare(results))["total"], 2)

    def test_reviewed_primary_preserves_identity_and_feedback(self):
        results = [
            report("general", item_acceptances={"general-1": {"status": "accepted"}}),
            report("skill", kind="specialist", version=2),
        ]
        prepared = self.prepare(results)
        self.assertEqual(prepared[0]["report_items"][0]["id"], "general-1")
        totals = _report_item_totals(prepared)
        self.assertEqual(totals["total"], 1)
        self.assertEqual(totals["accepted"], 1)
        self.assertEqual(totals["reviewed"], 1)

    def test_conflicting_feedback_stays_separate(self):
        results = [
            report("general", item_acceptances={"general-1": {"status": "accepted"}}),
            report(
                "skill",
                kind="specialist",
                version=2,
                item_acceptances={
                    "skill-1": {
                        "status": "rejected",
                        "rejection_reason": "classification_inaccurate",
                    }
                },
            ),
        ]
        self.assertEqual(_report_item_totals(self.prepare(results))["total"], 2)

    def test_task_report_limit_ranks_after_merge_and_keeps_saved_feedback(self):
        results = [
            report("general", item_acceptances={"general-1": {"status": "accepted"}}),
            report("skill", kind="specialist", version=1, task_report_limit=1),
        ]
        results[0]["structured_report"]["items"][0].update(
            description="较低置信度的另一个问题", confidence="low"
        )
        prepared = self.prepare(results)
        self.assertEqual(_report_item_totals(prepared)["total"], 1)
        self.assertEqual(_report_item_totals(prepared)["accepted"], 1)
        self.assertEqual(len(prepared[0]["task_omitted_report_items"]), 1)
        self.assertEqual(prepared[1]["report_items"][0]["confidence"], "high")

    def test_zero_task_limit_preserves_all_findings(self):
        results = [
            report("general"),
            report("skill", kind="specialist", version=1, task_report_limit=0),
        ]
        results[0]["structured_report"]["items"][0]["description"] = "独立问题"
        self.assertEqual(_report_item_totals(self.prepare(results))["total"], 2)
