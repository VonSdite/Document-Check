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
    get_skill_case,
    save_case,
    save_skill,
    select_skill_examples,
    selected_examples,
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
            select_skill_examples(db, get_skill(db, skill_id), [case_id])
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
            cases = skill_cases(db, skill_id)
            representative_ids = [
                case["id"]
                for kind in ("positive", "negative")
                for case in [c for c in cases if c["data"]["kind"] == kind][:2]
            ]
            select_skill_examples(db, get_skill(db, skill_id), representative_ids)
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
            self.assertEqual(result["validation_case"], snapshot["validation_case"])
        page = self.client.get(f"/admin/tasks/{task_id}")
        self.assertIn(case_data()["expected_result"], page.get_data(as_text=True))

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
        self.assertIsNone(form.select_one('input[name="reviewed"]'))
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

    def _start_validation(self, skill_id, case_id):
        model_id = self.fixture._configure_provider()
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={"action": "validate_case", "case_id": case_id, "model_id": model_id},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            return get_skill_case(get_db(), skill_id, case_id)["validation_task_id"]

    def test_document_picker_keeps_general_defaults_and_requires_specialist_choice(
        self,
    ):
        skill_id, _ = self.skill(enabled=True)
        for url in ("/", "/admin/tasks"):
            soup = BeautifulSoup(
                self.client.get(url).get_data(as_text=True), "html.parser"
            )
            general = soup.select('[data-check-group="general"] input[name="checks"]')
            self.assertTrue(general)
            self.assertTrue(all(field.has_attr("checked") for field in general))
            specialized = soup.select_one(
                f'[data-check-group="specialist"] input[value="{skill_id}"]'
            )
            self.assertIsNotNone(specialized)
            self.assertFalse(specialized.has_attr("checked"))

    def test_new_cases_require_explicit_example_selection_and_later_cases_can_be_used(
        self,
    ):
        skill_id, original_id = self.skill()
        with self.app.app_context():
            db = get_db()
            later_id = save_case(
                db, get_skill(db, skill_id), case_data(title="新增真实问题")
            )
            negative_id = save_case(
                db,
                get_skill(db, skill_id),
                case_data(title="新增正确反例", case_kind="negative"),
            )
            self.assertEqual(
                [c["id"] for c in selected_examples(skill_cases(db, skill_id))],
                [original_id],
            )
            select_skill_examples(db, get_skill(db, skill_id), [later_id, negative_id])
            snapshot = check_snapshot(db, get_skill(db, skill_id))
            self.assertEqual(snapshot["example_case_ids"], [later_id, negative_id])
            self.assertIn("新增正确反例", snapshot["prompt"])
            self.assertNotIn(case_data()["title"], snapshot["prompt"])
            version = get_skill(db, skill_id)["version"]
            select_skill_examples(db, get_skill(db, skill_id), [later_id, negative_id])
            self.assertEqual(get_skill(db, skill_id)["version"], version)
            save_case(
                db, get_skill(db, skill_id), case_data(title="修改已选示例"), later_id
            )
            self.assertTrue(
                get_skill_case(db, skill_id, later_id)["data"]["use_as_example"]
            )

    def test_editing_legacy_case_preserves_its_example_selection(self):
        skill_id, case_id = self.skill()
        with self.app.app_context():
            db = get_db()
            legacy_data = get_skill_case(db, skill_id, case_id)["data"]
            legacy_data.pop("use_as_example")
            db.execute(
                "UPDATE skill_cases SET case_json=? WHERE id=?",
                (json.dumps(legacy_data, ensure_ascii=False), case_id),
            )
            save_case(
                db,
                get_skill(db, skill_id),
                case_data(evidence="编辑后的旧版案例证据"),
                case_id,
            )
            snapshot = check_snapshot(db, get_skill(db, skill_id))
            self.assertEqual(snapshot["example_case_ids"], [case_id])
            self.assertIn("编辑后的旧版案例证据", snapshot["prompt"])

    def test_example_selection_rejects_over_budget_unconfirmed_and_foreign_cases(self):
        skill_id, first_id = self.skill()
        other_skill, foreign_id = self.skill()
        with self.app.app_context():
            db = get_db()
            second_id = save_case(
                db, get_skill(db, skill_id), case_data(title="正例二")
            )
            third_id = save_case(db, get_skill(db, skill_id), case_data(title="正例三"))
            pending_id = save_case(db, get_skill(db, skill_id), case_data(reviewed=""))
            db.commit()
            version = get_skill(db, skill_id)["version"]
        for ids in (
            [first_id, second_id, third_id],
            [pending_id],
            [foreign_id],
            ["invalid"],
        ):
            response = self.client.post(
                f"/admin/rules/skills/{skill_id}",
                data={"action": "select_examples", "example_case_ids": ids},
            )
            self.assertEqual(response.status_code, 400)
            with self.app.app_context():
                self.assertEqual(get_skill(get_db(), skill_id)["version"], version)
                self.assertEqual(
                    check_snapshot(get_db(), get_skill(get_db(), skill_id))[
                        "example_case_ids"
                    ],
                    [first_id],
                )
        self.assertNotEqual(other_skill, skill_id)

    def test_report_source_survives_rule_save_before_case_is_added(self):
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
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}",
            query_string={
                "source_task_id": task_id,
                "source_result_code": "consistency",
                "source_item_id": "consistency-1",
            },
            data={"action": "save_skill", **rule_data(target="更新后的专项目标")},
        )
        self.assertEqual(response.status_code, 302)
        soup = BeautifulSoup(
            self.client.get(response.location).get_data(as_text=True), "html.parser"
        )
        self.assertEqual(
            soup.select_one('#new-case input[name="source_task_id"]')["value"],
            str(task_id),
        )
        self.assertIn(
            "默认超时为 30 s",
            soup.select_one('#new-case textarea[name="evidence"]').text,
        )

    def test_duplicate_validation_submission_reuses_pending_task(self):
        skill_id, case_id = self.skill()
        task_id = self._start_validation(skill_id, case_id)
        self.assertEqual(self._start_validation(skill_id, case_id), task_id)
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            db.execute("UPDATE tasks SET status='canceled' WHERE id=?", (task_id,))
            db.commit()
        self.assertNotEqual(self._start_validation(skill_id, case_id), task_id)

    def test_validation_snapshot_keeps_expectation_out_of_prompt_and_ignores_task_cap(
        self,
    ):
        skill_id, case_id = self.skill()
        expectation = "仅用于复核的唯一期望结果 ABC123"
        with self.app.app_context():
            db = get_db()
            save_case(
                db,
                get_skill(db, skill_id),
                case_data(expected_result=expectation),
                case_id,
            )
            db.commit()
            set_setting("task_report_limit", 1)
        task_id = self._start_validation(skill_id, case_id)
        with self.app.app_context():
            task = (
                get_db()
                .execute("SELECT * FROM tasks WHERE id=?", (task_id,))
                .fetchone()
            )
            snapshot = json.loads(task["checks_snapshot_json"])[0]
            self.assertNotIn(expectation, snapshot["prompt"])
            self.assertEqual(
                snapshot["validation_case"]["expected_result"], expectation
            )
            self.assertEqual(snapshot["task_report_limit"], 0)

    def test_validation_review_records_current_version_and_marks_old_reviews(self):
        skill_id, case_id = self.skill()
        task_id = self._start_validation(skill_id, case_id)
        with self.app.app_context():
            db = get_db()
            version = get_skill(db, skill_id)["version"]
            db.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
            db.commit()
        with patch(
            "app.tasks.runner.run_check",
            return_value=json.dumps(report("result")["structured_report"]),
        ):
            TaskRunner(self.app).run(task_id)
        data = {
            "action": "review_validation",
            "case_id": case_id,
            "validation_task_id": task_id,
            "validation_verdict": "passed",
        }
        response = self.client.post(f"/admin/rules/skills/{skill_id}", data=data)
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            db = get_db()
            review = get_skill_case(db, skill_id, case_id)["data"]["validation_review"]
            self.assertEqual(review["version"], version)
            self.assertEqual(review["reviewer"], "ip:127.0.0.1")
            self.assertEqual(get_skill(db, skill_id)["version"], version)
            save_skill(db, get_skill(db, skill_id), rule_data(target="新版检查目标"))
            db.commit()
        page = self.client.get(f"/admin/rules/skills/{skill_id}").get_data(as_text=True)
        self.assertIn("历史版本复核", page)
        self.assertIn("0 条验证通过", page)
        rejected = self.client.post(f"/admin/rules/skills/{skill_id}", data=data)
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("重新验证", rejected.get_data(as_text=True))

    def test_validation_review_requires_completed_matching_task_and_failure_reason(
        self,
    ):
        skill_id, case_id = self.skill()
        task_id = self._start_validation(skill_id, case_id)
        data = {
            "action": "review_validation",
            "case_id": case_id,
            "validation_task_id": task_id,
            "validation_verdict": "passed",
        }
        for status in ("queued", "running", "partial", "failed", "canceled"):
            with self.app.app_context():
                get_db().execute(
                    "UPDATE tasks SET status=? WHERE id=?", (status, task_id)
                )
                get_db().commit()
            self.assertEqual(
                self.client.post(
                    f"/admin/rules/skills/{skill_id}", data=data
                ).status_code,
                400,
            )
        with self.app.app_context():
            get_db().execute(
                "UPDATE tasks SET status='completed' WHERE id=?", (task_id,)
            )
            get_db().commit()
        self.assertEqual(
            self.client.post(
                f"/admin/rules/skills/{skill_id}",
                data={**data, "validation_verdict": "failed"},
            ).status_code,
            400,
        )
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={
                **data,
                "validation_verdict": "failed",
                "validation_note": "未识别两处默认值冲突",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            db = get_db()
            self.assertEqual(
                get_skill_case(db, skill_id, case_id)["data"]["validation_review"][
                    "verdict"
                ],
                "failed",
            )
            db.execute(
                "UPDATE tasks SET checks_snapshot_json='[]' WHERE id=?", (task_id,)
            )
            db.commit()
        response = self.client.post(f"/admin/rules/skills/{skill_id}", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertIn("不匹配", response.get_data(as_text=True))

    def test_negative_case_can_be_reviewed_as_correctly_released(self):
        skill_id, _ = self.skill()
        with self.app.app_context():
            db = get_db()
            case_id = save_case(
                db,
                get_skill(db, skill_id),
                case_data(
                    case_kind="negative",
                    document_text="默认超时为 30 s；其他模式默认超时为 60 s。",
                    expected_result="不同运行模式应当放行",
                ),
            )
            db.commit()
        task_id = self._start_validation(skill_id, case_id)
        with self.app.app_context():
            get_db().execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
            get_db().commit()
        with patch("app.tasks.runner.run_check", return_value='{"items":[]}'):
            TaskRunner(self.app).run(task_id)
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={
                "action": "review_validation",
                "case_id": case_id,
                "validation_task_id": task_id,
                "validation_verdict": "passed",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(
                get_skill_case(get_db(), skill_id, case_id)["data"][
                    "validation_review"
                ]["verdict"],
                "passed",
            )

    def test_case_edit_and_revalidation_clear_old_verdicts(self):
        skill_id, case_id = self.skill()
        task_id = self._start_validation(skill_id, case_id)
        with self.app.app_context():
            get_db().execute(
                "UPDATE tasks SET status='completed' WHERE id=?", (task_id,)
            )
            get_db().commit()
        self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={
                "action": "review_validation",
                "case_id": case_id,
                "validation_task_id": task_id,
                "validation_verdict": "passed",
            },
        )
        new_task = self._start_validation(skill_id, case_id)
        self.assertNotEqual(new_task, task_id)
        with self.app.app_context():
            db = get_db()
            self.assertNotIn(
                "validation_review", get_skill_case(db, skill_id, case_id)["data"]
            )
            save_case(
                db,
                get_skill(db, skill_id),
                case_data(title="补齐证据后的案例"),
                case_id,
            )
            self.assertIsNone(
                get_skill_case(db, skill_id, case_id)["validation_task_id"]
            )

    def test_validation_review_checks_task_visibility(self):
        skill_id, case_id = self.skill()
        task_id = self._start_validation(skill_id, case_id)
        with self.app.app_context():
            db = get_db()
            register_subject("ip:127.0.0.1")
            replace_subject_permissions("ip:127.0.0.1", {"rules.manage"})
            db.execute(
                "UPDATE tasks SET status='completed', owner_subject='ip:10.0.0.8' WHERE id=?",
                (task_id,),
            )
            db.commit()
        user = self.app.test_client()
        page = user.get(f"/rules/skills/{skill_id}").get_data(as_text=True)
        self.assertNotIn("查看验证报告", page)
        response = user.post(
            f"/rules/skills/{skill_id}",
            data={
                "action": "review_validation",
                "case_id": case_id,
                "validation_task_id": task_id,
                "validation_verdict": "passed",
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_cookie_session_login_is_required_before_skill_mutation(self):
        skill_id, _ = self.skill()
        self.app.config["AUTH"] = {"mode": "cookie_session", "cookie_session": {}}
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}", data={"action": "delete_skill"}
        )
        self.assertEqual(response.status_code, 401)
        with self.app.app_context():
            self.assertIsNotNone(get_skill(get_db(), skill_id))

    def _quick_create(self, **extra):
        response = self.client.post(
            "/admin/rules",
            data={
                "action": "quick_create_skill",
                "evidence": "参数表：默认超时 30 秒。\n附录：默认超时 60 秒。",
                "rationale": "同一设备和运行模式的默认超时应当一致。",
                **extra,
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            item = (
                get_db()
                .execute(
                    "SELECT * FROM check_items WHERE kind='specialist' ORDER BY id DESC"
                )
                .fetchone()
            )
            case = skill_cases(get_db(), item["id"])[-1]
            return item["id"], case["id"]

    def _confirm_quick_case(self, skill_id, case_id, **extra):
        with self.app.app_context():
            version = get_skill(get_db(), skill_id)["version"]
        return self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={
                "action": "confirm_case",
                "version": version,
                "case_id": case_id,
                "confirm_evidence": "on",
                "enable_after_confirm": "on",
                **extra,
            },
        )

    def test_quick_creation_requires_only_original_and_reason_and_stays_draft(self):
        skill_id, case_id = self._quick_create()
        with self.app.app_context():
            db = get_db()
            item = get_skill(db, skill_id)
            case = get_skill_case(db, skill_id, case_id)
            self.assertFalse(item["enabled"])
            self.assertFalse(case["data"]["reviewed"])
            self.assertFalse(case["data"]["use_as_example"])
            self.assertEqual(case["data"]["document_text"], case["data"]["evidence"])
            self.assertEqual(case["data"]["context_scope"], "excerpt")
            self.assertIn("默认超时应当一致", item["prompt"])
            self.assertIn("核对全文", item["prompt"])
            self.assertEqual(case["data"]["corrected_text"], "")
        page = BeautifulSoup(
            self.client.get(f"/admin/rules/skills/{skill_id}").get_data(as_text=True),
            "html.parser",
        )
        self.assertIn("确认并启用此专项", page.select_one("#skill-start").text)
        self.assertFalse(page.select_one("#skill-rule-editor").has_attr("open"))
        self.assertIn("验证这段原文", page.text)

    def test_quick_confirmation_enables_and_preserves_snapshot_and_context_scope(self):
        skill_id, case_id = self._quick_create(
            corrected_text="经产品确认，统一为 30 秒。"
        )
        self.assertEqual(self._confirm_quick_case(skill_id, case_id).status_code, 302)
        with self.app.app_context():
            db = get_db()
            item = get_skill(db, skill_id)
            case = get_skill_case(db, skill_id, case_id)
            self.assertTrue(item["enabled"])
            self.assertTrue(case["data"]["reviewed"])
            self.assertEqual(case["data"]["context_scope"], "excerpt")
            snapshot = _enabled_check_item_snapshots(db, [skill_id], "document_check")[
                0
            ]
            self.assertEqual(snapshot["example_case_ids"], [case_id])
            self.assertIn("统一为 30 秒", snapshot["prompt"])
            version = item["version"]
        self.assertEqual(self._confirm_quick_case(skill_id, case_id).status_code, 302)
        with self.app.app_context():
            self.assertEqual(get_skill(get_db(), skill_id)["version"], version)
        page = self.client.get(f"/admin/rules/skills/{skill_id}").get_data(as_text=True)
        self.assertIn("去检查文档", page)

    def test_quick_confirmation_requires_acknowledgment_and_current_version(self):
        skill_id, case_id = self._quick_create()
        for extra in ({"confirm_evidence": ""}, {"version": ""}, {"version": "0"}):
            self.assertEqual(
                self._confirm_quick_case(skill_id, case_id, **extra).status_code,
                400,
            )
        with self.app.app_context():
            self.assertFalse(get_skill(get_db(), skill_id)["enabled"])
            self.assertFalse(
                get_skill_case(get_db(), skill_id, case_id)["data"]["reviewed"]
            )

    def test_quick_invalid_input_is_retained_without_creating_partial_records(self):
        for extra in ({"evidence": ""}, {"rationale": ""}, {"evidence": "原文" * 4001}):
            data = {
                "action": "quick_create_skill",
                "evidence": "需要保留的原文",
                "rationale": "需要保留的问题说明",
                **extra,
            }
            response = self.client.post("/admin/rules", data=data)
            self.assertEqual(response.status_code, 400)
            form = BeautifulSoup(
                response.get_data(as_text=True), "html.parser"
            ).select_one("#quick-skill-form")
            self.assertEqual(
                form.select_one('textarea[name="evidence"]').text, data["evidence"]
            )
            self.assertEqual(
                form.select_one('textarea[name="rationale"]').text, data["rationale"]
            )
            with self.app.app_context():
                self.assertEqual(
                    get_db()
                    .execute("SELECT COUNT(*) FROM check_items WHERE kind='specialist'")
                    .fetchone()[0],
                    0,
                )
                self.assertEqual(
                    get_db().execute("SELECT COUNT(*) FROM skill_cases").fetchone()[0],
                    0,
                )

    def test_quick_addition_keeps_existing_rule_and_existing_example_budget(self):
        skill_id, first_id = self.skill(enabled=True)
        with self.app.app_context():
            db = get_db()
            second_id = save_case(
                db, get_skill(db, skill_id), case_data(title="第二个已选案例")
            )
            select_skill_examples(db, get_skill(db, skill_id), [first_id, second_id])
            original_definition = get_skill(db, skill_id)["definition_json"]
            db.commit()
        response = self.client.post(
            f"/admin/rules/skills/{skill_id}",
            data={
                "action": "add_quick_case",
                "evidence": "新案例原文",
                "rationale": "新案例理由",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            case_id = skill_cases(get_db(), skill_id)[-1]["id"]
        response = self._confirm_quick_case(skill_id, case_id)
        self.assertEqual(response.status_code, 302)
        page = self.client.get(response.location).get_data(as_text=True)
        self.assertIn("示例已满", page)
        with self.app.app_context():
            db = get_db()
            snapshot = check_snapshot(db, get_skill(db, skill_id))
            self.assertEqual(snapshot["example_case_ids"], [first_id, second_id])
            self.assertEqual(
                get_skill(db, skill_id)["definition_json"], original_definition
            )
            self.assertTrue(get_skill_case(db, skill_id, case_id)["data"]["reviewed"])

    def test_quick_case_can_fill_empty_rule_and_keep_correct_negative_expectation(self):
        with self.app.app_context():
            skill_id = create_skill(get_db(), "默认值冲突")
            get_db().commit()
        url = f"/admin/rules/skills/{skill_id}"
        response = self.client.post(
            url,
            data={"action": "add_quick_case", "evidence": "原文", "rationale": "理由"},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertTrue(get_skill(get_db(), skill_id)["definition_json"])
            case_id = skill_cases(get_db(), skill_id)[-1]["id"]
        self._confirm_quick_case(skill_id, case_id)
        response = self.client.post(
            url,
            data={
                "action": "add_quick_case",
                "case_kind": "negative",
                "evidence": "模式 A 默认 30 秒；模式 B 默认 60 秒。",
                "rationale": "运行模式不同，应当放行。",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            negative = skill_cases(get_db(), skill_id)[-1]
            self.assertIn("应放行", negative["data"]["expected_result"])
        response = self._confirm_quick_case(
            skill_id, negative["id"], enable_after_confirm=""
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIn(
                negative["id"],
                check_snapshot(get_db(), get_skill(get_db(), skill_id))[
                    "example_case_ids"
                ],
            )

    def test_quick_forms_have_two_required_fields_and_optional_more_information(self):
        pages = [self.client.get("/admin/rules")]
        skill_id, _ = self.skill()
        pages.append(self.client.get(f"/admin/rules/skills/{skill_id}"))
        for response, selector in zip(pages, ("#quick-skill-form", "#new-case form")):
            form = BeautifulSoup(
                response.get_data(as_text=True), "html.parser"
            ).select_one(selector)
            self.assertEqual(
                [field["name"] for field in form.select("[required]")],
                ["evidence", "rationale"],
            )
            self.assertFalse(form.select_one("details").has_attr("open"))

    def test_quick_creation_and_confirmation_require_rules_permission(self):
        skill_id, case_id = self._quick_create()
        user = self.app.test_client()
        self.assertEqual(
            user.post(
                "/rules",
                data={
                    "action": "quick_create_skill",
                    "evidence": "原文",
                    "rationale": "理由",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            user.post(
                f"/rules/skills/{skill_id}",
                data={"action": "confirm_case", "case_id": case_id},
            ).status_code,
            403,
        )
        with self.app.app_context():
            register_subject("ip:127.0.0.1")
            replace_subject_permissions("ip:127.0.0.1", {"rules.manage"})
            get_db().commit()
        response = user.post(
            "/rules",
            data={
                "action": "quick_create_skill",
                "evidence": "原文",
                "rationale": "理由",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/rules/skills/", response.location)
        self.assertNotIn("/admin/", response.location)

    def test_report_can_create_quick_skill_with_verified_source_and_full_context(self):
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
        source = {
            "source_task_id": task_id,
            "source_result_code": "consistency",
            "source_item_id": "consistency-1",
        }
        page = self.client.get("/admin/rules", query_string=source)
        form = BeautifulSoup(page.get_data(as_text=True), "html.parser").select_one(
            "#quick-skill-form"
        )
        data = {
            field["name"]: field.get("value", "")
            for field in form.select("input[name]")
        }
        data.update(
            {field["name"]: field.text for field in form.select("textarea[name]")}
        )
        data["rationale"] = "人工确认两处描述相同对象且条件相同，默认值不一致。"
        response = self.client.post("/admin/rules", data=data)
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            case = (
                get_db()
                .execute("SELECT * FROM skill_cases ORDER BY id DESC")
                .fetchone()
            )
            value = json.loads(case["case_json"])
            self.assertEqual(value["source"], source)
            self.assertEqual(value["document_text"], case_data()["document_text"])
            self.assertEqual(value["context_scope"], "provided_text")
            self.assertFalse(value["reviewed"])

    def test_quick_source_access_is_checked_before_creating_rule(self):
        task_id = self.fixture._insert_task(ip="10.0.0.8")
        with self.app.app_context():
            register_subject("ip:127.0.0.1")
            replace_subject_permissions("ip:127.0.0.1", {"rules.manage"})
            get_db().commit()
        response = self.app.test_client().post(
            "/rules",
            data={
                "action": "quick_create_skill",
                "evidence": "原文",
                "rationale": "理由",
                "source_task_id": task_id,
                "source_result_code": "consistency",
                "source_item_id": "consistency-1",
            },
        )
        self.assertEqual(response.status_code, 404)
        with self.app.app_context():
            self.assertEqual(
                get_db()
                .execute("SELECT COUNT(*) FROM check_items WHERE kind='specialist'")
                .fetchone()[0],
                0,
            )

    def test_validation_transaction_failure_removes_task_and_uploaded_text(self):
        skill_id, case_id = self.skill()
        model_id = self.fixture._configure_provider()
        with self.app.app_context():
            get_db().execute("""CREATE TRIGGER reject_case_validation
                BEFORE UPDATE OF validation_task_id ON skill_cases
                BEGIN SELECT RAISE(FAIL, 'validation link failed'); END""")
            get_db().commit()
        with patch.object(self.app.logger, "error"):
            response = self.client.post(
                f"/admin/rules/skills/{skill_id}",
                data={
                    "action": "validate_case",
                    "case_id": case_id,
                    "model_id": model_id,
                },
            )
        self.assertEqual(response.status_code, 500)
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertIsNone(
                get_skill_case(db, skill_id, case_id)["validation_task_id"]
            )
        self.assertFalse(
            any(
                path.is_file()
                for path in Path(self.app.config["UPLOAD_FOLDER"]).rglob("*")
            )
        )


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

    def test_spaces_inside_evidence_values_remain_significant(self):
        results = [report("general"), report("skill", kind="specialist", version=1)]
        results[0]["structured_report"]["items"][0]["excerpt"] = "分组数值为 1 23"
        results[1]["structured_report"]["items"][0]["excerpt"] = "分组数值为 12 3"
        self.assertEqual(_report_item_totals(self.prepare(results))["total"], 2)

    def test_different_rejection_reasons_and_notes_are_kept_separate(self):
        results = [
            report(
                "general",
                item_acceptances={
                    "general-1": {
                        "status": "rejected",
                        "rejection_reason": "classification_inaccurate",
                        "rejection_note": "判定类型错误",
                    }
                },
            ),
            report(
                "skill",
                kind="specialist",
                version=1,
                item_acceptances={
                    "skill-1": {
                        "status": "rejected",
                        "rejection_reason": "not_applicable",
                        "rejection_note": "当前模式不适用",
                    }
                },
            ),
        ]
        self.assertEqual(_report_item_totals(self.prepare(results))["total"], 2)
