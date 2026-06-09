import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from bs4 import BeautifulSoup
from flask import Flask
from openpyxl import Workbook

from app.auth import SAML_USER_SESSION_KEY
from app.config import CONFIG_FILENAME
from app.db import get_db, get_ip_username, get_setting, init_db, seed_defaults, set_setting
from app.formatting import render_markdown
from app.routes import _find_enabled_model, _upload_destination, get_enabled_models, register_routes
from app.task_types import CONSISTENCY_TASK_TYPE, DOCUMENT_TASK_TYPE, IMAGE_TASK_TYPE


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _xlsx_bytes(rows, *, title: str = "Sheet1") -> io.BytesIO:
    output = io.BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    workbook.save(output)
    workbook.close()
    output.seek(0)
    return output


def _pdf_with_image_bytes() -> io.BytesIO:
    import fitz

    document = fitz.open()
    page = document.new_page(width=240, height=180)
    page.insert_text((24, 32), "图 1 是电源接线图。")
    page.insert_image(fitz.Rect(24, 52, 84, 112), stream=_TINY_PNG)
    output = io.BytesIO(document.tobytes())
    document.close()
    output.seek(0)
    return output


def _saml_auth_config() -> dict:
    return {
        "mode": "saml",
        "saml": {
            "sp_entity_id": "https://doc.example.com/auth/saml/metadata",
            "acs_url": "https://doc.example.com/auth/saml/acs",
            "idp_entity_id": "https://sso.example.com/idp",
            "idp_sso_url": "https://sso.example.com/login",
            "idp_x509_cert": "test-cert",
            "user_id_attribute": "uid",
            "username_attribute": "displayName",
        },
    }


class _FakeSamlAuth:
    def __init__(self):
        self.processed_request_id = None

    def login(self, return_to=None):
        self.return_to = return_to
        return "https://sso.example.com/login?SAMLRequest=test"

    def process_response(self, request_id=None):
        self.processed_request_id = request_id

    def get_last_request_id(self):
        return "REQ-1"

    def get_errors(self):
        return []

    def is_authenticated(self):
        return True

    def get_nameid(self):
        return "nameid-1"

    def get_attributes(self):
        return {"uid": ["100086"], "displayName": ["张三"]}

    def get_friendlyname_attributes(self):
        return {}


class AdminSettingsRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root_dir = Path(self.temp_dir.name)
        project_root = Path(__file__).resolve().parents[1]
        self.app = Flask(
            __name__,
            template_folder=str(project_root / "app" / "templates"),
            static_folder=str(project_root / "app" / "static"),
        )
        self.app.add_template_filter(render_markdown, "markdown")
        self.app.config.update(
            SECRET_KEY="test-secret",
            ADMIN_URL="/admin",
            ROOT_DIR=root_dir,
            DATABASE=str(root_dir / "test.sqlite3"),
            UPLOAD_FOLDER=str(root_dir / "uploads"),
            NETWORK={"proxy_mode": "direct", "proxy": "", "ssl_verify": False},
        )
        Path(self.app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
        with self.app.app_context():
            init_db()
            seed_defaults()
        register_routes(self.app)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_logged_in"] = True

    def tearDown(self):
        self.temp_dir.cleanup()

    def _logout_test_client(self):
        with self.client.session_transaction() as session:
            session.clear()

    def _configure_provider(self, owner_subject: str = "ip:127.0.0.1") -> str:
        with self.app.app_context():
            now = "2026-05-01 09:00:00"
            cursor = get_db().execute(
                """
                INSERT INTO user_model_providers(
                    owner_subject, name, api_base, api_key,
                    request_timeout, max_input_chars, is_active, created_at, updated_at
                )
                VALUES (?, '测试提供商', 'https://example.test/v1/chat/completions', '',
                        30, 80000, 1, ?, ?)
                """,
                (owner_subject, now, now),
            )
            provider_id = cursor.lastrowid
            get_db().execute(
                """
                INSERT INTO user_model_configs(provider_id, model_name, force_disable_thinking, sort_order, created_at, updated_at)
                VALUES (?, 'model-a', 0, 10, ?, ?)
                """,
                (provider_id, now, now),
            )
            get_db().commit()
        return f"{provider_id}:0:model-a"

    def _insert_task(
        self,
        *,
        task_type: str = DOCUMENT_TASK_TYPE,
        ip: str = "127.0.0.1",
        status: str = "completed",
        created_at: str = "2026-05-01 10:00:00",
        username_snapshot: str | None = None,
        owner_subject: str | None = None,
        owner_name_snapshot: str | None = None,
        owner_source: str | None = None,
    ):
        with self.app.app_context():
            get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source,
                    original_filename, stored_filename, file_type,
                    file_size, checks_json, model_name, api_base, status, progress, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, '测试文档.txt', 'stored.txt', 'txt', 12, '[]', 'model-a', 'https://example.test/v1/chat/completions', ?, 100, ?, ?)
                """,
                (
                    task_type,
                    ip,
                    username_snapshot,
                    owner_subject,
                    owner_name_snapshot,
                    owner_source,
                    status,
                    created_at,
                    created_at,
                ),
            )
            get_db().commit()

    def test_admin_delete_task_reports_locked_file_without_removing_task(self):
        self._insert_task()
        upload_path = Path(self.app.config["UPLOAD_FOLDER"]) / "stored.txt"
        upload_path.write_text("content", encoding="utf-8")
        with self.app.app_context():
            task_id = get_db().execute("SELECT id FROM tasks WHERE stored_filename = 'stored.txt'").fetchone()["id"]

        with patch("app.routes.remove_file", return_value=(False, "[WinError 32] 文件正被占用")):
            response = self.client.post(f"/admin/tasks/{task_id}/delete", follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("任务文件正被其他程序使用", response.get_data(as_text=True))
        with self.app.app_context():
            task = get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertIsNotNone(task)
        self.assertTrue(upload_path.exists())

    def test_diagnostics_fetch_returns_saved_state(self):
        response = self.client.post(
            "/admin/settings",
            data={"action": "diagnostics", "llm_stream_trace_enabled": "on"},
            headers={"X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"llm_stream_trace_enabled": True})
        with self.app.app_context():
            self.assertTrue(get_setting("llm_stream_trace_enabled"))

    def test_diagnostics_toggle_is_unchecked_by_default(self):
        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        toggle = soup.find("input", {"name": "llm_stream_trace_enabled"})
        self.assertIsNotNone(toggle)
        self.assertEqual(toggle.get("data-saved-checked"), "false")
        self.assertIsNone(toggle.get("checked"))

    def test_diagnostics_toggle_treats_text_false_as_unchecked(self):
        with self.app.app_context():
            set_setting("llm_stream_trace_enabled", "false")

        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        toggle = soup.find("input", {"name": "llm_stream_trace_enabled"})
        self.assertIsNotNone(toggle)
        self.assertEqual(toggle.get("data-saved-checked"), "false")
        self.assertIsNone(toggle.get("checked"))

    def test_diagnostics_fetch_can_disable_setting(self):
        self.client.post(
            "/admin/settings",
            data={"action": "diagnostics", "llm_stream_trace_enabled": "on"},
            headers={"X-Requested-With": "fetch"},
        )

        response = self.client.post(
            "/admin/settings",
            data={"action": "diagnostics", "llm_stream_trace_enabled": "off"},
            headers={"X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"llm_stream_trace_enabled": False})
        with self.app.app_context():
            self.assertFalse(get_setting("llm_stream_trace_enabled"))

    def test_diagnostics_accept_json_returns_saved_state(self):
        response = self.client.post(
            "/admin/settings",
            data={"action": "diagnostics", "llm_stream_trace_enabled": "on"},
            headers={"Accept": "application/json"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"llm_stream_trace_enabled": True})

    def test_admin_settings_shows_network_config(self):
        self.app.config["NETWORK"] = {
            "proxy_mode": "custom",
            "proxy": "http://127.0.0.1:7890",
            "ssl_verify": True,
        }

        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        form = soup.find("form", {"class": "settings-network-form"})
        self.assertEqual(form.get("data-network-proxy-mode"), "custom")
        self.assertEqual(soup.find("select", {"name": "proxy_mode"}).find("option", selected=True)["value"], "custom")
        proxy_input = soup.find("input", {"name": "proxy"})
        self.assertEqual(proxy_input.get("value"), "http://127.0.0.1:7890")
        self.assertIsNotNone(proxy_input.get("required"))
        self.assertIsNotNone(soup.find("input", {"name": "ssl_verify"}).get("checked"))

    def test_admin_settings_marks_proxy_field_hidden_by_default(self):
        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        form = soup.find("form", {"class": "settings-network-form"})
        proxy_field = soup.select_one(".settings-network-proxy-field")
        proxy_input = soup.find("input", {"name": "proxy"})
        self.assertEqual(form.get("data-network-proxy-mode"), "direct")
        self.assertIsNotNone(proxy_field)
        self.assertIsNone(proxy_input.get("required"))

    def test_admin_settings_saves_task_limits(self):
        response = self.client.post(
            "/admin/settings",
            data={
                "action": "concurrency",
                "global_concurrency": "4",
                "user_concurrency": "2",
                "check_item_concurrency": "3",
                "image_page_check_max_pages": "36",
                "report_retention_days": "14",
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(get_setting("global_concurrency"), 4)
            self.assertEqual(get_setting("user_concurrency"), 2)
            self.assertEqual(get_setting("check_item_concurrency"), 3)
            self.assertEqual(get_setting("image_page_check_max_pages"), 36)
            self.assertEqual(get_setting("report_retention_days"), 14)

    def test_admin_settings_saves_network_to_yaml_config(self):
        response = self.client.post(
            "/admin/settings",
            data={
                "action": "network",
                "proxy_mode": "custom",
                "proxy": " http://127.0.0.1:7890 ",
                "ssl_verify": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        expected = {"proxy_mode": "custom", "proxy": "http://127.0.0.1:7890", "ssl_verify": True}
        self.assertEqual(self.app.config["NETWORK"], expected)
        config = yaml.safe_load((self.app.config["ROOT_DIR"] / CONFIG_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(config["network"], expected)
        with self.app.app_context():
            self.assertIsNone(get_setting("network"))

    def test_admin_settings_saves_ip_username_mapping_in_ip_mode(self):
        self._insert_task(ip="10.0.0.8")

        response = self.client.post(
            "/admin/settings",
            data={"action": "ip_username", "ip": "10.0.0.8", "username": "张三"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/settings?tab=ip_users"))
        with self.app.app_context():
            self.assertEqual(get_ip_username("10.0.0.8"), "张三")

        settings_response = self.client.get("/admin/settings")
        settings_html = settings_response.get_data(as_text=True)
        self.assertIn("IP 用户标记", settings_html)
        self.assertNotIn("张三", settings_html)

        ip_tab_response = self.client.get("/admin/settings?tab=ip_users")
        ip_tab_html = ip_tab_response.get_data(as_text=True)
        ip_tab_soup = BeautifulSoup(ip_tab_html, "html.parser")
        row_form = ip_tab_soup.select_one(".settings-ip-row-form")
        self.assertIn("张三", ip_tab_html)
        self.assertNotIn("系统出站网络", ip_tab_html)
        self.assertIsNone(row_form.find("button"))
        self.assertIsNotNone(row_form.find("input", {"data-ip-username-input": ""}))

        json_response = self.client.post(
            "/admin/settings",
            data={"action": "ip_username", "ip": "10.0.0.8", "username": "李四"},
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(json_response.status_code, 200)
        self.assertEqual(json_response.get_json(), {"ok": True, "ip": "10.0.0.8", "username": "李四"})

    def test_admin_settings_hides_ip_username_mapping_outside_ip_mode(self):
        self.app.config["AUTH"] = {
            "mode": "trusted_header",
            "trusted_header": {
                "user_id": "X-SSO-User-Id",
                "username": "X-SSO-User-Name",
            },
        }

        response = self.client.get("/admin/settings")
        blocked = self.client.post(
            "/admin/settings",
            data={"action": "ip_username", "ip": "10.0.0.8", "username": "张三"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("IP 用户标记", response.get_data(as_text=True))
        self.assertEqual(blocked.status_code, 404)
        with self.app.app_context():
            self.assertEqual(get_ip_username("10.0.0.8"), "")

        self.app.config["AUTH"] = _saml_auth_config()
        saml_response = self.client.get("/admin/settings")
        self.assertEqual(saml_response.status_code, 200)
        self.assertNotIn("IP 用户标记", saml_response.get_data(as_text=True))

    def test_admin_settings_shows_document_and_consistency_prompt_groups(self):
        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        soup = BeautifulSoup(html, "html.parser")
        self.assertIn("单文档检查-提示词设置", html)
        self.assertIn("多文档对照检查-提示词设置", html)
        self.assertIn("图片检查-提示词设置", html)
        self.assertIn("consistency_check", html)
        self.assertIn("image_check", html)
        document_tip = soup.find("button", {"aria-label": "单文档检查-提示词设置说明"})
        consistency_tip = soup.find("button", {"aria-label": "多文档对照检查-提示词设置说明"})
        image_tip = soup.find("button", {"aria-label": "图片检查-提示词设置说明"})
        self.assertIsNotNone(document_tip)
        self.assertIsNotNone(consistency_tip)
        self.assertIsNotNone(image_tip)
        self.assertEqual(document_tip.get("data-tip"), "内置检查项不可删除；扩展检查项可新增、停用或删除。")
        self.assertEqual(consistency_tip.get("data-tip"), "内置检查项不可删除；扩展检查项可新增、停用或删除，提交多文档对照任务时可多选。")
        self.assertEqual(image_tip.get("data-tip"), "内置检查项不可删除；扩展检查项可新增、停用或删除，提交图片检查任务时可多选。")
        visible_descriptions = [item.get_text(strip=True) for item in soup.select(".settings-section-head p")]
        self.assertNotIn("内置检查项不可删除；扩展检查项可新增、停用或删除。", visible_descriptions)
        self.assertNotIn("内置检查项不可删除；扩展检查项可新增、停用或删除，提交多文档对照任务时可多选。", visible_descriptions)
        self.assertNotIn("内置检查项不可删除；扩展检查项可新增、停用或删除，提交图片检查任务时可多选。", visible_descriptions)

    def test_admin_overview_counts_tasks_in_selected_range(self):
        self._insert_task(ip="10.0.0.1", username_snapshot="测试用户A", created_at="2026-05-01 10:00:00")
        self._insert_task(
            task_type=CONSISTENCY_TASK_TYPE,
            ip="10.0.0.1",
            username_snapshot="测试用户A",
            status="failed",
            created_at="2026-05-01 11:00:00",
        )
        self._insert_task(ip="10.0.0.2", status="queued", created_at="2026-05-02 08:00:00")
        self._insert_task(ip="10.0.0.3", created_at="2026-04-30 23:59:59")

        response = self.client.get("/admin?start_date=2026-05-01&end_date=2026-05-02")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("统计概览", html)
        self.assertNotIn("平台统计", html)
        self.assertNotIn("2026-05-01 至 2026-05-02", html)
        self.assertIn("<span>活跃用户</span><strong>2</strong>", html)
        self.assertIn("<span>提交任务</span><strong>3</strong>", html)
        self.assertIn("<span>单文档检查任务</span><strong>2</strong>", html)
        self.assertIn("<span>多文档对照任务</span><strong>1</strong>", html)
        self.assertIn("<span>排队</span><strong>1</strong>", html)
        self.assertIn("<span>失败</span><strong>1</strong>", html)
        self.assertIn("测试用户A", html)
        self.assertIn("10.0.0.2", html)
        self.assertNotIn("10.0.0.3", html)

    def test_admin_overview_uses_ip_username_mapping(self):
        self._insert_task(ip="10.0.0.8", created_at="2026-05-01 10:00:00")
        self.client.post(
            "/admin/settings",
            data={"action": "ip_username", "ip": "10.0.0.8", "username": "张三"},
        )

        response = self.client.get("/admin?start_date=2026-05-01&end_date=2026-05-01")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("张三", html)
        self.assertIn("IP 10.0.0.8", html)
        self.assertNotIn("ip:10.0.0.8", html)

    def test_admin_task_owner_cell_avoids_duplicate_ip_metadata(self):
        self._insert_task(ip="127.0.0.1")

        response = self.client.get("/admin/tasks")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        soup = BeautifulSoup(html, "html.parser")
        owner_cell = soup.select_one(".task-owner-cell")
        self.assertIsNotNone(owner_cell)
        self.assertEqual(owner_cell.get_text(" ", strip=True), "127.0.0.1")
        self.assertNotIn("ip:127.0.0.1 · IP 127.0.0.1", html)

    def test_admin_overview_filters_tasks_by_auth_mode(self):
        self._insert_task(
            ip="10.0.0.1",
            owner_subject="ip:10.0.0.1",
            owner_source="ip",
            created_at="2026-05-01 10:00:00",
        )
        self._insert_task(
            ip="10.0.0.2",
            owner_subject="trusted_header:100086",
            owner_name_snapshot="张三",
            owner_source="trusted_header",
            created_at="2026-05-01 11:00:00",
        )
        self._insert_task(
            ip="10.0.0.3",
            owner_subject="saml:100086",
            owner_name_snapshot="李四",
            owner_source="saml",
            created_at="2026-05-01 12:00:00",
        )
        self.app.config["AUTH"] = {
            "mode": "trusted_header",
            "trusted_header": {
                "user_id": "X-SSO-User-Id",
                "username": "X-SSO-User-Name",
            },
        }

        response = self.client.get("/admin?start_date=2026-05-01&end_date=2026-05-01")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("<span>活跃用户</span><strong>1</strong>", html)
        self.assertIn("<span>提交任务</span><strong>1</strong>", html)
        self.assertIn("张三", html)
        self.assertNotIn("李四", html)
        self.assertNotIn("ip:10.0.0.1", html)

    def test_local_mode_admin_root_redirects_to_management_view(self):
        self.app.config["PLATFORM"] = False
        self._logout_test_client()

        response = self.client.get("/admin")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/"))

    def test_admin_model_route_requires_admin_login(self):
        self._logout_test_client()

        response = self.client.get("/admin/models")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_admin_model_page_uses_same_identity_as_user_model_page(self):
        self._configure_provider("ip:127.0.0.1")

        admin_response = self.client.get("/admin/models")
        user_response = self.client.get("/models")

        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(user_response.status_code, 200)
        self.assertIn("测试提供商", admin_response.get_data(as_text=True))
        self.assertIn("测试提供商", user_response.get_data(as_text=True))
        self.assertIn("模型管理", admin_response.get_data(as_text=True))

    def test_admin_model_page_saves_same_user_model_config(self):
        response = self.client.post(
            "/admin/models",
            data={
                "name": "Console 提供商",
                "api_base": "https://example.test/v1/chat/completions",
                "api_key": "",
                "request_timeout": "30",
                "max_input_chars": "80000",
                "is_active": "on",
                "model_configs": json.dumps(
                    [{"model_name": "console-model", "force_disable_thinking": False}],
                    ensure_ascii=False,
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/models"))
        user_response = self.client.get("/models")
        self.assertIn("Console 提供商", user_response.get_data(as_text=True))
        self.assertIn("console-model", user_response.get_data(as_text=True))

    def test_local_mode_root_shows_admin_view_without_login(self):
        self.app.config["PLATFORM"] = False
        self._logout_test_client()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("单文档检查任务", html)
        self.assertIn("模型管理", html)
        self.assertNotIn("用户管理", html)
        self.assertNotIn("退出", html)

    def test_local_mode_user_model_page_does_not_require_login(self):
        self.app.config["PLATFORM"] = False
        self._logout_test_client()

        response = self.client.get("/models")

        self.assertEqual(response.status_code, 200)
        self.assertIn("我的模型", response.get_data(as_text=True))

    def test_user_management_route_is_not_registered(self):
        platform_response = self.client.get("/admin/users")

        self.app.config["PLATFORM"] = False
        self._logout_test_client()
        local_response = self.client.get("/admin/users")

        self.assertEqual(platform_response.status_code, 404)
        self.assertEqual(local_response.status_code, 404)

    def test_user_models_saves_model_force_disable_thinking(self):
        response = self.client.post(
            "/models",
            data={
                "name": "测试提供商",
                "api_base": "https://example.test/v1/chat/completions",
                "api_key": "",
                "request_timeout": "30",
                "max_input_chars": "80000",
                "is_active": "on",
                "model_configs": json.dumps(
                    [
                        {"model_name": "model-a", "force_disable_thinking": True},
                        {"model_name": "model-b", "force_disable_thinking": False},
                    ],
                    ensure_ascii=False,
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            models = get_db().execute(
                """
                SELECT m.model_name, m.force_disable_thinking
                FROM user_model_configs m
                JOIN user_model_providers p ON p.id = m.provider_id
                WHERE p.owner_subject = ?
                ORDER BY m.sort_order
                """,
                ("ip:127.0.0.1",),
            ).fetchall()
        self.assertEqual(
            [(row["model_name"], bool(row["force_disable_thinking"])) for row in models],
            [
                ("model-a", True),
                ("model-b", False),
            ],
        )

    def test_user_models_accepts_million_char_input_limit(self):
        response = self.client.post(
            "/models",
            data={
                "name": "测试提供商",
                "api_base": "https://example.test/v1/chat/completions",
                "api_key": "",
                "request_timeout": "30",
                "max_input_chars": "1000000",
                "is_active": "on",
                "model_configs": json.dumps(
                    [{"model_name": "model-a", "force_disable_thinking": False}],
                    ensure_ascii=False,
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            row = get_db().execute("SELECT max_input_chars FROM user_model_providers").fetchone()
        self.assertEqual(row["max_input_chars"], 1000000)

    def test_user_models_allows_same_name_for_distinct_thinking_modes(self):
        response = self.client.post(
            "/models",
            data={
                "name": "测试提供商",
                "api_base": "https://example.test/v1/chat/completions",
                "api_key": "",
                "request_timeout": "30",
                "max_input_chars": "80000",
                "is_active": "on",
                "model_configs": json.dumps(
                    [
                        {"model_name": "same-model", "force_disable_thinking": False},
                        {"model_name": "same-model", "force_disable_thinking": True},
                        {"model_name": "same-model", "force_disable_thinking": False},
                    ],
                    ensure_ascii=False,
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            saved_models = get_db().execute(
                """
                SELECT m.model_name, m.force_disable_thinking
                FROM user_model_configs m
                ORDER BY m.sort_order
                """
            ).fetchall()
        self.assertEqual(
            [(row["model_name"], bool(row["force_disable_thinking"])) for row in saved_models],
            [
                ("same-model", False),
                ("same-model", True),
            ],
        )

        with self.app.app_context():
            models = get_enabled_models("ip:127.0.0.1")
            self.assertEqual(len(models), 2)
            self.assertEqual(len({model["id"] for model in models}), 2)
            by_mode = {model["force_disable_thinking"]: model for model in models}
            self.assertFalse(_find_enabled_model(by_mode[False]["id"], "ip:127.0.0.1")["force_disable_thinking"])
            self.assertTrue(_find_enabled_model(by_mode[True]["id"], "ip:127.0.0.1")["force_disable_thinking"])

    def test_user_model_test_endpoint_uses_submitted_model_config(self):
        self.app.config["NETWORK"] = {
            "proxy_mode": "custom",
            "proxy": "http://127.0.0.1:7890",
            "ssl_verify": True,
        }
        with patch("app.routes.test_model_connection", return_value="模型连通性测试通过。") as mocked_test:
            response = self.client.post(
                "/models/test",
                json={
                    "api_base": "https://example.test/v1/chat/completions",
                    "api_key": "sk-test",
                    "request_timeout": "30",
                    "model_name": "model-a",
                    "force_disable_thinking": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "message": "模型连通性测试通过。"})
        mocked_test.assert_called_once_with(
            api_base="https://example.test/v1/chat/completions",
            api_key="sk-test",
            proxy_mode="custom",
            proxy="http://127.0.0.1:7890",
            ssl_verify=True,
            request_timeout=30,
            model_name="model-a",
            force_disable_thinking=True,
        )

    def test_user_fetch_models_uses_system_network_config(self):
        self.app.config["NETWORK"] = {
            "proxy_mode": "system",
            "proxy": "",
            "ssl_verify": True,
        }
        with patch("app.routes.fetch_models", return_value=["model-a"]) as mocked_fetch:
            response = self.client.get(
                "/models/fetch",
                query_string={
                    "api_base": "https://example.test/v1/chat/completions",
                    "api_key": "sk-test",
                    "request_timeout": "30",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"fetched_models": ["model-a"], "fetched_count": 1})
        mocked_fetch.assert_called_once_with(
            api_base="https://example.test/v1/chat/completions",
            api_key="sk-test",
            proxy_mode="system",
            proxy="",
            ssl_verify=True,
            request_timeout=30,
        )

    def test_admin_settings_creates_consistency_check_item(self):
        response = self.client.post(
            "/admin/settings",
            data={
                "action": "create_check_item",
                "task_type": CONSISTENCY_TASK_TYPE,
                "name": "遗漏内容检查",
                "description": "检查资料是否遗漏素材关键内容",
                "prompt": "只检查资料遗漏。",
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            item = get_db().execute(
                """
                SELECT task_type, code, name, description, prompt, enabled
                FROM check_items
                WHERE name = ?
                """,
                ("遗漏内容检查",),
            ).fetchone()
        self.assertEqual(item["task_type"], CONSISTENCY_TASK_TYPE)
        self.assertTrue(item["code"].startswith("custom-consistency-"))
        self.assertEqual(item["description"], "检查资料是否遗漏素材关键内容")
        self.assertEqual(item["prompt"], "只检查资料遗漏。")
        self.assertEqual(item["enabled"], 1)

    def test_admin_settings_creates_image_check_item(self):
        response = self.client.post(
            "/admin/settings",
            data={
                "action": "create_check_item",
                "task_type": IMAGE_TASK_TYPE,
                "name": "接线颜色检查",
                "description": "检查线缆颜色是否符合图纸要求",
                "prompt": "只检查接线颜色。",
                "enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            item = get_db().execute(
                """
                SELECT task_type, code, name, description, prompt, enabled
                FROM check_items
                WHERE name = ?
                """,
                ("接线颜色检查",),
            ).fetchone()
        self.assertEqual(item["task_type"], IMAGE_TASK_TYPE)
        self.assertTrue(item["code"].startswith("custom-image-"))
        self.assertEqual(item["description"], "检查线缆颜色是否符合图纸要求")
        self.assertEqual(item["prompt"], "只检查接线颜色。")
        self.assertEqual(item["enabled"], 1)

    def test_upload_destination_uses_unique_name_for_same_second_uploads(self):
        with self.app.app_context():
            first_name, _ = _upload_destination("报告.txt", "127.0.0.1", "2026-05-22 12:00:00", "txt")
            second_name, _ = _upload_destination("报告.txt", "127.0.0.1", "2026-05-22 12:00:00", "txt")

        self.assertNotEqual(first_name, second_name)
        self.assertTrue(first_name.endswith(".txt"))
        self.assertTrue(second_name.endswith(".txt"))

    def test_create_task_rejects_disabled_check_item_before_saving_file(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            item = get_db().execute("SELECT id FROM check_items WHERE code = 'typo'").fetchone()
            get_db().execute("UPDATE check_items SET enabled = 0 WHERE id = ?", (item["id"],))
            get_db().commit()

        response = self.client.post(
            "/",
            data={
                "document": (io.BytesIO("测试文档".encode("utf-8")), "doc.txt"),
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            total = get_db().execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"]
        self.assertEqual(total, 0)
        self.assertEqual(list(Path(self.app.config["UPLOAD_FOLDER"]).iterdir()), [])

    def test_create_task_saves_check_snapshot_and_extracted_text(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            item = get_db().execute("SELECT id, code, name, prompt FROM check_items WHERE code = 'typo'").fetchone()

        response = self.client.post(
            "/",
            data={
                "document": (io.BytesIO("测试文档".encode("utf-8")), "doc.txt"),
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = get_db().execute("SELECT * FROM tasks").fetchone()
        snapshots = json.loads(task["checks_snapshot_json"])
        self.assertEqual(task["document_text"], "file: doc.txt\n\n测试文档")
        self.assertEqual(
            snapshots,
            [
                {
                    "id": item["id"],
                    "code": item["code"],
                    "name": item["name"],
                    "prompt": item["prompt"],
                }
            ],
        )

    def test_create_image_task_saves_extracted_image_metadata(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            item = get_db().execute(
                "SELECT id, code, name, prompt FROM check_items WHERE code = 'image-small-language-text'"
            ).fetchone()

        response = self.client.post(
            "/images",
            data={
                "document": (_pdf_with_image_bytes(), "diagram.pdf"),
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = get_db().execute("SELECT * FROM tasks").fetchone()
            image_root = Path(self.app.config["UPLOAD_FOLDER"]).parent / "extracted_images"
        meta = json.loads(task["document_meta_json"])
        snapshots = json.loads(task["checks_snapshot_json"])
        self.assertEqual(task["task_type"], IMAGE_TASK_TYPE)
        self.assertEqual(meta["source_document"]["file_type"], "pdf")
        self.assertEqual(len(meta["page_images"]), 1)
        self.assertIn("page001-screenshot", meta["page_images"][0]["filename"])
        self.assertTrue((image_root / meta["page_images"][0]["relative_path"]).is_file())
        self.assertIn("document_text:", task["document_text"])
        self.assertIn("extracted_images:", task["document_text"])
        self.assertIn("page_screenshots: 1", task["document_text"])
        self.assertEqual(
            snapshots,
            [
                {
                    "id": item["id"],
                    "code": item["code"],
                    "name": item["name"],
                    "prompt": item["prompt"],
                }
            ],
        )

    def test_create_image_task_rejects_non_pdf_document(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            item = get_db().execute(
                "SELECT id FROM check_items WHERE code = 'image-small-language-text'"
            ).fetchone()

        response = self.client.post(
            "/images",
            data={
                "document": (io.BytesIO(b"<html></html>"), "diagram.html"),
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            total = get_db().execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"]
        self.assertEqual(total, 0)

    def test_image_task_detail_hides_extracted_image_list(self):
        with self.app.app_context():
            now = "2026-05-22 12:00:00"
            meta = {
                "images": [
                    {
                        "filename": "0001_page001-image001.png",
                        "relative_path": "task/0001_page001-image001.png",
                        "mime_type": "image/png",
                        "position": "page001-image001",
                        "size_bytes": 1024,
                    }
                ],
                "page_images": [
                    {
                        "filename": "0001_page001-screenshot.png",
                        "relative_path": "task/0001_page001-screenshot.png",
                        "mime_type": "image/png",
                        "position": "page001-screenshot",
                        "size_bytes": 2048,
                    }
                ],
            }
            result_json = [
                {
                    "code": "image-figure-table-title-standard",
                    "name": "图表标题规范检查",
                    "result": "检查结果正文：page001 表格缺少表标题。",
                }
            ]
            cursor = get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, document_meta_json, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', '图纸.pdf', 'stored.pdf', 'pdf',
                        4096, ?, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (
                    IMAGE_TASK_TYPE,
                    json.dumps(meta, ensure_ascii=False),
                    json.dumps(result_json, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            get_db().commit()
            task_id = cursor.lastrowid

        detail = self.client.get(f"/admin/tasks/{task_id}")
        exported = self.client.get(f"/admin/tasks/{task_id}/export")

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(exported.status_code, 200)
        detail_html = detail.get_data(as_text=True)
        exported_html = exported.get_data(as_text=True)
        self.assertNotIn("提取图片", detail_html)
        self.assertNotIn("0001_page001-image001.png", detail_html)
        self.assertNotIn("0001_page001-screenshot.png", detail_html)
        self.assertIn("检查结果正文：page001 表格缺少表标题。", detail_html)
        self.assertNotIn("提取图片", exported_html)
        self.assertNotIn("0001_page001-image001.png", exported_html)
        self.assertNotIn("0001_page001-screenshot.png", exported_html)

    def test_create_task_uses_trusted_header_identity(self):
        model_id = self._configure_provider("trusted_header:100086")
        self.app.config["AUTH"] = {
            "mode": "trusted_header",
            "trusted_header": {
                "user_id": "X-SSO-User-Id",
                "username": "X-SSO-User-Name",
            },
        }
        with self.app.app_context():
            item = get_db().execute("SELECT id FROM check_items WHERE code = 'typo'").fetchone()

        response = self.client.post(
            "/",
            data={
                "document": (io.BytesIO("测试文档".encode("utf-8")), "doc.txt"),
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            headers={"X-SSO-User-Id": "100086", "X-SSO-User-Name": "张三"},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = get_db().execute("SELECT owner_subject, owner_name_snapshot, owner_source, ip FROM tasks").fetchone()
        self.assertEqual(task["owner_subject"], "trusted_header:100086")
        self.assertEqual(task["owner_name_snapshot"], "张三")
        self.assertEqual(task["owner_source"], "trusted_header")
        self.assertEqual(task["ip"], "127.0.0.1")

    def test_trusted_header_user_page_requires_sso_header(self):
        self.app.config["AUTH"] = {
            "mode": "trusted_header",
            "trusted_header": {
                "user_id": "X-SSO-User-Id",
                "username": "X-SSO-User-Name",
            },
        }

        response = self.client.get("/")

        self.assertEqual(response.status_code, 401)
        self.assertIn("未收到 SSO 用户信息", response.get_data(as_text=True))

    def test_trusted_header_admin_settings_still_uses_local_admin_login(self):
        self.app.config["AUTH"] = {
            "mode": "trusted_header",
            "trusted_header": {
                "user_id": "X-SSO-User-Id",
                "username": "X-SSO-User-Name",
            },
        }

        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn("系统设置", response.get_data(as_text=True))

    def test_trusted_header_admin_task_page_requires_same_sso_user(self):
        self.app.config["AUTH"] = {
            "mode": "trusted_header",
            "trusted_header": {
                "user_id": "X-SSO-User-Id",
                "username": "X-SSO-User-Name",
            },
        }

        response = self.client.get("/admin/tasks")

        self.assertEqual(response.status_code, 401)
        self.assertIn("未收到 SSO 用户信息", response.get_data(as_text=True))

    def test_trusted_header_admin_task_page_uses_sso_user_models(self):
        model_id = self._configure_provider("trusted_header:100086")
        self.app.config["AUTH"] = {
            "mode": "trusted_header",
            "trusted_header": {
                "user_id": "X-SSO-User-Id",
                "username": "X-SSO-User-Name",
            },
        }

        response = self.client.get(
            "/admin/tasks",
            headers={"X-SSO-User-Id": "100086", "X-SSO-User-Name": "张三"},
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("model-a", html)
        self.assertIn(f'value="{model_id}"', html)

    def test_consistency_check_items_are_unchecked_by_default(self):
        self._configure_provider()

        response = self.client.get("/consistency")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        form = soup.find("form", {"data-require-checks": "true"})
        self.assertIsNotNone(form)
        checkboxes = form.select('input[name="checks"]')
        self.assertTrue(checkboxes)
        self.assertTrue(all(checkbox.get("checked") is None for checkbox in checkboxes))

    def test_saml_user_page_redirects_to_saml_login(self):
        self.app.config["AUTH"] = _saml_auth_config()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/saml/login?next=/", response.headers["Location"])

    def test_saml_login_stores_request_id(self):
        self.app.config["AUTH"] = _saml_auth_config()
        fake_auth = _FakeSamlAuth()

        with patch("app.routes.create_saml_auth", return_value=fake_auth):
            response = self.client.get("/auth/saml/login?next=/consistency")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "https://sso.example.com/login?SAMLRequest=test")
        self.assertEqual(fake_auth.return_to, "/consistency")
        with self.client.session_transaction() as session:
            self.assertEqual(session["saml_request_id"], "REQ-1")

    def test_saml_acs_saves_session_identity(self):
        self.app.config["AUTH"] = _saml_auth_config()
        fake_auth = _FakeSamlAuth()
        with self.client.session_transaction() as session:
            session["saml_request_id"] = "REQ-1"

        with patch("app.routes.create_saml_auth", return_value=fake_auth):
            response = self.client.post(
                "/auth/saml/acs",
                data={"SAMLResponse": "test", "RelayState": "/consistency"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/consistency")
        self.assertEqual(fake_auth.processed_request_id, "REQ-1")
        with self.client.session_transaction() as session:
            self.assertEqual(session[SAML_USER_SESSION_KEY], {"user_id": "100086", "username": "张三"})
            self.assertNotIn("saml_request_id", session)

    def test_create_task_uses_saml_session_identity(self):
        model_id = self._configure_provider("saml:100086")
        self.app.config["AUTH"] = _saml_auth_config()
        with self.client.session_transaction() as session:
            session[SAML_USER_SESSION_KEY] = {"user_id": "100086", "username": "张三"}
        with self.app.app_context():
            item = get_db().execute("SELECT id FROM check_items WHERE code = 'typo'").fetchone()

        response = self.client.post(
            "/",
            data={
                "document": (io.BytesIO("测试文档".encode("utf-8")), "doc.txt"),
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = get_db().execute("SELECT owner_subject, owner_name_snapshot, owner_source FROM tasks").fetchone()
        self.assertEqual(task["owner_subject"], "saml:100086")
        self.assertEqual(task["owner_name_snapshot"], "张三")
        self.assertEqual(task["owner_source"], "saml")

    def test_saml_metadata_uses_sp_config_only(self):
        self.app.config["AUTH"] = _saml_auth_config()

        response = self.client.get("/auth/saml/metadata")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("EntityDescriptor", html)
        self.assertIn("https://doc.example.com/auth/saml/metadata", html)
        self.assertIn("https://doc.example.com/auth/saml/acs", html)

    def test_saml_admin_settings_still_uses_local_admin_login(self):
        self.app.config["AUTH"] = _saml_auth_config()

        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn("系统设置", response.get_data(as_text=True))

    def test_saml_admin_task_page_redirects_to_saml_login_for_same_user(self):
        self.app.config["AUTH"] = _saml_auth_config()

        response = self.client.get("/admin/tasks")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/saml/login?next=/admin/tasks", response.headers["Location"])

    def test_create_consistency_task_rejects_missing_checks_before_saving_file(self):
        model_id = self._configure_provider()

        response = self.client.post(
            "/consistency",
            data={
                "master_documents": (io.BytesIO("素材参数 10A".encode("utf-8")), "master.txt"),
                "related_documents": (io.BytesIO("资料参数 12A".encode("utf-8")), "related.txt"),
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            total = get_db().execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"]
        self.assertEqual(total, 0)
        self.assertEqual(list(Path(self.app.config["UPLOAD_FOLDER"]).iterdir()), [])

    def test_create_consistency_task_saves_combined_document_text(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            item = get_db().execute(
                "SELECT id, code, name, prompt FROM check_items WHERE code = 'consistency-cross-document'"
            ).fetchone()

        response = self.client.post(
            "/consistency",
            data={
                "master_documents": (_xlsx_bytes([["项目", "参数"], ["素材参数", "10A"]], title="素材参数表"), "master.xlsx"),
                "related_documents": (io.BytesIO("资料参数 12A".encode("utf-8")), "related.txt"),
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = get_db().execute("SELECT task_type, document_text, checks_snapshot_json FROM tasks").fetchone()
        self.assertEqual(task["task_type"], "consistency_check")
        self.assertIn("## 素材文档1：master.xlsx", task["document_text"])
        self.assertIn("# 工作表：素材参数表", task["document_text"])
        self.assertIn("素材参数 | 10A", task["document_text"])
        self.assertIn("## 资料1：related.txt", task["document_text"])
        self.assertEqual(
            json.loads(task["checks_snapshot_json"]),
            [
                {
                    "id": item["id"],
                    "code": item["code"],
                    "name": item["name"],
                    "prompt": item["prompt"],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
