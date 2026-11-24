import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bs4 import BeautifulSoup
from flask import Flask

from app.contracts.check_profiles import task_check_profile
from app.persistence.check_profiles import copy_check_profile
from app.persistence.connection import get_db
from app.persistence.defaults import reset_default_check_item_prompt, seed_defaults
from app.persistence.schema import init_db
from app.reporting.service import (
    _maybe_create_report_suppression_candidate,
    _raw_task_results,
    _result_report_items,
    _task_results,
)
from app.reporting.statistics import _task_report_stat_rows_for_where
from app.tasks.selection import selected_check_items
from tests import test_routes as fixtures


class CheckProfilesTest(unittest.TestCase):
    setUp = fixtures.AdminSettingsRouteTest.setUp
    tearDown = fixtures.AdminSettingsRouteTest.tearDown
    _configure_provider = fixtures.AdminSettingsRouteTest._configure_provider
    _insert_report_task = fixtures.AdminSettingsRouteTest._insert_report_task

    def check(self, profile="software", code="compliance"):
        with self.app.app_context():
            return dict(
                get_db()
                .execute(
                    "SELECT * FROM check_items WHERE profile_id = ? AND code = ?",
                    (profile, code),
                )
                .fetchone()
            )

    def submit(self, profile="software", check_ids=None):
        model = self._configure_provider()
        return self.client.post(
            "/",
            data={
                "model_id": model,
                "profile_id": profile,
                "checks": [str(value) for value in (check_ids or [self.check()["id"]])],
                "document": (io.BytesIO("软件接口文档".encode()), "software.txt"),
            },
        )

    def test_rules_page_is_separate_and_profiles_have_distinct_prompts(self):
        settings = BeautifulSoup(self.client.get("/admin/settings").data, "html.parser")
        self.assertIsNone(settings.select_one("[data-prompt-tabs]"))
        self.assertIsNone(settings.select_one("[data-report-suppression-rules]"))
        self.assertIsNotNone(settings.select_one('[name="global_concurrency"]'))
        response = self.client.get("/admin/rules?profile_id=software")
        self.assertEqual(response.status_code, 200)
        rules = BeautifulSoup(response.data, "html.parser")
        self.assertIsNotNone(rules.select_one("[data-prompt-tabs]"))
        self.assertIsNone(rules.select_one('[name="global_concurrency"]'))
        self.assertIn("软件文档方案", rules.get_text())
        self.assertNotEqual(self.check()["prompt"], self.check("default")["prompt"])

    def test_profile_picker_only_exposes_available_check_metadata(self):
        self._configure_provider()
        response = self.client.get("/?profile_id=software")
        soup = BeautifulSoup(response.data, "html.parser")
        profiles = json.loads(soup.select_one("[data-check-profiles]").string)
        self.assertEqual([p["id"] for p in profiles], ["default", "software"])
        self.assertNotIn("prompt", profiles[1]["checks"][0])
        ids = {int(node["value"]) for node in soup.select('[name="checks"]')}
        self.assertIn(self.check()["id"], ids)
        self.assertNotIn(self.check("default")["id"], ids)

    def test_submission_freezes_profile_and_prompt_for_retry_and_export(self):
        original = self.check()
        self.assertEqual(self.submit().status_code, 302)
        with self.app.app_context():
            db = get_db()
            task = dict(db.execute("SELECT * FROM tasks").fetchone())
            snapshot = task_check_profile(task)
            self.assertEqual(snapshot["id"], "software")
            self.assertIsInstance(snapshot["revision"], int)
            db.execute(
                "UPDATE check_items SET prompt = '新提示词' WHERE id = ?",
                (original["id"],),
            )
            db.execute(
                "UPDATE check_profiles SET name = '修改后的方案', enabled = 0 WHERE id = 'software'"
            )
            db.commit()
            task["retry_check_codes_json"] = '["compliance"]'
            self.assertEqual(
                selected_check_items(db, task)[0]["prompt"], original["prompt"]
            )
            self.assertEqual(task_check_profile(task), snapshot)
        page = self.client.get(f"/admin/tasks/{task['id']}").get_data(as_text=True)
        self.assertIn("软件文档方案", page)
        self.assertNotIn("修改后的方案", page)

    def test_rejects_disabled_unknown_and_mixed_profile_submissions(self):
        own, other = self.check()["id"], self.check("default")["id"]
        for profile, ids in (
            ("software", [other]),
            ("software", [own, other]),
            ("missing", [own]),
        ):
            self.submit(profile, ids)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE check_profiles SET enabled = 0 WHERE id = 'software'")
            db.commit()
        self.submit()
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0
            )
        self.assertEqual(
            list(Path(self.app.config["UPLOAD_FOLDER"]).rglob("*.txt")), []
        )

    def test_editing_and_reordering_enforce_profile_scope(self):
        item = self.check()
        data = {
            "action": "prompt",
            "profile_id": "default",
            "item_id": item["id"],
            "name": "错误修改",
            "prompt": "错误提示词",
            "enabled": "on",
        }
        self.assertEqual(self.client.post("/admin/rules", data=data).status_code, 400)
        self.assertEqual(
            self.client.post(
                "/admin/rules",
                data={
                    "action": "reorder_check_items",
                    "profile_id": "default",
                    "item_ids": [item["id"]],
                },
            ).status_code,
            400,
        )
        data.update(
            profile_id="software",
            name=item["name"],
            prompt="软件专属规则",
            default_selected="off",
        )
        response = self.client.post("/admin/rules", data=data)
        self.assertEqual(response.status_code, 302)
        self.assertIn("profile_id=software", response.location)
        self.assertEqual(self.check()["prompt"], "软件专属规则")
        self.assertEqual(self.check()["default_selected"], 0)
        self.assertNotEqual(self.check("default")["prompt"], "软件专属规则")

    def test_copy_is_independent_and_keeps_software_reset_template(self):
        with self.app.app_context():
            db = get_db()
            profile_id = copy_check_profile("software", "软件接口方案")
            db.commit()
            item = db.execute(
                "SELECT * FROM check_items WHERE profile_id = ? AND code = 'compliance'",
                (profile_id,),
            ).fetchone()
            db.execute(
                "UPDATE check_items SET prompt = '自定义' WHERE id = ?", (item["id"],)
            )
            self.assertTrue(reset_default_check_item_prompt(item["id"]))
            restored = db.execute(
                "SELECT prompt FROM check_items WHERE id = ?", (item["id"],)
            ).fetchone()[0]
            self.assertEqual(restored, item["prompt"])

    def test_scope_isolates_suppression_candidates_reports_and_statistics(self):
        ids = [self._insert_report_task(), self._insert_report_task()]
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE tasks SET check_profile_id = 'software' WHERE id = ?", (ids[1],)
            )
            rule_ids = []
            for task_id in ids:
                task = db.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                result = _raw_task_results(task)[0]
                item = _result_report_items(result)[0]
                rule_id = _maybe_create_report_suppression_candidate(
                    db,
                    task=task,
                    result_code=result["code"],
                    result=result,
                    item_id=item["id"],
                    acceptance_status="rejected",
                    rejection_reason="model_error",
                    rejection_note="已核实",
                )
                rule_ids.append(rule_id)
            self.assertNotEqual(*rule_ids)
            # Only the default profile has an enabled, confirmed ignore rule.
            fixtures._insert_suppression_review_fixture(db, rule_ids[0])
            db.execute(
                "UPDATE report_suppression_rules SET enabled = 1 WHERE id = ?",
                (rule_ids[0],),
            )
            db.commit()
            for task_id, expected in zip(ids, (1, 0)):
                task = db.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                results = _task_results(task)
                self.assertEqual(len(results[0]["suppressed_report_items"]), expected)
            rows = _task_report_stat_rows_for_where("t.id IN (?, ?)", tuple(ids))
            self.assertEqual(
                {row["id"]: row["suppressed"] for row in rows}, {ids[0]: 1, ids[1]: 0}
            )
        response = self.client.get("/admin/rules?profile_id=software")
        soup = BeautifulSoup(response.data, "html.parser")
        self.assertIsNone(soup.select_one(f'[name="rule_id"][value="{rule_ids[0]}"]'))
        self.assertEqual(
            self.client.post(
                "/admin/rules",
                data={
                    "action": "report_suppression_rule",
                    "profile_id": "software",
                    "rule_id": rule_ids[0],
                    "operation": "delete",
                },
            ).status_code,
            400,
        )

    def test_init_is_idempotent_and_preserves_customized_software_prompt(self):
        item = self.check()
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE check_items SET prompt = '保留软件规则', enabled = 0 WHERE id = ?",
                (item["id"],),
            )
            db.commit()
            before = [
                tuple(row)
                for row in db.execute("SELECT * FROM check_items ORDER BY id")
            ]
            init_db()
            seed_defaults()
            self.assertEqual(
                before,
                [
                    tuple(row)
                    for row in db.execute("SELECT * FROM check_items ORDER BY id")
                ],
            )


