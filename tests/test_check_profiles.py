import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bs4 import BeautifulSoup
from flask import Flask
from openpyxl import load_workbook

from app.checks.catalog import get_enabled_check_items
from app.contracts.check_profiles import task_check_profile
from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.persistence.check_item_pool import (
    profile_check_ids,
    save_profile_check_items,
)
from app.persistence.check_profiles import copy_check_profile
from app.persistence.connection import get_db
from app.persistence.defaults import reset_default_check_item_prompt, seed_defaults
from app.persistence.schema import init_db
from app.reporting.excel import build_report_workbook
from app.reporting.service import (
    _maybe_create_report_suppression_candidate,
    _raw_task_results,
    _result_report_items,
    _task_results,
)
from app.reporting.statistics import _task_report_stat_rows_for_where
from app.tasks.selection import selected_check_items
from app.tasks.submission import _enabled_check_item_snapshots
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
                    "SELECT c.* FROM check_items c JOIN check_profile_items m ON m.check_item_id = c.id WHERE m.profile_id = ? AND (c.template_code = ? OR c.code = ?)",
                    (profile, code, code),
                )
                .fetchone()
            )

    def submit(self, profile="software", check_ids=None):
        model = self._configure_provider()
        if check_ids is None:
            with self.app.app_context():
                check_ids = [
                    item["id"]
                    for item in get_enabled_check_items(DOCUMENT_TASK_TYPE, profile)
                ]
        return self.client.post(
            "/",
            data={
                "model_id": model,
                "profile_id": profile,
                "checks": [str(value) for value in check_ids],
                "document": (io.BytesIO("软件接口文档".encode()), "software.txt"),
            },
        )

    def test_rules_page_is_separate_and_profiles_have_distinct_prompts(self):
        settings = BeautifulSoup(self.client.get("/admin/settings").data, "html.parser")
        self.assertEqual(
            {
                panel["data-prompt-panel"]
                for panel in settings.select("[data-prompt-panel]")
            },
            {
                CONSISTENCY_TASK_TYPE,
                LANGUAGE_CONSISTENCY_TASK_TYPE,
                IMAGE_TASK_TYPE,
                VIDEO_TASK_TYPE,
            },
        )
        self.assertIsNone(settings.select_one('select[name="profile_id"]'))
        self.assertIsNotNone(settings.select_one("[data-report-suppression-rules]"))
        self.assertIsNotNone(settings.select_one('[name="global_concurrency"]'))
        response = self.client.get("/admin/rules?profile_id=software")
        self.assertEqual(response.status_code, 200)
        rules = BeautifulSoup(response.data, "html.parser")
        self.assertIsNotNone(rules.select_one("[data-profile-composition]"))
        self.assertIsNone(rules.select_one("[data-prompt-tabs]"))
        rules = BeautifulSoup(
            self.client.get("/admin/rules?tab=items&profile_id=software").data,
            "html.parser",
        )
        self.assertEqual(
            [
                panel["data-prompt-panel"]
                for panel in rules.select("[data-prompt-panel]")
            ],
            [DOCUMENT_TASK_TYPE],
        )
        self.assertEqual(rules.select("[data-prompt-tab]"), [])
        self.assertIsNone(rules.select_one('[name="global_concurrency"]'))
        self.assertIn("软件文档方案", rules.get_text())
        self.assertNotEqual(self.check()["prompt"], self.check("default")["prompt"])

    def test_other_task_forms_use_system_checks_even_when_document_profiles_are_disabled(
        self,
    ):
        self._configure_provider()
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE check_profiles SET enabled = 0")
            db.commit()
            expected = {
                task_type: {
                    str(row["id"])
                    for row in db.execute(
                        "SELECT id FROM check_items WHERE profile_id = 'default' AND task_type = ? AND enabled = 1",
                        (task_type,),
                    )
                }
                for task_type in (
                    CONSISTENCY_TASK_TYPE,
                    LANGUAGE_CONSISTENCY_TASK_TYPE,
                    IMAGE_TASK_TYPE,
                    VIDEO_TASK_TYPE,
                )
            }
        for path, task_type in (
            ("/consistency", CONSISTENCY_TASK_TYPE),
            ("/language-consistency", LANGUAGE_CONSISTENCY_TASK_TYPE),
            ("/images", IMAGE_TASK_TYPE),
            ("/videos", VIDEO_TASK_TYPE),
        ):
            for prefix in ("", "/admin"):
                with self.subTest(path=prefix + path):
                    response = self.client.get(prefix + path + "?profile_id=software")
                    self.assertEqual(response.status_code, 200)
                    soup = BeautifulSoup(response.data, "html.parser")
                    self.assertIsNone(soup.select_one('[name="profile_id"]'))
                    self.assertIsNone(soup.select_one("[data-check-profiles]"))
                    self.assertEqual(
                        {node["value"] for node in soup.select('[name="checks"]')},
                        expected[task_type],
                    )
            with self.app.app_context():
                ids = [int(value) for value in expected[task_type]]
                snapshots = _enabled_check_item_snapshots(get_db(), ids, task_type)
                self.assertEqual(
                    {str(item["id"]) for item in snapshots}, expected[task_type]
                )
                self.assertTrue(all("profile" not in item for item in snapshots))
                self.assertEqual(
                    _enabled_check_item_snapshots(get_db(), ids, task_type, "software"),
                    [],
                )

    def test_pages_reject_check_operations_outside_their_task_scope(self):
        with self.app.app_context():
            image = dict(
                get_db()
                .execute(
                    "SELECT * FROM check_items WHERE task_type = ? AND profile_id = 'default' LIMIT 1",
                    (IMAGE_TASK_TYPE,),
                )
                .fetchone()
            )
        document = self.check("default")
        for path, item in (("/admin/rules", image), ("/admin/settings", document)):
            for action in (
                "prompt",
                "delete_check_item",
                "reorder_check_items",
                "create_check_item",
            ):
                with self.subTest(path=path, action=action):
                    response = self.client.post(
                        path,
                        data={
                            "action": action,
                            "profile_id": "default",
                            "task_type": item["task_type"],
                            "item_id": item["id"],
                            "item_ids": [item["id"]],
                            "name": "越界检查项",
                            "prompt": "越界提示词",
                        },
                    )
                    self.assertEqual(response.status_code, 400)
        for action in ("save_profile", "copy_profile"):
            self.assertEqual(
                self.client.post(
                    "/admin/settings", data={"action": action, "name": "越界方案"}
                ).status_code,
                400,
            )

    def test_system_check_edits_do_not_change_document_profile_revision(self):
        with self.app.app_context():
            db = get_db()
            image = dict(
                db.execute(
                    "SELECT * FROM check_items WHERE task_type = ? AND profile_id = 'default' LIMIT 1",
                    (IMAGE_TASK_TYPE,),
                ).fetchone()
            )
            revision = db.execute(
                "SELECT revision FROM check_profiles WHERE id = 'default'"
            ).fetchone()[0]
        response = self.client.post(
            "/admin/settings",
            data={
                "action": "prompt",
                "item_id": image["id"],
                "name": image["name"],
                "prompt": "系统图片提示词",
                "enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin/settings"))
        with self.app.app_context():
            db = get_db()
            self.assertEqual(
                db.execute(
                    "SELECT prompt FROM check_items WHERE id = ?", (image["id"],)
                ).fetchone()[0],
                "系统图片提示词",
            )
            self.assertEqual(
                db.execute(
                    "SELECT revision FROM check_profiles WHERE id = 'default'"
                ).fetchone()[0],
                revision,
            )
            db.execute(
                "UPDATE check_items SET prompt = '单文档提示词' WHERE profile_id = 'default' AND code = 'compliance'"
            )
            self.assertEqual(
                db.execute(
                    "SELECT revision FROM check_profiles WHERE id = 'default'"
                ).fetchone()[0],
                revision + 1,
            )

    def test_copy_of_default_profile_contains_only_document_checks(self):
        with self.app.app_context():
            db = get_db()
            copied = copy_check_profile("default", "单文档副本")
            self.assertEqual(profile_check_ids(copied), profile_check_ids("default"))
            types = {
                row[0]
                for row in db.execute(
                    "SELECT c.task_type FROM check_items c JOIN check_profile_items m ON m.check_item_id = c.id WHERE m.profile_id = ?",
                    (copied,),
                )
            }
            self.assertEqual(types, {DOCUMENT_TASK_TYPE})

    def test_other_task_reports_do_not_display_document_profile_metadata(self):
        task_id = self._insert_report_task()
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE tasks SET task_type = ? WHERE id = ?",
                (IMAGE_TASK_TYPE, task_id),
            )
            db.commit()
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            self.assertIsNone(task_check_profile(task))
            workbook = load_workbook(build_report_workbook(task), read_only=True)
            self.assertNotIn("检查方案", workbook.sheetnames)
            workbook.close()
        response = self.client.get(f"/admin/tasks/{task_id}")
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.data, "html.parser")
        self.assertNotIn(
            "检查方案",
            [node.get_text(strip=True) for node in soup.select(".report-meta-list dt")],
        )

    def test_suppression_management_is_split_between_documents_and_other_tasks(self):
        task_ids = [self._insert_report_task(), self._insert_report_task()]
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE tasks SET task_type = ? WHERE id = ?",
                (CONSISTENCY_TASK_TYPE, task_ids[1]),
            )
            rule_ids = []
            for task_id in task_ids:
                task = db.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                result = _raw_task_results(task)[0]
                item = _result_report_items(result)[0]
                rule_ids.append(
                    _maybe_create_report_suppression_candidate(
                        db,
                        task=task,
                        result_code=result["code"],
                        result=result,
                        item_id=item["id"],
                        acceptance_status="rejected",
                        rejection_reason="model_error",
                        rejection_note="已核实",
                    )
                )
            db.commit()
        for path, own, other in (
            ("/admin/rules", rule_ids[0], rule_ids[1]),
            ("/admin/settings", rule_ids[1], rule_ids[0]),
        ):
            soup = BeautifulSoup(self.client.get(path).data, "html.parser")
            self.assertIsNotNone(soup.select_one(f'[name="rule_id"][value="{own}"]'))
            self.assertIsNone(soup.select_one(f'[name="rule_id"][value="{other}"]'))
            data = {
                "action": "report_suppression_rule",
                "rule_id": other,
                "operation": "delete",
            }
            self.assertEqual(self.client.post(path, data=data).status_code, 400)
            data["rule_id"] = own
            response = self.client.post(path, data=data)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.startswith(path))

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
            task["retry_check_codes_json"] = json.dumps([original["code"]])
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

    def test_pool_edits_update_all_referencing_profiles(self):
        item = self.check()
        with self.app.app_context():
            db = get_db()
            save_profile_check_items(
                "default", profile_check_ids("default") | {item["id"]}
            )
            db.commit()
            before = {
                row["id"]: row["revision"]
                for row in db.execute("SELECT id, revision FROM check_profiles")
            }
        response = self.client.post(
            "/admin/rules?tab=items",
            data={
                "action": "prompt",
                "profile_id": "default",
                "item_id": item["id"],
                "name": item["name"],
                "prompt": "共用软件规则",
                "enabled": "on",
                "tab": "items",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("tab=items", response.location)
        self.assertEqual(self.check()["prompt"], "共用软件规则")
        self.assertNotEqual(self.check("default")["prompt"], "共用软件规则")
        with self.app.app_context():
            for row in get_db().execute("SELECT id, revision FROM check_profiles"):
                self.assertGreater(row["revision"], before[row["id"]])

    def test_copy_reuses_pool_items_and_keeps_software_reset_template(self):
        with self.app.app_context():
            db = get_db()
            original_ids = profile_check_ids("software")
            count = db.execute("SELECT COUNT(*) FROM check_items").fetchone()[0]
            profile_id = copy_check_profile("software", "软件接口方案")
            self.assertEqual(profile_check_ids(profile_id), original_ids)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM check_items").fetchone()[0], count
            )
            item = dict(
                db.execute(
                    "SELECT * FROM check_items WHERE profile_id = 'software' AND template_code = 'compliance'"
                ).fetchone()
            )
            save_profile_check_items(profile_id, [item["id"]])
            self.assertEqual(profile_check_ids("software"), original_ids)
            db.execute(
                "UPDATE check_items SET prompt = '自定义' WHERE id = ?", (item["id"],)
            )
            self.assertTrue(reset_default_check_item_prompt(item["id"]))
            self.assertEqual(
                db.execute(
                    "SELECT prompt FROM check_items WHERE id = ?", (item["id"],)
                ).fetchone()[0],
                item["prompt"],
            )

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
                        "SELECT COUNT(*) FROM check_items WHERE template_code = 'compliance' AND pool_item_id = id"
                    ).fetchone()[0],
                    2,
                )
