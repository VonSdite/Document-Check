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
from app.reporting.suppression import (
    REVIEW_MIGRATION_KEY,
    enable_suppression_rule,
    refresh_suppression_reviews,
    suppression_evidence,
)
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

    def initial(
        self,
        index=0,
        subject="author",
        status="rejected",
        token=None,
        admin=False,
        reason="false_positive",
        note="",
    ):
        return self.client.post(
            f"{'/admin' if admin else ''}/tasks/{self.task_id}/report-items",
            json={
                "result_code": "consistency",
                "item_id": self.items[index]["id"],
                "acceptance_status": status,
                "rejection_reason": reason if status == "rejected" else "",
                "rejection_note": note,
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
        admin=False,
    ):
        return (client or self.client).post(
            f"{'/admin' if admin else ''}/tasks/{self.task_id}/reviews",
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

    def test_ip_initial_can_be_finalized_by_local_admin(self):
        with self.app.app_context():
            get_db().execute(
                "UPDATE tasks SET owner_subject='ip:127.0.0.1' WHERE id=?",
                (self.task_id,),
            )
            get_db().commit()
        for index, status in enumerate(("accepted", "rejected")):
            self.assertEqual(
                self.initial(index, subject="ip:127.0.0.1", status=status).status_code,
                200,
            )
        self.assertEqual(self.state()["initial"]["actor_source"], "ip")
        page = self.client.get(
            f"/admin/tasks/{self.task_id}/reviews",
            headers={"X-Test-Subject": "ip:127.0.0.1"},
        )
        soup = BeautifulSoup(page.data, "html.parser")
        self.assertEqual(len(soup.select("select[name=decision]")), 1)
        self.assertIn("当前审核身份：本地管理员", page.get_data(as_text=True))
        self.assertEqual(
            self.final(0, subject="ip:127.0.0.1", admin=True).status_code, 409
        )
        self.assertEqual(
            self.final(1, subject="ip:127.0.0.1", admin=True).status_code, 200
        )
        final = self.state(1)["final"]
        self.assertEqual(final["actor_subject"], "local_admin:primary")
        self.assertEqual(final["actor_name"], "本地管理员")
        self.assertEqual(final["actor_source"], "local_admin")
        with self.app.app_context():
            self.assertEqual(
                review_context(self.task())["progress"]["status"], "completed"
            )

    def test_local_admin_can_final_own_initial_with_separate_history(self):
        self.assertEqual(
            self.initial(subject="ip:127.0.0.1", admin=True).status_code, 200
        )
        self.assertEqual(
            self.state()["initial"]["actor_subject"], "local_admin:primary"
        )
        page = self.client.get(
            f"/admin/tasks/{self.task_id}/reviews",
            headers={"X-Test-Subject": "ip:127.0.0.1"},
        )
        self.assertEqual(
            len(
                BeautifulSoup(page.data, "html.parser").select("select[name=decision]")
            ),
            1,
        )
        self.assertEqual(
            self.final(subject="ip:127.0.0.1", admin=True).status_code, 200
        )
        state = self.state()
        self.assertEqual(
            state["initial"]["actor_subject"], state["final"]["actor_subject"]
        )
        self.assertEqual(state["final"]["initial_review_id"], state["initial"]["id"])
        self.assertEqual(len(state["history"]), 2)

    def test_cookie_admin_keeps_individual_identity(self):
        self.assertEqual(self.initial(admin=True).status_code, 200)
        self.assertEqual(
            self.state()["initial"]["actor_subject"], "cookie_session:author"
        )
        self.assertEqual(self.final(subject="author", admin=True).status_code, 200)
        self.assertEqual(
            self.state()["final"]["actor_subject"], "cookie_session:author"
        )

    def test_ip_final_requires_grant_and_stops_after_revocation(self):
        with self.app.app_context():
            register_subject("ip:127.0.0.1")
            replace_subject_permissions("ip:127.0.0.1", {"tasks.manage_all"})
        self.initial(subject="ip:127.0.0.1")
        response = self.client.post(
            f"/tasks/{self.task_id}/reviews",
            json={
                "stage": "final",
                "result_code": "consistency",
                "item_id": self.items[0]["id"],
                "decision": "confirmed",
                "review_token": self.state()["token"],
                "is_superadmin": True,
                "actor": {"subject": "local_admin:primary", "source": "local_admin"},
            },
            headers={"X-Test-Subject": "ip:127.0.0.1"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.state()["history"]), 1)
        with self.app.app_context():
            replace_subject_permissions(
                "ip:127.0.0.1", {"reports.final_review", "tasks.manage_all"}
            )
        self.assertEqual(self.final(subject="ip:127.0.0.1").status_code, 200)
        state = self.state()
        self.assertEqual(
            state["initial"]["actor_subject"], state["final"]["actor_subject"]
        )
        self.assertEqual(state["final"]["actor_source"], "ip")
        self.assertEqual(state["final"]["initial_review_id"], state["initial"]["id"])
        with self.app.app_context():
            replace_subject_permissions("ip:127.0.0.1", {"tasks.manage_all"})
        self.assertEqual(self.final(subject="ip:127.0.0.1").status_code, 403)
        self.assertEqual(len(self.state()["history"]), 2)

    def test_rejected_findings_require_final_review_permission(self):
        self.assertEqual(self.initial().status_code, 200)
        self.assertEqual(self.initial(1, status="rejected").status_code, 200)
        self.assertEqual(self.final(subject="reader").status_code, 403)
        self.assertEqual(self.final(subject="author").status_code, 200)
        self.assertEqual(
            self.state()["final"]["actor_subject"], "cookie_session:author"
        )
        with self.app.app_context():
            progress = final_review_progress([self.task_id], refresh=False)[
                self.task_id
            ]
            self.assertEqual((progress["confirmed"], progress["total"]), (1, 2))
            self.assertEqual(progress["required_count"], 2)
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

    def test_only_rejected_items_enter_final_review_and_count_toward_completion(self):
        for status in ("pending", "accepted"):
            with self.subTest(status=status):
                self.assertEqual(self.initial(status=status).status_code, 200)
                before = self.state()
                self.assertFalse(before["requires_final"])
                self.assertEqual(before["status"], "not_required")
                response = self.final()
                self.assertEqual(response.status_code, 409)
                self.assertIn("仅初审结果为“不认可”", response.get_json()["error"])
                self.assertEqual(self.state()["history"], before["history"])
                with self.app.app_context():
                    progress = final_review_progress([self.task_id])[self.task_id]
                    self.assertEqual(progress["required_count"], 0)
                    self.assertEqual(progress["pending"], 0)
                    self.assertEqual(progress["status"], "not_required")
                for prefix in ("", "/admin"):
                    page = self.client.get(f"{prefix}/tasks/{self.task_id}/reviews")
                    soup = BeautifulSoup(page.data, "html.parser")
                    self.assertFalse(soup.select("[data-report-item]"))
                    self.assertFalse(soup.select("[data-final-review-group]"))
                    empty = soup.select_one("[data-final-review-empty]")
                    self.assertFalse(empty.has_attr("hidden"))
                    self.assertIn("无需终审", empty.get_text())

        self.initial(status="pending")
        self.initial(1)
        with self.app.app_context():
            progress = final_review_progress([self.task_id])[self.task_id]
            self.assertEqual(progress["total"], 2)
            self.assertEqual(progress["required_count"], 1)
            self.assertEqual(progress["pending"], 1)
        self.assertEqual(self.final(1).status_code, 200)
        with self.app.app_context():
            progress = final_review_progress([self.task_id])[self.task_id]
            self.assertEqual(progress["confirmed"], 1)
            self.assertEqual(progress["status"], "completed")
        page = self.client.get(f"/tasks/{self.task_id}/reviews")
        soup = BeautifulSoup(page.data, "html.parser")
        self.assertEqual(
            [item["data-item-id"] for item in soup.select("[data-report-item]")],
            [self.items[1]["id"]],
        )
        self.assertEqual(len(soup.select("[data-final-review-form]")), 1)
        self.assertTrue(soup.select_one("[data-final-review-empty]").has_attr("hidden"))
        self.assertIn(
            "终审确认 1/1", soup.select_one("[data-report-review-progress]").text
        )

        for status in ("accepted", "pending"):
            self.initial(1, status=status)
            state = self.state(1)
            self.assertFalse(state["requires_final"])
            self.assertIsNone(state["final"])
            self.assertTrue(
                any(event["stage"] == "final" for event in state["history"])
            )
            self.assertEqual(self.final(1).status_code, 409)
            with self.app.app_context():
                progress = final_review_progress([self.task_id])[self.task_id]
                self.assertEqual(progress["status"], "not_required")
                self.assertEqual(progress["confirmed"], 0)
                self.assertEqual(progress["required_count"], 0)
            page = self.client.get(f"/tasks/{self.task_id}/reviews")
            soup = BeautifulSoup(page.data, "html.parser")
            self.assertFalse(soup.select("[data-report-item]"))
            self.assertFalse(soup.select("[data-final-review-group]"))
            self.assertFalse(soup.select("[data-final-review-form]"))
            self.assertFalse(
                soup.select_one("[data-final-review-empty]").has_attr("hidden")
            )
            queue = self.client.get("/final-reviews?final_status=not_required")
            self.assertIn(f"任务 #{self.task_id}", queue.get_data(as_text=True))

        self.initial(1)
        self.assertEqual(self.state(1)["status"], "pending")
        self.assertIsNone(self.state(1)["final"])
        self.assertEqual(self.final(1).status_code, 200)

    def test_accepted_legacy_final_stays_in_history_and_exports(self):
        self.initial(status="accepted")
        with self.app.app_context():
            db = get_db()
            event = dict(db.execute("SELECT * FROM report_review_events").fetchone())
            initial_id = event.pop("id")
            event.update(
                stage="final",
                decision="confirmed",
                initial_review_id=initial_id,
                note="已保存的历史终审说明",
            )
            db.execute(
                f"INSERT INTO report_review_events ({','.join(event)}) "
                f"VALUES ({','.join('?' for _ in event)})",
                tuple(event.values()),
            )
            db.commit()
            progress = final_review_progress([self.task_id])[self.task_id]
            self.assertEqual(progress["status"], "not_required")
            self.assertEqual(progress["confirmed"], 0)
            self.assertEqual(progress["required_count"], 0)
        state = self.state()
        self.assertIsNone(state["final"])
        self.assertEqual(len(state["history"]), 2)
        for suffix in ("/reviews", "/export"):
            page = self.client.get(f"/tasks/{self.task_id}{suffix}")
            self.assertEqual(page.status_code, 200)
            if suffix == "/export":
                self.assertIn("已保存的历史终审说明", page.get_data(as_text=True))
            else:
                soup = BeautifulSoup(page.data, "html.parser")
                self.assertFalse(soup.select("[data-report-item]"))
            self.assertIn("无需终审", page.get_data(as_text=True))
            self.assertNotIn("需重新终审", page.get_data(as_text=True))
        exported = self.client.get(f"/tasks/{self.task_id}/export.xlsx")
        workbook = load_workbook(io.BytesIO(exported.data))
        self.assertEqual(workbook["初审与终审"].cell(2, 7).value, "无需终审")
        self.assertIsNone(workbook["初审与终审"].cell(2, 10).value)
        self.assertIn(
            "已保存的历史终审说明",
            [cell.value for row in workbook["审核记录"] for cell in row],
        )
        workbook.close()

    def test_final_scope_cache_upgrade_preserves_history_and_rebuilds_progress(self):
        from app.persistence.schema import _ensure_final_review_stats_scope

        self.initial()
        self.initial(1, status="accepted")
        self.final()
        before = self.state()
        with self.app.app_context():
            db = get_db()
            db.execute("ALTER TABLE task_final_review_stats DROP COLUMN required_count")
            db.execute("UPDATE task_final_review_stats SET confirmed = total")
            db.commit()
            _ensure_final_review_stats_scope(db)
            self.assertEqual(final_review_progress([self.task_id], refresh=False), {})
            progress = final_review_progress([self.task_id])[self.task_id]
            self.assertEqual(progress["total"], 2)
            self.assertEqual(progress["initial_count"], 2)
            self.assertEqual(progress["required_count"], 1)
            self.assertEqual(progress["confirmed"], 1)
            self.assertEqual(progress["status"], "completed")
            _ensure_final_review_stats_scope(db)
            self.assertEqual(
                final_review_progress([self.task_id], refresh=False)[self.task_id],
                {**progress, "task_id": self.task_id},
            )
        self.assertEqual(self.state(), before)

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

    def test_report_table_and_separate_final_review_page_have_distinct_entries(self):
        initial_note = "结合上下文核对，原文参数含义一致。"
        self.initial(note=initial_note)
        for prefix in ("", "/admin"):
            report = self.client.get(f"{prefix}/tasks/{self.task_id}")
            soup = BeautifulSoup(report.data, "html.parser")
            self.assertEqual(len(soup.select(".report-table [data-report-item]")), 2)
            self.assertFalse(soup.select("[data-audit-item], [data-final-review-form]"))
            self.assertFalse(soup.select("[data-report-review-progress]"))
            self.assertIsNotNone(
                soup.find("a", href=f"{prefix}/tasks/{self.task_id}/reviews")
            )
            response = self.client.get(f"{prefix}/tasks/{self.task_id}/reviews")
            self.assertEqual(response.status_code, 200)
            soup = BeautifulSoup(response.data, "html.parser")
            self.assertIsNotNone(soup.select_one("[data-report-audit]"))
            self.assertEqual(
                [item["data-item-id"] for item in soup.select("[data-report-item]")],
                [self.items[0]["id"]],
            )
            self.assertEqual(len(soup.select("[data-final-review-form]")), 1)
            initial = soup.select_one("[data-initial-review-cell]")
            self.assertFalse(
                initial.select('input:not([type="hidden"]), select, textarea, button')
            )
            for text in ("不认可", "模型误报", initial_note, "author"):
                self.assertIn(text, initial.get_text())
            self.assertIn(self.state()["initial"]["created_at"], initial.get_text())
            self.assertFalse(soup.select("[data-report-rejection-modal]"))
            self.assertIsNotNone(soup.find("a", href=f"{prefix}/tasks/{self.task_id}"))
            before = self.state()
            attempted_initial = self.client.post(
                f"{prefix}/tasks/{self.task_id}/reviews",
                json={
                    "stage": "initial",
                    "result_code": "consistency",
                    "item_id": self.items[0]["id"],
                    "acceptance_status": "accepted",
                    "review_token": before["token"],
                },
            )
            self.assertEqual(attempted_initial.status_code, 409)
            self.assertIn("检查报告页面", attempted_initial.get_json()["error"])
            self.assertEqual(self.state(), before)
            queue = self.client.get(f"{prefix}/final-reviews")
            soup = BeautifulSoup(queue.data, "html.parser")
            self.assertIsNotNone(
                soup.find("a", href=f"{prefix}/tasks/{self.task_id}/reviews")
            )

        response = self.client.post(
            f"/tasks/{self.task_id}/reviews",
            data={
                "stage": "final",
                "result_code": "consistency",
                "item_id": self.items[0]["id"],
                "decision": "confirmed",
                "review_token": self.state()["token"],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.location,
            f"/tasks/{self.task_id}/reviews#review-consistency-{self.items[0]['id']}",
        )

    def test_inline_save_returns_current_controls_history_and_progress(self):
        self.initial()
        note = '已核对 <参数> & "条件"。\n</textarea><script>test</script>'
        response = self.client.post(
            f"/tasks/{self.task_id}/reviews",
            json={
                "stage": "final",
                "result_code": "consistency",
                "item_id": self.items[0]["id"],
                "decision": "returned",
                "note": note,
                "review_token": self.state()["token"],
                "inline_review": True,
                "final_review_page": True,
            },
            headers={"X-Test-Subject": "author"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        final = BeautifulSoup(payload["review_fragments"]["final"], "html.parser")
        self.assertEqual(final.select_one("option[selected]")["value"], "returned")
        self.assertEqual(final.select_one("textarea[name=note]").get_text(), note)
        self.assertIsNone(final.find("script"))
        initial = BeautifulSoup(payload["review_fragments"]["initial"], "html.parser")
        self.assertFalse(
            initial.select('input:not([type="hidden"]), select, textarea, button')
        )
        self.assertIn("不认可", initial.get_text())
        self.assertIn("cookie_session:author", payload["review_fragments"]["history"])
        page = self.client.get(
            f"/tasks/{self.task_id}/reviews", headers={"X-Test-Subject": "reviewer"}
        )
        soup = BeautifulSoup(page.data, "html.parser")
        self.assertEqual(soup.select_one("textarea[name=note]").get_text(), note)
        initial = self.client.post(
            f"/tasks/{self.task_id}/report-items",
            json={
                "result_code": "consistency",
                "item_id": self.items[0]["id"],
                "acceptance_status": "accepted",
                "review_token": self.state()["token"],
                "inline_review": True,
            },
        )
        self.assertEqual(initial.status_code, 200)
        fragments = initial.get_json()["review_fragments"]
        self.assertIn("data-report-acceptance-status", fragments["initial"])
        final = BeautifulSoup(fragments["final"], "html.parser")
        self.assertIsNone(final.select_one("[data-final-review-form]"))
        self.assertIn("无需终审", final.get_text())
        self.assertNotIn("需重新终审", final.get_text())
        self.assertIn("提单修改", fragments["history"])

    def test_legacy_feedback_needs_initial_record_before_authorized_ip_final(self):
        with self.app.app_context():
            raw = _raw_task_results(self.task())
            raw[0]["item_acceptances"] = {self.items[0]["id"]: {"status": "rejected"}}
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
        self.assertEqual(self.final(subject="ip:192.0.2.10").status_code, 200)
        self.assertEqual(self.state()["final"]["actor_subject"], "ip:192.0.2.10")

    def test_final_review_includes_rejected_suppressed_and_limited_items(self):
        self.initial()
        self.final()
        with self.app.app_context():
            raw = _raw_task_results(self.task())
            raw[0]["issue_output_limit"] = 1
            get_db().execute(
                "UPDATE tasks SET result_json=? WHERE id=?",
                (json.dumps(raw), self.task_id),
            )
            get_db().execute("UPDATE report_suppression_rules SET enabled = 1")
            get_db().commit()
            self.assertEqual(review_context(self.task())["progress"]["total"], 2)
            self.assertEqual(
                review_context(self.task())["progress"]["required_count"], 1
            )
        page = self.client.get(f"/tasks/{self.task_id}/reviews")
        soup = BeautifulSoup(page.data, "html.parser")
        self.assertEqual(
            [item["data-item-id"] for item in soup.select("[data-audit-item]")],
            [self.items[0]["id"]],
        )
        self.assertIn("已忽略误报", soup.select_one("[data-audit-item]").get_text())

        report = self.client.get(f"/tasks/{self.task_id}")
        soup = BeautifulSoup(report.data, "html.parser")
        self.assertEqual(len(soup.select("[data-report-item]")), 1)
        self.assertEqual(len(soup.select(".result-raw-details .is-suppressed")), 1)
        self.assertFalse(soup.select("[data-final-review-form]"))

        self.initial(1)
        with self.app.app_context():
            get_db().execute("UPDATE report_suppression_rules SET enabled = 0")
            get_db().commit()
        page = self.client.get(f"/tasks/{self.task_id}/reviews")
        soup = BeautifulSoup(page.data, "html.parser")
        self.assertEqual(len(soup.select("[data-audit-item]")), 2)
        additional = soup.select_one(f'[data-item-id="{self.items[1]["id"]}"]')
        self.assertIn("补充审核条目", additional.get_text())

    def test_excel_cleans_initial_and_final_notes_without_changing_reviews(self):
        initial_note = "初审\x00说明\n保留换行"
        final_note = "终审\x0b说明\t保留制表"
        self.assertEqual(self.initial(note=initial_note).status_code, 200)
        self.assertEqual(self.final(note=final_note).status_code, 200)
        export = self.client.get(f"/admin/tasks/{self.task_id}/export.xlsx")
        self.assertEqual(export.status_code, 200)
        workbook = load_workbook(io.BytesIO(export.data))
        self.addCleanup(workbook.close)
        for sheet_name, header, expected in (
            ("报告条目", "人工原因", "初审说明\n保留换行"),
            ("审核记录", "初审说明", "初审说明\n保留换行"),
            ("审核记录", "终审说明", "终审说明\t保留制表"),
            ("初审与终审", "终审说明", "终审说明\t保留制表"),
        ):
            with self.subTest(sheet=sheet_name, header=header):
                sheet = workbook[sheet_name]
                column = next(cell.column for cell in sheet[1] if cell.value == header)
                self.assertIn(
                    expected,
                    [row[column - 1].value for row in sheet.iter_rows(min_row=2)],
                )
        self.assertEqual(
            self.state()["initial"]["acceptance"]["rejection_note"], initial_note
        )
        self.assertEqual(self.state()["final"]["note"], final_note)

    def test_excel_records_importer_preserves_final_on_noop_and_rejects_stale(self):
        self.initial()
        self.initial(1)
        self.final()
        export = self.client.get(f"/tasks/{self.task_id}/export.xlsx")
        workbook = load_workbook(io.BytesIO(export.data))
        self.assertIn("审核记录", workbook.sheetnames)
        self.assertIn("初审与终审", workbook.sheetnames)

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
        sheet.cell(2, headers.index(REPORT_ACCEPTANCE_LABEL) + 1).value = "认可"
        sheet.cell(2, headers.index("不认可原因") + 1).value = ""
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
        upload(buffer.getvalue())
        self.assertEqual(self.state()["status"], "not_required")
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
            "app.reporting.reviews.record_initial_review",
            side_effect=RuntimeError("write failure"),
        ):
            self.assertEqual(self.initial(status="rejected").status_code, 500)
        self.assertEqual(self.state()["token"], before["token"])

    def suppression_rows(self):
        with self.app.app_context():
            refresh_suppression_reviews()
            return [
                dict(row)
                for row in get_db().execute(
                    "SELECT * FROM report_suppression_rules ORDER BY id"
                )
            ]

    def test_all_rejection_reasons_enter_library_only_after_confirmed_final(self):
        from app.reporting.constants import REPORT_REJECTION_REASONS

        for reason, label in REPORT_REJECTION_REASONS.items():
            with self.subTest(reason=reason):
                with self.app.app_context():
                    get_db().execute("DELETE FROM report_suppression_rules")
                    get_db().commit()
                initial = self.initial(reason=reason, note="核对原文后的具体原因")
                self.assertEqual(initial.status_code, 200)
                self.assertFalse(initial.get_json()["suppression_candidate_created"])
                self.assertEqual(self.suppression_rows(), [])
                for decision in ("returned", "deferred"):
                    self.assertEqual(
                        self.final(decision=decision, note="需要核对").status_code, 200
                    )
                    self.assertEqual(self.suppression_rows(), [])
                self.assertEqual(self.final(note="已核实初审 <依据>").status_code, 200)
                rows = self.suppression_rows()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reason"], label)
                self.assertEqual(rows[0]["enabled"], 0)
                with self.app.app_context():
                    proof = suppression_evidence([rows[0]["id"]])[rows[0]["id"]][0]
                self.assertTrue(proof["is_current"])
                self.assertEqual(
                    proof["initial"]["actor_subject"], "cookie_session:author"
                )
                self.assertEqual(
                    proof["final"]["actor_subject"], "cookie_session:reviewer"
                )
                self.assertEqual(
                    proof["initial"]["acceptance"]["rejection_note"],
                    "核对原文后的具体原因",
                )
                self.assertEqual(proof["final"]["note"], "已核实初审 <依据>")
                page = self.client.get("/admin/rules?rule_status=candidate")
                self.assertIn("审核依据", page.get_data(as_text=True))
                self.assertIn("已核实初审 &lt;依据&gt;", page.get_data(as_text=True))

    def test_invalidated_confirmation_disables_rule_and_preserves_evidence(self):
        self.initial()
        self.final(note="原终审说明")
        rule_id = self.suppression_rows()[0]["id"]
        with self.app.app_context():
            self.assertTrue(enable_suppression_rule(rule_id))
        self.initial(1, status="accepted")
        self.assertEqual(self.suppression_rows()[0]["enabled"], 1)
        for decision in ("returned", "deferred"):
            self.final(decision=decision, note="补充核对")
            self.assertEqual(self.suppression_rows()[0]["enabled"], 0)
            with self.app.app_context():
                self.assertFalse(enable_suppression_rule(rule_id))
                self.assertFalse(
                    suppression_evidence([rule_id])[rule_id][0]["is_current"]
                )
            self.final()
            with self.app.app_context():
                self.assertTrue(enable_suppression_rule(rule_id))
        for status in ("rejected", "accepted", "pending"):
            self.initial(status=status)
            self.assertEqual(self.suppression_rows()[0]["enabled"], 0)
            self.initial()
            self.final()
            with self.app.app_context():
                self.assertTrue(enable_suppression_rule(rule_id))
        self.assertEqual(len(self.suppression_rows()), 1)
        with self.app.app_context():
            proofs = suppression_evidence([rule_id])[rule_id]
            self.assertEqual(sum(proof["is_current"] for proof in proofs), 1)
            self.assertIn("原终审说明", [proof["final"]["note"] for proof in proofs])

    def test_report_change_and_source_deletion_revalidate_library(self):
        from app.reporting.service import _enabled_report_suppression_rules

        self.initial()
        self.final()
        rule_id = self.suppression_rows()[0]["id"]
        with self.app.app_context():
            self.assertTrue(enable_suppression_rule(rule_id))
            raw = _raw_task_results(self.task())
            raw[0]["result"] = raw[0]["result"].replace(
                "参数 A 前后不一致", "参数 A 的适用条件缺失"
            )
            get_db().execute(
                "UPDATE tasks SET result_json = ? WHERE id = ?",
                (json.dumps(raw), self.task_id),
            )
            get_db().commit()
            self.assertEqual(_enabled_report_suppression_rules("document_check"), {})
            self.assertEqual(
                get_db()
                .execute("SELECT enabled FROM report_suppression_rules")
                .fetchone()[0],
                0,
            )
            get_db().execute("DELETE FROM tasks WHERE id = ?", (self.task_id,))
            get_db().commit()
            refresh_suppression_reviews()
            proof = suppression_evidence([rule_id])[rule_id][0]
            self.assertIsNone(proof["task_id"])
            self.assertFalse(proof["is_current"])
            self.assertEqual(proof["final"]["decision"], "confirmed")

    def test_same_rule_keeps_independent_confirmations_from_multiple_reports(self):
        from app.reporting.reviews import find_review_item, save_final_review
        from app.reporting.service import update_report_item_review

        self.initial()
        self.final()
        rule_id = self.suppression_rows()[0]["id"]
        other_id = self._insert_report_task()
        actor = {
            "subject": "local_admin:primary",
            "name": "管理员",
            "source": "local_admin",
        }
        with self.app.app_context():
            task = dict(
                get_db()
                .execute("SELECT * FROM tasks WHERE id = ?", (other_id,))
                .fetchone()
            )
            item_id = _result_report_items(_raw_task_results(task)[0])[0]["id"]
            response = update_report_item_review(
                task,
                {
                    "result_code": "consistency",
                    "item_id": item_id,
                    "acceptance_status": "rejected",
                    "rejection_reason": "evidence_insufficient",
                },
                actor=actor,
            )
            save_final_review(
                other_id,
                {
                    "result_code": "consistency",
                    "item_id": item_id,
                    "review_token": response["review_token"],
                    "decision": "confirmed",
                },
                actor,
            )
            self.assertTrue(enable_suppression_rule(rule_id))
        self.initial(status="accepted")
        self.assertEqual(len(self.suppression_rows()), 1)
        self.assertEqual(self.suppression_rows()[0]["enabled"], 1)
        with self.app.app_context():
            proofs = suppression_evidence([rule_id])[rule_id]
            self.assertEqual(len(proofs), 2)
            self.assertEqual(
                [p["source_task_id"] for p in proofs if p["is_current"]], [other_id]
            )
            task = (
                get_db()
                .execute("SELECT * FROM tasks WHERE id = ?", (other_id,))
                .fetchone()
            )
            state = find_review_item(task, "consistency", item_id)[2]
            save_final_review(
                other_id,
                {
                    "result_code": "consistency",
                    "item_id": item_id,
                    "review_token": state["token"],
                    "decision": "returned",
                    "note": "补充说明",
                },
                actor,
            )
        self.assertEqual(self.suppression_rows()[0]["enabled"], 0)

    def test_final_and_suppression_evidence_commit_atomically(self):
        self.initial()
        before = self.state()
        with self.app.app_context():
            get_db().execute(
                "CREATE TRIGGER fail_evidence BEFORE INSERT ON report_suppression_reviews "
                "BEGIN SELECT RAISE(ABORT, 'evidence write failure'); END"
            )
            get_db().commit()
        self.assertEqual(self.final().status_code, 500)
        self.assertEqual(self.state()["token"], before["token"])
        self.assertEqual(self.suppression_rows(), [])

    def test_existing_rules_require_real_current_final_and_migration_is_idempotent(
        self,
    ):
        self.initial()
        self.final()
        rule_id = self.suppression_rows()[0]["id"]
        self.initial(1)
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM report_suppression_reviews")
            db.execute(
                "UPDATE report_suppression_rules SET enabled = 1 WHERE id = ?",
                (rule_id,),
            )
            db.execute("DELETE FROM settings WHERE key = ?", (REVIEW_MIGRATION_KEY,))
            old_rule_id = db.execute(
                "INSERT INTO report_suppression_rules (task_type, check_code, fingerprint, item_json, enabled, created_at, updated_at) "
                "VALUES ('document_check', 'consistency', 'unreviewed', '{}', 1, 'now', 'now')"
            ).lastrowid
            db.commit()
            refresh_suppression_reviews()
            self.assertEqual(
                db.execute(
                    "SELECT enabled FROM report_suppression_rules WHERE id = ?",
                    (rule_id,),
                ).fetchone()[0],
                1,
            )
            self.assertTrue(enable_suppression_rule(rule_id))
            self.assertFalse(enable_suppression_rule(old_rule_id))
            refresh_suppression_reviews()
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM report_suppression_reviews"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM report_suppression_rules").fetchone()[
                    0
                ],
                2,
            )
        response = self.client.post(
            "/admin/rules",
            data={
                "action": "report_suppression_rule",
                "operation": "enable",
                "rule_id": old_rule_id,
            },
            follow_redirects=True,
        )
        self.assertIn("仅有有效终审确认", response.get_data(as_text=True))
        soup = BeautifulSoup(response.data, "html.parser")
        self.assertEqual(
            len(
                soup.select(
                    '[data-rule-status="awaiting_final"] button[value="enable"][disabled]'
                )
            ),
            1,
        )

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
