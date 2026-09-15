import re
import secrets
from pathlib import Path

import yaml

DEFAULT_ADMIN_URL = "/console"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 31945
DEFAULT_URL_PREFIX = ""
DEFAULT_REAL_IP_HEADER = ""
DEFAULT_PROXY_FIX = False
DEFAULT_MAX_UPLOAD_MB = 1024
DEFAULT_WEB_WORKERS = 1
DEFAULT_WEB_THREADS = 16
DEFAULT_MAX_TASK_PROCESSES = 4
DEFAULT_CONSOLE_LOG_LEVEL = "WARNING"
CONSOLE_LOG_LEVELS = {"INFO", "WARNING", "ERROR", "CRITICAL"}
DEFAULT_AUTH_MODE = "ip"
AUTH_MODES = {"ip", "cookie_session"}
DEFAULT_PROXY_MODE = "direct"
PROXY_MODES = {"direct", "system", "custom"}
DEFAULT_SSL_VERIFY = False
CONFIG_FILENAME = "config.yaml"
HTTP_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")


def load_local_config(root_dir: Path) -> dict:
    config_path = root_dir / CONFIG_FILENAME
    if not config_path.exists():
        config = _default_config()
        _write_config(config_path, config)
        return config

    config = _read_config(config_path)
    original = _dump_config(config)
    config = _normalize_config(config)
    normalized = _dump_config(config)
    if normalized != original:
        _write_config(config_path, config)
    return config


def save_network_config(root_dir: Path, value) -> dict:
    config_path = root_dir / CONFIG_FILENAME
    config = _read_config(config_path) if config_path.exists() else _default_config()
    config["network"] = normalize_network_config(value)
    config = _normalize_config(config)
    _write_config(config_path, config)
    return config["network"]


def _read_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}
    if not isinstance(config, dict):
        config = {}
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
            "url_prefix": DEFAULT_URL_PREFIX,
            "real_ip_header": DEFAULT_REAL_IP_HEADER,
            "proxy_fix": DEFAULT_PROXY_FIX,
            "max_upload_mb": DEFAULT_MAX_UPLOAD_MB,
            "web_workers": DEFAULT_WEB_WORKERS,
            "web_threads": DEFAULT_WEB_THREADS,
        },
        "worker": {
            "max_task_processes": DEFAULT_MAX_TASK_PROCESSES,
        },
        "logging": {"console_level": DEFAULT_CONSOLE_LOG_LEVEL},
        "network": {
            "proxy_mode": DEFAULT_PROXY_MODE,
            "proxy": "",
            "ssl_verify": DEFAULT_SSL_VERIFY,
        },
        "auth": {
            "mode": DEFAULT_AUTH_MODE,
            "cookie_session": {
                "userinfo_url": "",
                "cookie_header_name": "cookie",
                "ssl_verify": DEFAULT_SSL_VERIFY,
                "timeout": 3,
                "cache_ttl": 600,
                "cache_grace": 600,
                "avatar_url_template": "",
                "avatar_field": "",
                "field_mapping": {
                    "user_id": "",
                    "username": "",
                    "employee_number": "",
                    "extra_fields": {},
                },
                "login_url": "",
            },
        },
    }


def _write_config(config_path: Path, config: dict):
    config_path.write_text(_dump_config(config), encoding="utf-8", newline="\n")


def _dump_config(config: dict) -> str:
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)


