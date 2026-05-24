import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app import _runtime_root_dir, create_app
from app.config import CONFIG_FILENAME, load_local_config, save_network_config


def _write_config(root_dir: str, config: dict):
    config_path = Path(root_dir) / CONFIG_FILENAME
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )


class ProviderConfigTest(unittest.TestCase):
    def test_default_admin_url_and_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_local_config(Path(temp_dir))

        self.assertFalse(config["platform"])
        self.assertEqual(config["admin_url"], "/console")
        self.assertEqual(config["server"]["host"], "127.0.0.1")
        self.assertEqual(config["server"]["port"], 31945)
        self.assertEqual(config["network"], {"proxy_mode": "direct", "proxy": "", "ssl_verify": False})
        self.assertEqual(config["auth"]["mode"], "ip")
        self.assertEqual(config["auth"]["trusted_header"], {"user_id": "", "username": ""})
        self.assertEqual(config["auth"]["saml"]["sp_entity_id"], "")

    def test_auth_trusted_header_config_is_normalized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "127.0.0.1", "port": 5000},
                    "auth": {
                        "mode": "trusted_header",
                        "trusted_header": {
                            "user_id": " X-SSO-User-Id ",
                            "username": "X-SSO-User-Name",
                        },
                    },
                    "providers": [],
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(
            config["auth"],
            {
                "mode": "trusted_header",
                "trusted_header": {
                    "user_id": "X-SSO-User-Id",
                    "username": "X-SSO-User-Name",
                },
                "saml": {
                    "sp_entity_id": "",
                    "acs_url": "",
                    "idp_entity_id": "",
                    "idp_sso_url": "",
                    "idp_x509_cert": "",
                    "user_id_attribute": "",
                    "username_attribute": "",
                },
            },
        )

    def test_auth_saml_config_is_normalized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "127.0.0.1", "port": 5000},
                    "auth": {
                        "mode": "saml",
                        "saml": {
                            "sp_entity_id": " https://doc.example.com/auth/saml/metadata ",
                            "acs_url": "https://doc.example.com/auth/saml/acs",
                            "idp_entity_id": " https://sso.example.com/idp ",
                            "idp_sso_url": "https://sso.example.com/login",
                            "idp_x509_cert": " test-cert ",
                            "user_id_attribute": " uid ",
                            "username_attribute": "displayName",
                        },
                    },
                    "providers": [],
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["auth"]["mode"], "saml")
        self.assertEqual(config["auth"]["saml"]["sp_entity_id"], "https://doc.example.com/auth/saml/metadata")
        self.assertEqual(config["auth"]["saml"]["idp_x509_cert"], "test-cert")
        self.assertEqual(config["auth"]["saml"]["user_id_attribute"], "uid")

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
                    "network": {"proxy_mode": "custom", "proxy": "", "ssl_verify": "off"},
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["network"], {"proxy_mode": "direct", "proxy": "", "ssl_verify": False})

    def test_save_network_config_writes_yaml_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "platform": True,
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "0.0.0.0", "port": 5000},
                    "network": {"proxy_mode": "direct", "proxy": "", "ssl_verify": False},
                },
            )

            network = save_network_config(
                Path(temp_dir),
                {"proxy_mode": "custom", "proxy": " http://127.0.0.1:7890 ", "ssl_verify": True},
            )
            config = yaml.safe_load((Path(temp_dir) / CONFIG_FILENAME).read_text(encoding="utf-8"))

        self.assertEqual(
            network,
            {"proxy_mode": "custom", "proxy": "http://127.0.0.1:7890", "ssl_verify": True},
        )
        self.assertTrue(config["platform"])
        self.assertEqual(config["network"], network)

    def test_default_config_uses_yaml_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            load_local_config(Path(temp_dir))

            self.assertTrue((Path(temp_dir) / CONFIG_FILENAME).exists())
            self.assertFalse((Path(temp_dir) / "config.local.json").exists())

    def test_platform_accepts_false_like_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "platform": "false",
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "0.0.0.0", "port": 5000},
                    "providers": [],
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertFalse(config["platform"])

    def test_non_platform_app_forces_loopback_host(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_config(
                temp_dir,
                {
                    "platform": False,
                    "secret_key": "test",
                    "admin": {"username": "admin", "password": "password"},
                    "admin_url": "/admin",
                    "server": {"host": "0.0.0.0", "port": 5000},
                    "providers": [],
                },
            )

            with (
                patch("app._runtime_root_dir", return_value=Path(temp_dir)),
                patch("app._configure_logging"),
            ):
                created_app = create_app()
            try:
                self.assertFalse(created_app.config["PLATFORM"])
                self.assertEqual(created_app.config["LISTEN_HOST"], "127.0.0.1")
                self.assertEqual(created_app.config["LISTEN_PORT"], 5000)
            finally:
                created_app.extensions["task_scheduler"].stop()

    def test_app_without_config_defaults_to_non_platform(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("app._runtime_root_dir", return_value=Path(temp_dir)),
                patch("app._configure_logging"),
            ):
                created_app = create_app()
            try:
                config = load_local_config(Path(temp_dir))

                self.assertFalse(created_app.config["PLATFORM"])
                self.assertEqual(created_app.config["LISTEN_HOST"], "127.0.0.1")
                self.assertFalse(config["platform"])
                self.assertEqual(config["server"]["host"], "127.0.0.1")
                self.assertTrue((Path(temp_dir) / CONFIG_FILENAME).exists())
            finally:
                created_app.extensions["task_scheduler"].stop()

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

    def test_frozen_runtime_root_uses_executable_directory(self):
        executable = Path(tempfile.gettempdir()) / "DocumentCheck" / "DocumentCheck.exe"
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(executable)),
        ):
            root_dir = _runtime_root_dir()

        self.assertEqual(root_dir, executable.parent)


if __name__ == "__main__":
    unittest.main()
