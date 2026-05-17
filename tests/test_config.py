import json
import tempfile
import unittest
from pathlib import Path

from app.config import load_local_config


class ProviderConfigTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
