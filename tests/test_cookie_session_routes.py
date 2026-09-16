import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from app.identity import cookie_session as cookie_session_cache
from app.identity.cookie_session import CookieSessionExpired
from app.identity.service import current_identity
from app.infrastructure.config import _normalize_auth
from app.persistence.connection import get_db
from app.persistence.settings import (
    get_setting,
    migrate_ip_owner_to_subject,
    set_ip_username,
    sync_identity_profile,
)
from scripts.audit_ip_owners import remaining_ip_owners
from tests import test_routes


class CookieSessionRoutesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_routes.AdminSettingsRouteTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.app
        self.client = self.fixture.client
        cookie_session_cache._cache.clear()
        cookie_session_cache._inflight.clear()
        cookie_session_cache._next_cleanup = 0
        self.app.config["AUTH"] = _normalize_auth(
            {
                "mode": "cookie_session",
                "cookie_session": {
                    "userinfo_url": "https://userinfo.example.test/user",
                    "cookie_header_name": "X-Upstream-Cookie",
                    "field_mapping": {"user_id": "uuid"},
                    "login_url": "https://login.example.test/login?app=docs#signin",
                },
            }
        )
        self.client.set_cookie("enterprise-ticket", "review-ticket")
        resolver = patch(
            "app.identity.service.resolve_userinfo",
            return_value=(
                {"user_id": "user-a", "username": "张三", "avatar": ""},
                None,
            ),
        )
        self.resolve = resolver.start()
        self.addCleanup(resolver.stop)

    def _enable_cookie_session_for_ips(self, *ips: str):
        self.app.config["AUTH"] = _normalize_auth(
            {
                "mode": "ip",
                "cookie_session": {
                    "userinfo_url": "https://userinfo.example.test/user",
                    "cookie_header_name": "X-Upstream-Cookie",
                    "field_mapping": {"user_id": "uuid"},
                    "login_url": "https://login.example.test/login?app=docs#signin",
                    "enabled_ips": list(ips),
                },
            }
        )

    def test_migrated_and_own_tasks_are_visible_but_same_ip_peer_is_private(self):
        insert = self.fixture._insert_task
        legacy = insert()
        own = insert(
            ip="10.0.0.1",
            owner_subject="cookie_session:user-a",
            owner_source="cookie_session",
        )
        peer = insert(
            owner_subject="cookie_session:user-b", owner_source="cookie_session"
        )
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        self.assertEqual(
            {int(row["data-task-id"]) for row in soup.select("[data-task-id]")},
            {legacy, own},
        )
        self.assertEqual(self.client.get(f"/tasks/{legacy}").status_code, 200)
        self.assertEqual(self.client.get(f"/tasks/{own}").status_code, 200)
        for suffix in ("", "/export", "/export.xlsx"):
            self.assertEqual(self.client.get(f"/tasks/{peer}{suffix}").status_code, 404)
        self.assertEqual(
            self.client.post(
                f"/tasks/{peer}/cancel-check", json={"code": "compliance"}
            ).status_code,
            404,
        )
        status = self.client.get(f"/task-statuses?ids={legacy},{own},{peer}").get_json()
        self.assertEqual({row["id"] for row in status["tasks"]}, {legacy, own})
        self.assertEqual(self.client.get(f"/admin/tasks/{peer}").status_code, 200)

    def test_browser_cookie_and_migration_are_resolved_once_per_request(self):
        self.fixture._configure_provider("ip:127.0.0.1")
        with patch(
            "app.identity.service.migrate_ip_owner_to_subject",
            wraps=migrate_ip_owner_to_subject,
        ) as migrate:
            response = self.client.get("/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.resolve.call_count, 1)
        self.assertIn("enterprise-ticket=review-ticket", self.resolve.call_args.args[1])
        migrate.assert_called_once_with("127.0.0.1", "cookie_session:user-a")
        with self.app.app_context():
            owner = (
                get_db()
                .execute("SELECT owner_subject FROM user_model_providers")
                .fetchone()[0]
            )
        self.assertEqual(owner, "cookie_session:user-a")

    def test_ip_mode_cookie_session_rollout_migrates_enabled_ip_only(self):
        self._enable_cookie_session_for_ips("10.0.0.8")
        matching_task = self.fixture._insert_task(
            ip="10.0.0.8", owner_subject="ip:10.0.0.8", owner_source="ip"
        )
        other_task = self.fixture._insert_task(
            ip="10.0.0.9", owner_subject="ip:10.0.0.9", owner_source="ip"
        )
        self.fixture._configure_provider("ip:10.0.0.8")
        self.fixture._configure_provider("ip:10.0.0.9")

        response = self.client.get("/models", headers={"X-Real-IP": "10.0.0.8"})

        self.assertEqual(response.status_code, 200)
        self.resolve.assert_called_once()
        with self.app.app_context():
            tasks = {
                row["id"]: row
                for row in get_db()
                .execute("SELECT id, owner_subject, owner_source, ip FROM tasks")
                .fetchall()
            }
            provider_owners = [
                row["owner_subject"]
                for row in (
                    get_db()
                    .execute(
                        "SELECT owner_subject FROM user_model_providers ORDER BY owner_subject"
                    )
                    .fetchall()
                )
            ]
        self.assertEqual(tasks[matching_task]["owner_subject"], "cookie_session:user-a")
        self.assertEqual(tasks[matching_task]["owner_source"], "cookie_session")
        self.assertEqual(tasks[matching_task]["ip"], "10.0.0.8")
        self.assertEqual(tasks[other_task]["owner_subject"], "ip:10.0.0.9")
        self.assertEqual(provider_owners, ["cookie_session:user-a", "ip:10.0.0.9"])

    def test_ip_mode_cookie_session_rollout_keeps_other_ips_on_ip_identity(self):
        self._enable_cookie_session_for_ips("10.0.0.8")
        self.fixture._configure_provider("ip:10.0.0.9")
        self.resolve.reset_mock()

        response = self.client.get("/models", headers={"X-Real-IP": "10.0.0.9"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("测试提供商", response.text)
        self.resolve.assert_not_called()
        with self.app.app_context():
            owner = (
                get_db()
                .execute("SELECT owner_subject FROM user_model_providers")
                .fetchone()["owner_subject"]
            )
        self.assertEqual(owner, "ip:10.0.0.9")

    def test_ip_mode_cookie_session_rollout_requires_cookie_for_enabled_ip(self):
        self._enable_cookie_session_for_ips("10.0.0.8")
        self.resolve.return_value = (None, CookieSessionExpired("401"))

        response = self.client.get("/", headers={"X-Real-IP": "10.0.0.8"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("login.example.test", response.location)
        self.assertEqual(
            parse_qs(urlsplit(response.location).query)["redirect"],
            ["/"],
        )

    def test_login_redirect_preserves_query_and_existing_login_parameters(self):
        self.resolve.return_value = (None, CookieSessionExpired("401"))
        response = self.client.get("/?status=failed&page=2&keyword=a%26b")
        self.assertEqual(response.status_code, 302)
        parts = urlsplit(response.location)
        query = parse_qs(parts.query)
        self.assertEqual(query["app"], ["docs"])
        self.assertEqual(query["redirect"], ["/?status=failed&page=2&keyword=a%26b"])
        self.assertEqual(parts.fragment, "signin")

    def test_fetch_receives_401_and_returns_to_page_instead_of_action(self):
        self.resolve.return_value = (None, CookieSessionExpired("401"))
        response = self.client.post(
            "/tasks/1/cancel-check",
            json={"code": "compliance"},
            headers={
                "X-Requested-With": "fetch",
                "X-Return-To": "/?page=2",
            },
        )
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.is_json)
        self.assertEqual(response.headers["X-Login-URL"], response.json["login_url"])
        self.assertEqual(
            parse_qs(urlsplit(response.json["login_url"]).query)["redirect"],
            ["/?page=2"],
        )

    def test_ip_audit_excludes_cookie_owners_and_reports_remaining_models(self):
        self.fixture._insert_task(ip="10.0.0.8", username_snapshot="王五")
        self.fixture._insert_task(ip="10.0.0.8")
        self.fixture._insert_task(
            ip="10.0.0.8",
            owner_subject="cookie_session:someone",
            owner_source="cookie_session",
        )
        self.fixture._configure_provider("ip:10.0.0.9")
        with self.app.app_context():
            set_ip_username("10.0.0.9", "赵六")
        rows = remaining_ip_owners(Path(self.app.config["DATABASE"]))
        self.assertEqual(
            rows,
            [
                {
                    "ip": "10.0.0.8",
                    "username": "王五",
                    "task_count": 2,
                    "provider_count": 0,
                    "model_count": 0,
                },
                {
                    "ip": "10.0.0.9",
                    "username": "赵六",
                    "task_count": 0,
                    "provider_count": 1,
                    "model_count": 1,
                },
            ],
        )
        with self.app.app_context():
            migrate_ip_owner_to_subject("10.0.0.8", "cookie_session:user-a")
            migrate_ip_owner_to_subject("10.0.0.9", "cookie_session:user-b")
        self.assertEqual(remaining_ip_owners(Path(self.app.config["DATABASE"])), [])

    def test_audit_missing_database_is_not_created(self):
        missing = Path(self.app.config["ROOT_DIR"]) / "missing.sqlite3"
        with self.assertRaises(FileNotFoundError):
            remaining_ip_owners(missing)
        self.assertFalse(missing.exists())

    def _profile(self, version, name="新姓名", number="00222"):
        return {
            "user_id": "user-a",
            "username": name,
            "employee_number": number,
            "avatar": f"https://avatar.example.test/{number}",
            "_profile_version": version,
        }

    def test_profile_changes_update_all_task_views_and_preserve_ownership_snapshots(
        self,
    ):
        self.resolve.return_value = (self._profile(1, "旧姓名", "00111"), None)
        task = self.fixture._insert_task(
            owner_subject="cookie_session:user-a",
            owner_source="cookie_session",
            owner_name_snapshot="提交姓名",
            username_snapshot="提交工号",
        )
        peer = self.fixture._insert_task(
            owner_subject="cookie_session:user-b",
            owner_source="cookie_session",
            owner_name_snapshot="其他用户",
        )
        self.fixture._configure_provider("cookie_session:user-a")
        self.assertIn("旧姓名（00111）", self.client.get(f"/tasks/{task}").text)
        self.resolve.return_value = (self._profile(2), None)
        response = self.client.get("/task-statuses", query_string={"ids": str(task)})
        self.assertEqual(
            json.loads(response.headers["X-User-Profile"])["label"], "新姓名（00222）"
        )
        self.assertEqual(
            response.json["tasks"][0]["owner_profile_label"], "新姓名（00222）"
        )
        for url in (
            f"/tasks/{task}",
            f"/admin/tasks/{task}",
            f"/tasks/{task}/export",
            f"/admin/tasks/{task}/export",
            "/admin/tasks",
            "/admin?start_date=2026-05-01&end_date=2026-05-01",
        ):
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200)
                self.assertIn("新姓名（00222）", page.text)
                self.assertNotIn("旧姓名（00111）", page.text)
                self.assertNotIn("提交姓名", page.text)
        for keyword, expected in (
            ("新姓名", {task}),
            ("00222", {task}),
            ("旧姓名", set()),
            ("00111", set()),
            ("提交姓名", set()),
        ):
            page = self.client.get("/admin/tasks", query_string={"keyword": keyword})
            soup = BeautifulSoup(page.text, "html.parser")
            self.assertEqual(
                {int(row["data-task-id"]) for row in soup.select("[data-task-id]")},
                expected,
            )
        self.assertIn("其他用户", self.client.get(f"/admin/tasks/{peer}").text)
        self.assertEqual(self.client.get(f"/tasks/{peer}").status_code, 404)
        with self.app.app_context():
            row = (
                get_db()
                .execute(
                    "SELECT owner_subject, owner_name_snapshot, username_snapshot FROM tasks WHERE id = ?",
                    (task,),
                )
                .fetchone()
            )
            self.assertEqual(
                tuple(row), ("cookie_session:user-a", "提交姓名", "提交工号")
            )
            self.assertEqual(
                get_db()
                .execute("SELECT owner_subject FROM user_model_providers")
                .fetchone()[0],
                "cookie_session:user-a",
            )

    def test_admin_keyword_search_matches_ip_and_account_in_cookie_session_mode(self):
        own = self.fixture._insert_task(
            ip="10.1.2.3",
            owner_subject="cookie_session:user-a",
            owner_source="cookie_session",
        )
        peer = self.fixture._insert_task(
            ip="10.9.9.9",
            owner_subject="cookie_session:user-b",
            owner_source="cookie_session",
        )
        page = self.client.get("/admin/tasks")
        self.assertEqual(page.status_code, 200)
        soup = BeautifulSoup(page.text, "html.parser")
        keyword_input = soup.select_one('.filter-bar input[name="keyword"]')
        self.assertIsNotNone(keyword_input)
        self.assertEqual(
            keyword_input.get("placeholder"), "按文档名称、用户名称或 IP 搜索"
        )
        for keyword, expected in (
            ("10.1.2.3", {own}),
            ("user-a", {own}),
            ("cookie_session:user-a", {own}),
            ("10.9.9.9", {peer}),
            ("user-b", {peer}),
        ):
            with self.subTest(keyword=keyword):
                page = self.client.get(
                    "/admin/tasks", query_string={"keyword": keyword, "_partial": "1"}
                )
                self.assertEqual(page.status_code, 200)
                soup = BeautifulSoup(page.text, "html.parser")
                self.assertEqual(
                    {int(row["data-task-id"]) for row in soup.select("[data-task-id]")},
                    expected,
                )

    def test_old_cookie_uses_latest_profile_without_writing_or_authorizing_expired_cookie(
        self,
    ):
        self.resolve.return_value = (self._profile(20), None)
        self.client.get("/")
        self.resolve.return_value = (self._profile(10, "旧姓名", "00111"), None)
        with self.app.test_request_context(
            "/", headers={"Cookie": "ticket=old-browser"}
        ):
            db = get_db()
            before = db.total_changes
            identity = current_identity()
            self.assertEqual(identity.label, "新姓名（00222）")
            self.assertEqual(identity.employee_number, "00222")
            self.assertEqual(identity.avatar, "https://avatar.example.test/00222")
            self.assertEqual(identity.profile_version, 20)
            self.assertEqual(db.total_changes, before)
        self.resolve.return_value = (None, CookieSessionExpired("401"))
        response = self.client.get(
            "/task-statuses", headers={"X-Requested-With": "fetch"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("X-User-Profile", response.headers)

    def test_admin_without_user_cookie_reads_persisted_profile(self):
        task = self.fixture._insert_task(
            owner_subject="cookie_session:user-a", owner_source="cookie_session"
        )
        self.resolve.return_value = (self._profile(20), None)
        self.client.get("/")
        self.resolve.return_value = (None, CookieSessionExpired("401"))
        page = self.client.get(f"/admin/tasks/{task}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("新姓名（00222）", page.text)
        self.assertNotIn("X-User-Profile", page.headers)
        overview = self.client.get("/admin?start_date=2026-05-01&end_date=2026-05-01")
        self.assertIn("新姓名（00222）", overview.text)

    def test_profile_change_refreshes_detail_even_without_new_model_output(self):
        task = self.fixture._insert_task(
            owner_subject="cookie_session:user-a",
            owner_source="cookie_session",
            status="running",
        )
        self.resolve.return_value = (self._profile(1, "旧姓名", "00111"), None)
        first = self.client.get(f"/tasks/{task}?_poll=1").json
        self.resolve.return_value = (self._profile(2), None)
        updated = self.client.get(
            f"/tasks/{task}", query_string={"_poll": "1", "revision": first["revision"]}
        ).json
        self.assertNotEqual(updated["revision"], first["revision"])
        self.assertIn("新姓名（00222）", updated["html"])
        self.resolve.return_value = (self._profile(3), None)
        unchanged = self.client.get(
            f"/tasks/{task}",
            query_string={"_poll": "1", "revision": updated["revision"]},
        ).json
        self.assertNotIn("html", unchanged)

    def test_profile_upsert_rejects_racing_older_result(self):
        with self.app.app_context():
            key = "identity_profile:cookie_session:user-a"
            latest = {"version": 20, "display_name": "新姓名"}
            sync_identity_profile("cookie_session:user-a", latest)
            with patch(
                "app.persistence.settings.get_setting", side_effect=[None, latest]
            ):
                result = sync_identity_profile(
                    "cookie_session:user-a", {"version": 10, "display_name": "旧姓名"}
                )
            self.assertEqual(result, latest)
            self.assertEqual(get_setting(key), latest)
