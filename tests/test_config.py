import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app.bootstrap.factory import create_app
from app.infrastructure.config import (
    CONFIG_FILENAME,
    DEFAULT_MAX_UPLOAD_MB,
    load_local_config,
    save_network_config,
)


def _write_config(root_dir: str, config: dict):
    config_path = Path(root_dir) / CONFIG_FILENAME
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )


class ProviderConfigTest(unittest.TestCase):
    def test_all_table_templates_load_column_resize_support(self):
        template_dir = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
        table_templates = []
        for template_path in template_dir.glob("*.html"):
            source = template_path.read_text(encoding="utf-8")
            if "<table" not in source:
                continue
            table_templates.append(template_path.name)
            parents = [
                parent.read_text(encoding="utf-8")
                for parent in template_dir.glob("*.html")
                if ('{% include "' + template_path.name + '" %}')
                in parent.read_text(encoding="utf-8")
            ]
            self.assertTrue(
                '{% extends "base.html" %}' in source
                or "{{ table_resize_js|safe }}" in source
                or (
                    parents
                    and all('{% extends "base.html" %}' in parent for parent in parents)
                ),
                f"{template_path.name} 及其使用页面未加载表格列宽拖拽脚本",
            )

        self.assertGreater(len(table_templates), 0)

    def test_default_admin_url_and_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_local_config(Path(temp_dir))

        self.assertNotIn("platform", config)
        self.assertEqual(config["admin_url"], "/console")
        self.assertEqual(config["server"]["host"], "127.0.0.1")
        self.assertEqual(config["server"]["port"], 31945)
        self.assertEqual(config["server"]["url_prefix"], "")
        self.assertEqual(config["server"]["real_ip_header"], "")
        self.assertFalse(config["server"]["proxy_fix"])
        self.assertEqual(config["server"]["max_upload_mb"], DEFAULT_MAX_UPLOAD_MB)
        self.assertEqual(
            config["network"],
            {"proxy_mode": "direct", "proxy": "", "ssl_verify": False},
        )
        self.assertEqual(config["auth"]["mode"], "ip")
        self.assertEqual(config["auth"]["cookie_session"]["userinfo_url"], "")
        self.assertEqual(config["logging"], {"console_level": "WARNING"})

    def test_console_logging_defaults_and_normalization_are_persisted(self):
        for value, expected in (
            (None, "WARNING"),
            ([], "WARNING"),
            ({}, "WARNING"),
            ({"console_level": " info "}, "INFO"),
            ({"console_level": "error"}, "ERROR"),
            ({"console_level": "critical"}, "CRITICAL"),
            ({"console_level": "invalid"}, "WARNING"),
        ):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                config = {"secret_key": "test", "logging": value}
                _write_config(temp_dir, config)
                normalized = load_local_config(Path(temp_dir))
                self.assertEqual(normalized["logging"], {"console_level": expected})
                self.assertEqual(normalized["secret_key"], "test")
                self.assertEqual(load_local_config(Path(temp_dir)), normalized)

    def test_server_proxy_config_is_normalized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {
                        "host": "0.0.0.0",
                        "port": 5000,
                        "url_prefix": " /infoCheck/ ",
                        "real_ip_header": " X-Real-IP ",
                        "proxy_fix": "true",
                        "max_upload_mb": "2048",
                    },
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["server"]["url_prefix"], "/infoCheck")
        self.assertEqual(config["server"]["real_ip_header"], "X-Real-IP")
        self.assertTrue(config["server"]["proxy_fix"])
        self.assertEqual(config["server"]["max_upload_mb"], 2048)

    def test_invalid_max_upload_limit_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {
                        "host": "127.0.0.1",
                        "port": 5000,
                        "max_upload_mb": 0,
                    },
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["server"]["max_upload_mb"], DEFAULT_MAX_UPLOAD_MB)

    def test_invalid_real_ip_header_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {
                        "host": "127.0.0.1",
                        "port": 5000,
                        "real_ip_header": "X Real IP",
                    },
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["server"]["real_ip_header"], "")

    def test_invalid_section_types_fall_back_to_safe_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": None,
                    "admin": "invalid",
                    "server": ["invalid"],
                    "auth": {"mode": "ip"},
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertTrue(config["secret_key"])
        self.assertEqual(config["admin"], {"username": "admin", "password": "admin123"})
        self.assertEqual(config["server"]["host"], "127.0.0.1")
        self.assertEqual(config["server"]["port"], 31945)
        self.assertEqual(config["auth"]["mode"], "ip")

    def test_explicit_empty_admin_password_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": ""},
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["admin"]["password"], "")

    def test_normalization_preserves_unknown_admin_and_server_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {
                        "username": "admin",
                        "password": "secret",
                        "note": "保留",
                    },
                    "server": {
                        "host": "127.0.0.1",
                        "port": 31945,
                        "extension": {"enabled": True},
                    },
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["admin"]["note"], "保留")
        self.assertEqual(config["server"]["extension"], {"enabled": True})

    def test_auth_cookie_session_config_is_normalized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "127.0.0.1", "port": 5000},
                    "auth": {
                        "mode": "cookie_session",
                        "cookie_session": {
                            "userinfo_url": " https://example.com/api/user ",
                            "cookie_header_name": " cookie ",
                            "ssl_verify": "false",
                            "timeout": "5",
                            "cache_ttl": "600",
                            "cache_grace": "0",
                            "avatar_url_template": " https://example.com/face/{user_id}/120 ",
                            "avatar_field": " employeeNum ",
                            "enabled_ips": [
                                " 10.0.0.8 ",
                                "10.0.0.8",
                                "2001:db8::1",
                                "",
                            ],
                            "field_mapping": {
                                "user_id": " employeeNum ",
                                "username": "displayCnName",
                                "employee_number": " employeeNum ",
                                "extra_fields": {" ": " ", "dept": " department "},
                            },
                            "login_url": " https://login.example.com/ ",
                        },
                    },
                    "providers": [],
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["auth"]["mode"], "cookie_session")
        cs = config["auth"]["cookie_session"]
        self.assertEqual(cs["userinfo_url"], "https://example.com/api/user")
        self.assertEqual(cs["cookie_header_name"], "cookie")
        self.assertFalse(cs["ssl_verify"])
        self.assertEqual(cs["timeout"], 5)
        self.assertEqual(cs["cache_ttl"], 600)
        self.assertEqual(cs["cache_grace"], 0)
        self.assertEqual(
            cs["avatar_url_template"], "https://example.com/face/{user_id}/120"
        )
        self.assertEqual(cs["avatar_field"], "employeeNum")
        self.assertEqual(cs["enabled_ips"], ["10.0.0.8", "2001:db8::1"])
        self.assertEqual(cs["field_mapping"]["user_id"], "employeeNum")
        self.assertEqual(cs["field_mapping"]["username"], "displayCnName")
        self.assertEqual(cs["field_mapping"]["employee_number"], "employeeNum")
        self.assertEqual(cs["field_mapping"]["extra_fields"], {"dept": "department"})
        self.assertEqual(cs["login_url"], "https://login.example.com/")

    def test_network_config_is_normalized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "127.0.0.1", "port": 5000},
                    "network": {
                        "proxy_mode": " CUSTOM ",
                        "proxy": " http://127.0.0.1:7890 ",
                        "ssl_verify": "true",
                    },
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(
            config["network"],
            {
                "proxy_mode": "custom",
                "proxy": "http://127.0.0.1:7890",
                "ssl_verify": True,
            },
        )

    def test_custom_network_proxy_without_address_falls_back_to_direct(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "127.0.0.1", "port": 5000},
                    "network": {
                        "proxy_mode": "custom",
                        "proxy": "",
                        "ssl_verify": "off",
                    },
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(
            config["network"],
            {"proxy_mode": "direct", "proxy": "", "ssl_verify": False},
        )

    def test_save_network_config_writes_yaml_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "0.0.0.0", "port": 5000},
                    "network": {
                        "proxy_mode": "direct",
                        "proxy": "",
                        "ssl_verify": False,
                    },
                },
            )

            network = save_network_config(
                Path(temp_dir),
                {
                    "proxy_mode": "custom",
                    "proxy": " http://127.0.0.1:7890 ",
                    "ssl_verify": True,
                },
            )
            config = yaml.safe_load(
                (Path(temp_dir) / CONFIG_FILENAME).read_text(encoding="utf-8")
            )

        self.assertEqual(
            network,
            {
                "proxy_mode": "custom",
                "proxy": "http://127.0.0.1:7890",
                "ssl_verify": True,
            },
        )
        self.assertNotIn("platform", config)
        self.assertEqual(config["network"], network)

    def test_default_config_uses_yaml_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            load_local_config(Path(temp_dir))

            self.assertTrue((Path(temp_dir) / CONFIG_FILENAME).exists())
            self.assertFalse((Path(temp_dir) / "config.local.json").exists())

    def test_invalid_auth_prevents_startup_and_preserves_config(self):
        for auth in (
            None,
            [],
            "ip",
            *(
                {"mode": mode}
                for mode in ("saml", "trusted_header", "cookie_sesion", "", None, False)
            ),
        ):
            with self.subTest(auth=auth), tempfile.TemporaryDirectory() as temp_dir:
                _write_config(temp_dir, {"auth": auth})
                path = Path(temp_dir) / CONFIG_FILENAME
                original = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "auth"):
                    create_app(Path(temp_dir))
                self.assertEqual(path.read_bytes(), original)

    def test_extra_fields_type_is_validated(self):
        for value in ("bad", ["bad"], 1):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                _write_config(
                    temp_dir,
                    {
                        "auth": {
                            "mode": "ip",
                            "cookie_session": {
                                "field_mapping": {"extra_fields": value}
                            },
                        }
                    },
                )
                with self.assertRaisesRegex(ValueError, "extra_fields"):
                    load_local_config(Path(temp_dir))

    def test_cookie_session_enabled_ips_are_validated(self):
        for value in ("10.0.0.8", ["not-an-ip"], {"ip": "10.0.0.8"}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp_dir:
                _write_config(
                    temp_dir,
                    {
                        "auth": {
                            "mode": "ip",
                            "cookie_session": {"enabled_ips": value},
                        }
                    },
                )
                with self.assertRaisesRegex(ValueError, "enabled_ips"):
                    load_local_config(Path(temp_dir))

    def test_app_uses_configured_listen_host(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "0.0.0.0", "port": 5000},
                    "providers": [],
                },
            )

            with (
                patch(
                    "app.infrastructure.runtime._runtime_root_dir",
                    return_value=Path(temp_dir),
                ),
                patch("app.infrastructure.runtime._configure_logging"),
            ):
                created_app = create_app()
            self.assertNotIn("PLATFORM", created_app.config)
            self.assertEqual(created_app.config["LISTEN_HOST"], "0.0.0.0")
            self.assertEqual(created_app.config["LISTEN_PORT"], 5000)
            self.assertEqual(created_app.config["MAX_UPLOAD_MB"], DEFAULT_MAX_UPLOAD_MB)
            self.assertEqual(
                created_app.config["MAX_CONTENT_LENGTH"],
                DEFAULT_MAX_UPLOAD_MB * 1024 * 1024,
            )
            self.assertEqual(created_app.config["WEB_WORKERS"], 1)
            self.assertEqual(created_app.config["WEB_THREADS"], 16)
            self.assertEqual(created_app.config["MAX_TASK_PROCESSES"], 4)

    def test_app_without_config_requires_admin_login(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch(
                    "app.infrastructure.runtime._runtime_root_dir",
                    return_value=Path(temp_dir),
                ),
                patch("app.infrastructure.runtime._configure_logging"),
            ):
                created_app = create_app()
            config = load_local_config(Path(temp_dir))

            self.assertNotIn("PLATFORM", created_app.config)
            self.assertEqual(created_app.config["LISTEN_HOST"], "127.0.0.1")
            self.assertNotIn("platform", config)
            self.assertEqual(config["server"]["host"], "127.0.0.1")
            self.assertEqual(config["server"]["max_upload_mb"], DEFAULT_MAX_UPLOAD_MB)
            self.assertEqual(config["server"]["web_workers"], 1)
            self.assertEqual(config["server"]["web_threads"], 16)
            self.assertEqual(config["worker"]["max_task_processes"], 4)
            self.assertTrue((Path(temp_dir) / CONFIG_FILENAME).exists())
            response = created_app.test_client().get("/console")
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith("/console/login"))

    def test_app_uses_configured_url_prefix_for_generated_urls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {
                        "host": "127.0.0.1",
                        "port": 5000,
                        "url_prefix": "/infoCheck",
                    },
                },
            )

            with (
                patch(
                    "app.infrastructure.runtime._runtime_root_dir",
                    return_value=Path(temp_dir),
                ),
                patch("app.infrastructure.runtime._configure_logging"),
            ):
                created_app = create_app()
            response = created_app.test_client().get("/")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(created_app.config["APPLICATION_ROOT"], "/infoCheck")
            self.assertRegex(html, r'href="/infoCheck/static/app\.css(?:\?[^\"]*)?"')
            self.assertRegex(html, r'src="/infoCheck/static/app\.js(?:\?[^\"]*)?"')
            self.assertIn('src="/infoCheck/static/table-resize.js"', html)

    def test_config_drops_legacy_providers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "127.0.0.1", "port": 5000},
                    "providers": [
                        {
                            "id": "provider-1",
                            "name": "测试提供商",
                            "api_base": "https://example.test/v1/chat/completions",
                            "models": ["model-a"],
                        }
                    ],
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertNotIn("providers", config)


if __name__ == "__main__":
    unittest.main()
