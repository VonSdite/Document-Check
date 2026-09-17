import json
from pathlib import Path

from flask import current_app, flash, g, redirect
from markupsafe import Markup
from werkzeug.exceptions import RequestEntityTooLarge

from app.contracts.task_types import task_type_label
from app.identity.models import UserIdentity, ip_subject
from app.identity.service import (
    AuthenticationRequired,
    client_ip,
    current_identity,
    subject_label,
)
from app.reporting.constants import (
    REPORT_ACCEPTANCE_STATUSES,
    REPORT_ITEM_FIELDS,
    REPORT_ITEM_TYPE_LABEL,
    REPORT_REJECTION_REASON_HINTS,
    REPORT_REJECTION_REASONS,
)
from app.tasks.files import _task_source_files_available
from app.tasks.submission import _consistency_task_title
from app.web.auth import _identity_label, _owner_display, _owner_meta
from app.web.common import (
    _current_relative_url,
    _max_upload_mb,
    _request_entity_too_large_redirect,
)
from app.web.constants import STATUS_LABELS

FRONTEND_INJECTION_FILENAME = "frontend-injection.html"


def register_presentation_routes(app):
    app.add_template_global(STATUS_LABELS, "STATUS_LABELS")

    app.add_template_global(REPORT_ITEM_FIELDS, "REPORT_ITEM_FIELDS")

    app.add_template_global(REPORT_ITEM_TYPE_LABEL, "REPORT_ITEM_TYPE_LABEL")

    app.add_template_global(REPORT_ACCEPTANCE_STATUSES, "REPORT_ACCEPTANCE_STATUSES")

    app.add_template_global(REPORT_REJECTION_REASONS, "REPORT_REJECTION_REASONS")

    app.add_template_global(
        REPORT_REJECTION_REASON_HINTS, "REPORT_REJECTION_REASON_HINTS"
    )

    app.add_template_global(lambda: app.config["ADMIN_URL"], "admin_url")

    app.add_template_global(subject_label, "subject_label")

    app.add_template_global(_owner_display, "owner_display")

    app.add_template_global(_owner_meta, "owner_meta")

    app.add_template_global(_consistency_task_title, "consistency_task_title")

    app.add_template_global(_current_relative_url, "current_relative_url")

    app.add_template_global(_task_source_files_available, "task_source_files_available")

    @app.context_processor
    def inject_globals():
        try:
            identity = current_identity()
        except AuthenticationRequired:
            # console 页面未携带统一登录 Cookie 时回退到 IP 身份，保证页面可渲染。
            ip = client_ip()
            identity = UserIdentity(
                subject=ip_subject(ip), display_name="", source="ip", ip=ip
            )
        auth_config = current_app.config.get("AUTH", {})
        return {
            "auth_mode": auth_config.get("mode", "ip"),
            "status_labels": STATUS_LABELS,
            "nav_identity": _identity_label(identity),
            "nav_avatar": getattr(identity, "avatar", "") or "",
            "nav_subject": identity.subject,
            "nav_profile_version": identity.profile_version,
            "local_frontend_injection": _local_frontend_injection_html(),
            "task_type_label": task_type_label,
            "max_upload_mb": _max_upload_mb(),
        }

    @app.after_request
    def include_user_profile(response):
        identity = g.get("user_identity")
        if (
            identity is not None
            and identity.source == "cookie_session"
            and response.status_code < 400
        ):
            response.headers["X-User-Profile"] = json.dumps(
                {
                    "subject": identity.subject,
                    "label": identity.label,
                    "avatar": identity.avatar,
                    "version": str(identity.profile_version),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def request_entity_too_large(error):
        del error
        limit = _max_upload_mb()
        flash(
            f"上传文件过大，当前上传上限为 {limit}MB。请压缩视频，或在本地 config.yaml 中调整 server.max_upload_mb 后重启服务。",
            "error",
        )
        return redirect(_request_entity_too_large_redirect()), 303


def _local_frontend_injection_html() -> Markup:
    path = _local_frontend_injection_path()
    try:
        if not path.is_file():
            return Markup("")
        return Markup(path.read_text(encoding="utf-8"))
    except OSError as error:
        current_app.logger.warning("读取本地前端注入片段失败: %s", error)
        return Markup("")


def _local_frontend_injection_path() -> Path:
    root_dir = current_app.config.get("ROOT_DIR")
    if root_dir:
        return Path(root_dir) / "instance" / FRONTEND_INJECTION_FILENAME
    return Path(current_app.instance_path) / FRONTEND_INJECTION_FILENAME
