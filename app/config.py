import json
import secrets
from pathlib import Path


DEFAULT_ADMIN_URL = "/_gate_ops_9f2c7a"


def load_local_config(root_dir: Path) -> dict:
    config_path = root_dir / "config.local.json"
    if not config_path.exists():
        config = {
            "secret_key": secrets.token_urlsafe(32),
            "admin": {
                "username": "admin",
                "password": "admin123",
            },
            "admin_url": DEFAULT_ADMIN_URL,
        }
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return config

    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)

    config.setdefault("secret_key", secrets.token_urlsafe(32))
    config.setdefault("admin", {})
    config["admin"].setdefault("username", "admin")
    config["admin"].setdefault("password", "admin123")
    config.setdefault("admin_url", DEFAULT_ADMIN_URL)
    return config
