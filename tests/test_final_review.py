import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from bs4 import BeautifulSoup
from flask import g, request
from openpyxl import load_workbook

from app.identity.models import UserIdentity
from app.persistence.connection import get_db
from app.persistence.permissions import register_subject, replace_subject_permissions
from app.persistence.schema import init_db
from app.reporting.constants import REPORT_ACCEPTANCE_LABEL
from app.reporting.reviews import final_review_progress, review_context
from app.reporting.service import _raw_task_results, _result_report_items
from tests import test_routes as route_fixtures


class FinalReviewTest(unittest.TestCase):
    setUpFixture = route_fixtures.AdminSettingsRouteTest.setUp
    tearDown = route_fixtures.AdminSettingsRouteTest.tearDown
    _insert_report_task = route_fixtures.AdminSettingsRouteTest._insert_report_task

    def setUp(self):
        self.setUpFixture()
        self.task_id = self._insert_report_task()
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE tasks SET owner_subject='cookie_session:author' WHERE id=?",
                (self.task_id,),
            )
            db.commit()
            for subject in ("author", "reviewer", "reader"):
                register_subject("cookie_session:" + subject)
            replace_subject_permissions(
                "cookie_session:reviewer", {"reports.final_review"}
            )
            replace_subject_permissions("cookie_session:reader", {"tasks.view_all"})
            replace_subject_permissions(
                "cookie_session:author", {"reports.final_review"}
            )
            task = self.task()
            self.items = _result_report_items(_raw_task_results(task)[0])

        @self.app.before_request
        def test_identity():
            subject = request.headers.get("X-Test-Subject", "author")
            source = "ip" if subject.startswith("ip:") else "cookie_session"
            g.user_identity = UserIdentity(
                subject=subject if source == "ip" else "cookie_session:" + subject,
                display_name=subject,
                source=source,
                ip="127.0.0.1",
            )

    def task(self):
        return dict(
            get_db()
            .execute("SELECT * FROM tasks WHERE id=?", (self.task_id,))
            .fetchone()
        )

    def state(self, index=0):
        with self.app.app_context():
            return review_context(self.task())["results"][0]["report_items"][index][
                "review"
            ]

    def initial(self, index=0, subject="author", status="accepted", token=None):
        return self.client.post(
            f"/tasks/{self.task_id}/report-items",
            json={
                "result_code": "consistency",
                "item_id": self.items[index]["id"],
                "acceptance_status": status,
                "rejection_reason": "false_positive" if status == "rejected" else "",
                "review_token": token or self.state(index)["token"],
            },
            headers={"X-Test-Subject": subject},
        )

    def final(
        self,
        index=0,
        subject="reviewer",
        decision="confirmed",
        note="",
        token=None,
        client=None,
    ):
        return (client or self.client).post(
            f"/tasks/{self.task_id}/reviews",
            json={
                "stage": "final",
                "result_code": "consistency",
                "item_id": self.items[index]["id"],
                "decision": decision,
                "note": note,
                "review_token": token or self.state(index)["token"],
            },
            headers={"X-Test-Subject": subject},
        )

    def test_all_findings_require_independent_final_review(self):
        self.assertEqual(self.initial().status_code, 200)
        self.assertEqual(self.initial(1, status="rejected").status_code, 200)
        self.assertEqual(self.final(subject="author").status_code, 409)
        self.assertEqual(self.final(subject="reader").status_code, 403)
        self.assertEqual(self.final().status_code, 200)
        with self.app.app_context():
            progress = final_review_progress([self.task_id], refresh=False)[
                self.task_id
            ]
            self.assertEqual((progress["confirmed"], progress["total"]), (1, 2))
            self.assertEqual(progress["status"], "in_progress")
        self.assertEqual(self.final(1).status_code, 200)
        with self.app.app_context():
            self.assertEqual(
                final_review_progress([self.task_id], refresh=False)[self.task_id][
                    "status"
                ],
                "completed",
            )
            events = (
                get_db()
                .execute("SELECT * FROM report_review_events ORDER BY id")
                .fetchall()
            )
            self.assertEqual(len(events), 4)
            self.assertEqual(events[-1]["actor_subject"], "cookie_session:reviewer")
            self.assertEqual(events[-1]["initial_review_id"], events[1]["id"])
            self.assertTrue(events[-1]["created_at"])
        page = self.client.get(
            f"/tasks/{self.task_id}/reviews", headers={"X-Test-Subject": "reviewer"}
        )
        self.assertEqual(page.status_code, 200)
        self.assertEqual(
            len(BeautifulSoup(page.data, "html.parser").select("[data-audit-item]")), 2
        )

    def test_changed_initial_report_and_retry_invalidate_final(self):
        self.initial()
        old = self.state()["token"]
        self.final()
        self.assertEqual(self.initial(token=old).status_code, 409)
        self.initial(status="rejected")
        self.assertEqual(self.state()["status"], "pending")
        with self.app.app_context():
            progress = final_review_progress([self.task_id], refresh=False)[
                self.task_id
            ]
            self.assertEqual(progress["confirmed"], 0)
        self.assertEqual(len(self.state()["history"]), 3)
        self.final()
        with self.app.app_context():
            task = self.task()
            raw = _raw_task_results(task)
            report = json.loads(raw[0]["result"])
            report["items"][0]["id"] = self.items[0]["id"]
            report["items"][0]["description"] = "重新检查后的不同证据"
            raw[0]["result"] = json.dumps(report, ensure_ascii=False)
            get_db().execute(
                "UPDATE tasks SET result_json=? WHERE id=?",
                (json.dumps(raw), self.task_id),
            )
            get_db().commit()
            progress = final_review_progress([self.task_id])[self.task_id]
            self.assertEqual(progress["confirmed"], 0)
        self.assertEqual(self.state()["status"], "awaiting_initial")
        self.assertEqual(len(self.state()["history"]), 4)

    def test_return_defer_and_resubmit_preserve_decisions(self):
        self.initial()
        self.assertEqual(self.final(decision="returned").status_code, 409)
        self.assertEqual(
            self.final(decision="returned", note="请核对原文参数").status_code, 200
        )
        self.assertEqual(self.state()["status"], "returned")
        queue = self.client.get(
            "/final-reviews?final_status=returned",
            headers={"X-Test-Subject": "reviewer"},
        )
        self.assertIn(f"任务 #{self.task_id}", queue.get_data(as_text=True))
        self.initial()
        self.assertEqual(self.state()["status"], "pending")
        self.assertEqual(
            self.final(decision="deferred", note="需要补充原始依据").status_code, 200
        )
        self.assertEqual(self.state()["status"], "deferred")

    def test_legacy_feedback_needs_recorded_identity_and_ip_cannot_final(self):
        with self.app.app_context():
            raw = _raw_task_results(self.task())
            raw[0]["item_acceptances"] = {self.items[0]["id"]: {"status": "accepted"}}
            get_db().execute(
                "UPDATE tasks SET result_json=? WHERE id=?",
                (json.dumps(raw), self.task_id),
            )
            get_db().commit()
        self.assertEqual(self.final().status_code, 409)
        self.initial()
        with self.app.app_context():
            register_subject("ip:192.0.2.10")
            replace_subject_permissions("ip:192.0.2.10", {"reports.final_review"})
        self.assertEqual(self.final(subject="ip:192.0.2.10").status_code, 409)
        self.assertEqual(self.final().status_code, 200)

    def test_full_review_includes_suppressed_and_limited_items(self):
        with self.app.app_context():
            raw = _raw_task_results(self.task())
            raw[0]["issue_output_limit"] = 1
            get_db().execute(
                "UPDATE tasks SET result_json=? WHERE id=?",
                (json.dumps(raw), self.task_id),
            )
            get_db().execute(
                "INSERT INTO report_suppression_rules(task_type, check_code, fingerprint, item_json, enabled, created_at, updated_at) "
                "VALUES ('document_check','consistency','test',?,1,'now','now')",
                (json.dumps(self.items[0]),),
            )
            get_db().commit()
            self.assertEqual(review_context(self.task())["progress"]["total"], 2)
        page = self.client.get(f"/tasks/{self.task_id}/reviews")
        self.assertEqual(
            len(BeautifulSoup(page.data, "html.parser").select("[data-audit-item]")), 2
        )

    def test_excel_records_importer_preserves_final_on_noop_and_rejects_stale(self):
        self.initial()
        self.initial(1)
        self.final()
        export = self.client.get(f"/tasks/{self.task_id}/export.xlsx")
        workbook = load_workbook(io.BytesIO(export.data))
        self.assertIn("审核记录", workbook.sheetnames)
        self.assertIn("双人审核", workbook.sheetnames)

        def upload(payload):
            return self.client.post(
                f"/tasks/{self.task_id}/import.xlsx",
                data={"report_excel": (io.BytesIO(payload), "report.xlsx")},
                follow_redirects=True,
            )

        self.assertEqual(upload(export.data).status_code, 200)
        self.assertEqual(self.state()["status"], "confirmed")
        sheet = workbook["报告条目"]
        headers = [cell.value for cell in sheet[1]]
        sheet.cell(2, headers.index(REPORT_ACCEPTANCE_LABEL) + 1).value = "不认可"
        sheet.cell(2, headers.index("不认可原因") + 1).value = "模型误报"
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
        upload(buffer.getvalue())
        self.assertEqual(self.state()["status"], "pending")
        with self.app.app_context():
            self.assertEqual(
                final_review_progress([self.task_id], refresh=False)[self.task_id][
                    "confirmed"
                ],
                0,
            )
        self.assertEqual(self.state()["initial"]["channel"], "excel")
        self.assertEqual(
            self.state()["initial"]["actor_subject"], "cookie_session:author"
        )
        before = len(self.state()["history"])
        upload(export.data)
        self.assertEqual(len(self.state()["history"]), before)

    def test_concurrent_final_requests_and_atomic_initial_failure(self):
        self.initial()
        token = self.state()["token"]

        def submit():
            with self.app.test_client() as client:
                return self.final(token=token, client=client).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: submit(), range(2))), [200, 409])
        before = self.state()
        with patch(
            "app.reporting.service._maybe_create_report_suppression_candidate",
            side_effect=RuntimeError("write failure"),
        ):
            self.assertEqual(self.initial(status="rejected").status_code, 500)
        self.assertEqual(self.state()["token"], before["token"])

    def test_permissions_origin_queue_and_schema_are_consistent(self):
        self.assertEqual(
            self.client.get(
                "/final-reviews", headers={"X-Test-Subject": "reader"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/final-reviews", headers={"X-Test-Subject": "reviewer"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                f"/tasks/{self.task_id}/reviews",
                json={},
                headers={"Origin": "https://other.example"},
            ).status_code,
            403,
        )
        with self.app.app_context():
            with patch.object(self.app, "teardown_appcontext"):
                init_db()
        self.initial()
        with self.app.app_context():
            self.assertEqual(
                get_db()
                .execute("SELECT COUNT(*) FROM report_review_events")
                .fetchone()[0],
                1,
            )

    def test_permission_schema_upgrade_preserves_existing_grants(self):
        from app.persistence.schema import _ensure_final_review_permission

        with self.app.app_context():
            db = get_db()
            db.execute("DROP TABLE user_permissions")
            db.execute(
                "CREATE TABLE user_permissions (subject TEXT NOT NULL, permission TEXT NOT NULL CHECK(permission IN ('tasks.view_all', 'tasks.manage_all', 'stats.view_all', 'rules.manage')), PRIMARY KEY(subject, permission), FOREIGN KEY(subject) REFERENCES user_identities(subject) ON DELETE CASCADE)"
            )
            db.execute(
                "INSERT INTO user_permissions VALUES ('cookie_session:author', 'tasks.view_all')"
            )
            db.commit()
            _ensure_final_review_permission(db)
            _ensure_final_review_permission(db)
            db.execute(
                "INSERT INTO user_permissions VALUES ('cookie_session:reviewer', 'reports.final_review')"
            )
            db.commit()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM user_permissions").fetchone()[0], 2
            )

    def test_removed_item_keeps_snapshot_in_history_and_export(self):
        self.initial()
        self.final()
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE tasks SET result_json='[]' WHERE id=?", (self.task_id,))
            db.commit()
            context = review_context(self.task())
            self.assertEqual(context["progress"]["total"], 0)
            self.assertEqual(len(context["archived_history"]), 2)
        page = self.client.get(f"/tasks/{self.task_id}/reviews")
        self.assertIn("其他版本条目的审核记录", page.get_data(as_text=True))
        self.assertIn("参数 A 前后不一致", page.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
