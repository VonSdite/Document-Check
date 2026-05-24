import secrets
import threading
import uuid
from datetime import datetime
from pathlib import Path

import yaml


DEFAULT_ADMIN_URL = "/console"
DEFAULT_PLATFORM = False
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LOCAL_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 31945
DEFAULT_REQUEST_TIMEOUT = 3600
DEFAULT_MAX_INPUT_CHARS = 80000
DEFAULT_SSL_VERIFY = False
DEFAULT_AUTH_MODE = "ip"
PROXY_MODES = {"direct", "system", "custom"}
AUTH_MODES = {"ip", "trusted_header"}
_CONFIG_LOCK = threading.Lock()
CONFIG_FILENAME = "config.yaml"


def load_local_config(root_dir: Path) -> dict:
    config_path = root_dir / CONFIG_FILENAME
    if not config_path.exists():
        config = _default_config()
        _write_config(config_path, config)
        return config

    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}
    if not isinstance(config, dict):
        config = {}

    original = _dump_config(config)
    config = _normalize_config(config)
    normalized = _dump_config(config)
    if normalized != original:
        _write_config(config_path, config)
    return config


def save_local_config(root_dir: Path, config: dict) -> dict:
    with _CONFIG_LOCK:
        config = _normalize_config(config)
        config_path = root_dir / CONFIG_FILENAME
        _write_config(config_path, config)
        return config


def _default_config() -> dict:
    return {
        "platform": DEFAULT_PLATFORM,
        "secret_key": secrets.token_urlsafe(32),
        "admin": {
            "username": "admin",
            "password": "admin123",
        },
        "admin_url": DEFAULT_ADMIN_URL,
        "server": {
            "host": DEFAULT_LISTEN_HOST if DEFAULT_PLATFORM else DEFAULT_LOCAL_LISTEN_HOST,
            "port": DEFAULT_LISTEN_PORT,
        },
        "auth": {
            "mode": DEFAULT_AUTH_MODE,
            "trusted_header": {
                "user": "",
                "name": "",
                "email": "",
            },
        },
        "providers": [],
    }


def _write_config(config_path: Path, config: dict):
    config_path.write_text(_dump_config(config), encoding="utf-8", newline="\n")


def _dump_config(config: dict) -> str:
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def _normalize_config(config: dict) -> dict:
    config["platform"] = _normalize_bool(config.get("platform"), DEFAULT_PLATFORM)
    config.setdefault("secret_key", secrets.token_urlsafe(32))
    config.setdefault("admin", {})
    config["admin"].setdefault("username", "admin")
    config["admin"].setdefault("password", "admin123")
    config["admin_url"] = _normalize_admin_url(config.get("admin_url", DEFAULT_ADMIN_URL))
    config.setdefault("server", {})
    default_host = DEFAULT_LISTEN_HOST if config["platform"] else DEFAULT_LOCAL_LISTEN_HOST
    config["server"].setdefault("host", default_host)
    config["server"]["port"] = _normalize_port(config["server"].get("port", DEFAULT_LISTEN_PORT))
    config["auth"] = _normalize_auth(config.get("auth", {}))
    config["providers"] = _normalize_providers(config.get("providers", []))
    return config


def _normalize_auth(value) -> dict:
    if not isinstance(value, dict):
        value = {}
    mode = str(value.get("mode") or DEFAULT_AUTH_MODE).strip().lower()
    if mode not in AUTH_MODES:
        mode = DEFAULT_AUTH_MODE
    trusted_header = value.get("trusted_header", {})
    if not isinstance(trusted_header, dict):
        trusted_header = {}
    return {
        "mode": mode,
        "trusted_header": {
            "user": str(trusted_header.get("user") or "").strip(),
            "name": str(trusted_header.get("name") or "").strip(),
            "email": str(trusted_header.get("email") or "").strip(),
        },
    }


def _normalize_providers(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    providers = []
    seen_ids = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        provider = _normalize_provider(item)
        if provider["id"] in seen_ids:
            provider["id"] = uuid.uuid4().hex
        seen_ids.add(provider["id"])
        providers.append(provider)
    return providers


def _normalize_provider(provider: dict) -> dict:
    now = _now_text()
    proxy_mode = str(provider.get("proxy_mode", "direct") or "direct")
    if proxy_mode not in PROXY_MODES:
        proxy_mode = "direct"
    proxy = str(provider.get("proxy", "") or "").strip() if proxy_mode == "custom" else ""
    return {
        "id": str(provider.get("id") or uuid.uuid4().hex),
        "name": str(provider.get("name", "") or "").strip(),
        "api_base": str(provider.get("api_base", "") or "").strip(),
        "api_key": str(provider.get("api_key", "") or ""),
        "proxy_mode": proxy_mode,
        "proxy": proxy,
        "ssl_verify": _normalize_bool(provider.get("ssl_verify"), DEFAULT_SSL_VERIFY),
        "request_timeout": _normalize_int(provider.get("request_timeout"), DEFAULT_REQUEST_TIMEOUT),
        "max_input_chars": _normalize_int(provider.get("max_input_chars"), DEFAULT_MAX_INPUT_CHARS),
        "is_active": bool(provider.get("is_active", True)),
        "models": _normalize_models(provider.get("models", [])),
        "created_at": str(provider.get("created_at") or now),
        "updated_at": str(provider.get("updated_at") or now),
    }


def _normalize_models(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    models = []
    seen = set()
    for item in value:
        if isinstance(item, dict):
            model_name = str(item.get("model_name") or item.get("id") or "").strip()
            force_disable_thinking = _normalize_bool(item.get("force_disable_thinking"), False)
        else:
            model_name = str(item or "").strip()
            force_disable_thinking = False
        key = (model_name, force_disable_thinking)
        if not model_name or key in seen:
            continue
        seen.add(key)
        models.append(
            {
                "model_name": model_name,
                "force_disable_thinking": force_disable_thinking,
            }
        )
    return models


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


def _normalize_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