class CheckProfileMigrationTest(unittest.TestCase):
    def test_existing_ids_and_prompts_survive_scoped_schema_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "legacy.sqlite3")
            with closing(sqlite3.connect(database)) as db:
                db.execute("""CREATE TABLE check_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_type TEXT NOT NULL DEFAULT 'document_check',
                    code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, description TEXT, prompt TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                db.execute(
                    "INSERT INTO check_items(id, code, name, prompt, created_at, updated_at) VALUES (77, 'compliance', '已有检查', '用户自定义提示词', '2026-01-01', '2026-01-01')"
                )
                db.execute(
                    "INSERT INTO check_items(id, code, name, prompt, created_at, updated_at) VALUES (9999, 'removed', '已删除检查', '临时', '2026-01-01', '2026-01-01')"
                )
                db.execute("DELETE FROM check_items WHERE id = 9999")
                db.commit()
            app = Flask(__name__)
            app.config["DATABASE"] = database
            with app.app_context():
                init_db()
                seed_defaults()
                db = get_db()
                row = db.execute("SELECT * FROM check_items WHERE id = 77").fetchone()
                self.assertEqual(row["profile_id"], "default")
                self.assertEqual(row["prompt"], "用户自定义提示词")
                self.assertGreater(
                    db.execute(
                        "SELECT MIN(id) FROM check_items WHERE id != 77"
                    ).fetchone()[0],
                    9999,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM check_items WHERE code = 'compliance'"
                    ).fetchone()[0],
                    2,
                )