def _normalize_config(config: dict) -> dict:
    config.pop("platform", None)
    secret_key = config.get("secret_key")
    if not isinstance(secret_key, str) or not secret_key:
        config["secret_key"] = secrets.token_urlsafe(32)

    admin = config.get("admin")
    if not isinstance(admin, dict):
        admin = {}
    config["admin"] = admin
    admin_username = admin.get("username")
    admin_password = admin.get("password")
    admin["username"] = "admin" if admin_username is None else str(admin_username)
    admin["password"] = "admin123" if admin_password is None else str(admin_password)
    config["admin_url"] = _normalize_admin_url(
        config.get("admin_url", DEFAULT_ADMIN_URL)
    )

    server = config.get("server")
    if not isinstance(server, dict):
        server = {}
    config["server"] = server
    default_host = DEFAULT_LISTEN_HOST
    server["host"] = str(server.get("host") or default_host).strip() or default_host
    server["port"] = _normalize_port(server.get("port", DEFAULT_LISTEN_PORT))
    server["url_prefix"] = _normalize_url_prefix(
        server.get("url_prefix", DEFAULT_URL_PREFIX)
    )
    server["real_ip_header"] = _normalize_header_name(
        server.get("real_ip_header", DEFAULT_REAL_IP_HEADER)
    )
    server["proxy_fix"] = _normalize_bool(server.get("proxy_fix"), DEFAULT_PROXY_FIX)
    server["max_upload_mb"] = _normalize_positive_int(
        server.get("max_upload_mb", DEFAULT_MAX_UPLOAD_MB),
        DEFAULT_MAX_UPLOAD_MB,
    )
    server["web_workers"] = _normalize_positive_int(
        server.get("web_workers", DEFAULT_WEB_WORKERS),
        DEFAULT_WEB_WORKERS,
    )
    server["web_threads"] = _normalize_positive_int(
        server.get("web_threads", DEFAULT_WEB_THREADS),
        DEFAULT_WEB_THREADS,
    )
    worker = config.get("worker")
    if not isinstance(worker, dict):
        worker = {}
    config["worker"] = worker
    worker["max_task_processes"] = _normalize_positive_int(
        worker.get("max_task_processes", DEFAULT_MAX_TASK_PROCESSES),
        DEFAULT_MAX_TASK_PROCESSES,
    )
    log_config = config.get("logging")
    if not isinstance(log_config, dict):
        log_config = {}
    console_level = str(log_config.get("console_level") or "").strip().upper()
    config["logging"] = {
        "console_level": console_level
        if console_level in CONSOLE_LOG_LEVELS
        else DEFAULT_CONSOLE_LOG_LEVEL,
    }
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
        raise ValueError("auth 必须是配置对象，auth.mode 只允许 ip 或 cookie_session")
    mode = str(value.get("mode", DEFAULT_AUTH_MODE)).strip().lower()
    if mode not in AUTH_MODES:
        raise ValueError("auth.mode 只允许 ip 或 cookie_session")
    cookie_session = value.get("cookie_session", {})
    if not isinstance(cookie_session, dict):
        cookie_session = {}
    field_mapping = cookie_session.get("field_mapping", {})
    if not isinstance(field_mapping, dict):
        field_mapping = {}
    return {
        "mode": mode,
        "cookie_session": _normalize_cookie_session(cookie_session, field_mapping),
    }


def _normalize_cookie_session(cookie_session: dict, field_mapping: dict) -> dict:
    extra_fields = field_mapping.get("extra_fields", {})
    if not isinstance(extra_fields, dict):
        raise ValueError(
            "auth.cookie_session.field_mapping.extra_fields 必须是配置对象"
        )
    return {
        "userinfo_url": str(cookie_session.get("userinfo_url") or "").strip(),
        "cookie_header_name": _normalize_header_name(
            cookie_session.get("cookie_header_name")
        )
        or "cookie",
        "ssl_verify": _normalize_bool(
            cookie_session.get("ssl_verify"), DEFAULT_SSL_VERIFY
        ),
        "timeout": _normalize_positive_int(cookie_session.get("timeout"), 3),
        "cache_ttl": _normalize_positive_int(cookie_session.get("cache_ttl"), 600),
        "cache_grace": _normalize_non_negative_int(
            cookie_session.get("cache_grace"), 600
        ),
        "avatar_url_template": str(
            cookie_session.get("avatar_url_template") or ""
        ).strip(),
        "avatar_field": str(cookie_session.get("avatar_field") or "").strip(),
        "field_mapping": {
            "user_id": str(field_mapping.get("user_id") or "").strip(),
            "username": str(field_mapping.get("username") or "").strip(),
            "employee_number": str(field_mapping.get("employee_number") or "").strip(),
            "extra_fields": {
                str(k).strip(): str(v).strip()
                for k, v in extra_fields.items()
                if str(k).strip() and str(v).strip()
            },
        },
        "login_url": str(cookie_session.get("login_url") or "").strip(),
    }


def _normalize_admin_url(value: str) -> str:
    value = str(value or DEFAULT_ADMIN_URL).strip().rstrip("/")
    if not value:
        return DEFAULT_ADMIN_URL
    if not value.startswith("/"):
        value = f"/{value}"
    return value


def _normalize_url_prefix(value: str) -> str:
    value = str(value or DEFAULT_URL_PREFIX).strip().rstrip("/")
    if not value or value == "/":
        return ""
    if not value.startswith("/"):
        value = f"/{value}"
    return value


def _normalize_header_name(value: str) -> str:
    value = str(value or DEFAULT_REAL_IP_HEADER).strip()
    if not value or not HTTP_HEADER_NAME_RE.fullmatch(value):
        return ""
    return value


def _normalize_port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LISTEN_PORT
    if 1 <= port <= 65535:
        return port
    return DEFAULT_LISTEN_PORT


def _normalize_positive_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return number


def _normalize_non_negative_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < 0:
        return default
    return number


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
