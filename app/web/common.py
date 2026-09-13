from urllib.parse import urlsplit

from flask import current_app, request, url_for


def _wants_json_response() -> bool:
    return (
        request.headers.get("X-Requested-With") == "fetch"
        or request.accept_mimetypes.best == "application/json"
    )


def _form_bool(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _max_upload_mb() -> int:
    try:
        return max(1, int(current_app.config.get("MAX_UPLOAD_MB") or 1))
    except (TypeError, ValueError):
        return 1


def _max_task_processes() -> int:
    try:
        return max(1, int(current_app.config.get("MAX_TASK_PROCESSES") or 4))
    except (TypeError, ValueError):
        return 4


def _request_entity_too_large_redirect() -> str:
    upload_endpoints = {
        "user_tasks",
        "user_new_task",
        "user_consistency",
        "user_language_consistency",
        "user_images",
        "user_videos",
        "admin_tasks",
        "admin_consistency",
        "admin_language_consistency",
        "admin_images",
        "admin_videos",
    }
    if request.endpoint in upload_endpoints:
        return url_for(request.endpoint)
    referrer = _same_origin_referrer_path()
    if referrer:
        return referrer
    return url_for("user_tasks")


def _same_origin_referrer_path() -> str:
    referrer = str(request.referrer or "").strip()
    if not referrer:
        return ""
    parsed = urlsplit(referrer)
    if parsed.netloc and parsed.netloc != request.host:
        return ""
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return _safe_next_path(path)


def _current_relative_url() -> str:
    path = request.full_path if request.query_string else request.path
    script_root = request.script_root.rstrip("/")
    return f"{script_root}{path}".rstrip("?") or url_for("user_tasks")


def _safe_next_path(value, fallback: str | None = None) -> str:
    fallback = fallback or url_for("user_tasks")
    value = str(value or "").strip()
    if not value:
        return fallback
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not value.startswith("/")
        or value.startswith("//")
    ):
        return fallback
    script_root = request.script_root.rstrip("/")
    if script_root and value != script_root and not value.startswith(f"{script_root}/"):
        return fallback
    return value


def _row_value(row, key: str, default=None):
    if row is None:
        return default
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key, default)
    return default
