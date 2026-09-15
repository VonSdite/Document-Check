import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler
from flask import current_app, g, jsonify, request

from app.infrastructure.config import DEFAULT_CONSOLE_LOG_LEVEL
from app.infrastructure.logging import _ensure_console_handler
from app.persistence.connection import get_db

logger = logging.getLogger(__name__)
ACCESS_LOG_FORMAT = "%(asctime)s %(levelname)s [access] pid=%(process)d %(message)s"
REQUEST_ID_HEADER = "X-Request-ID"
ACCESS_LOG_MAX_BYTES = 10 * 1024 * 1024
ACCESS_LOG_BACKUP_COUNT = 4
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
_HEALTH_ENDPOINTS = {"health_live", "health_ready"}


def configure_access_logging(app) -> None:
    access_log_file = Path(app.config["ACCESS_LOG_FILE"])
    access_log_file.parent.mkdir(parents=True, exist_ok=True)
    logger_suffix = hashlib.sha256(
        str(access_log_file.resolve()).encode("utf-8")
    ).hexdigest()[:12]
    access_logger = logging.getLogger(f"document_check.access.{logger_suffix}")
    access_logger.setLevel(logging.INFO)

    if not _has_file_handler(access_logger, access_log_file):
        file_handler = ConcurrentRotatingFileHandler(
            access_log_file,
            maxBytes=ACCESS_LOG_MAX_BYTES,
            backupCount=ACCESS_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(ACCESS_LOG_FORMAT))
        access_logger.addHandler(file_handler)

    _ensure_console_handler(
        access_logger,
        level=app.config.get("CONSOLE_LOG_LEVEL", DEFAULT_CONSOLE_LOG_LEVEL),
        log_format=ACCESS_LOG_FORMAT,
    )
    access_logger.propagate = False
    app.extensions["access_logger"] = access_logger
    logger.info("访问日志已启用：%s", access_log_file)


def register_observability(app) -> None:
    @app.before_request
    def log_request_start():
        request_id = _request_id_from_header(request.headers.get(REQUEST_ID_HEADER))
        g.request_id = request_id
        g.request_started_at = time.perf_counter()
        g.access_log_enabled = request.endpoint not in _HEALTH_ENDPOINTS
        if not g.access_log_enabled:
            return None
        _log_access_event(
            "request_start",
            request_id=request_id,
            method=request.method,
            path=request.path,
            script_root=request.script_root,
            remote_addr=request.remote_addr or "",
            scheme=request.scheme,
            host=request.host,
            forwarded_for=_header_value("X-Forwarded-For"),
            forwarded_proto=_header_value("X-Forwarded-Proto"),
            forwarded_host=_header_value("X-Forwarded-Host"),
            forwarded_prefix=_header_value("X-Forwarded-Prefix"),
        )
        return None

    @app.after_request
    def log_request_end(response):
        request_id = getattr(g, "request_id", None) or uuid.uuid4().hex
        response.headers[REQUEST_ID_HEADER] = request_id
        if not getattr(g, "access_log_enabled", True):
            return response
        duration_ms = _request_duration_ms()
        level = logging.WARNING if response.status_code >= 500 else logging.INFO
        _log_access_event(
            "request_end",
            level=level,
            request_id=request_id,
            method=request.method,
            path=request.path,
            status=response.status_code,
            duration_ms=duration_ms,
            response_bytes=response.content_length,
        )
        return response

    @app.teardown_request
    def log_request_error(error):
        if error is None or not getattr(g, "access_log_enabled", True):
            return
        _log_access_event(
            "request_error",
            level=logging.ERROR,
            request_id=getattr(g, "request_id", "-"),
            method=request.method,
            path=request.path,
            duration_ms=_request_duration_ms(),
            error_type=type(error).__name__,
        )

    @app.get("/health/live")
    def health_live():
        return jsonify(status="ok")

    @app.get("/health/ready")
    def health_ready():
        checks = readiness_checks(current_app)
        ready = all(value == "ok" for value in checks.values())
        _log_readiness_transition(current_app, ready, checks)
        return (
            jsonify(status="ready" if ready else "not_ready", checks=checks),
            200 if ready else 503,
        )


def log_startup_self_check(app) -> None:
    with app.app_context():
        checks = readiness_checks(app)
    status = "ok" if all(value == "ok" for value in checks.values()) else "failed"
    app.extensions["readiness_status"] = status == "ok"
    logger.info(
        "启动自检 status=%s pid=%s python=%s host=%s port=%s "
        "url_prefix=%s proxy_fix=%s checks=%s",
        status,
        os.getpid(),
        sys.version.split()[0],
        app.config["LISTEN_HOST"],
        app.config["LISTEN_PORT"],
        app.config["APPLICATION_ROOT"],
        app.config["PROXY_FIX"],
        json.dumps(checks, ensure_ascii=False, separators=(",", ":")),
    )


def readiness_checks(app) -> dict[str, str]:
    checks = {
        "database": _database_status(),
        "uploads": _directory_status(app.config["UPLOAD_FOLDER"]),
        "images": _directory_status(app.config["IMAGE_FOLDER"]),
        "logs": _directory_status(Path(app.config["LOG_FILE"]).parent),
        "scheduler": _scheduler_status(app),
    }
    return checks


def _database_status() -> str:
    try:
        get_db().execute("SELECT 1").fetchone()
    except Exception:
        return "error"
    return "ok"


def _directory_status(value) -> str:
    path = Path(value)
    if not path.is_dir():
        return "error"
    if not os.access(path, os.R_OK | os.W_OK):
        return "error"
    return "ok"


def _scheduler_status(app) -> str:
    probe = app.extensions.get("task_supervisor_probe")
    if probe is None:
        from app.tasks.supervisor import supervisor_is_ready

        def probe():
            return supervisor_is_ready(app)

    try:
        return "ok" if probe() else "error"
    except Exception:
        return "error"


def _log_readiness_transition(app, ready: bool, checks: dict[str, str]) -> None:
    previous = app.extensions.get("readiness_status")
    app.extensions["readiness_status"] = ready
    if previous is ready:
        return
    level = logging.INFO if ready else logging.WARNING
    logger.log(
        level,
        "就绪状态变化 status=%s checks=%s",
        "ready" if ready else "not_ready",
        json.dumps(checks, ensure_ascii=False, separators=(",", ":")),
    )


def _request_id_from_header(value: str | None) -> str:
    candidate = str(value or "").strip()
    if _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid.uuid4().hex


def _header_value(name: str) -> str:
    return str(request.headers.get(name) or "")[:512]


def _request_duration_ms() -> float:
    started_at = getattr(g, "request_started_at", None)
    if started_at is None:
        return 0.0
    return round((time.perf_counter() - started_at) * 1000, 3)


def _log_access_event(event: str, *, level: int = logging.INFO, **fields) -> None:
    access_logger = current_app.extensions.get("access_logger", logger)
    payload = {"event": event, **fields}
    access_logger.log(
        level,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def _has_file_handler(target_logger, log_file: Path) -> bool:
    return any(
        isinstance(handler, ConcurrentRotatingFileHandler)
        and Path(handler.baseFilename) == log_file
        for handler in target_logger.handlers
    )
