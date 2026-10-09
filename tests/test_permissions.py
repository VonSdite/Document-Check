import io
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from app.identity.permissions import ASSIGNABLE_PERMISSIONS, normalize_permissions
from app.infrastructure.config import _normalize_auth
from app.persistence.connection import close_db, get_db
from app.persistence.permissions import (
    register_subject,
    replace_subject_permissions,
    subject_permissions,
)
from app.persistence.schema import init_db
from app.persistence.settings import get_setting, set_ip_username, sync_identity_profile
from app.web import register_routes
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

    def test_console_entry_remains_accessible_after_user_grants(self):
        for cookie_mode in (False, True):
            if cookie_mode:
                self.cookie_mode()
            for grants in (
                (),
                *((permission,) for permission in ASSIGNABLE_PERMISSIONS),
                tuple(ASSIGNABLE_PERMISSIONS),
            ):
                with self.subTest(cookie_mode=cookie_mode, grants=grants):
                    self.grant(*grants)
                    response = self.client.get("/admin")
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.location, "/admin/login")
                    self.assertEqual(self.client.get("/admin/login").status_code, 200)
            self.grant("tasks.manage_all")
            response = self.client.post(
                "/admin/login", data={"username": "root", "password": "test-root"}
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, "/admin")
            self.assertEqual(self.client.get("/admin").status_code, 200)
            self.assertEqual(self.client.get("/admin/settings").status_code, 200)
            self.assertEqual(self.client.get("/admin/permissions").status_code, 200)
            with self.app.app_context():
                self.assertEqual(
                    subject_permissions(self.subject),
                    {"tasks.view_all", "tasks.manage_all"},
                )
            self.client.post("/admin/logout")

    def test_console_login_is_independent_of_cookie_identity_validity(self):
        self.cookie_mode()
        self.grant("stats.view_all")
        self.resolve.return_value = (None, None)
        self.assertEqual(self.client.get("/admin").location, "/admin/login")
        self.assertEqual(self.client.get("/admin/login").status_code, 200)
        response = self.client.post(
            "/admin/login", data={"username": "root", "password": "test-root"}
        )
        self.assertEqual(response.location, "/admin")
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/admin/settings").status_code, 200)

    def test_all_console_routes_require_superadmin_even_with_all_user_grants(self):
        for cookie_mode in (False, True):
            if cookie_mode:
                self.cookie_mode()
            self.grant(*ASSIGNABLE_PERMISSIONS)
            for rule in self.app.url_map.iter_rules():
                if not rule.endpoint.startswith("admin_") or rule.endpoint in {
                    "admin_login",
                    "admin_dashboard",
                }:
                    continue
                path = rule.rule.replace("<int:task_id>", "1").replace(
                    "<media_id>", "missing"
                )
                for method in sorted(rule.methods):
                    with self.subTest(
                        cookie_mode=cookie_mode, path=path, method=method
                    ):
                        response = self.client.open(path, method=method)
                        self.assertEqual(response.status_code, 403)
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
            for path in (
                "/admin/tasks",
                "/admin/rules",
                "/admin/settings",
                "/admin/models",
            ):
                with self.subTest(cookie_mode=cookie_mode, superadmin_path=path):
                    self.assertEqual(self.root.get(path).status_code, 200)

    def test_custom_console_prefix_is_superadmin_only_and_user_paths_stay_independent(
        self,
    ):
        for cookie_mode in (False, True):
            if cookie_mode:
                self.cookie_mode()
            self.grant(*ASSIGNABLE_PERMISSIONS)
            app = Flask(
                __name__,
                template_folder=self.app.template_folder,
                static_folder=self.app.static_folder,
            )
            app.config.update(self.app.config, ADMIN_URL="/private/ops")
            app.teardown_appcontext(close_db)
            app.add_template_filter(self.app.jinja_env.filters["markdown"], "markdown")
            register_routes(app)
            client = app.test_client()
            client.set_cookie("enterprise-ticket", "a")
            for path in ("/all/tasks", "/rules", "/overview"):
                with self.subTest(cookie_mode=cookie_mode, user_path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("/private/ops", response.text)
            self.assertEqual(client.get("/private/ops").location, "/private/ops/login")
            self.assertEqual(client.get("/private/ops/tasks").status_code, 403)
            self.assertEqual(client.get("/private/ops-extra").status_code, 404)
            response = client.post(
                "/private/ops/login", data={"username": "root", "password": "test-root"}
            )
            self.assertEqual(response.location, "/private/ops")
            page = client.get("/private/ops/tasks")
            self.assertEqual(page.status_code, 200)
            soup = BeautifulSoup(page.text, "html.parser")
            self.assertIsNotNone(
                soup.select_one('nav.nav a[href="/private/ops/tasks"]')
            )
            self.assertIsNone(soup.select_one('nav.nav a[href="/all/tasks"]'))

    def test_ip_permissions_ignore_unconfigured_headers_and_use_configured_proxy_identity(
        self,
    ):
        self.grant("rules.manage", subject="ip:10.0.0.8")
        for name in ("X-Real-IP", "X-Forwarded-For"):
            self.assertEqual(
                self.client.get(
                    "/rules",
                    headers={name: "10.0.0.8", "X-Requested-With": "fetch"},
                ).status_code,
                403,
            )
        self.app.config["REAL_IP_HEADER"] = "X-Verified-IP"
        self.assertEqual(
            self.client.get(
                "/rules",
                headers={"X-Verified-IP": "10.0.0.8", "X-Real-IP": "10.0.0.9"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/rules",
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
                "/rules", headers={"X-Forwarded-For": "10.0.0.8"}
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
        self.assertEqual(self.client.get("/all/tasks").status_code, 200)
        self.assertEqual(self.save(self.subject, ["stats.view_all"]).status_code, 302)
        self.assertEqual(self.client.get("/overview").status_code, 200)
        self.assertEqual(self.client.get("/all/tasks").status_code, 403)
        self.assertEqual(self.save(self.subject, []).status_code, 302)
        self.assertEqual(
            self.client.get(
                "/overview", headers={"X-Requested-With": "fetch"}
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
                for route in ("/", "/models", "/all/tasks", "/rules"):
                    soup = BeautifulSoup(self.client.get(route).text, "html.parser")
                    for href in (
                        "/overview",
                        "/all/tasks",
                        "/all/consistency",
                        "/all/language-consistency",
                        "/all/images",
                        "/all/videos",
                        "/rules",
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
                soup = BeautifulSoup(self.client.get("/rules").text, "html.parser")
                self.assertIsNotNone(soup.select_one('nav.nav a[href="/"]'))
                self.assertIsNone(soup.select_one('nav.nav a[href="/all/tasks"]'))
                self.assertIsNone(soup.select_one('nav.nav a[href="/overview"]'))

    def test_view_permission_reads_all_task_types_but_preserves_own_default_actions(
        self,
    ):
        self.grant("tasks.view_all")
        routes = (
            ("document_check", "/all/tasks"),
            ("consistency_check", "/all/consistency"),
            ("language_consistency_check", "/all/language-consistency"),
            ("image_check", "/all/images"),
            ("video_check", "/all/videos"),
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
                    self.client.get(f"/all/tasks/{other}").status_code, 200
                )
        own = self.fixture._insert_task()
        self.assertEqual(self.client.post(f"/all/tasks/{own}/delete").status_code, 302)
        self.assertEqual(self.client.get("/overview").status_code, 403)

    def test_all_task_pages_and_refreshes_keep_user_links_in_both_identity_modes(self):
        Path(self.app.config["UPLOAD_FOLDER"], "stored.txt").write_text(
            "原始文件", encoding="utf-8"
        )
        routes = (
            ("document_check", "/all/tasks"),
            ("consistency_check", "/all/consistency"),
            ("language_consistency_check", "/all/language-consistency"),
            ("image_check", "/all/images"),
            ("video_check", "/all/videos"),
        )
        for cookie_mode in (False, True):
            if cookie_mode:
                self.cookie_mode()
            self.grant(*ASSIGNABLE_PERMISSIONS)
            for task_type, route in routes:
                task = self.fixture._insert_task(task_type=task_type, status="partial")
                for path in (route, f"{route}?_partial=1", f"/all/tasks/{task}"):
                    with self.subTest(cookie_mode=cookie_mode, path=path):
                        page = self.client.get(path)
                        self.assertEqual(page.status_code, 200)
                        self.assertEqual(page.headers["Cache-Control"], "no-store")
                        self.assertNotIn("/admin", page.text)
                        soup = BeautifulSoup(page.text, "html.parser")
                        if urlparse(path).path == route:
                            self.assertIsNotNone(
                                soup.select_one(f'a[href="/all/tasks/{task}"]')
                            )
                            refresh = soup.select_one("[data-refresh-url]")
                            self.assertEqual(
                                urlparse(refresh["data-refresh-url"]).path,
                                "/all/task-statuses",
                            )
                            for link in soup.select(".pagination-controls a"):
                                self.assertEqual(urlparse(link["href"]).path, route)
                        else:
                            self.assertEqual(
                                soup.select_one(".report-back-button")["href"], route
                            )
                refreshed = self.client.get(f"/all/tasks/{task}?_poll=1").json
                self.assertNotIn("/admin", refreshed["html"])
                self.assertIn(f"/all/tasks/{task}/export", refreshed["html"])

    def test_all_task_submissions_and_action_redirects_keep_user_paths(self):
        for cookie_mode in (False, True):
            if cookie_mode:
                self.cookie_mode()
            self.grant("tasks.manage_all")
            for path, destination in (
                ("/all/tasks", "/all/tasks"),
                ("/all/tasks/new", "/all/tasks"),
                ("/all/consistency", "/all/consistency"),
                ("/all/language-consistency", "/all/language-consistency"),
                ("/all/images", "/all/images"),
                ("/all/videos", "/all/videos"),
                ("/all/tasks/bulk-delete", "/all/tasks"),
            ):
                with self.subTest(cookie_mode=cookie_mode, path=path):
                    response = self.client.post(path)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.location, destination)
            task = self.fixture._insert_task(task_type="image_check")
            response = self.client.get(f"/all/tasks/{task}/document")
            self.assertEqual(response.location, f"/all/tasks/{task}")
            response = self.client.post(f"/all/tasks/{task}/import.xlsx")
            self.assertEqual(response.location, f"/all/tasks/{task}")
            response = self.client.post(f"/all/tasks/{task}/delete")
            self.assertEqual(response.location, "/all/images")
            model_id = self.fixture._configure_provider(self.subject)
            with self.app.app_context():
                check_id = (
                    get_db()
                    .execute(
                        "SELECT id FROM check_items WHERE task_type = 'document_check' AND enabled = 1 LIMIT 1"
                    )
                    .fetchone()["id"]
                )
            response = self.client.post(
                "/all/tasks",
                data={
                    "document": (io.BytesIO("待检查文档".encode("utf-8")), "检查.txt"),
                    "checks": [str(check_id)],
                    "model_id": model_id,
                },
            )
            self.assertEqual(response.location, "/all/tasks")
            with self.app.app_context():
                task = (
                    get_db()
                    .execute(
                        "SELECT owner_subject, status FROM tasks ORDER BY id DESC LIMIT 1"
                    )
                    .fetchone()
                )
                self.assertEqual(tuple(task), (self.subject, "queued"))

    def test_all_task_upload_limit_redirects_to_same_user_page(self):
        self.grant("tasks.view_all")
        self.app.config["MAX_CONTENT_LENGTH"] = 1
        response = self.client.post(
            "/all/videos", data={"video": (io.BytesIO(b"video"), "test.mp4")}
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, "/all/videos")

    def test_view_only_report_has_readonly_reviews_and_covers_download_export_and_polling(
        self,
    ):
        self.grant("tasks.view_all")
        task = self.report_task()
        Path(self.app.config["UPLOAD_FOLDER"], "stored.txt").write_text(
            "原始文件", encoding="utf-8"
        )
        page = self.client.get(f"/all/tasks/{task}")
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
                response = self.client.get(f"/all/tasks/{task}{suffix}")
                self.assertEqual(response.status_code, 200)
                response.close()
        progress = self.client.get(f"/all/tasks/{task}?_poll=1").get_json()
        self.assertFalse(progress["can_manage"])
        states = self.client.get(
            f"/all/tasks/{task}/model-output?state_only=1"
        ).get_json()
        self.assertFalse(states["can_manage"])
        statuses = self.client.get(f"/all/task-statuses?ids={task}").get_json()
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
                    self.client.post(f"/all/tasks/{other}{suffix}").status_code, 403
                )
        response = self.client.post(
            "/all/tasks/bulk-delete", data={"task_ids": [str(own), str(other)]}
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
        page = self.client.get(f"/all/tasks/{task}")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertIsNotNone(soup.select_one("[data-report-acceptance-status]"))
        self.assertIsNotNone(soup.select_one("[data-report-import-trigger]"))
        item = soup.select_one("[data-report-item]")
        response = self.client.post(
            f"/all/tasks/{task}/report-items",
            json={
                "result_code": item["data-result-code"],
                "item_id": item["data-item-id"],
                "acceptance_status": "accepted",
            },
        )
        self.assertEqual(response.status_code, 200)
        queued = self.fixture._insert_task(ip="10.0.0.9", status="queued")
        self.assertEqual(
            self.client.post(f"/all/tasks/{queued}/cancel").status_code, 302
        )
        self.assertEqual(
            self.client.post(
                "/all/tasks/bulk-delete", data={"task_ids": [str(task), str(queued)]}
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
        before = self.client.get(f"/all/tasks/{task}?_poll=1").get_json()
        self.assertTrue(before["can_manage"])
        self.grant("tasks.view_all")
        after = self.client.get(
            f"/all/tasks/{task}?_poll=1&revision={before['revision']}"
        ).get_json()
        self.assertFalse(after["can_manage"])
        self.assertIn("html", after)
        self.assertNotIn("data-report-acceptance-status", after["html"])
        self.assertEqual(self.client.post(f"/all/tasks/{task}/delete").status_code, 403)
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
                    f"/all/tasks/{task}{suffix}",
                    headers={"X-Requested-With": "fetch"},
                ).status_code,
                403,
            )

    def test_stats_permission_has_separate_navigation_and_no_task_access(self):
        for cookie_mode in (False, True):
            if cookie_mode:
                self.cookie_mode()
            with self.subTest(cookie_mode=cookie_mode):
                self.grant("stats.view_all")
                soup = BeautifulSoup(self.client.get("/").text, "html.parser")
                self.assertIsNotNone(soup.select_one('nav.nav a[href="/overview"]'))
                self.assertIsNone(soup.select_one(".topbar-account a"))
                page = self.client.get("/overview")
                soup = BeautifulSoup(page.text, "html.parser")
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.headers["Cache-Control"], "no-store")
                self.assertIsNotNone(
                    soup.select_one('nav.nav a.active[href="/overview"]')
                )
                self.assertIsNone(soup.select_one('nav a[href="/all/tasks"]'))
                self.assertIsNotNone(soup.select_one('nav.nav a[href="/"]'))
                self.assertIsNone(soup.select_one('nav a[href="/admin/permissions"]'))
                self.assertIsNone(soup.select_one('form[action="/admin/logout"]'))
                self.assertEqual(self.client.get("/all/tasks").status_code, 403)
                self.assertEqual(self.client.get("/admin/overview").status_code, 403)
                self.assertEqual(
                    soup.select_one(".overview-filter")["action"], "/overview"
                )
                today = date.today()
                for period, days in (("today", 1), ("7-days", 7), ("30-days", 30)):
                    href = soup.select_one(f'[data-range="{period}"]')["href"]
                    self.assertEqual(urlparse(href).path, "/overview")
                    dates = {
                        "start_date": [(today - timedelta(days=days - 1)).isoformat()],
                        "end_date": [today.isoformat()],
                    }
                    self.assertEqual(parse_qs(urlparse(href).query), dates)
                    filtered = self.client.get(href)
                    self.assertEqual(filtered.status_code, 200)
                    filtered_soup = BeautifulSoup(filtered.text, "html.parser")
                    self.assertIsNotNone(
                        filtered_soup.select_one(f'[data-range="{period}"].active')
                    )
                    for name, value in dates.items():
                        self.assertEqual(
                            filtered_soup.select_one(f'input[name="{name}"]')["value"],
                            value[0],
                        )

    def test_user_overview_requires_stats_permission_after_revocation(self):
        for cookie_mode in (False, True):
            if cookie_mode:
                self.cookie_mode()
            for grants in ((), ("tasks.manage_all",), ("rules.manage",)):
                with self.subTest(cookie_mode=cookie_mode, grants=grants):
                    self.grant(*grants)
                    self.assertEqual(self.client.get("/overview").status_code, 403)
                    self.grant(*grants, "stats.view_all")
                    self.assertEqual(self.client.get("/overview").status_code, 200)
                    self.grant(*grants)
                    for headers in ({}, {"X-Requested-With": "fetch"}):
                        response = self.client.get("/overview", headers=headers)
                        self.assertEqual(response.status_code, 403)
                        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_superadmin_overview_links_keep_console_path(self):
        for cookie_mode in (False, True):
            if cookie_mode:
                self.cookie_mode()
                self.resolve.return_value = (None, None)
            for route in ("/admin", "/admin/overview"):
                with self.subTest(cookie_mode=cookie_mode, route=route):
                    response = self.root.get(route)
                    self.assertEqual(response.status_code, 200)
                    soup = BeautifulSoup(response.text, "html.parser")
                    self.assertIsNotNone(
                        soup.select_one('nav.nav a.active[href="/admin"]')
                    )
                    self.assertEqual(
                        soup.select_one(".overview-filter")["action"], route
                    )
                    for link in soup.select(".overview-quick-filters a"):
                        self.assertEqual(urlparse(link["href"]).path, route)
                        self.assertEqual(self.root.get(link["href"]).status_code, 200)

    def test_rules_permission_uses_separate_page_and_rejects_runtime_setting_actions(
        self,
    ):
        self.grant("rules.manage")
        page = self.client.get("/rules")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertEqual(page.status_code, 200)
        self.assertIsNotNone(soup.select_one("[data-prompt-tabs]"))
        self.assertIsNotNone(soup.select_one("[data-report-suppression-rules]"))
        for action in ("concurrency", "network", "diagnostics", "ip_username"):
            self.assertIsNone(
                soup.select_one(f'input[name="action"][value="{action}"]')
            )
            self.assertEqual(
                self.client.post("/rules", data={"action": action}).status_code,
                403,
            )
        response = self.client.post(
            "/rules",
            data={
                "action": "create_check_item",
                "task_type": "document_check",
                "name": "授权规则",
                "prompt": "检查文档中的事实。",
                "enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/rules")
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
        page = self.client.get("/all/tasks")
        soup = BeautifulSoup(page.text, "html.parser")
        self.assertIsNone(soup.select_one('nav a[href="/admin/permissions"]'))
        self.assertIsNone(soup.select_one('nav a[href="/admin/settings"]'))
        self.assertIsNotNone(soup.select_one('nav a[href="/rules"]'))

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
                        f"/all/tasks/{task}/cancel", headers=headers
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(
                        "/rules",
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
                f"/all/tasks/{task}/cancel", headers={"Origin": "http://localhost"}
            ).status_code,
            302,
        )

    def test_stats_grant_changes_list_metrics_and_polling_permissions(self):
        self.grant("tasks.view_all")
        task = self.fixture._insert_task(ip="10.0.0.9")
        before = self.client.get(f"/all/task-statuses?ids={task}").get_json()
        self.assertEqual(before["permission_signature"], "0:0")
        self.grant("tasks.view_all", "stats.view_all")
        soup = BeautifulSoup(self.client.get("/all/tasks").text, "html.parser")
        self.assertTrue(soup.select(".admin-metric-group"))
        after = self.client.get(f"/all/task-statuses?ids={task}").get_json()
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
        self.assertEqual(self.client.get(f"/all/tasks/{task}").status_code, 200)
        self.resolve.return_value = (
            {"user_id": "b", "username": "同 IP 用户", "_profile_version": 2},
            None,
        )
        self.client.set_cookie("enterprise-ticket", "b")
        self.assertEqual(
            self.client.get(
                f"/all/tasks/{task}", headers={"X-Requested-With": "fetch"}
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
                f"/all/tasks/{task}", environ_overrides={"REMOTE_ADDR": "10.0.0.8"}
            ).status_code,
            200,
        )
        self.assertEqual(self.client.post(f"/all/tasks/{task}/delete").status_code, 403)

    def test_cookie_auth_failure_never_uses_ip_grants_and_root_can_still_manage_permissions(
        self,
    ):
        self.cookie_mode()
        self.grant(*ASSIGNABLE_PERMISSIONS, subject="ip:127.0.0.1")
        self.grant("rules.manage")
        self.resolve.return_value = (None, None)
        for route in ("/rules", "/all/tasks", "/overview"):
            self.assertEqual(
                self.client.get(
                    route, headers={"X-Requested-With": "fetch"}
                ).status_code,
                401,
            )
        response = self.client.get(
            "/overview?start_date=2026-05-01&end_date=2026-05-07"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlparse(response.location).netloc, "login.example.test")
        self.assertEqual(
            parse_qs(urlparse(response.location).query)["redirect"],
            ["/overview?start_date=2026-05-01&end_date=2026-05-07"],
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
                "/all/tasks", headers={"X-Requested-With": "fetch"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/all/tasks",
                environ_overrides={"REMOTE_ADDR": "10.0.0.8"},
                headers={"X-Requested-With": "fetch"},
            ).status_code,
            403,
        )
        self.grant("rules.manage", subject="ip:10.0.0.8")
        self.assertEqual(
            self.client.get(
                "/rules", environ_overrides={"REMOTE_ADDR": "10.0.0.8"}
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
