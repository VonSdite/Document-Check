import json
import sqlite3
import unittest
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from app.contracts.user_domains import MAX_DOMAIN_NAME_LENGTH, MAX_USER_DOMAINS
from app.persistence.connection import get_db
from app.persistence.permissions import (
    permission_users,
    register_subject,
    replace_subject_permissions,
    subject_permissions,
)
from app.persistence.schema import init_db
from app.persistence.settings import sync_identity_profile
from app.persistence.user_domains import (
    product_domains,
    replace_user_domains,
    user_domains,
)
from tests import test_routes


class UserDomainTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_routes.AdminSettingsRouteTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.app
        self.admin = self.fixture.client
        self.subject = "ip:127.0.0.1"
        self.other = "cookie_session:domain-user"
        with self.app.app_context():
            register_subject(self.subject)
            register_subject(self.other)

    def save(self, domains, *, client=None, json_response=True, **extra):
        self.admin.get("/admin/permissions")
        with self.admin.session_transaction() as session:
            token = session["permission_csrf_token"]
        return (client or self.admin).post(
            "/admin/permissions",
            data={
                "action": "domains",
                "subject": self.subject,
                "domains": domains,
                "csrf_token": token,
                **extra,
            },
            headers={"Accept": "application/json"} if json_response else {},
        )

    def test_new_users_are_unassigned_and_domains_are_reusable_and_clearable(self):
        with self.app.app_context():
            self.assertEqual(user_domains([self.subject]), {self.subject: []})
        result = self.save(["  电源  ", "AI   服务", "ai 服务", "", "电源"])
        self.assertEqual(result.status_code, 200)
        domains = result.get_json()["domains"]
        self.assertEqual({item["name"] for item in domains}, {"电源", "AI 服务"})
        reused = self.save(["ai 服务"], subject=self.other).get_json()["domains"]
        self.assertEqual(
            reused, [item for item in domains if item["name"] == "AI 服务"]
        )
        self.assertEqual(self.save([]).get_json()["domains"], [])
        with self.app.app_context():
            self.assertEqual(product_domains(), domains)
            self.assertEqual(user_domains([self.other])[self.other], reused)
        self.assertEqual(self.save(["电源"]).get_json()["available_domains"], domains)

    def test_domains_and_permissions_save_independently(self):
        with self.app.app_context():
            replace_subject_permissions(self.subject, {"rules.manage"})
        self.save(["工控"])
        with self.app.app_context():
            self.assertEqual(subject_permissions(self.subject), {"rules.manage"})
        response = self.save([], action="permissions", permissions=["stats.view_all"])
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.assertEqual(subject_permissions(self.subject), {"stats.view_all"})
            self.assertEqual(
                user_domains([self.subject])[self.subject][0]["name"], "工控"
            )
        self.save([])
        with self.app.app_context():
            self.assertEqual(subject_permissions(self.subject), {"stats.view_all"})

    def test_identity_profile_changes_preserve_domains_on_the_stable_subject(self):
        self.save(["电源"], subject=self.other)
        with self.app.app_context():
            for version, label in [(1, "用户甲（100）"), (2, "新姓名（200）")]:
                sync_identity_profile(
                    self.other,
                    {"label": label, "display_name": label, "version": version},
                )
            rows, _, total = permission_users(keyword="新姓名", page=1, per_page=20)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["subject"], self.other)
            self.assertEqual(rows[0]["domains"][0]["name"], "电源")
            self.assertEqual(user_domains([self.subject])[self.subject], [])

    def test_domain_writes_require_superadmin_and_valid_page_token(self):
        self.save(["电源"])
        with self.app.app_context():
            replace_subject_permissions(
                self.subject,
                {
                    "tasks.view_all",
                    "tasks.manage_all",
                    "stats.view_all",
                    "rules.manage",
                },
            )
        self.assertEqual(
            self.save(["工控"], client=self.app.test_client()).status_code, 403
        )
        for token in ("", "wrong-token"):
            with self.subTest(token=token):
                self.assertEqual(self.save(["工控"], csrf_token=token).status_code, 400)
        self.assertEqual(self.save(["工控"], subject="ip:unknown").status_code, 404)
        self.assertEqual(self.save(["工控"], action="unknown").status_code, 400)
        with self.app.app_context():
            self.assertEqual([item["name"] for item in product_domains()], ["电源"])
            self.assertEqual(
                user_domains([self.subject])[self.subject][0]["name"], "电源"
            )

    def test_invalid_names_leave_assignments_and_catalog_unchanged(self):
        self.save(["电源"])
        for values in (
            ["新领域", "长" * (MAX_DOMAIN_NAME_LENGTH + 1)],
            [f"领域{index}" for index in range(MAX_USER_DOMAINS + 1)],
        ):
            with self.subTest(values=values):
                response = self.save(values)
                self.assertEqual(response.status_code, 400)
                self.assertIn("最多", response.get_json()["error"])
        with self.app.app_context():
            self.assertEqual([item["name"] for item in product_domains()], ["电源"])
            self.assertEqual(
                user_domains([self.subject])[self.subject][0]["name"], "电源"
            )
        values = ["长" * MAX_DOMAIN_NAME_LENGTH] + [
            f"领域{i}" for i in range(MAX_USER_DOMAINS - 1)
        ]
        self.assertEqual(len(self.save(values).get_json()["domains"]), MAX_USER_DOMAINS)

    def test_assignment_failure_rolls_back_catalog_and_previous_assignments(self):
        self.save(["电源"])
        with self.app.app_context():
            db = get_db()
            db.executescript(
                "CREATE TEMP TRIGGER reject_assignment BEFORE INSERT ON user_product_domains "
                "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END;"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                replace_user_domains(self.subject, ["工控"])
            self.assertEqual([item["name"] for item in product_domains()], ["电源"])
            self.assertEqual(
                user_domains([self.subject])[self.subject][0]["name"], "电源"
            )

    def test_filters_and_pagination_count_users_once_and_keep_query(self):
        with self.app.app_context():
            for index in range(23):
                subject = f"cookie_session:filter-{index:02}"
                register_subject(subject)
                replace_user_domains(subject, ["工控", "电源"])
            domain_id = next(
                item["id"] for item in product_domains() if item["name"] == "工控"
            )
            rows, page, total = permission_users(
                keyword="filter-", domain=str(domain_id), page=2, per_page=20
            )
            self.assertEqual((page, total, len(rows)), (2, 23, 3))
            self.assertTrue(all(len(row["domains"]) == 2 for row in rows))
            unassigned, _, total = permission_users(
                keyword="", domain="unassigned", page=1, per_page=20
            )
            self.assertEqual(total, 2)
            self.assertEqual(
                {row["subject"] for row in unassigned}, {self.subject, self.other}
            )
        query = {
            "domain": str(domain_id),
            "keyword": "filter-",
            "page": 2,
            "per_page": 20,
        }
        soup = BeautifulSoup(
            self.admin.get("/admin/permissions", query_string=query).text, "html.parser"
        )
        self.assertEqual(len(soup.select("[data-permission-user]")), 3)
        self.assertIn("共 23 条", soup.select_one(".pagination-summary").text)
        for link in soup.select(".pagination a[href]"):
            params = parse_qs(urlparse(link["href"]).query)
            self.assertEqual(params["domain"], [str(domain_id)])
            self.assertEqual(params["keyword"], ["filter-"])
        for form in soup.select(".pagination form, [data-permission-form]"):
            self.assertEqual(
                form.select_one('[name="domain"]')["value"], str(domain_id)
            )
        empty = BeautifulSoup(
            self.admin.get("/admin/permissions?domain=999999").text, "html.parser"
        )
        self.assertIsNotNone(
            empty.select_one("[data-user-domains-page] [data-domain-filter]")
        )
        self.assertFalse(empty.select("[data-permission-user]"))

    def test_invalid_filters_are_rejected_and_form_redirects_keep_filters(self):
        for domain in ("invalid", "-1", "1 OR 1=1", "１", "9" * 19):
            with self.subTest(domain=domain):
                response = self.admin.get(
                    "/admin/permissions", query_string={"domain": domain}
                )
                self.assertEqual(response.status_code, 400)
        for action in ("domains", "permissions"):
            response = self.save(
                ["电源"],
                json_response=False,
                action=action,
                domain="unassigned",
                keyword="127",
                page="2",
                per_page="50",
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(
                parse_qs(urlparse(response.location).query),
                {
                    "domain": ["unassigned"],
                    "keyword": ["127"],
                    "page": ["2"],
                    "per_page": ["50"],
                },
            )

    def test_domain_names_are_rendered_as_text_and_json_data(self):
        values = ["<img src=x onerror=alert(1)>", "'\"领域", "{{ 7 * 7 }}"]
        self.save(values)
        soup = BeautifulSoup(self.admin.get("/admin/permissions").text, "html.parser")
        root = soup.select_one('[data-user-domain-editor][data-subject="ip:127.0.0.1"]')
        self.assertEqual(
            {item["name"] for item in json.loads(root["data-domains"])}, set(values)
        )
        self.assertEqual(
            {tag.text for tag in root.select(".user-domain-tag")}, set(values)
        )
        self.assertFalse(root.select("img, script"))

    def test_existing_database_adds_domain_tables_and_reinitialization_preserves_data(
        self,
    ):
        with self.app.app_context():
            replace_subject_permissions(self.subject, {"rules.manage"})
            db = get_db()
            db.executescript(
                "DROP TABLE user_product_domains; DROP TABLE product_domains;"
            )
            init_db()
            self.assertEqual(user_domains([self.subject])[self.subject], [])
            self.assertEqual(subject_permissions(self.subject), {"rules.manage"})
            replace_user_domains(self.subject, ["工控"])
            before = product_domains()
            init_db()
            self.assertEqual(product_domains(), before)
            self.assertEqual(user_domains([self.subject])[self.subject], before)
            self.assertEqual(subject_permissions(self.subject), {"rules.manage"})


if __name__ == "__main__":
    unittest.main()
