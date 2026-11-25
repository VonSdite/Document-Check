import io
import json
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from app.contracts.task_types import DOCUMENT_TASK_TYPE
from app.persistence.check_item_pool import (
    document_check_pool,
    profile_check_ids,
    save_profile_check_items,
    seed_check_item_pool,
)
from app.persistence.connection import get_db
from app.reporting.service import (
    _maybe_create_report_suppression_candidate,
    _raw_task_results,
    _result_report_items,
    _task_results,
)
from app.reporting.statistics import _task_report_stat_rows_for_where
from app.tasks.selection import selected_check_items
from app.tasks.submission import _enabled_check_item_snapshots
from tests import test_check_profiles as profiles
from tests import test_routes as fixtures


class CheckItemPoolTest(unittest.TestCase):
    setUp = fixtures.AdminSettingsRouteTest.setUp
    tearDown = fixtures.AdminSettingsRouteTest.tearDown
    _configure_provider = fixtures.AdminSettingsRouteTest._configure_provider
    check = profiles.CheckProfilesTest.check
    submit = profiles.CheckProfilesTest.submit

    def test_local_checks_are_shared_and_scheme_memberships_are_independent(self):
        for code in ("sensitive-terms", "common-terms", "hyperlink-validity"):
            self.assertEqual(
                self.check("default", code)["id"], self.check("software", code)["id"]
            )
        software = self.check()
        general = self.check("default")
        with self.app.app_context():
            other_ids = profile_check_ids("default")
        response = self.client.post(
            "/admin/rules",
            data={
                "action": "save_profile",
                "save_checks": "1",
                "profile_id": "software",
                "name": "软件组合",
                "enabled": "on",
                "profile_checks": [general["id"], software["id"]],
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(
                profile_check_ids("software"), {general["id"], software["id"]}
            )
            self.assertEqual(profile_check_ids("default"), other_ids)
        self.submit()
        with self.app.app_context():
            db = get_db()
            task = dict(db.execute("SELECT * FROM tasks").fetchone())
            snapshots = json.loads(task["checks_snapshot_json"])
            self.assertEqual(
                {item["code"] for item in snapshots},
                {general["code"], software["code"]},
            )
            self.assertEqual(len({item["code"] for item in snapshots}), 2)
            self.assertEqual(
                {item["prompt"] for item in snapshots},
                {general["prompt"], software["prompt"]},
            )
            save_profile_check_items("software", [software["id"]])
            db.execute(
                "UPDATE check_items SET prompt = '修改后的提示词' WHERE id = ?",
                (general["id"],),
            )
            self.assertEqual(
                {item["prompt"] for item in selected_check_items(db, task)},
                {general["prompt"], software["prompt"]},
            )

    def test_membership_rejects_invalid_items_and_rolls_back_metadata_errors(self):
        with self.app.app_context():
            before = profile_check_ids("software")
            image_id = (
                get_db()
                .execute(
                    "SELECT id FROM check_items WHERE task_type = 'image_check' LIMIT 1"
                )
                .fetchone()[0]
            )
        for ids, name in (
            ([image_id], "错误方案"),
            ([9999999], "错误方案"),
            (["invalid"], "错误方案"),
            ([], ""),
        ):
            self.client.post(
                "/admin/rules",
                data={
                    "action": "save_profile",
                    "save_checks": "1",
                    "profile_id": "software",
                    "name": name,
                    "profile_checks": ids,
                    "enabled": "on",
                },
            )
            with self.app.app_context():
                self.assertEqual(profile_check_ids("software"), before)

    def test_submission_requires_exact_scheme_items_even_without_profile_field(self):
        with self.app.app_context():
            expected = profile_check_ids("software")
        self.submit(check_ids=list(expected)[1:])
        self.submit(check_ids=[*expected, self.check("default")["id"]])
        self.client.post(
            "/",
            data={
                "model_id": self._configure_provider(),
                "checks": [str(self.check("default")["id"])],
                "document": (io.BytesIO(b"document"), "doc.txt"),
            },
        )
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0
            )
        self.submit()
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1
            )

    def test_pool_creation_and_deletion_manage_references_without_changing_snapshots(
        self,
    ):
        self.client.post(
            "/admin/rules?tab=items",
            data={
                "action": "create_check_item",
                "profile_id": "software",
                "task_type": DOCUMENT_TASK_TYPE,
                "name": "通用扩展规则",
                "prompt": "核对产品名",
                "enabled": "on",
            },
        )
        with self.app.app_context():
            db = get_db()
            item = db.execute(
                "SELECT * FROM check_items WHERE name = '通用扩展规则'"
            ).fetchone()
            self.assertIsNotNone(item)
            item_id = item["id"]
            self.assertNotIn(item_id, profile_check_ids("software"))
            for profile in ("default", "software"):
                save_profile_check_items(profile, [item_id])
            db.commit()
        self.submit()
        with self.app.app_context():
            db = get_db()
            before = {
                row["id"]: row["revision"]
                for row in db.execute("SELECT * FROM check_profiles")
            }
            task = dict(db.execute("SELECT * FROM tasks").fetchone())
        self.client.post(
            "/admin/rules?tab=items",
            data={"action": "delete_check_item", "item_id": item_id},
        )
        with self.app.app_context():
            db = get_db()
            for profile in ("default", "software"):
                self.assertEqual(profile_check_ids(profile), set())
                self.assertGreater(
                    db.execute(
                        "SELECT revision FROM check_profiles WHERE id = ?", (profile,)
                    ).fetchone()[0],
                    before[profile],
                )
            self.assertEqual(selected_check_items(db, task)[0]["prompt"], "核对产品名")

    def test_submission_page_has_readonly_checks_and_disabled_items_are_skipped(self):
        item = self.check()
        with self.app.app_context():
            get_db().execute(
                "UPDATE check_items SET enabled = 0 WHERE id = ?", (item["id"],)
            )
            get_db().commit()
        self.submit()
        with self.app.app_context():
            task = get_db().execute("SELECT * FROM tasks").fetchone()
            self.assertNotIn(item["id"], json.loads(task["checks_json"]))
            self.assertIn(item["id"], profile_check_ids("software"))
        soup = BeautifulSoup(
            self.client.get("/?profile_id=software").data, "html.parser"
        )
        checkboxes = soup.select('input[type="checkbox"][name="checks"]')
        hidden = soup.select('input[type="hidden"][name="checks"]')
        self.assertTrue(checkboxes)
        self.assertTrue(
            all(
                node.has_attr("checked") and node.has_attr("disabled")
                for node in checkboxes
            )
        )
        self.assertEqual(
            {node["value"] for node in checkboxes}, {node["value"] for node in hidden}
        )


