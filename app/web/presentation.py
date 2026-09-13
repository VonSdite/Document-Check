from flask import current_app, flash, redirect
from werkzeug.exceptions import RequestEntityTooLarge

from app.contracts.task_types import task_type_label
from app.identity.service import current_identity, subject_label
from app.reporting.constants import (
    REPORT_ACCEPTANCE_STATUSES,
    REPORT_ITEM_FIELDS,
    REPORT_ITEM_TYPE_LABEL,
    REPORT_REJECTION_REASON_HINTS,
    REPORT_REJECTION_REASONS,
)
from app.tasks.files import _task_source_files_available
from app.tasks.submission import _consistency_task_title
from app.web.auth import _identity_label, _owner_display, _owner_meta, _platform_enabled
from app.web.common import (
    _current_relative_url,
    _max_upload_mb,
    _request_entity_too_large_redirect,
)
from app.web.constants import STATUS_LABELS


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
        identity = current_identity()
        auth_config = current_app.config.get("AUTH", {})
        return {
            "platform_mode": _platform_enabled(),
            "auth_mode": auth_config.get("mode", "ip"),
            "status_labels": STATUS_LABELS,
            "nav_identity": _identity_label(identity),
            "task_type_label": task_type_label,
            "max_upload_mb": _max_upload_mb(),
        }

    @app.errorhandler(RequestEntityTooLarge)
    def request_entity_too_large(error):
        del error
        limit = _max_upload_mb()
        flash(
            f"上传文件过大，当前上传上限为 {limit}MB。请压缩视频，或在本地 config.yaml 中调整 server.max_upload_mb 后重启服务。",
            "error",
        )
        return redirect(_request_entity_too_large_redirect()), 303
