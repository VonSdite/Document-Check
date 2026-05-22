import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import _runtime_root_dir
from app.config import load_local_config


class ProviderConfigTest(unittest.TestCase):
    def test_default_admin_url_and_port(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["admin_url"], "/console")
        self.assertEqual(config["server"]["port"], 31945)

    def test_provider_ssl_verify_defaults_to_false(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.local.json"
            config_path.write_text(
                json.dumps(
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
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )

            config = load_local_config(Path(temp_dir))

        self.assertFalse(config["providers"][0]["ssl_verify"])

    def test_provider_max_input_chars_defaults_to_80000(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.local.json"
            config_path.write_text(
                json.dumps(
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
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )

            config = load_local_config(Path(temp_dir))

        self.assertEqual(config["providers"][0]["max_input_chars"], 80000)

    def test_provider_ssl_verify_accepts_form_like_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.local.json"
            config_path.write_text(
                json.dumps(
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
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )

            config = load_local_config(Path(temp_dir))

        self.assertTrue(config["providers"][0]["ssl_verify"])

    def test_provider_models_preserve_force_disable_thinking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.local.json"
            config_path.write_text(
                json.dumps(
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
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
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
            config_path = Path(temp_dir) / "config.local.json"
            config_path.write_text(
                json.dumps(
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
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
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
