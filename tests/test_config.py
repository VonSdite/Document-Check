import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app import _runtime_root_dir, create_app
from app.config import CONFIG_FILENAME, load_local_config


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

        self.assertTrue(config["platform"])
        self.assertEqual(config["admin_url"], "/console")
        self.assertEqual(config["server"]["port"], 31945)

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

    def test_frozen_app_without_config_defaults_to_non_platform(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "DocumentCheck.exe"
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
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

    def test_provider_ssl_verify_defaults_to_false(self):
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
                            "api_base": "https://example.test/v1",
                            "models": ["model-a"],
                        }
                    ],
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertFalse(config["providers"][0]["ssl_verify"])

    def test_provider_max_input_chars_defaults_to_80000(self):
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
                            "api_base": "https://example.test/v1",
                            "models": ["model-a"],
                        }
                    ],
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["providers"][0]["max_input_chars"], 80000)

    def test_provider_ssl_verify_accepts_form_like_values(self):
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
                            "api_base": "https://example.test/v1",
                            "ssl_verify": "on",
                            "models": ["model-a"],
                        }
                    ],
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertTrue(config["providers"][0]["ssl_verify"])

    def test_provider_models_preserve_force_disable_thinking(self):
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
                            "models": [
                                {"model_name": "model-a", "force_disable_thinking": "on"},
                                "model-b",
                            ],
                        }
                    ],
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(
            config["providers"][0]["models"],
            [
                {"model_name": "model-a", "force_disable_thinking": True},
                {"model_name": "model-b", "force_disable_thinking": False},
            ],
        )

    def test_provider_models_allow_same_name_for_distinct_thinking_modes(self):
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
                            "models": [
                                {"model_name": "same-model", "force_disable_thinking": False},
                                {"model_name": "same-model", "force_disable_thinking": True},
                                {"model_name": "same-model", "force_disable_thinking": False},
                                {"model_name": "same-model", "force_disable_thinking": True},
                            ],
                        }
                    ],
                },
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(
            config["providers"][0]["models"],
            [
                {"model_name": "same-model", "force_disable_thinking": False},
                {"model_name": "same-model", "force_disable_thinking": True},
            ],
        )

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
