import json
import secrets
import threading
import uuid
from datetime import datetime
from pathlib import Path


DEFAULT_ADMIN_URL = "/console"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 31945
DEFAULT_REQUEST_TIMEOUT = 3600
DEFAULT_MAX_INPUT_CHARS = 80000
DEFAULT_SSL_VERIFY = False
PROXY_MODES = {"direct", "system", "custom"}
_CONFIG_LOCK = threading.Lock()


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


def save_local_config(root_dir: Path, config: dict) -> dict:
    with _CONFIG_LOCK:
        config = _normalize_config(config)
        config_path = root_dir / "config.local.json"
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
        "providers": [],
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
    config["providers"] = _normalize_providers(config.get("providers", []))
    return config


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
