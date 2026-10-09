import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from werkzeug.middleware.proxy_fix import ProxyFix

from app.identity.permissions import ASSIGNABLE_PERMISSIONS, normalize_permissions
from app.infrastructure.config import _normalize_auth
from app.persistence.connection import get_db
from app.persistence.permissions import (
    register_subject,
    replace_subject_permissions,
    subject_permissions,
)
from app.persistence.schema import init_db
from app.persistence.settings import get_setting, set_ip_username, sync_identity_profile
from tests import test_routes


class UserPermissionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_routes.AdminSettingsRouteTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.app
        self.root = self.fixture.client
        self.client = self.app.test_client()
        self.subject = "ip:127.0.0.1"
        self.app.config.update(ADMIN_USERNAME="root", ADMIN_PASSWORD="test-root")

    def grant(self, *permissions, subject=None):
        subject = subject or self.subject
        with self.app.app_context():
            register_subject(subject)
            replace_subject_permissions(
                subject, normalize_permissions(set(permissions))
            )

    def save(self, subject, permissions, *, headers=None, **extra):
        page = self.root.get("/admin/permissions")
        self.assertEqual(page.status_code, 200)
        with self.root.session_transaction() as session:
            token = session["permission_csrf_token"]
        return self.root.post(
            "/admin/permissions",
            data={
                "subject": subject,
                "permissions": permissions,
                "csrf_token": token,
                **extra,
            },
            headers=headers,
        )

    def cookie_mode(self, *, rollout=False):
        self.app.config["AUTH"] = _normalize_auth(
            {
                "mode": "ip" if rollout else "cookie_session",
                "cookie_session": {
                    "userinfo_url": "https://userinfo.example.test/user",
                    "field_mapping": {"user_id": "uuid"},
                    "login_url": "https://login.example.test/login",
                    "enabled_ips": ["127.0.0.1"] if rollout else [],
                },
            }
        )
        self.client.set_cookie("enterprise-ticket", "a")
        resolver = patch(
            "app.identity.service.resolve_userinfo",
            return_value=(
                {
                    "user_id": "a",
                    "username": "张三",
                    "employee_number": "123",
                    "_profile_version": 1,
                },
                None,
            ),
        )
        self.resolve = resolver.start()
        self.addCleanup(resolver.stop)
        self.subject = "cookie_session:a"

    def report_task(self, *, subject=None, status="partial"):
        task_id = self.fixture._insert_report_task(status=status)
        with self.app.app_context():
            get_db().execute(
                "UPDATE tasks SET owner_subject = ?, owner_source = ? WHERE id = ?",
                (
                    subject or "ip:10.0.0.9",
                    "cookie_session"
                    if (subject or "").startswith("cookie_session:")
                    else "ip",
                    task_id,
                ),
            )
            get_db().commit()
        return task_id

    def test_defaults_register_users_and_preserve_private_business_pages(self):
        own = self.fixture._insert_task()
        other = self.fixture._insert_task(ip="10.0.0.9")
        page = self.client.get("/")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(
            {int(row["data-task-id"]) for row in soup.select("[data-task-id]")}, {own}
        )
        self.assertIsNone(soup.select_one("[data-management-entry]"))
        self.assertEqual(self.client.get(f"/tasks/{other}").status_code, 404)
        self.assertEqual(self.client.get("/models").status_code, 200)
        with self.app.app_context():
            self.assertEqual(subject_permissions(self.subject), frozenset())
            self.assertIsNotNone(
                get_db()
                .execute(
                    "SELECT subject FROM user_identities WHERE subject = ?",
                    (self.subject,),
                )
                .fetchone()
            )
        self.assertEqual(self.client.post(f"/tasks/{own}/delete").status_code, 302)

    def test_ip_permissions_ignore_unconfigured_headers_and_use_configured_proxy_identity(
        self,
    ):
        self.grant("rules.manage", subject="ip:10.0.0.8")
        for name in ("X-Real-IP", "X-Forwarded-For"):
            self.assertEqual(
                self.client.get(
                    "/admin/rules",
                    headers={name: "10.0.0.8", "X-Requested-With": "fetch"},
                ).status_code,
                403,
            )
        self.app.config["REAL_IP_HEADER"] = "X-Verified-IP"
        self.assertEqual(
            self.client.get(
                "/admin/rules",
                headers={"X-Verified-IP": "10.0.0.8", "X-Real-IP": "10.0.0.9"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/admin/rules",
                headers={
                    "X-Verified-IP": "invalid",
                    "X-Forwarded-For": "10.0.0.8",
                    "X-Requested-With": "fetch",
                },
            ).status_code,
            403,
        )
        self.app.config["REAL_IP_HEADER"] = ""
        self.app.wsgi_app = ProxyFix(self.app.wsgi_app, x_for=1)
        self.assertEqual(
            self.client.get(
                "/admin/rules", headers={"X-Forwarded-For": "10.0.0.8"}
            ).status_code,
            200,
        )

    def test_root_page_lists_only_assignable_permissions_and_filters_known_users(self):
        self.client.get("/")
        with self.app.app_context():
            register_subject("cookie_session:stable-b")
            sync_identity_profile(
                "cookie_session:stable-b",
                {
                    "display_name": "李四",
                    "employee_number": "888",
                    "avatar": "",
                    "label": "李四（888）",
                    "version": 1,
                },
            )
            set_ip_username("10.0.0.8", "办公室")
        page = self.root.get("/admin/permissions")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertEqual(
            {
                checkbox["value"]
                for checkbox in soup.select('input[name="permissions"]')
            },
            set(ASSIGNABLE_PERMISSIONS),
        )
        self.assertIsNotNone(soup.select_one('nav a[href="/admin/permissions"]'))
        self.assertIsNone(soup.select_one("[data-permission-form] button"))
        self.assertIsNone(soup.select_one("[data-permission-status]"))
        self.assertEqual(len(soup.select(".permission-table th")), 5)
        self.assertIsNone(soup.select_one('[name="source"]'))
        self.assertEqual(
            soup.select_one('[data-permission-user="ip:10.0.0.8"] .subline').text,
            "ip:10.0.0.8",
        )
        self.assertIsNone(
            soup.select_one('[data-permission-user="cookie_session:stable-b"] .subline')
        )
        self.assertIsNone(soup.select_one(".page-title p"))
        self.assertEqual(
            soup.select_one('input[name="keyword"]')["placeholder"], "搜索用户"
        )
        self.assertEqual(
            soup.select_one(
                '[data-permission-user="cookie_session:stable-b"] strong'
            ).text,
            "李四（888）",
        )
        tips = soup.select(".permission-table th .help-tip")
        self.assertEqual(len(tips), len(ASSIGNABLE_PERMISSIONS))
        for tip, permission in zip(tips, ASSIGNABLE_PERMISSIONS.values()):
            self.assertEqual(tip.text, "?")
            self.assertEqual(tip["data-tip"], permission["description"])
            self.assertEqual(tip["aria-label"], permission["label"] + "说明")
            self.assertEqual(tip["type"], "button")
        self.assertIsNone(soup.select_one('input[name="permissions"][disabled]'))
        self.assertIsNone(soup.select_one(".topbar-account a"))
        for row in soup.select("[data-permission-user]"):
            form_id = row.select_one("[data-permission-form]")["id"]
            self.assertTrue(
                all(
                    checkbox["form"] == form_id
                    for checkbox in row.select('input[name="permissions"]')
                )
            )
        page = self.root.get("/admin/permissions?keyword=888")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertEqual(
            [
                row["data-permission-user"]
                for row in soup.select("[data-permission-user]")
            ],
            ["cookie_session:stable-b"],
        )
        for keyword in ("办公室", "ip:10.0.0.8", "10.0.0.8"):
            with self.subTest(keyword=keyword):
                page = self.root.get(
                    "/admin/permissions", query_string={"keyword": keyword}
                )
                soup = BeautifulSoup(page.text, "html.parser")
                self.assertEqual(
                    [
                        row["data-permission-user"]
                        for row in soup.select("[data-permission-user]")
                    ],
                    ["ip:10.0.0.8"],
                )
                row = soup.select_one('[data-permission-user="ip:10.0.0.8"]')
                self.assertEqual(row.select_one("strong").text, "办公室")
                self.assertEqual(row.select_one(".subline").text, "ip:10.0.0.8")

    def test_user_pagination_preserves_filters_and_uses_shared_page_controls(self):
        with self.app.app_context():
            for index in range(61):
                register_subject(f"cookie_session:permission-user-{index:03d}")
            register_subject("cookie_session:unrelated-user")
            register_subject("ip:10.0.0.8")
        for per_page, page, expected_page, pages, rows in (
            (20, 2, 2, 4, 20),
            (50, 2, 2, 2, 11),
            (100, 2, 1, 1, 61),
            (20, 999, 4, 4, 1),
            (999, 2, 2, 4, 20),
        ):
            with self.subTest(per_page=per_page, page=page):
                response = self.root.get(
                    "/admin/permissions",
                    query_string={
                        "keyword": "permission-user-",
                        "per_page": per_page,
                        "page": page,
                    },
                )
                self.assertEqual(response.status_code, 200)
                soup = BeautifulSoup(response.text, "html.parser")
                self.assertEqual(len(soup.select("[data-permission-user]")), rows)
                size = per_page if per_page in (20, 50, 100) else 20
                filters = {
                    "keyword": "permission-user-",
                }
                size_form = soup.select_one(".pagination .page-size-form")
                jump_form = soup.select_one(".pagination .page-jump-form")
                for form in (size_form, jump_form):
                    self.assertEqual(form["action"], "/admin/permissions")
                    for name, value in filters.items():
                        self.assertEqual(
                            form.select_one(f'input[name="{name}"]')["value"], value
                        )
                self.assertEqual(
                    size_form.select_one('input[name="page"]')["value"], "1"
                )
                select = size_form.select_one("[data-page-size-select]")
                self.assertEqual(
                    [option["value"] for option in select.select("option")],
                    ["20", "50", "100"],
                )
                self.assertEqual(
                    select.select_one("option[selected]")["value"], str(size)
                )
                page_input = jump_form.select_one('input[name="page"]')
                self.assertEqual(page_input["value"], str(expected_page))
                self.assertEqual(page_input["max"], str(pages))
                self.assertEqual(
                    jump_form.select_one('input[name="per_page"]')["value"], str(size)
                )
                self.assertIsNone(
                    soup.select_one('.filter-bar select[name="per_page"]')
                )
                self.assertEqual(
                    soup.select_one('.filter-bar input[name="per_page"]')["value"],
                    str(size),
                )
                self.assertEqual(
                    soup.select_one(".pagination-summary > span").text, "共 61 条"
                )
                links = soup.select(".pagination-controls a")
                for link, destination in zip(
                    links, (max(1, expected_page - 1), min(pages, expected_page + 1))
                ):
                    self.assertEqual(
                        parse_qs(urlparse(link["href"]).query),
                        {
                            "keyword": [filters["keyword"]],
                            "per_page": [str(size)],
                            "page": [str(destination)],
                        },
                    )
        response = self.root.get("/admin/permissions?keyword=missing-user&page=999")
        soup = BeautifulSoup(response.text, "html.parser")
        self.assertEqual(soup.select_one(".pagination-summary > span").text, "共 0 条")
        self.assertEqual(
            soup.select_one('.page-jump-form input[name="page"]')["max"], "1"
        )
        self.assertTrue(
            all(
                "disabled" in link["class"]
                for link in soup.select(".pagination-controls a")
            )
        )

    def test_root_can_grant_replace_and_revoke_without_losing_its_session(self):
        self.client.get("/")
        self.assertEqual(
            self.save(self.subject, ["tasks.manage_all", "rules.manage"]).status_code,
            302,
        )
        with self.app.app_context():
            self.assertEqual(
                subject_permissions(self.subject),
                {"tasks.view_all", "tasks.manage_all", "rules.manage"},
            )
        self.assertEqual(self.client.get("/admin/tasks").status_code, 200)
        self.assertEqual(self.save(self.subject, ["stats.view_all"]).status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/admin/tasks").status_code, 403)
        self.assertEqual(self.save(self.subject, []).status_code, 302)
        self.assertEqual(
            self.client.get(
                "/admin", headers={"X-Requested-With": "fetch"}
            ).status_code,
            403,
        )
        self.assertEqual(self.root.get("/admin/settings").status_code, 200)
        self.assertEqual(self.root.get("/admin/permissions").status_code, 200)

    def test_invalid_csrf_unknown_subject_and_nonassignable_permissions_are_rejected(
        self,
    ):
        self.grant("stats.view_all")
        for token in ("", "wrong-token", "无效令牌"):
            with self.subTest(token=token):
                self.assertEqual(
                    self.save(
                        self.subject, ["tasks.manage_all"], csrf_token=token
                    ).status_code,
                    400,
                )
        for permission in (
            "system.manage",
            "permissions.manage",
            "superadmin",
            "unknown",
        ):
            with self.subTest(permission=permission):
                self.assertEqual(self.save(self.subject, [permission]).status_code, 400)
        self.assertEqual(
            self.save("cookie_session:unknown", ["tasks.view_all"]).status_code, 404
        )
        with self.app.app_context():
            self.assertEqual(subject_permissions(self.subject), {"stats.view_all"})

    def test_auto_save_returns_confirmed_permissions_and_supports_full_revocation(self):
        headers = {"X-Requested-With": "fetch", "Accept": "application/json"}
        for subject in (self.subject, "cookie_session:stable-b"):
            with self.subTest(subject=subject):
                self.grant(subject=subject)
                for choices, expected in (
                    (["tasks.manage_all"], ["tasks.manage_all", "tasks.view_all"]),
                    (
                        ["stats.view_all", "rules.manage"],
                        ["rules.manage", "stats.view_all"],
                    ),
                    ([], []),
                ):
                    response = self.save(subject, choices, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        response.get_json(),
                        {"subject": subject, "permissions": expected},
                    )
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    with self.app.app_context():
                        self.assertEqual(subject_permissions(subject), set(expected))
        with self.root.session_transaction() as session:
            self.assertFalse(session.get("_flashes"))

    def test_auto_save_preserves_authorization_and_page_token_validation(self):
        self.grant("stats.view_all")
        headers = {"X-Requested-With": "fetch"}
        for subject, permissions, extra, status in (
            (self.subject, [], {"csrf_token": "wrong-token"}, 400),
            (self.subject, ["permissions.manage"], {}, 400),
            ("cookie_session:unknown", ["tasks.view_all"], {}, 404),
        ):
            response = self.save(subject, permissions, headers=headers, **extra)
            self.assertEqual(response.status_code, status)
        self.assertEqual(
            self.client.post(
                "/admin/permissions",
                data={"subject": self.subject, "permissions": "tasks.manage_all"},
                headers=headers,
            ).status_code,
            403,
        )
        with self.app.app_context():
            self.assertEqual(subject_permissions(self.subject), {"stats.view_all"})

    def test_navigation_uses_grants_directly_on_user_and_management_pages(self):
        for cookie_mode in (False, True):
            if cookie_mode:
                self.cookie_mode()
            with self.subTest(cookie_mode=cookie_mode):
                self.grant("tasks.view_all", "stats.view_all", "rules.manage")
                for route in ("/", "/models", "/admin/tasks", "/admin/rules"):
                    soup = BeautifulSoup(self.client.get(route).text, "html.parser")
                    for href in (
                        "/admin",
                        "/admin/tasks",
                        "/admin/consistency",
                        "/admin/language-consistency",
                        "/admin/images",
                        "/admin/videos",
                        "/admin/rules",
                        "/models",
                    ):
                        self.assertIsNotNone(
                            soup.select_one(f'nav.nav a[href="{href}"]')
                        )
                    self.assertIsNone(soup.select_one(".topbar-account a"))
                    self.assertIsNone(
                        soup.select_one('nav.nav a[href="/admin/permissions"]')
                    )
                self.grant("rules.manage")
                soup = BeautifulSoup(
                    self.client.get("/admin/rules").text, "html.parser"
                )
                self.assertIsNotNone(soup.select_one('nav.nav a[href="/"]'))
                self.assertIsNone(soup.select_one('nav.nav a[href="/admin/tasks"]'))
                self.assertIsNone(soup.select_one('nav.nav a[href="/admin"]'))

    def test_view_permission_reads_all_task_types_but_preserves_own_default_actions(
        self,
    ):
        self.grant("tasks.view_all")
        routes = (
            ("document_check", "/admin/tasks"),
            ("consistency_check", "/admin/consistency"),
            ("language_consistency_check", "/admin/language-consistency"),
            ("image_check", "/admin/images"),
            ("video_check", "/admin/videos"),
        )
        for task_type, route in routes:
            with self.subTest(task_type=task_type):
                own = self.fixture._insert_task(task_type=task_type)
                other = self.fixture._insert_task(task_type=task_type, ip="10.0.0.9")
                page = self.client.get(route)
                soup = BeautifulSoup(page.text, "html.parser")
                self.assertEqual(page.status_code, 200)
                self.assertIsNone(soup.select_one(".admin-metric-group"))
                self.assertIsNotNone(
                    soup.select_one(f'[data-task-id="{own}"] [data-bulk-task]')
                )
                self.assertIsNone(
                    soup.select_one(f'[data-task-id="{other}"] [data-bulk-task]')
                )
                self.assertIsNone(soup.select_one(f'[data-task-id="{other}"] form'))
                self.assertEqual(
                    self.client.get(f"/admin/tasks/{other}").status_code, 200
                )
        own = self.fixture._insert_task()
        self.assertEqual(
            self.client.post(f"/admin/tasks/{own}/delete").status_code, 302
        )
        self.assertEqual(self.client.get("/admin").status_code, 403)

    def test_view_only_report_has_readonly_reviews_and_covers_download_export_and_polling(
        self,
    ):
        self.grant("tasks.view_all")
        task = self.report_task()
        Path(self.app.config["UPLOAD_FOLDER"], "stored.txt").write_text(
            "原始文件", encoding="utf-8"
        )
        page = self.client.get(f"/admin/tasks/{task}")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        self.assertTrue(soup.select("[data-report-item]"))
        self.assertIsNone(soup.select_one("[data-report-acceptance-status]"))
        self.assertIsNone(soup.select_one("[data-report-import-trigger]"))
        self.assertIsNone(soup.select_one("[data-retry-check]"))
        for suffix in (
            "/export",
            "/export.xlsx",
            "/document",
            "/model-output?state_only=1",
        ):
            with self.subTest(suffix=suffix):
                response = self.client.get(f"/admin/tasks/{task}{suffix}")
                self.assertEqual(response.status_code, 200)
                response.close()
        progress = self.client.get(f"/admin/tasks/{task}?_poll=1").get_json()
        self.assertFalse(progress["can_manage"])
        states = self.client.get(
            f"/admin/tasks/{task}/model-output?state_only=1"
        ).get_json()
        self.assertFalse(states["can_manage"])
        statuses = self.client.get(f"/admin/task-statuses?ids={task}").get_json()
        self.assertEqual(statuses["counts"], {})
        self.assertEqual([row["id"] for row in statuses["tasks"]], [task])

    def test_view_permission_cannot_modify_others_via_any_mutation_or_mixed_bulk_request(
        self,
    ):
        self.grant("tasks.view_all")
        own = self.fixture._insert_task()
        other = self.fixture._insert_task(ip="10.0.0.9")
        for suffix in (
            "/cancel",
            "/retry",
            "/delete",
            "/cancel-check",
            "/retry-check",
            "/report-items",
            "/import.xlsx",
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    self.client.post(f"/admin/tasks/{other}{suffix}").status_code, 403
                )
        response = self.client.post(
            "/admin/tasks/bulk-delete", data={"task_ids": [str(own), str(other)]}
        )
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            self.assertEqual(
                get_db()
                .execute("SELECT COUNT(*) FROM tasks WHERE id IN (?, ?)", (own, other))
                .fetchone()[0],
                2,
            )

    def test_manage_permission_changes_ui_and_allows_foreign_report_review_and_task_actions(
        self,
    ):
        self.grant("tasks.manage_all")
        task = self.report_task()
        page = self.client.get(f"/admin/tasks/{task}")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertIsNotNone(soup.select_one("[data-report-acceptance-status]"))
        self.assertIsNotNone(soup.select_one("[data-report-import-trigger]"))
        item = soup.select_one("[data-report-item]")
        response = self.client.post(
            f"/admin/tasks/{task}/report-items",
            json={
                "result_code": item["data-result-code"],
                "item_id": item["data-item-id"],
                "acceptance_status": "accepted",
            },
        )
        self.assertEqual(response.status_code, 200)
        queued = self.fixture._insert_task(ip="10.0.0.9", status="queued")
        self.assertEqual(
            self.client.post(f"/admin/tasks/{queued}/cancel").status_code, 302
        )
        self.assertEqual(
            self.client.post(
                "/admin/tasks/bulk-delete", data={"task_ids": [str(task), str(queued)]}
            ).status_code,
            302,
        )
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0
            )

    def test_revoking_management_updates_detail_revision_and_output_flags(self):
        self.grant("tasks.manage_all")
        task = self.report_task()
        before = self.client.get(f"/admin/tasks/{task}?_poll=1").get_json()
        self.assertTrue(before["can_manage"])
        self.grant("tasks.view_all")
        after = self.client.get(
            f"/admin/tasks/{task}?_poll=1&revision={before['revision']}"
        ).get_json()
        self.assertFalse(after["can_manage"])
        self.assertIn("html", after)
        self.assertNotIn("data-report-acceptance-status", after["html"])
        self.assertEqual(
            self.client.post(f"/admin/tasks/{task}/delete").status_code, 403
        )
        self.grant()
        for suffix in (
            "",
            "?_poll=1",
            "/export",
            "/document",
            "/model-output?state_only=1",
        ):
            self.assertEqual(
                self.client.get(
                    f"/admin/tasks/{task}{suffix}",
                    headers={"X-Requested-With": "fetch"},
                ).status_code,
                403,
            )

    def test_stats_permission_has_separate_navigation_and_no_task_access(self):
        self.grant("stats.view_all")
        soup = BeautifulSoup(self.client.get("/").text, "html.parser")
        self.assertIsNotNone(soup.select_one('nav.nav a[href="/admin"]'))
        self.assertIsNone(soup.select_one(".topbar-account a"))
        page = self.client.get("/admin")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertEqual(page.status_code, 200)
        self.assertIsNone(soup.select_one('nav a[href="/admin/tasks"]'))
        self.assertIsNotNone(soup.select_one('nav.nav a[href="/"]'))
        self.assertIsNone(soup.select_one('nav a[href="/admin/permissions"]'))
        self.assertIsNone(soup.select_one('form[action="/admin/logout"]'))
        self.assertEqual(self.client.get("/admin/tasks").status_code, 403)

    def test_rules_permission_uses_separate_page_and_rejects_runtime_setting_actions(
        self,
    ):
        self.grant("rules.manage")
        page = self.client.get("/admin/rules")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertEqual(page.status_code, 200)
        self.assertIsNotNone(soup.select_one("[data-prompt-tabs]"))
        self.assertIsNotNone(soup.select_one("[data-report-suppression-rules]"))
        for action in ("concurrency", "network", "diagnostics", "ip_username"):
            self.assertIsNone(
                soup.select_one(f'input[name="action"][value="{action}"]')
            )
            self.assertEqual(
                self.client.post("/admin/rules", data={"action": action}).status_code,
                403,
            )
        response = self.client.post(
            "/admin/rules",
            data={
                "action": "create_check_item",
                "task_type": "document_check",
                "name": "授权规则",
                "prompt": "检查文档中的事实。",
                "enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin/rules")
        with self.app.app_context():
            self.assertIsNotNone(
                get_db()
                .execute("SELECT id FROM check_items WHERE name = '授权规则'")
                .fetchone()
            )
            self.assertEqual(get_setting("global_concurrency"), 3)

    def test_even_all_grants_cannot_access_or_modify_superadmin_functions(self):
        self.grant(*ASSIGNABLE_PERMISSIONS)
        for route in (
            "/admin/settings",
            "/admin/settings?tab=ip_users",
            "/admin/settings/task-cache",
            "/admin/permissions",
        ):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 403)
        for route in (
            "/admin/settings",
            "/admin/settings/task-cache/cleanup",
            "/admin/permissions",
        ):
            self.assertEqual(
                self.client.post(route, json={"task_ids": [1]}).status_code, 403
            )
        page = self.client.get("/admin/tasks")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertIsNone(soup.select_one('nav a[href="/admin/permissions"]'))
        self.assertIsNone(soup.select_one('nav a[href="/admin/settings"]'))
        self.assertIsNotNone(soup.select_one('nav a[href="/admin/rules"]'))

    def test_management_mutations_reject_cross_site_browser_requests(self):
        self.grant("tasks.manage_all", "rules.manage")
        task = self.fixture._insert_task(ip="10.0.0.9", status="queued")
        for headers in (
            {"Origin": "https://other.example.test"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Origin": "null"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(
                    self.client.post(
                        f"/admin/tasks/{task}/cancel", headers=headers
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(
                        "/admin/rules",
                        headers=headers,
                        data={
                            "action": "create_check_item",
                            "name": "外部表单",
                            "prompt": "内容",
                        },
                    ).status_code,
                    403,
                )
        with self.app.app_context():
            self.assertEqual(
                get_db()
                .execute("SELECT status FROM tasks WHERE id = ?", (task,))
                .fetchone()[0],
                "queued",
            )
            self.assertIsNone(
                get_db()
                .execute("SELECT id FROM check_items WHERE name = '外部表单'")
                .fetchone()
            )
        self.assertEqual(
            self.client.post(
                f"/admin/tasks/{task}/cancel", headers={"Origin": "http://localhost"}
            ).status_code,
            302,
        )

    def test_stats_grant_changes_list_metrics_and_polling_permissions(self):
        self.grant("tasks.view_all")
        task = self.fixture._insert_task(ip="10.0.0.9")
        before = self.client.get(f"/admin/task-statuses?ids={task}").get_json()
        self.assertEqual(before["permission_signature"], "0:0")
        self.grant("tasks.view_all", "stats.view_all")
        soup = BeautifulSoup(self.client.get("/admin/tasks").text, "html.parser")
        self.assertTrue(soup.select(".admin-metric-group"))
        after = self.client.get(f"/admin/task-statuses?ids={task}").get_json()
        self.assertEqual(after["counts"]["tasks"], 1)
        self.assertEqual(after["permission_signature"], "0:1")

    def test_cookie_users_at_the_same_ip_have_independent_grants_and_grants_follow_stable_id(
        self,
    ):
        self.cookie_mode()
        self.grant("tasks.view_all")
        task = self.fixture._insert_task(
            owner_subject="cookie_session:b", owner_source="cookie_session"
        )
        self.assertEqual(self.client.get(f"/admin/tasks/{task}").status_code, 200)
        self.resolve.return_value = (
            {"user_id": "b", "username": "同 IP 用户", "_profile_version": 2},
            None,
        )
        self.client.set_cookie("enterprise-ticket", "b")
        self.assertEqual(
            self.client.get(
                f"/admin/tasks/{task}", headers={"X-Requested-With": "fetch"}
            ).status_code,
            403,
        )
        self.resolve.return_value = (
            {
                "user_id": "a",
                "username": "新姓名",
                "employee_number": "999",
                "_profile_version": 3,
            },
            None,
        )
        self.client.set_cookie("enterprise-ticket", "new-ticket")
        self.assertEqual(
            self.client.get(
                f"/admin/tasks/{task}", environ_overrides={"REMOTE_ADDR": "10.0.0.8"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(f"/admin/tasks/{task}/delete").status_code, 403
        )

    def test_cookie_auth_failure_never_uses_ip_grants_and_root_can_still_manage_permissions(
        self,
    ):
        self.cookie_mode()
        self.grant(*ASSIGNABLE_PERMISSIONS, subject="ip:127.0.0.1")
        self.grant("rules.manage")
        self.resolve.return_value = (None, None)
        for route in ("/admin/rules", "/admin/tasks", "/admin"):
            self.assertEqual(
                self.client.get(
                    route, headers={"X-Requested-With": "fetch"}
                ).status_code,
                401,
            )
        self.assertEqual(self.root.get("/admin/permissions").status_code, 200)
        self.assertEqual(self.root.get("/admin/settings").status_code, 200)

    def test_data_migration_and_cookie_rollout_keep_ip_and_cookie_grants_separate(self):
        self.grant("tasks.manage_all", subject="ip:127.0.0.1")
        task = self.fixture._insert_task()
        self.cookie_mode(rollout=True)
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.app.app_context():
            self.assertEqual(
                get_db()
                .execute("SELECT owner_subject FROM tasks WHERE id = ?", (task,))
                .fetchone()[0],
                self.subject,
            )
            self.assertEqual(subject_permissions(self.subject), frozenset())
            self.assertEqual(
                subject_permissions("ip:127.0.0.1"),
                {"tasks.view_all", "tasks.manage_all"},
            )
        self.assertEqual(
            self.client.get(
                "/admin/tasks", headers={"X-Requested-With": "fetch"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/admin/tasks",
                environ_overrides={"REMOTE_ADDR": "10.0.0.8"},
                headers={"X-Requested-With": "fetch"},
            ).status_code,
            403,
        )
        self.grant("rules.manage", subject="ip:10.0.0.8")
        self.assertEqual(
            self.client.get(
                "/admin/rules", environ_overrides={"REMOTE_ADDR": "10.0.0.8"}
            ).status_code,
            200,
        )

    def test_existing_data_initialization_registers_users_without_assigning_permissions(
        self,
    ):
        task = self.fixture._insert_task(ip="10.0.0.9")
        self.fixture._configure_provider("cookie_session:provider-owner")
        with self.app.app_context():
            sync_identity_profile(
                "cookie_session:profile-owner",
                {
                    "display_name": "资料用户",
                    "employee_number": "",
                    "avatar": "",
                    "label": "资料用户",
                    "version": 1,
                },
            )
            db = get_db()
            db.execute("DROP TABLE user_permissions")
            db.execute("DROP TABLE user_identities")
            db.commit()
            init_db()
            subjects = {
                row[0] for row in db.execute("SELECT subject FROM user_identities")
            }
            self.assertTrue(
                {
                    "ip:10.0.0.9",
                    "cookie_session:provider-owner",
                    "cookie_session:profile-owner",
                }
                <= subjects
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM user_permissions").fetchone()[0], 0
            )
            self.assertIsNotNone(
                db.execute("SELECT id FROM tasks WHERE id = ?", (task,)).fetchone()
            )
            register_subject("ip:10.0.0.9")
            replace_subject_permissions("ip:10.0.0.9", {"rules.manage"})
            init_db()
            self.assertEqual(subject_permissions("ip:10.0.0.9"), {"rules.manage"})


if __name__ == "__main__":
    unittest.main()
