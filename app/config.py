import secrets
from pathlib import Path

import yaml


DEFAULT_ADMIN_URL = "/console"
DEFAULT_PLATFORM = False
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LOCAL_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 31945
DEFAULT_AUTH_MODE = "ip"
AUTH_MODES = {"ip", "trusted_header", "saml"}
DEFAULT_PROXY_MODE = "direct"
PROXY_MODES = {"direct", "system", "custom"}
DEFAULT_SSL_VERIFY = False
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
        "network": {
            "proxy_mode": DEFAULT_PROXY_MODE,
            "proxy": "",
            "ssl_verify": DEFAULT_SSL_VERIFY,
        },
        "auth": {
            "mode": DEFAULT_AUTH_MODE,
            "trusted_header": {
                "user_id": "",
                "username": "",
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
    config["network"] = normalize_network_config(config.get("network", {}))
    config["auth"] = _normalize_auth(config.get("auth", {}))
    config.pop("providers", None)
    return config


def normalize_network_config(value) -> dict:
    if not isinstance(value, dict):
        value = {}
    proxy_mode = str(value.get("proxy_mode") or DEFAULT_PROXY_MODE).strip().lower()
    if proxy_mode not in PROXY_MODES:
        proxy_mode = DEFAULT_PROXY_MODE
    proxy = str(value.get("proxy") or "").strip()
    if proxy_mode != "custom":
        proxy = ""
    elif not proxy:
        proxy_mode = DEFAULT_PROXY_MODE
    return {
        "proxy_mode": proxy_mode,
        "proxy": proxy,
        "ssl_verify": _normalize_bool(value.get("ssl_verify"), DEFAULT_SSL_VERIFY),
    }


def _normalize_auth(value) -> dict:
    if not isinstance(value, dict):
        value = {}
    mode = str(value.get("mode") or DEFAULT_AUTH_MODE).strip().lower()
    if mode not in AUTH_MODES:
        mode = DEFAULT_AUTH_MODE
    trusted_header = value.get("trusted_header", {})
    if not isinstance(trusted_header, dict):
        trusted_header = {}
    saml = value.get("saml", {})
    if not isinstance(saml, dict):
        saml = {}
    return {
        "mode": mode,
        "trusted_header": {
            "user_id": str(trusted_header.get("user_id") or "").strip(),
            "username": str(trusted_header.get("username") or "").strip(),
        },
        "saml": {
            "sp_entity_id": str(saml.get("sp_entity_id") or "").strip(),
            "acs_url": str(saml.get("acs_url") or "").strip(),
            "idp_entity_id": str(saml.get("idp_entity_id") or "").strip(),
            "idp_sso_url": str(saml.get("idp_sso_url") or "").strip(),
            "idp_x509_cert": str(saml.get("idp_x509_cert") or "").strip(),
            "user_id_attribute": str(saml.get("user_id_attribute") or "").strip(),
            "username_attribute": str(saml.get("username_attribute") or "").strip(),
        },
    }


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
