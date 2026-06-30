import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from bs4 import BeautifulSoup
from bs4.element import Tag
from flask import Flask
from openpyxl import Workbook, load_workbook

from app.auth import SAML_USER_SESSION_KEY
from app.config import CONFIG_FILENAME
from app.db import get_db, get_ip_username, get_setting, init_db, seed_defaults, set_setting
from app.formatting import render_markdown
from app.routes import _find_enabled_model, _upload_destination, get_enabled_models, register_routes
from app.task_types import CONSISTENCY_TASK_TYPE, DOCUMENT_TASK_TYPE, IMAGE_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE, VIDEO_TASK_TYPE


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _required_tag(value: object) -> Tag:
    if not isinstance(value, Tag):
        raise AssertionError("expected HTML tag")
    return value


def _xlsx_bytes(rows, *, title: str = "Sheet1") -> io.BytesIO:
    output = io.BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
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
            MAX_UPLOAD_MB=1024,
            MAX_CONTENT_LENGTH=1024 * 1024 * 1024,
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
        toggle = _required_tag(soup.find("input", {"name": "llm_stream_trace_enabled"}))
        self.assertEqual(toggle.get("data-saved-checked"), "false")
        self.assertIsNone(toggle.get("checked"))

    def test_diagnostics_toggle_treats_text_false_as_unchecked(self):
        with self.app.app_context():
            set_setting("llm_stream_trace_enabled", "false")

        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        toggle = _required_tag(soup.find("input", {"name": "llm_stream_trace_enabled"}))
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
        form = _required_tag(soup.find("form", {"class": "settings-network-form"}))
        proxy_mode_select = _required_tag(soup.find("select", {"name": "proxy_mode"}))
        selected_option = _required_tag(proxy_mode_select.find("option", selected=True))
        self.assertEqual(form.get("data-network-proxy-mode"), "custom")
        self.assertEqual(selected_option.get("value"), "custom")
        proxy_input = _required_tag(soup.find("input", {"name": "proxy"}))
        self.assertEqual(proxy_input.get("value"), "http://127.0.0.1:7890")
        self.assertIsNotNone(proxy_input.get("required"))
        ssl_verify_input = _required_tag(soup.find("input", {"name": "ssl_verify"}))
        self.assertIsNotNone(ssl_verify_input.get("checked"))

    def test_admin_settings_marks_proxy_field_hidden_by_default(self):
        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        form = _required_tag(soup.find("form", {"class": "settings-network-form"}))
        proxy_field = soup.select_one(".settings-network-proxy-field")
        proxy_input = _required_tag(soup.find("input", {"name": "proxy"}))
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
                "issue_output_limit": "0",
                "report_retention_days": "14",
            },
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(get_setting("global_concurrency"), 4)
            self.assertEqual(get_setting("user_concurrency"), 2)
            self.assertEqual(get_setting("check_item_concurrency"), 3)
            self.assertEqual(get_setting("image_page_check_max_pages"), 36)
            self.assertEqual(get_setting("issue_output_limit"), 0)
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
        row_form = _required_tag(ip_tab_soup.select_one(".settings-ip-row-form"))
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

    def test_admin_settings_shows_ip_username_mapping_in_local_ip_mode(self):
        self.app.config["PLATFORM"] = False
        self._insert_task(ip="10.0.0.8")

        settings_response = self.client.get("/admin/settings")
        ip_tab_response = self.client.get("/admin/settings?tab=ip_users")

        self.assertEqual(settings_response.status_code, 200)
        self.assertIn("IP 用户标记", settings_response.get_data(as_text=True))
        self.assertEqual(ip_tab_response.status_code, 200)
        ip_tab_html = ip_tab_response.get_data(as_text=True)
        self.assertIn("IP 用户标记", ip_tab_html)
        self.assertIn("10.0.0.8", ip_tab_html)
        self.assertNotIn("系统出站网络", ip_tab_html)

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
        self.assertIn("跨语种文档一致性对比-提示词设置", html)
        self.assertIn("图片检查-提示词设置", html)
        self.assertIn("视频检查-提示词设置", html)
        self.assertIn("consistency_check", html)
        self.assertIn("language_consistency_check", html)
        self.assertIn("image_check", html)
        self.assertIn("video_check", html)
        document_tip = _required_tag(soup.find("button", {"aria-label": "单文档检查-提示词设置说明"}))
        consistency_tip = _required_tag(soup.find("button", {"aria-label": "多文档对照检查-提示词设置说明"}))
        language_tip = _required_tag(soup.find("button", {"aria-label": "跨语种文档一致性对比-提示词设置说明"}))
        image_tip = _required_tag(soup.find("button", {"aria-label": "图片检查-提示词设置说明"}))
        video_tip = _required_tag(soup.find("button", {"aria-label": "视频检查-提示词设置说明"}))
        self.assertEqual(document_tip.get("data-tip"), "内置检查项不可删除；扩展检查项可新增、停用或删除。")
        self.assertEqual(consistency_tip.get("data-tip"), "内置检查项不可删除；扩展检查项可新增、停用或删除，提交多文档对照任务时可多选。")
        self.assertEqual(language_tip.get("data-tip"), "内置检查项不可删除；扩展检查项可新增、停用或删除，提交跨语种对比任务时可多选。")
        self.assertEqual(image_tip.get("data-tip"), "内置检查项不可删除；扩展检查项可新增、停用或删除，提交图片检查任务时可多选。")
        self.assertEqual(video_tip.get("data-tip"), "内置检查项不可删除；扩展检查项可新增、停用或删除，提交视频检查任务时可多选。")
        visible_descriptions = [item.get_text(strip=True) for item in soup.select(".settings-section-head p")]
        self.assertNotIn("内置检查项不可删除；扩展检查项可新增、停用或删除。", visible_descriptions)
        self.assertNotIn("内置检查项不可删除；扩展检查项可新增、停用或删除，提交多文档对照任务时可多选。", visible_descriptions)
        self.assertNotIn("内置检查项不可删除；扩展检查项可新增、停用或删除，提交跨语种对比任务时可多选。", visible_descriptions)
        self.assertNotIn("内置检查项不可删除；扩展检查项可新增、停用或删除，提交图片检查任务时可多选。", visible_descriptions)
        self.assertNotIn("内置检查项不可删除；扩展检查项可新增、停用或删除，提交视频检查任务时可多选。", visible_descriptions)

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
        self._insert_task(task_type=LANGUAGE_CONSISTENCY_TASK_TYPE, ip="10.0.0.2", created_at="2026-05-02 09:00:00")
        self._insert_task(task_type=VIDEO_TASK_TYPE, ip="10.0.0.2", created_at="2026-05-02 10:00:00")
        self._insert_task(ip="10.0.0.3", created_at="2026-04-30 23:59:59")

        response = self.client.get("/admin?start_date=2026-05-01&end_date=2026-05-02")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("统计概览", html)
        self.assertNotIn("平台统计", html)
        self.assertNotIn("2026-05-01 至 2026-05-02", html)
        self.assertIn("<span>活跃用户</span><strong>2</strong>", html)
        self.assertIn("<span>提交任务</span><strong>5</strong>", html)
        self.assertIn("<span>单文档检查任务</span><strong>2</strong>", html)
        self.assertIn("<span>多文档对照任务</span><strong>1</strong>", html)
        self.assertIn("<span>跨语种对比任务</span><strong>1</strong>", html)
        self.assertIn("<span>视频检查任务</span><strong>1</strong>", html)
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
        owner_cell = _required_tag(soup.select_one(".task-owner-cell"))
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

    def test_ip_identity_uses_configured_real_ip_header(self):
        self.app.config["REAL_IP_HEADER"] = "X-Real-IP"
        model_id = self._configure_provider("ip:10.20.30.40")
        with self.app.app_context():
            item = get_db().execute("SELECT id FROM check_items WHERE code = 'typo'").fetchone()

        response = self.client.post(
            "/",
            data={
                "document": (io.BytesIO("测试文档".encode("utf-8")), "doc.txt"),
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            headers={"X-Real-IP": "10.20.30.40"},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = get_db().execute("SELECT owner_subject, ip FROM tasks").fetchone()
        self.assertEqual(task["owner_subject"], "ip:10.20.30.40")
        self.assertEqual(task["ip"], "10.20.30.40")

    def test_invalid_real_ip_header_falls_back_to_remote_addr(self):
        self.app.config["REAL_IP_HEADER"] = "X-Real-IP"
        self._configure_provider("ip:127.0.0.1")

        response = self.client.get("/models", headers={"X-Real-IP": "not-an-ip"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("测试提供商", response.get_data(as_text=True))

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
            thinking_enabled_model = _find_enabled_model(by_mode[False]["id"], "ip:127.0.0.1")
            thinking_disabled_model = _find_enabled_model(by_mode[True]["id"], "ip:127.0.0.1")
            assert thinking_enabled_model is not None
            assert thinking_disabled_model is not None
            self.assertFalse(thinking_enabled_model["force_disable_thinking"])
            self.assertTrue(thinking_disabled_model["force_disable_thinking"])

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

    def test_admin_settings_creates_video_check_item(self):
        response = self.client.post(
            "/admin/settings",
            data={
                "action": "create_check_item",
                "task_type": VIDEO_TASK_TYPE,
                "name": "铭牌信息检查",
                "description": "检查视频中设备铭牌是否清晰",
                "prompt": "只检查铭牌清晰度。",
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
                ("铭牌信息检查",),
            ).fetchone()
        self.assertEqual(item["task_type"], VIDEO_TASK_TYPE)
        self.assertTrue(item["code"].startswith("custom-video-"))
        self.assertEqual(item["description"], "检查视频中设备铭牌是否清晰")
        self.assertEqual(item["prompt"], "只检查铭牌清晰度。")
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

    def test_document_task_form_allows_multiple_uploads(self):
        self._configure_provider()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        form = _required_tag(soup.find("form", {"data-prevent-double-submit": "true"}))
        self.assertEqual(form.get("data-submitting-label"), "提交中...")
        self.assertIn("请勿重复提交", form.get("data-submitting-message", ""))
        progress = _required_tag(form.select_one("[data-submit-progress]"))
        self.assertTrue(progress.has_attr("hidden"))
        self.assertIn("请勿重复提交", progress.get_text(strip=True))
        upload = _required_tag(soup.find("input", {"name": "document"}))
        self.assertTrue(upload.has_attr("multiple"))
        self.assertIsNone(upload.get("data-file-limit"))
        field = _required_tag(upload.find_parent(class_="multi-file-field"))
        self.assertIsNotNone(field.select_one("[data-file-list]"))

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

    def test_create_task_creates_one_task_per_uploaded_document(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            item = get_db().execute(
                "SELECT id, code, name, prompt FROM check_items WHERE code = 'typo'"
            ).fetchone()

        response = self.client.post(
            "/",
            data={
                "document": [
                    (io.BytesIO(f"document {index}".encode("utf-8")), f"doc-{index:02d}.txt")
                    for index in range(21)
                ],
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            tasks = get_db().execute(
                """
                SELECT original_filename, document_text, status, checks_snapshot_json
                FROM tasks
                ORDER BY original_filename ASC
                """
            ).fetchall()
            uploaded_files = list(Path(self.app.config["UPLOAD_FOLDER"]).iterdir())

        self.assertEqual(len(tasks), 21)
        self.assertEqual(tasks[0]["original_filename"], "doc-00.txt")
        self.assertEqual(tasks[-1]["original_filename"], "doc-20.txt")
        self.assertTrue(all(task["status"] == "queued" for task in tasks))
        self.assertEqual(tasks[0]["document_text"], "file: doc-00.txt\n\ndocument 0")
        self.assertEqual(tasks[-1]["document_text"], "file: doc-20.txt\n\ndocument 20")
        self.assertEqual(len(uploaded_files), 21)
        snapshots = [json.loads(task["checks_snapshot_json"]) for task in tasks]
        self.assertTrue(all(snapshot[0]["code"] == item["code"] for snapshot in snapshots))

    def test_create_task_rejects_entire_batch_when_one_document_has_no_text(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            item = get_db().execute("SELECT id FROM check_items WHERE code = 'typo'").fetchone()

        response = self.client.post(
            "/",
            data={
                "document": [
                    (io.BytesIO(b"valid document"), "valid.txt"),
                    (io.BytesIO(b"   "), "blank.txt"),
                ],
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            total = get_db().execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"]
            uploaded_files = list(Path(self.app.config["UPLOAD_FOLDER"]).iterdir())
        self.assertEqual(total, 0)
        self.assertEqual(uploaded_files, [])

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

    def test_create_video_task_saves_extracted_frame_metadata(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            item = get_db().execute(
                "SELECT id, code, name, prompt FROM check_items WHERE code = 'video-installation-sequence'"
            ).fetchone()

        def fake_extract_video_frames(video_path, output_dir, *, source_filename="", max_frames=16):
            output_dir.mkdir(parents=True, exist_ok=True)
            frame_path = output_dir / "0001_t000001000.jpg"
            frame_path.write_bytes(_TINY_PNG)
            return (
                [
                    {
                        "id": "frame-0001",
                        "filename": "0001_t000001000.jpg",
                        "stored_filename": "0001_t000001000.jpg",
                        "relative_path": "0001_t000001000.jpg",
                        "mime_type": "image/jpeg",
                        "position": "00:01.000",
                        "source": source_filename,
                        "size_bytes": frame_path.stat().st_size,
                        "kind": "video_frame",
                        "timestamp_seconds": 1.0,
                    }
                ],
                {
                    "duration_seconds": 8.0,
                    "selected_timestamps": [1.0],
                    "max_frames": max_frames,
                    "frame_count": 1,
                    "strategy": "uniform-sampling",
                },
            )

        with patch("app.routes.extract_video_frames", side_effect=fake_extract_video_frames):
            response = self.client.post(
                "/videos",
                data={
                    "video": (io.BytesIO(b"video-bytes"), "install.mp4"),
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
        self.assertEqual(task["task_type"], VIDEO_TASK_TYPE)
        self.assertEqual(task["file_type"], "mp4")
        self.assertEqual(meta["source_video"]["file_type"], "mp4")
        self.assertEqual(meta["frame_selection"]["frame_count"], 1)
        self.assertEqual(len(meta["frames"]), 1)
        self.assertEqual(meta["frames"][0]["position"], "00:01.000")
        self.assertTrue((image_root / meta["frames"][0]["relative_path"]).is_file())
        self.assertIn("video_context:", task["document_text"])
        self.assertIn("video_frames:", task["document_text"])
        self.assertIn("00:01.000", task["document_text"])
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

    def test_create_video_task_rejects_unsupported_file(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            item = get_db().execute(
                "SELECT id FROM check_items WHERE code = 'video-installation-sequence'"
            ).fetchone()

        response = self.client.post(
            "/videos",
            data={
                "video": (io.BytesIO(b"<html></html>"), "install.html"),
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            total = get_db().execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"]
        self.assertEqual(total, 0)

    def test_oversized_upload_shows_chinese_limit_message(self):
        self.app.config["MAX_UPLOAD_MB"] = 1
        self.app.config["MAX_CONTENT_LENGTH"] = 1

        response = self.client.post(
            "/videos",
            data={"video": (io.BytesIO(b"video-bytes"), "install.mp4")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("上传文件过大，当前上传上限为 1MB", html)
        self.assertIn("创建视频检查", html)

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

    def test_task_detail_renders_report_items_and_counts(self):
        with self.app.app_context():
            now = "2026-05-23 12:00:00"
            result_json = [
                {
                    "code": "consistency",
                    "name": "全文一致性检查",
                    "result": (
                        "总体结论：存在一致性风险。\n\n"
                        "1. 问题类型：参数不一致\n"
                        "位置：第1章、第2章\n"
                        "原文摘录：A 为 10；A 为 20\n"
                        "问题描述：同一参数前后不一致\n"
                        "影响说明：客户可能按错误参数配置\n"
                        "修改建议：统一参数值。\n\n"
                        "2. 建议：补充适用范围\n"
                        "位置：第3章\n"
                        "原文摘录：安装前检查环境\n"
                        "修改建议：补充温度范围。"
                    ),
                }
            ]
            cursor = get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', 'report.txt', 'stored.txt', 'txt',
                        1024, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (DOCUMENT_TASK_TYPE, json.dumps(result_json, ensure_ascii=False), now, now),
            )
            get_db().commit()
            task_id = cursor.lastrowid

        detail = self.client.get(f"/admin/tasks/{task_id}")
        exported = self.client.get(f"/admin/tasks/{task_id}/export")

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(exported.status_code, 200)
        soup = BeautifulSoup(detail.get_data(as_text=True), "html.parser")
        items = soup.select("[data-report-item]")
        self.assertEqual(len(items), 2)
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="issue"]')).get_text(strip=True), "1")
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="suggestion"]')).get_text(strip=True), "1")
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="non_issue"]')).get_text(strip=True), "0")
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="pending_issue_acceptance"]')).get_text(strip=True), "1")
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="issue_detection_rate"]')).get_text(strip=True), "50.0%")
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="issue_acceptance_rate"]')).get_text(strip=True), "-")
        self.assertIn("AI 检查条目统计", exported.get_data(as_text=True))
        self.assertIn("条目 1", exported.get_data(as_text=True))

    def test_task_report_exports_excel_for_statistics(self):
        with self.app.app_context():
            now = "2026-05-23 12:10:00"
            structured_report = {
                "summary": "发现 1 个明确问题，1 个建议。",
                "items": [
                    {
                        "status": "issue",
                        "category": "参数不一致",
                        "location": "第1章、第2章",
                        "excerpt": "A 为 10；A 为 20",
                        "description": "同一参数前后不一致",
                        "impact": "客户可能按错误参数配置",
                        "suggestion": "统一参数值。",
                    },
                    {
                        "status": "suggestion",
                        "category": "补充说明",
                        "location": "第3章",
                        "excerpt": "安装前检查环境",
                        "description": "未说明温度范围",
                        "impact": "",
                        "suggestion": "补充适用范围。",
                    },
                ],
            }
            result_json = [
                {
                    "code": "consistency",
                    "name": "全文一致性检查",
                    "result": json.dumps(structured_report, ensure_ascii=False),
                }
            ]
            cursor = get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', 'report.txt', 'stored.txt', 'txt',
                        1024, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (DOCUMENT_TASK_TYPE, json.dumps(result_json, ensure_ascii=False), now, now),
            )
            get_db().commit()
            task_id = cursor.lastrowid

        detail = self.client.get(f"/admin/tasks/{task_id}")
        soup = BeautifulSoup(detail.get_data(as_text=True), "html.parser")
        item_id = _required_tag(soup.select_one("[data-report-item]"))["data-item-id"]
        review_response = self.client.post(
            f"/admin/tasks/{task_id}/report-items",
            json={
                "result_code": "consistency",
                "item_id": item_id,
                "item_type": "issue",
                "acceptance_status": "rejected",
                "rejection_reason": "false_positive",
                "rejection_note": "上下文可解释",
            },
        )
        self.assertEqual(review_response.status_code, 200)

        response = self.client.get(f"/admin/tasks/{task_id}/export.xlsx")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.assertIn(f'document-check-report-{task_id}.xlsx', response.headers["Content-Disposition"])
        workbook = load_workbook(io.BytesIO(response.data), read_only=True, data_only=True)
        try:
            self.assertEqual(workbook.sheetnames, ["报告条目", "统计"])
            report_rows = list(workbook["报告条目"].iter_rows(values_only=True))
            self.assertEqual(
                report_rows[0],
                (
                    "任务ID",
                    "任务类型",
                    "文件名称",
                    "检查项",
                    "条目",
                    "问题类型",
                    "位置",
                    "原文/证据",
                    "问题描述",
                    "影响",
                    "修改建议",
                    "条目判定",
                    "是否接纳",
                    "不接纳原因",
                    "人工原因",
                ),
            )
            self.assertEqual(len(report_rows), 3)
            self.assertEqual(report_rows[1][0], task_id)
            self.assertEqual(report_rows[1][2], "report.txt")
            self.assertEqual(report_rows[1][3], "全文一致性检查")
            self.assertEqual(report_rows[1][4], "条目 1")
            self.assertEqual(report_rows[1][5], "参数不一致")
            self.assertEqual(report_rows[1][11], "问题")
            self.assertEqual(report_rows[1][12], "不接纳")
            self.assertEqual(report_rows[1][13], "模型误报")
            self.assertEqual(report_rows[1][14], "上下文可解释")
            self.assertEqual(report_rows[2][10], "补充适用范围。")
            self.assertEqual(report_rows[2][11], "建议")
            stats = dict(workbook["统计"].iter_rows(min_row=2, values_only=True))
            self.assertEqual(stats["问题"], 1)
            self.assertEqual(stats["建议"], 1)
            self.assertEqual(stats["不接纳问题"], 1)
            self.assertEqual(stats["问题检出率"], "50.0%")
            self.assertEqual(stats["问题接纳率"], "0.0%")
        finally:
            workbook.close()

    def test_task_detail_renders_structured_json_report_table(self):
        with self.app.app_context():
            now = "2026-05-23 12:30:00"
            structured_report = {
                "summary": "发现 1 个明确问题，1 个需人工确认项。",
                "items": [
                    {
                        "status": "issue",
                        "category": "参数不一致",
                        "location": "第1章、第2章",
                        "excerpt": "A 为 10；A 为 20",
                        "description": "同一参数前后不一致",
                        "impact": "客户可能按错误参数配置",
                        "suggestion": "统一参数值。",
                    },
                    {
                        "status": "suggestion",
                        "category": "需人工确认",
                        "location": "第3章",
                        "excerpt": "安装前检查环境",
                        "description": "未说明温度范围，证据不足需人工确认。",
                        "impact": "",
                        "suggestion": "确认后补充适用范围。",
                    },
                ],
            }
            result_json = [
                {
                    "code": "consistency",
                    "name": "全文一致性检查",
                    "result": json.dumps(structured_report, ensure_ascii=False),
                }
            ]
            cursor = get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', 'report.txt', 'stored.txt', 'txt',
                        1024, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (DOCUMENT_TASK_TYPE, json.dumps(result_json, ensure_ascii=False), now, now),
            )
            get_db().commit()
            task_id = cursor.lastrowid

        detail = self.client.get(f"/admin/tasks/{task_id}")

        self.assertEqual(detail.status_code, 200)
        soup = BeautifulSoup(detail.get_data(as_text=True), "html.parser")
        headers = [node.get_text(strip=True) for node in soup.select(".report-table th")]
        self.assertEqual(
            headers,
            ["条目", "问题类型", "位置", "原文/证据", "问题描述", "影响", "修改建议", "条目判定", "是否接纳", "不接纳原因"],
        )
        rows = soup.select("tr[data-report-item]")
        self.assertEqual(len(rows), 2)
        self.assertEqual(_required_tag(rows[0].select_one("[data-report-item-type]")).get("data-saved-value"), "issue")
        self.assertEqual(_required_tag(rows[1].select_one("[data-report-item-type]")).get("data-saved-value"), "suggestion")
        acceptance = _required_tag(rows[0].select_one("[data-report-acceptance-status]"))
        reason = _required_tag(rows[0].select_one("[data-report-rejection-reason]"))
        note = _required_tag(rows[0].select_one("[data-report-rejection-note]"))
        self.assertEqual(acceptance.get("data-saved-value"), "pending")
        self.assertTrue(reason.has_attr("disabled"))
        self.assertTrue(note.has_attr("disabled"))
        self.assertIn("同一参数前后不一致", rows[0].get_text(" ", strip=True))
        self.assertIn("确认后补充适用范围", rows[1].get_text(" ", strip=True))
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="issue"]')).get_text(strip=True), "1")
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="suggestion"]')).get_text(strip=True), "1")

    def test_language_consistency_no_action_items_are_non_issues(self):
        with self.app.app_context():
            now = "2026-05-23 12:45:00"
            structured_report = {
                "summary": "发现 1 个实质差异，1 个无须修改差异。",
                "items": [
                    {
                        "status": "issue",
                        "category": "缺失与增补",
                        "location": "文档B 第10页目录",
                        "excerpt": "Measurement Methods of PV Optimizers",
                        "description": "中文版目录缺少英文标题中的冠词，但不影响用户理解。",
                        "impact": "无实质影响。",
                        "suggestion": "无需修改。",
                    },
                    {
                        "status": "issue",
                        "category": "关键事实差异",
                        "location": "文档A 第3页 / 文档B 第4页",
                        "excerpt": "额定功率 500W / Rated power 550W",
                        "description": "同一型号的额定功率不一致。",
                        "impact": "客户可能按错误参数配置。",
                        "suggestion": "核实并统一额定功率。",
                    },
                ],
            }
            result_json = [
                {
                    "code": "language-consistency-cross-lingual",
                    "name": "跨语种内容一致性对比",
                    "result": json.dumps(structured_report, ensure_ascii=False),
                }
            ]
            cursor = get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', 'cross-language.txt', 'stored.txt', 'txt',
                        1024, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (LANGUAGE_CONSISTENCY_TASK_TYPE, json.dumps(result_json, ensure_ascii=False), now, now),
            )
            get_db().commit()
            task_id = cursor.lastrowid

        response = self.client.get(f"/admin/tasks/{task_id}")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        rows = soup.select("tr[data-report-item]")
        self.assertEqual(len(rows), 2)
        self.assertEqual(_required_tag(rows[0].select_one("[data-report-item-type]")).get("data-saved-value"), "non_issue")
        self.assertEqual(_required_tag(rows[1].select_one("[data-report-item-type]")).get("data-saved-value"), "issue")
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="issue"]')).get_text(strip=True), "1")
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="non_issue"]')).get_text(strip=True), "1")
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="issue_detection_rate"]')).get_text(strip=True), "50.0%")

    def test_task_detail_parses_double_encoded_structured_json_report(self):
        with self.app.app_context():
            now = "2026-05-23 13:00:00"
            structured_report = {
                "summary": "发现错别字和标点问题。",
                "items": [
                    {
                        "status": "issue",
                        "category": "标点误用",
                        "location": "[第9页] 章节：1 安全注意事项",
                        "excerpt": "或/和",
                        "description": "“或/和”中斜杠前后存在多余空格。",
                        "impact": "影响阅读体验和文档规范性。",
                        "suggestion": "改为“或和”或按规范统一表达。",
                    }
                ],
            }
            result_json = [
                {
                    "code": "typo",
                    "name": "错别字检查",
                    "result": json.dumps(json.dumps(structured_report, ensure_ascii=False), ensure_ascii=False),
                }
            ]
            cursor = get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', 'report.txt', 'stored.txt', 'txt',
                        1024, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (DOCUMENT_TASK_TYPE, json.dumps(result_json, ensure_ascii=False), now, now),
            )
            get_db().commit()
            task_id = cursor.lastrowid

        response = self.client.get(f"/admin/tasks/{task_id}")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        rows = soup.select("tr[data-report-item]")
        self.assertEqual(len(rows), 1)
        row_text = rows[0].get_text(" ", strip=True)
        self.assertIn("标点误用", row_text)
        self.assertIn("[第9页] 章节：1 安全注意事项", row_text)
        self.assertIn("“或/和”中斜杠前后存在多余空格。", row_text)
        self.assertNotIn('"items"', row_text)
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="issue"]')).get_text(strip=True), "1")

    def test_task_detail_parses_structured_json_with_raw_newline_in_string(self):
        with self.app.app_context():
            now = "2026-05-23 13:30:00"
            raw_json = (
                '{"summary":"发现 1 个问题","items":[{"status":"issue","category":"格式问题",'
                '"location":"第1页","excerpt":"第一行\n第二行","description":"描述包含原文换行",'
                '"impact":"影响阅读","suggestion":"删除多余换行"}]}'
            )
            result_json = [
                {
                    "code": "typo",
                    "name": "错别字检查",
                    "result": raw_json,
                }
            ]
            cursor = get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', 'report.txt', 'stored.txt', 'txt',
                        1024, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (DOCUMENT_TASK_TYPE, json.dumps(result_json, ensure_ascii=False), now, now),
            )
            get_db().commit()
            task_id = cursor.lastrowid

        response = self.client.get(f"/admin/tasks/{task_id}")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        row = _required_tag(soup.select_one("tr[data-report-item]"))
        row_text = row.get_text(" ", strip=True)
        self.assertIn("格式问题", row_text)
        self.assertIn("第一行", row_text)
        self.assertIn("第二行", row_text)
        self.assertNotIn('"items"', row_text)

    def test_task_detail_splits_bold_numbered_compliance_items(self):
        with self.app.app_context():
            now = "2026-05-24 09:00:00"
            result_json = [
                {
                    "code": "compliance",
                    "name": "文档规范性检查",
                    "result": (
                        "总体规范性结论：该资料存在面向客户表达风险。\n\n"
                        "---\n\n"
                        "## 问题逐条列表\n\n"
                        "**1. 问题类型：技术信息呈现（严重错误）**\n"
                        "- 位置：第29页 / 3.2 参数说明\n"
                        "- 原文摘录：支持 220V 输入。\n"
                        "- 问题描述：参数呈现与客户资料规范不一致。\n"
                        "- 客户影响：客户可能按错误信息配置。\n"
                        "- 修改建议：核实并修正文档参数。\n\n"
                        "**2. 问题类型：客户资料定位（内部口吻）**\n"
                        "- 位置：第3页 / 注意事项\n"
                        "- 原文摘录：研发确认后再发布。\n"
                        "- 问题描述：面向客户资料出现内部流程口吻。\n"
                        "- 客户影响：影响客户对资料正式性的判断。\n"
                        "- 修改建议：改为客户可理解的正式表述。"
                    ),
                }
            ]
            cursor = get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', 'report.txt', 'stored.txt', 'txt',
                        1024, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (DOCUMENT_TASK_TYPE, json.dumps(result_json, ensure_ascii=False), now, now),
            )
            get_db().commit()
            task_id = cursor.lastrowid

        detail = self.client.get(f"/admin/tasks/{task_id}")

        self.assertEqual(detail.status_code, 200)
        soup = BeautifulSoup(detail.get_data(as_text=True), "html.parser")
        items = soup.select("[data-report-item]")
        self.assertEqual(len(items), 2)
        self.assertIn("技术信息呈现", items[0].get_text(" ", strip=True))
        self.assertIn("客户资料定位", items[1].get_text(" ", strip=True))
        self.assertNotIn("总体规范性结论", items[0].get_text(" ", strip=True))
        self.assertEqual(_required_tag(soup.select_one('[data-report-count="total"]')).get_text(strip=True), "2")

    def test_admin_task_list_shows_report_item_totals(self):
        with self.app.app_context():
            now = "2026-05-24 10:00:00"
            result_json = [
                {
                    "code": "compliance",
                    "name": "文档规范性检查",
                    "result": (
                        "1. 问题类型：参数错误\n"
                        "位置：第1页\n"
                        "问题描述：参数前后不一致。\n\n"
                        "2. 建议：补充适用范围\n"
                        "修改建议：增加适用范围说明。\n\n"
                        "3. 非问题：未发现客户风险\n"
                        "问题描述：该表述无需修改。"
                    ),
                }
            ]
            get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', 'report.txt', 'stored.txt', 'txt',
                        1024, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (DOCUMENT_TASK_TYPE, json.dumps(result_json, ensure_ascii=False), now, now),
            )
            get_db().commit()

        response = self.client.get("/admin/tasks")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        self.assertEqual(_required_tag(soup.select_one('[data-admin-report-count="issue"]')).get_text(strip=True), "1")
        self.assertEqual(_required_tag(soup.select_one('[data-admin-report-count="suggestion"]')).get_text(strip=True), "1")
        self.assertEqual(_required_tag(soup.select_one('[data-admin-report-count="non_issue"]')).get_text(strip=True), "1")
        self.assertEqual(_required_tag(soup.select_one('[data-admin-report-count="total"]')).get_text(strip=True), "3")

    def test_user_task_list_pagination_allows_page_jump(self):
        for index in range(21):
            self._insert_task(created_at=f"2026-05-01 10:{index:02d}:00")

        response = self.client.get("/?page=2")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        form = _required_tag(soup.select_one(".pagination .page-jump-form"))
        page_input = _required_tag(form.select_one('input[name="page"]'))
        self.assertEqual(form.get("method"), "get")
        self.assertEqual(form.get("action"), "/")
        self.assertEqual(page_input.get("type"), "number")
        self.assertEqual(page_input.get("min"), "1")
        self.assertEqual(page_input.get("max"), "2")
        self.assertEqual(page_input.get("value"), "2")
        self.assertIsNotNone(form.select_one('button[type="submit"]'))

    def test_admin_task_list_page_jump_preserves_filters(self):
        for index in range(21):
            self._insert_task(status="completed", created_at=f"2026-05-01 10:{index:02d}:00")

        response = self.client.get("/admin/tasks?status=completed&owner=127.0.0.1&page=2")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        form = _required_tag(soup.select_one(".pagination .page-jump-form"))
        page_input = _required_tag(form.select_one('input[name="page"]'))
        status_input = _required_tag(form.select_one('input[name="status"]'))
        owner_input = _required_tag(form.select_one('input[name="owner"]'))
        self.assertEqual(form.get("method"), "get")
        self.assertEqual(form.get("action"), "/admin/tasks")
        self.assertEqual(page_input.get("value"), "2")
        self.assertEqual(page_input.get("max"), "2")
        self.assertEqual(status_input.get("value"), "completed")
        self.assertEqual(owner_input.get("value"), "127.0.0.1")

    def test_report_item_type_update_persists_classification(self):
        with self.app.app_context():
            now = "2026-05-24 12:00:00"
            result_json = [
                {
                    "code": "compliance",
                    "name": "文档规范性检查",
                    "result": (
                        "1. 问题类型：内部备注残留\n"
                        "位置：第1章\n"
                        "原文摘录：TODO：研发确认\n"
                        "问题描述：面向客户资料中残留内部备注\n"
                        "影响说明：影响客户信任\n"
                        "修改建议：删除内部备注。"
                    ),
                }
            ]
            cursor = get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', 'report.txt', 'stored.txt', 'txt',
                        1024, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (DOCUMENT_TASK_TYPE, json.dumps(result_json, ensure_ascii=False), now, now),
            )
            get_db().commit()
            task_id = cursor.lastrowid

        detail = self.client.get(f"/admin/tasks/{task_id}")
        soup = BeautifulSoup(detail.get_data(as_text=True), "html.parser")
        item = _required_tag(soup.select_one("[data-report-item]"))
        item_id = item["data-item-id"]

        response = self.client.post(
            f"/admin/tasks/{task_id}/report-items",
            json={
                "result_code": "compliance",
                "item_id": item_id,
                "item_type": "non_issue",
                "acceptance_status": "rejected",
                "rejection_reason": "false_positive",
                "rejection_note": "原文上下文可解释",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["totals"]["issue"], 0)
        self.assertEqual(payload["totals"]["non_issue"], 1)
        self.assertEqual(payload["acceptance_status"], "rejected")
        self.assertEqual(payload["rejection_reason"], "false_positive")
        self.assertEqual(payload["rejection_note"], "原文上下文可解释")
        self.assertEqual(payload["totals"]["issue_detection_rate"], "0.0%")
        self.assertEqual(payload["totals"]["issue_acceptance_rate"], "-")
        with self.app.app_context():
            task = get_db().execute("SELECT result_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            stored = json.loads(task["result_json"])
        self.assertEqual(stored[0]["item_classifications"][item_id], "non_issue")
        self.assertEqual(
            stored[0]["item_acceptances"][item_id],
            {
                "status": "rejected",
                "rejection_reason": "false_positive",
                "rejection_note": "原文上下文可解释",
            },
        )

        accepted_response = self.client.post(
            f"/admin/tasks/{task_id}/report-items",
            json={
                "result_code": "compliance",
                "item_id": item_id,
                "item_type": "issue",
                "acceptance_status": "accepted",
            },
        )

        self.assertEqual(accepted_response.status_code, 200)
        accepted_payload = accepted_response.get_json()
        self.assertEqual(accepted_payload["totals"]["issue"], 1)
        self.assertEqual(accepted_payload["totals"]["accepted_issue"], 1)
        self.assertEqual(accepted_payload["totals"]["pending_issue_acceptance"], 0)
        self.assertEqual(accepted_payload["totals"]["issue_detection_rate"], "100.0%")
        self.assertEqual(accepted_payload["totals"]["issue_acceptance_rate"], "100.0%")

    def test_report_item_reject_requires_reason(self):
        with self.app.app_context():
            now = "2026-05-24 12:30:00"
            result_json = [
                {
                    "code": "compliance",
                    "name": "文档规范性检查",
                    "result": (
                        "1. 问题类型：参数错误\n"
                        "位置：第1章\n"
                        "问题描述：参数错误\n"
                        "修改建议：修正参数。"
                    ),
                }
            ]
            cursor = get_db().execute(
                """
                INSERT INTO tasks(
                    task_type, ip, original_filename, stored_filename, file_type,
                    file_size, result_json, checks_json, model_name, api_base,
                    status, progress, created_at, updated_at
                )
                VALUES (?, '127.0.0.1', 'report.txt', 'stored.txt', 'txt',
                        1024, ?, '[]', 'model-a', 'https://example.test/v1/chat/completions',
                        'completed', 100, ?, ?)
                """,
                (DOCUMENT_TASK_TYPE, json.dumps(result_json, ensure_ascii=False), now, now),
            )
            get_db().commit()
            task_id = cursor.lastrowid

        detail = self.client.get(f"/admin/tasks/{task_id}")
        soup = BeautifulSoup(detail.get_data(as_text=True), "html.parser")
        item_id = _required_tag(soup.select_one("[data-report-item]"))["data-item-id"]

        response = self.client.post(
            f"/admin/tasks/{task_id}/report-items",
            json={
                "result_code": "compliance",
                "item_id": item_id,
                "item_type": "issue",
                "acceptance_status": "rejected",
                "rejection_reason": "",
                "rejection_note": "",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "不接纳时必须选择或填写原因。")

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
        form = _required_tag(soup.find("form", {"data-require-checks": "true"}))
        self.assertEqual(form.get("autocomplete"), "off")
        self.assertEqual(form.get("data-default-unchecked-checks"), "true")
        checkboxes = form.select('input[name="checks"]')
        self.assertTrue(checkboxes)
        self.assertTrue(all(checkbox.get("checked") is None for checkbox in checkboxes))
        self.assertTrue(all(checkbox.get("autocomplete") == "off" for checkbox in checkboxes))

    def test_language_consistency_check_items_are_checked_by_default(self):
        self._configure_provider()

        response = self.client.get("/language-consistency")

        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
        form = _required_tag(soup.find("form", {"data-require-checks": "true"}))
        self.assertEqual(form.get("data-check-required-message"), "请至少选择一个跨语种对比项。")
        self.assertIsNotNone(form.select_one('input[name="document_a"]'))
        self.assertIsNotNone(form.select_one('input[name="document_b"]'))
        checkboxes = form.select('input[name="checks"]')
        self.assertTrue(checkboxes)
        self.assertTrue(all(checkbox.get("checked") is not None for checkbox in checkboxes))

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

    def test_create_language_consistency_task_rejects_missing_checks_before_saving_file(self):
        model_id = self._configure_provider()

        response = self.client.post(
            "/language-consistency",
            data={
                "document_a": (io.BytesIO("中文参数 10A".encode("utf-8")), "zh.txt"),
                "document_b": (io.BytesIO("English parameter 10A".encode("utf-8")), "en.txt"),
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            total = get_db().execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"]
        self.assertEqual(total, 0)
        self.assertEqual(list(Path(self.app.config["UPLOAD_FOLDER"]).iterdir()), [])

    def test_create_language_consistency_task_saves_static_precheck(self):
        model_id = self._configure_provider()
        with self.app.app_context():
            item = get_db().execute(
                "SELECT id, code, name, prompt FROM check_items WHERE code = 'language-consistency-cross-lingual'"
            ).fetchone()

        response = self.client.post(
            "/language-consistency",
            data={
                "document_a": (
                    io.BytesIO("1. 安装要求\n设备电流为 10A。\n访问 https://example.com/a。".encode("utf-8")),
                    "zh.txt",
                ),
                "document_b": (
                    io.BytesIO("1. Installation requirements\nThe device current is 12A.\nVisit https://example.com/b.".encode("utf-8")),
                    "en.txt",
                ),
                "checks": [str(item["id"])],
                "model_id": model_id,
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            task = get_db().execute(
                "SELECT task_type, original_filename, file_type, document_text, document_meta_json, checks_snapshot_json FROM tasks"
            ).fetchone()
        self.assertEqual(task["task_type"], LANGUAGE_CONSISTENCY_TASK_TYPE)
        self.assertEqual(task["file_type"], "双文档")
        self.assertIn("跨语种对比：zh.txt / en.txt", task["original_filename"])
        self.assertIn("# 静态预检摘要", task["document_text"])
        self.assertIn("文档A独有硬线索", task["document_text"])
        self.assertIn("文档B独有硬线索", task["document_text"])
        self.assertIn("10a", task["document_text"])
        self.assertIn("12a", task["document_text"])
        self.assertIn("# 文档A：zh.txt", task["document_text"])
        self.assertIn("# 文档B：en.txt", task["document_text"])
        meta = json.loads(task["document_meta_json"])
        self.assertEqual([group["role"] for group in meta["groups"]], ["document_a", "document_b"])
        self.assertIn("文档A独有硬线索", meta["static_precheck"])
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