class CheckItemPoolMigrationTest(unittest.TestCase):
    tearDown = fixtures.AdminSettingsRouteTest.tearDown
    _insert_report_task = fixtures.AdminSettingsRouteTest._insert_report_task

    def setUp(self):
        with patch("app.persistence.check_item_pool.seed_check_item_pool"):
            fixtures.AdminSettingsRouteTest.setUp(self)

    def test_migration_preserves_custom_prompts_ids_memberships_and_local_execution(
        self,
    ):
        with self.app.app_context():
            db = get_db()
            local = dict(
                db.execute(
                    "SELECT * FROM check_items WHERE profile_id = 'software' AND code = 'common-terms'"
                ).fetchone()
            )
            db.execute(
                "UPDATE check_items SET prompt = '软件词表提示', default_selected = 1 WHERE id = ?",
                (local["id"],),
            )
            db.execute(
                "UPDATE check_items SET default_selected = 0 WHERE profile_id = 'software' AND code = 'compliance'"
            )
            ids = {row["id"] for row in db.execute("SELECT id FROM check_items")}
            seed_check_item_pool(db)
            self.assertEqual(
                {row["id"] for row in db.execute("SELECT id FROM check_items")}, ids
            )
            new_local = db.execute(
                "SELECT * FROM check_items WHERE id = ?", (local["id"],)
            ).fetchone()
            self.assertEqual(new_local["prompt"], "软件词表提示")
            self.assertNotEqual(new_local["code"], local["code"])
            snapshots = _enabled_check_item_snapshots(
                db, [local["id"]], DOCUMENT_TASK_TYPE, "software"
            )
            self.assertEqual(snapshots[0]["executor_code"], "common-terms")
            selected = selected_check_items(
                db, {"checks_snapshot_json": json.dumps(snapshots), "checks_json": "[]"}
            )
            self.assertEqual(selected[0]["executor_code"], "common-terms")
            compliance_id = db.execute(
                "SELECT check_item_id FROM check_item_aliases WHERE profile_id = 'software' AND code = 'compliance'"
            ).fetchone()[0]
            self.assertNotIn(compliance_id, profile_check_ids("software"))
            before = [dict(row) for row in document_check_pool(db)]
            memberships = profile_check_ids("software")
            seed_check_item_pool(db)
            self.assertEqual([dict(row) for row in document_check_pool(db)], before)
            self.assertEqual(profile_check_ids("software"), memberships)

    def test_migrated_suppression_matches_old_and_new_tasks_in_its_scheme_only(self):
        task_ids = [self._insert_report_task() for _ in range(3)]
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE tasks SET check_profile_id = 'software', check_profile_snapshot_json = ? WHERE id IN (?, ?)",
                ('{"id":"software"}', *task_ids[:2]),
            )
            task = dict(
                db.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_ids[0],)
                ).fetchone()
            )
            result = _raw_task_results(task)[0]
            item = _result_report_items(result)[0]

            def candidate(source, output):
                return _maybe_create_report_suppression_candidate(
                    db,
                    task=source,
                    result_code=output["code"],
                    result=output,
                    item_id=_result_report_items(output)[0]["id"],
                    acceptance_status="rejected",
                    rejection_reason="model_error",
                    rejection_note="已核实",
                )

            rule_id = candidate(task, result)
            fixtures._insert_suppression_review_fixture(db, rule_id)
            db.execute(
                "UPDATE report_suppression_rules SET enabled = 1 WHERE id = ?",
                (rule_id,),
            )
            original_rule = dict(
                db.execute(
                    "SELECT * FROM report_suppression_rules WHERE id = ?", (rule_id,)
                ).fetchone()
            )
            seed_check_item_pool(db)
            canonical = db.execute(
                "SELECT canonical_code FROM check_item_aliases WHERE profile_id = 'software' AND code = 'consistency'"
            ).fetchone()[0]
            migrated = dict(
                db.execute(
                    "SELECT * FROM report_suppression_rules WHERE id = ?", (rule_id,)
                ).fetchone()
            )
            self.assertEqual(migrated["check_code"], canonical)
            self.assertEqual(migrated["fingerprint"], original_rule["fingerprint"])
            self.assertEqual(migrated["source_result_code"], "consistency")
            self.assertEqual(migrated["source_item_id"], item["id"])
            new_result = {**result, "code": canonical}
            db.execute(
                "UPDATE tasks SET result_json = ?, check_profile_snapshot_json = ? WHERE id = ?",
                (
                    json.dumps([new_result]),
                    '{"id":"software","checks_format":2}',
                    task_ids[1],
                ),
            )
            db.commit()
            for task_id, expected in zip(task_ids, (1, 1, 0)):
                saved = dict(
                    db.execute(
                        "SELECT * FROM tasks WHERE id = ?", (task_id,)
                    ).fetchone()
                )
                self.assertEqual(
                    len(_task_results(saved)[0]["suppressed_report_items"]), expected
                )
                if expected:
                    self.assertEqual(
                        candidate(saved, _raw_task_results(saved)[0]), rule_id
                    )
            old = dict(
                db.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_ids[0],)
                ).fetchone()
            )
            self.assertEqual(old["result_json"], task["result_json"])
            self.assertEqual(old["checks_snapshot_json"], task["checks_snapshot_json"])
            self.assertEqual(
                old["check_profile_snapshot_json"], task["check_profile_snapshot_json"]
            )
            stats = _task_report_stat_rows_for_where(
                "t.id IN (?, ?, ?)", tuple(task_ids)
            )
            self.assertEqual(
                {row["id"]: row["suppressed"] for row in stats},
                dict(zip(task_ids, (1, 1, 0))),
            )
