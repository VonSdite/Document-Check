import json
import secrets
from pathlib import Path


DEFAULT_ADMIN_URL = "/_gate_ops_9f2c7a"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 5000


def load_local_config(root_dir: Path) -> dict:
    config_path = root_dir / "config.local.json"
    if not config_path.exists():
        config = _default_config()
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return config

    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)

    original = json.dumps(config, ensure_ascii=False, sort_keys=True)
    config = _normalize_config(config)
    normalized = json.dumps(config, ensure_ascii=False, sort_keys=True)
    if normalized != original:
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return config


def _default_config() -> dict:
    return {
        "secret_key": secrets.token_urlsafe(32),
        "admin": {
            "username": "admin",
            "password": "admin123",
        },
        "admin_url": DEFAULT_ADMIN_URL,
        "server": {
            "host": DEFAULT_LISTEN_HOST,
            "port": DEFAULT_LISTEN_PORT,
        },
    }


def _normalize_config(config: dict) -> dict:
    config.setdefault("secret_key", secrets.token_urlsafe(32))
    config.setdefault("admin", {})
    config["admin"].setdefault("username", "admin")
    config["admin"].setdefault("password", "admin123")
    config["admin_url"] = _normalize_admin_url(config.get("admin_url", DEFAULT_ADMIN_URL))
    config.setdefault("server", {})
    config["server"].setdefault("host", DEFAULT_LISTEN_HOST)
    config["server"]["port"] = _normalize_port(config["server"].get("port", DEFAULT_LISTEN_PORT))
    return config


def _normalize_admin_url(value: str) -> str:
    value = str(value or DEFAULT_ADMIN_URL).strip().rstrip("/")
    if not value:
        return DEFAULT_ADMIN_URL
    if not value.startswith("/"):
        value = f"/{value}"
    return value


def _normalize_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LISTEN_PORT
    if 1 <= port <= 65535:
        return port
    return DEFAULT_LISTEN_PORT
