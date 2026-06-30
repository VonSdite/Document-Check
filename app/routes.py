import hmac
import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    Response,
    send_file,
    session,
    url_for,
)
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from werkzeug.exceptions import RequestEntityTooLarge

from .auth import SAML_USER_SESSION_KEY, AuthenticationRequired, UserIdentity, current_identity, subject_label
from .config import save_network_config
from .db import (
    default_check_item_codes,
    get_bool_setting,
    get_db,
    get_ip_username,
    get_setting,
    now_text,
    owner_subject_from_ip,
    reset_default_check_item_prompt,
    set_ip_username,
    set_setting,
)
from .documents import DocumentReadError, allowed_file, extension_of, extract_text, format_document_text
from .file_cleanup import (
    describe_failures,
    remove_directory_tree,
    remove_empty_directory as cleanup_remove_empty_directory,
    remove_file,
)
from .images import (
    DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES,
    candidate_pdf_pages_for_image_check,
    default_image_folder,
    extract_images,
    format_image_document_text,
    image_items_from_meta,
    image_path_from_item,
    render_pdf_page_images,
)
from .llm import DEFAULT_ISSUE_OUTPUT_LIMIT, LLMError, test_model_connection
from .model_discovery import ModelDiscoveryError, fetch_models
from .network import outbound_network_config
from .saml import SamlConfigError, create_saml_auth, saml_sp_metadata
from .task_types import (
    CONSISTENCY_MAX_DATA_FILES,
    CONSISTENCY_MAX_MATERIAL_FILES,
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
    document_groups_from_meta,
    task_type_label,
)
from .videos import allowed_video_file, extract_video_frames, format_video_document_text, video_extension_of


STATUS_LABELS = {
    "queued": "排队中",
    "running": "检查中",
    "completed": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}
TASKS_PER_PAGE = 20
CHECK_ITEM_CONCURRENCY_DEFAULT = 1
REPORT_RETENTION_DAYS_DEFAULT = 0
ISSUE_OUTPUT_LIMIT_DEFAULT = DEFAULT_ISSUE_OUTPUT_LIMIT
PROVIDER_TIMEOUT_DEFAULT = 3600
PROVIDER_TIMEOUT_MIN = 30
PROVIDER_TIMEOUT_MAX = 7200
MODEL_TEST_TIMEOUT_MAX = 60
PROVIDER_INPUT_LIMIT_DEFAULT = 80000
PROVIDER_INPUT_LIMIT_MIN = 5000
PROVIDER_INPUT_LIMIT_MAX = 1000000
CONSOLE_USER_ENDPOINTS = {
    "admin_tasks",
    "admin_new_task",
    "admin_consistency",
    "admin_language_consistency",
    "admin_images",
    "admin_videos",
    "admin_models",
}
INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f/\\<>:"|?*]+')
REPORT_ITEM_TYPES = {
    "issue": "问题",
    "suggestion": "建议",
    "non_issue": "非问题",
}
REPORT_ITEM_TYPE_ORDER = ("issue", "suggestion", "non_issue")
REPORT_ACCEPTANCE_STATUSES = {
    "pending": "未确认",
    "accepted": "接纳",
    "rejected": "不接纳",
}
REPORT_REJECTION_REASONS = {
    "model_hallucination": "模型幻觉",
    "false_positive": "模型误报",
    "evidence_insufficient": "证据不足",
    "not_applicable": "不适用",
    "other": "其他",
}
REPORT_ITEM_FIELDS = (
    ("category", "问题类型"),
    ("location", "位置"),
    ("excerpt", "原文/证据"),
    ("description", "问题描述"),
    ("impact", "影响"),
    ("suggestion", "修改建议"),
)
REPORT_EXPORT_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
REPORT_EXPORT_HEADER_FILL = PatternFill("solid", fgColor="EAF0F8")
REPORT_EXPORT_HEADER_FONT = Font(bold=True)
REPORT_TOTAL_EXPORT_ROWS = (
    ("问题", "issue"),
    ("建议", "suggestion"),
    ("非问题", "non_issue"),
    ("接纳问题", "accepted_issue"),
    ("不接纳问题", "rejected_issue"),
    ("待确认问题", "pending_issue_acceptance"),
    ("问题检出率", "issue_detection_rate"),
    ("问题接纳率", "issue_acceptance_rate"),
    ("合计", "total"),
)
REPORT_ITEM_TYPE_LABEL = "条目判定"
REPORT_ITEM_START_RE = re.compile(
    r"^(?:(?:问题|建议|风险|疑点|不一致|偏差|错误|缺失)\s*\d*[:：]|"
    r"(?:\d{1,3}[.、)]|\(\d{1,3}\)|（\d{1,3}）)\s*(?:[*_`~]{1,3}\s*)?\S)"
)
REPORT_ITEM_PREFIX_RE = re.compile(
    r"^\s*(?:(?:[-+*]\s+)|(?:#{1,6}\s+)|(?:[*_`~]{1,3}\s*))*"
)
REPORT_JSON_ITEM_KEYS = ("items", "issues", "report_items", "findings", "problems")
REPORT_JSON_SUMMARY_KEYS = ("summary", "overall", "conclusion", "总体结论", "总结")
REPORT_STATUS_KEYS = ("status", "状态", "classification", "item_type", "结论类型", "问题状态", "type")
REPORT_FIELD_ALIASES = {
    "category": ("category", "issue_type", "problem_type", "type", "问题类型", "类型", "检查类型"),
    "location": ("location", "position", "where", "位置", "位置线索", "页码", "章节", "图片位置"),
    "excerpt": (
        "excerpt",
        "quote",
        "original",
        "evidence",
        "document_a_evidence",
        "document_b_evidence",
        "原文摘录",
        "原文",
        "证据",
        "文档A证据",
        "文档B证据",
        "文档线索",
        "图片可见证据",
        "可见线索",
    ),
    "description": ("description", "issue", "problem", "finding", "疑似问题", "问题描述", "偏差说明", "差异说明", "冲突或缺失说明", "问题判断"),
    "impact": ("impact", "risk", "影响", "影响说明", "客户影响", "可能影响"),
    "suggestion": ("suggestion", "recommendation", "fix", "修改建议", "建议修改", "建议处理方式", "需核对的依据"),
}
REPORT_NO_ACTION_IMPACT_MARKERS = (
    "无实质影响",
    "无实际影响",
    "没有实质影响",
    "没有实际影响",
    "无明显影响",
    "不造成实质影响",
    "影响不大",
    "影响较小",
    "影响很小",
    "无影响",
    "不影响理解",
    "不影响使用",
    "nosubstantiveimpact",
    "nomaterialimpact",
    "nosignificantimpact",
    "norealimpact",
    "noactualimpact",
    "doesnotaffect",
)
REPORT_NO_ACTION_SUGGESTION_MARKERS = (
    "无需修改",
    "无须修改",
    "不需修改",
    "不需要修改",
    "无需处理",
    "无须处理",
    "不需处理",
    "不需要处理",
    "无需调整",
    "无须调整",
    "不需调整",
    "无需修正",
    "保持不变",
    "nomodificationrequired",
    "nomodificationneeded",
    "noneedtomodify",
    "noneedtochange",
    "nochangeneeded",
    "noactionrequired",
    "noactionneeded",
)
REPORT_LEGACY_LABEL_FIELDS = {
    "问题类型": "category",
    "类型": "category",
    "对象类型": "category",
    "位置": "location",
    "位置线索": "location",
    "图片名称或位置": "location",
    "图片位置": "location",
    "文档线索": "location",
    "原文": "excerpt",
    "原文摘录": "excerpt",
    "文档A证据": "excerpt",
    "文档B证据": "excerpt",
    "资料表述": "excerpt",
    "冲突表述": "excerpt",
    "图片可见内容": "excerpt",
    "图片可见证据": "excerpt",
    "可见内容线索": "excerpt",
    "可见线索": "excerpt",
    "识别到的文字": "excerpt",
    "问题": "description",
    "问题描述": "description",
    "疑似问题": "description",
    "偏差说明": "description",
    "差异说明": "description",
    "冲突或缺失说明": "description",
    "问题判断": "description",
    "不匹配原因": "description",
    "理由": "description",
    "影响": "impact",
    "影响说明": "impact",
    "客户影响": "impact",
    "可能影响": "impact",
    "建议": "suggestion",
    "修改建议": "suggestion",
    "建议修改": "suggestion",
    "建议处理方式": "suggestion",
    "建议补充的标题形式": "suggestion",
}
LANGUAGE_STATIC_TOKEN_RE = re.compile(
    r"https?://[^\s<>\]\)\"']+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b"
    r"|\bv?\d+(?:\.\d+){1,4}\b"
    r"|\b\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?\b"
    r"|\b\d+(?:[.,]\d+)*(?:\s?(?:%|ms|s|m|mm|cm|km|kg|g|KB|MB|GB|TB|V|A|W|Hz|kHz|MHz|GHz|°C|℃))?\b",
    re.IGNORECASE,
)
LANGUAGE_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|第[一二三四五六七八九十百千万\d]+[章节篇部]\s*|"
    r"(?:\d+|[A-Z])(?:[.\-、)]\d*){0,5}[.\-、)]?\s+|[一二三四五六七八九十]+[、.]\s*)\S"
)


def register_routes(app):
    app.add_template_global(STATUS_LABELS, "STATUS_LABELS")
    app.add_template_global(REPORT_ITEM_FIELDS, "REPORT_ITEM_FIELDS")
    app.add_template_global(REPORT_ITEM_TYPE_LABEL, "REPORT_ITEM_TYPE_LABEL")
    app.add_template_global(REPORT_ACCEPTANCE_STATUSES, "REPORT_ACCEPTANCE_STATUSES")
    app.add_template_global(REPORT_REJECTION_REASONS, "REPORT_REJECTION_REASONS")
    app.add_template_global(lambda: app.config["ADMIN_URL"], "admin_url")
    app.add_template_global(subject_label, "subject_label")
    app.add_template_global(_owner_display, "owner_display")
    app.add_template_global(_owner_meta, "owner_meta")

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

    @app.before_request
    def require_saml_user_session():
        if not _platform_enabled() or not _saml_mode_enabled() or not _needs_saml_user_session(request.endpoint):
            return None
        if _has_saml_user_session():
            return None
        return redirect(url_for("saml_login", next=_current_relative_url()))

    @app.get("/auth/saml/login")
    def saml_login():
        if not _saml_mode_enabled():
            abort(404)
        try:
            auth = create_saml_auth()
            redirect_url = auth.login(return_to=_safe_next_path(request.args.get("next")))
        except SamlConfigError as error:
            abort(503, description=str(error))
        except Exception:
            current_app.logger.exception("生成 SAML 登录请求失败")
            abort(503, description="SAML 登录配置无效，请联系管理员。")
        session["saml_request_id"] = auth.get_last_request_id()
        return redirect(redirect_url)

    @app.post("/auth/saml/acs")
    def saml_acs():
        if not _saml_mode_enabled():
            abort(404)
        try:
            auth = create_saml_auth()
            request_id = session.pop("saml_request_id", None)
            auth.process_response(request_id=request_id)
        except SamlConfigError as error:
            abort(503, description=str(error))
        except Exception:
            current_app.logger.exception("处理 SAML 回调失败")
            abort(401, description="SAML 登录失败，请重新从公司统一入口访问。")

        if auth.get_errors() or not auth.is_authenticated():
            current_app.logger.warning("SAML 回调校验失败：%s", ", ".join(auth.get_errors()))
            abort(401, description="SAML 登录失败，请重新从公司统一入口访问。")

        user_id, username = _saml_user_from_response(auth)
        if not user_id:
            abort(401, description="SAML 响应缺少用户 ID，请联系管理员检查 SSO 属性映射。")
        session[SAML_USER_SESSION_KEY] = {"user_id": user_id, "username": username or user_id}
        return redirect(_safe_next_path(request.form.get("RelayState")))

    @app.get("/auth/saml/metadata")
    def saml_metadata():
        if not _saml_mode_enabled():
            abort(404)
        try:
            metadata = saml_sp_metadata()
        except SamlConfigError as error:
            abort(503, description=str(error))
        except Exception:
            current_app.logger.exception("生成 SAML metadata 失败")
            abort(503, description="SAML SP metadata 配置无效，请联系管理员。")
        return Response(metadata, mimetype="application/samlmetadata+xml")

    @app.post("/auth/saml/logout")
    def saml_logout():
        if not _saml_mode_enabled():
            abort(404)
        session.pop(SAML_USER_SESSION_KEY, None)
        session.pop("saml_request_id", None)
        return redirect(url_for("user_tasks"))

    @app.route("/", methods=["GET", "POST"])
    def user_tasks():
        if not _platform_enabled():
            if request.method == "POST":
                return create_task_for_identity(current_identity(), admin_created=True)
            return _render_admin_tasks_page()

        identity = _current_user_identity()
        if request.method == "POST":
            return create_task_for_identity(identity, admin_created=False)
        page = _page_arg()
        total = get_db().execute(
            "SELECT COUNT(*) AS total FROM tasks WHERE COALESCE(owner_subject, 'ip:' || ip) = ? AND task_type = ?",
            (identity.subject, DOCUMENT_TASK_TYPE),
        ).fetchone()["total"]
        page = _bounded_page(page, total, TASKS_PER_PAGE)
        rows = get_db().execute(
            """
            SELECT t.*,
                   COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_owner_name,
                   COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_username,
                   COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
            FROM tasks t
            WHERE COALESCE(t.owner_subject, 'ip:' || t.ip) = ? AND t.task_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (identity.subject, DOCUMENT_TASK_TYPE, TASKS_PER_PAGE, (page - 1) * TASKS_PER_PAGE),
        ).fetchall()
        stats = _task_stats_for_where("COALESCE(owner_subject, 'ip:' || ip) = ? AND task_type = ?", (identity.subject, DOCUMENT_TASK_TYPE))
        return render_template(
            "user_tasks.html",
            ip=identity.ip,
            identity=identity,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, TASKS_PER_PAGE),
            check_items=get_enabled_check_items(),
            models=get_enabled_models(identity.subject),
            active_nav=DOCUMENT_TASK_TYPE,
        )

    @app.route("/tasks/new", methods=["GET", "POST"])
    def user_new_task():
        if not _platform_enabled():
            if request.method == "POST":
                return create_task_for_identity(current_identity(), admin_created=True)
            return redirect(url_for("user_tasks"))

        identity = _current_user_identity()
        if request.method == "POST":
            return create_task_for_identity(identity, admin_created=False)
        return redirect(url_for("user_tasks"))

    @app.route("/consistency", methods=["GET", "POST"])
    def user_consistency():
        if not _platform_enabled():
            if request.method == "POST":
                return create_consistency_task_for_identity(current_identity(), admin_created=True)
            return _render_admin_consistency_page()

        identity = _current_user_identity()
        if request.method == "POST":
            return create_consistency_task_for_identity(identity, admin_created=False)

        page = _page_arg()
        total = get_db().execute(
            "SELECT COUNT(*) AS total FROM tasks WHERE COALESCE(owner_subject, 'ip:' || ip) = ? AND task_type = ?",
            (identity.subject, CONSISTENCY_TASK_TYPE),
        ).fetchone()["total"]
        page = _bounded_page(page, total, TASKS_PER_PAGE)
        rows = get_db().execute(
            """
            SELECT t.*,
                   COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_owner_name,
                   COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_username,
                   COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
            FROM tasks t
            WHERE COALESCE(t.owner_subject, 'ip:' || t.ip) = ? AND t.task_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (identity.subject, CONSISTENCY_TASK_TYPE, TASKS_PER_PAGE, (page - 1) * TASKS_PER_PAGE),
        ).fetchall()
        stats = _task_stats_for_where("COALESCE(owner_subject, 'ip:' || ip) = ? AND task_type = ?", (identity.subject, CONSISTENCY_TASK_TYPE))
        return render_template(
            "user_consistency.html",
            ip=identity.ip,
            identity=identity,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, TASKS_PER_PAGE),
            check_items=get_enabled_check_items(CONSISTENCY_TASK_TYPE),
            models=get_enabled_models(identity.subject),
            active_nav=CONSISTENCY_TASK_TYPE,
        )

    @app.route("/language-consistency", methods=["GET", "POST"])
    def user_language_consistency():
        if not _platform_enabled():
            if request.method == "POST":
                return create_language_consistency_task_for_identity(current_identity(), admin_created=True)
            return _render_admin_language_consistency_page()

        identity = _current_user_identity()
        if request.method == "POST":
            return create_language_consistency_task_for_identity(identity, admin_created=False)

        page = _page_arg()
        total = get_db().execute(
            "SELECT COUNT(*) AS total FROM tasks WHERE COALESCE(owner_subject, 'ip:' || ip) = ? AND task_type = ?",
            (identity.subject, LANGUAGE_CONSISTENCY_TASK_TYPE),
        ).fetchone()["total"]
        page = _bounded_page(page, total, TASKS_PER_PAGE)
        rows = get_db().execute(
            """
            SELECT t.*,
                   COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_owner_name,
                   COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_username,
                   COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
            FROM tasks t
            WHERE COALESCE(t.owner_subject, 'ip:' || t.ip) = ? AND t.task_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (identity.subject, LANGUAGE_CONSISTENCY_TASK_TYPE, TASKS_PER_PAGE, (page - 1) * TASKS_PER_PAGE),
        ).fetchall()
        stats = _task_stats_for_where(
            "COALESCE(owner_subject, 'ip:' || ip) = ? AND task_type = ?",
            (identity.subject, LANGUAGE_CONSISTENCY_TASK_TYPE),
        )
        return render_template(
            "user_language_consistency.html",
            ip=identity.ip,
            identity=identity,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, TASKS_PER_PAGE),
            check_items=get_enabled_check_items(LANGUAGE_CONSISTENCY_TASK_TYPE),
            models=get_enabled_models(identity.subject),
            active_nav=LANGUAGE_CONSISTENCY_TASK_TYPE,
        )

    @app.route("/images", methods=["GET", "POST"])
    def user_images():
        if not _platform_enabled():
            if request.method == "POST":
                return create_image_task_for_identity(current_identity(), admin_created=True)
            return _render_admin_images_page()

        identity = _current_user_identity()
        if request.method == "POST":
            return create_image_task_for_identity(identity, admin_created=False)

        page = _page_arg()
        total = get_db().execute(
            "SELECT COUNT(*) AS total FROM tasks WHERE COALESCE(owner_subject, 'ip:' || ip) = ? AND task_type = ?",
            (identity.subject, IMAGE_TASK_TYPE),
        ).fetchone()["total"]
        page = _bounded_page(page, total, TASKS_PER_PAGE)
        rows = get_db().execute(
            """
            SELECT t.*,
                   COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_owner_name,
                   COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_username,
                   COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
            FROM tasks t
            WHERE COALESCE(t.owner_subject, 'ip:' || t.ip) = ? AND t.task_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (identity.subject, IMAGE_TASK_TYPE, TASKS_PER_PAGE, (page - 1) * TASKS_PER_PAGE),
        ).fetchall()
        stats = _task_stats_for_where("COALESCE(owner_subject, 'ip:' || ip) = ? AND task_type = ?", (identity.subject, IMAGE_TASK_TYPE))
        return render_template(
            "user_images.html",
            ip=identity.ip,
            identity=identity,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, TASKS_PER_PAGE),
            check_items=get_enabled_check_items(IMAGE_TASK_TYPE),
            models=get_enabled_models(identity.subject),
            active_nav=IMAGE_TASK_TYPE,
        )

    @app.route("/videos", methods=["GET", "POST"])
    def user_videos():
        if not _platform_enabled():
            if request.method == "POST":
                return create_video_task_for_identity(current_identity(), admin_created=True)
            return _render_admin_videos_page()

        identity = _current_user_identity()
        if request.method == "POST":
            return create_video_task_for_identity(identity, admin_created=False)

        page = _page_arg()
        total = get_db().execute(
            "SELECT COUNT(*) AS total FROM tasks WHERE COALESCE(owner_subject, 'ip:' || ip) = ? AND task_type = ?",
            (identity.subject, VIDEO_TASK_TYPE),
        ).fetchone()["total"]
        page = _bounded_page(page, total, TASKS_PER_PAGE)
        rows = get_db().execute(
            """
            SELECT t.*,
                   COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_owner_name,
                   COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_username,
                   COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
            FROM tasks t
            WHERE COALESCE(t.owner_subject, 'ip:' || t.ip) = ? AND t.task_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (identity.subject, VIDEO_TASK_TYPE, TASKS_PER_PAGE, (page - 1) * TASKS_PER_PAGE),
        ).fetchall()
        stats = _task_stats_for_where("COALESCE(owner_subject, 'ip:' || ip) = ? AND task_type = ?", (identity.subject, VIDEO_TASK_TYPE))
        return render_template(
            "user_videos.html",
            ip=identity.ip,
            identity=identity,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, TASKS_PER_PAGE),
            check_items=get_enabled_check_items(VIDEO_TASK_TYPE),
            models=get_enabled_models(identity.subject),
            active_nav=VIDEO_TASK_TYPE,
        )

    @app.get("/tasks/<int:task_id>")
    def user_task_detail(task_id):
        task = _get_user_task_or_local_admin(task_id)
        results = _task_results(task)
        return render_template(
            "task_detail.html",
            mode="admin" if not _platform_enabled() else "user",
            task=task,
            results=results,
            report_totals=_report_item_totals(results),
            report_item_types=REPORT_ITEM_TYPES,
            report_classification_url=url_for("admin_update_report_item_type" if not _platform_enabled() else "user_update_report_item_type", task_id=task_id),
            document_groups=_task_document_groups(task),
            active_nav=task["task_type"] or DOCUMENT_TASK_TYPE,
            back_endpoint=_task_list_endpoint(not _platform_enabled(), task["task_type"]),
        )

    @app.post("/tasks/<int:task_id>/report-items")
    def user_update_report_item_type(task_id):
        task = _get_user_task_or_local_admin(task_id)
        return _update_report_item_type(task)

    @app.get("/tasks/<int:task_id>/export")
    def user_export_task(task_id):
        task = _get_user_task_or_local_admin(task_id)
        return _export_task_report(task)

    @app.get("/tasks/<int:task_id>/export.xlsx")
    def user_export_task_excel(task_id):
        task = _get_user_task_or_local_admin(task_id)
        return _export_task_report_excel(task)

    @app.get("/tasks/<int:task_id>/document")
    def user_download_task_document(task_id):
        task = _get_user_task_or_local_admin(task_id)
        return _download_task_document(task, "user_task_detail")

    @app.post("/tasks/<int:task_id>/cancel")
    def user_cancel_task(task_id):
        task = _get_user_task_or_local_admin(task_id)
        _cancel_task(task)
        flash("已提交取消请求。", "success")
        return redirect(_task_action_redirect("user_tasks"))

    @app.post("/tasks/<int:task_id>/delete")
    def user_delete_task(task_id):
        task = _get_user_task_or_local_admin(task_id)
        if _delete_task(task):
            flash("任务已删除。", "success")
        return redirect(url_for(_task_list_endpoint(False, task["task_type"])))

    @app.route("/models", methods=["GET", "POST"])
    def user_models():
        return _model_management_response(_model_page_identity(), "user_models")

    @app.get("/models/fetch")
    def user_fetch_models():
        _model_page_identity()
        provider_data = _provider_query_data()
        if isinstance(provider_data, str):
            return {"error": provider_data}, 400
        network = outbound_network_config()
        try:
            models = fetch_models(
                api_base=provider_data["api_base"],
                api_key=provider_data["api_key"],
                proxy_mode=network["proxy_mode"],
                proxy=network["proxy"],
                ssl_verify=network["ssl_verify"],
                request_timeout=provider_data["request_timeout"],
            )
        except ModelDiscoveryError as exc:
            return {"error": str(exc)}, 400
        return {"fetched_models": models, "fetched_count": len(models)}

    @app.post("/models/test")
    def user_test_model():
        _model_page_identity()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return {"ok": False, "error": "请求数据格式不正确。"}, 400
        provider_data = _provider_payload_data(data)
        if isinstance(provider_data, str):
            return {"ok": False, "error": provider_data}, 400
        model_name = str(data.get("model_name") or "").strip()
        if not model_name:
            return {"ok": False, "error": "请先填写模型 ID。"}, 400
        network = outbound_network_config()
        try:
            message = test_model_connection(
                api_base=provider_data["api_base"],
                api_key=provider_data["api_key"],
                proxy_mode=network["proxy_mode"],
                proxy=network["proxy"],
                ssl_verify=network["ssl_verify"],
                request_timeout=min(provider_data["request_timeout"], MODEL_TEST_TIMEOUT_MAX),
                model_name=model_name,
                force_disable_thinking=_form_bool(data.get("force_disable_thinking")),
            )
        except LLMError as exc:
            return {"ok": False, "error": str(exc)}, 400
        return {"ok": True, "message": message}

    admin_prefix = app.config["ADMIN_URL"]

    @app.route(f"{admin_prefix}/login", methods=["GET", "POST"])
    def admin_login():
        if not _platform_enabled():
            return redirect(url_for("user_tasks"))
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            ok = hmac.compare_digest(username, current_app.config["ADMIN_USERNAME"]) and hmac.compare_digest(
                password, current_app.config["ADMIN_PASSWORD"]
            )
            if ok:
                session["admin_logged_in"] = True
                flash("管理员已登录。", "success")
                return redirect(url_for("admin_dashboard"))
            flash("账号或密码不正确。", "error")
        return render_template("admin_login.html")

    @app.post(f"{admin_prefix}/logout")
    def admin_logout():
        session.pop("admin_logged_in", None)
        if not _platform_enabled():
            return redirect(url_for("user_tasks"))
        flash("管理员已退出。", "success")
        return redirect(url_for("admin_login"))

    @app.get(admin_prefix)
    @admin_required
    def admin_dashboard():
        if not _platform_enabled():
            return redirect(url_for("user_tasks"))
        selected_range = _admin_overview_range()
        overview = _admin_overview_data(selected_range["start_at"], selected_range["end_at"])
        return render_template(
            "admin_overview.html",
            selected_range=selected_range,
            totals=overview["totals"],
            daily_rows=overview["daily_rows"],
            user_rows=overview["user_rows"],
            active_nav="overview",
        )

    @app.route(f"{admin_prefix}/tasks", methods=["GET", "POST"])
    @admin_required
    def admin_tasks():
        if request.method == "POST":
            return create_task_for_identity(_console_user_identity(), admin_created=True)
        return _render_admin_tasks_page()

    @app.route(f"{admin_prefix}/tasks/new", methods=["GET", "POST"])
    @admin_required
    def admin_new_task():
        if request.method == "POST":
            return create_task_for_identity(_console_user_identity(), admin_created=True)
        return redirect(url_for("admin_tasks"))

    @app.route(f"{admin_prefix}/consistency", methods=["GET", "POST"])
    @admin_required
    def admin_consistency():
        if request.method == "POST":
            return create_consistency_task_for_identity(_console_user_identity(), admin_created=True)
        return _render_admin_consistency_page()

    @app.route(f"{admin_prefix}/language-consistency", methods=["GET", "POST"])
    @admin_required
    def admin_language_consistency():
        if request.method == "POST":
            return create_language_consistency_task_for_identity(_console_user_identity(), admin_created=True)
        return _render_admin_language_consistency_page()

    @app.route(f"{admin_prefix}/images", methods=["GET", "POST"])
    @admin_required
    def admin_images():
        if request.method == "POST":
            return create_image_task_for_identity(_console_user_identity(), admin_created=True)
        return _render_admin_images_page()

    @app.route(f"{admin_prefix}/videos", methods=["GET", "POST"])
    @admin_required
    def admin_videos():
        if request.method == "POST":
            return create_video_task_for_identity(_console_user_identity(), admin_created=True)
        return _render_admin_videos_page()

    @app.route(f"{admin_prefix}/models", methods=["GET", "POST"])
    @admin_required
    def admin_models():
        if not _platform_enabled():
            return redirect(url_for("user_models"))
        return _model_management_response(_console_user_identity(), "admin_models")

    @app.get(f"{admin_prefix}/tasks/<int:task_id>")
    @admin_required
    def admin_task_detail(task_id):
        task = _get_task_or_404(task_id)
        results = _task_results(task)
        return render_template(
            "task_detail.html",
            mode="admin",
            task=task,
            results=results,
            report_totals=_report_item_totals(results),
            report_item_types=REPORT_ITEM_TYPES,
            report_classification_url=url_for("admin_update_report_item_type", task_id=task_id),
            document_groups=_task_document_groups(task),
            active_nav=task["task_type"] or DOCUMENT_TASK_TYPE,
            back_endpoint=_task_list_endpoint(True, task["task_type"]),
        )

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/report-items")
    @admin_required
    def admin_update_report_item_type(task_id):
        task = _get_task_or_404(task_id)
        return _update_report_item_type(task)

    @app.get(f"{admin_prefix}/tasks/<int:task_id>/export")
    @admin_required
    def admin_export_task(task_id):
        task = _get_task_or_404(task_id)
        return _export_task_report(task)

    @app.get(f"{admin_prefix}/tasks/<int:task_id>/export.xlsx")
    @admin_required
    def admin_export_task_excel(task_id):
        task = _get_task_or_404(task_id)
        return _export_task_report_excel(task)

    @app.get(f"{admin_prefix}/tasks/<int:task_id>/document")
    @admin_required
    def admin_download_task_document(task_id):
        task = _get_task_or_404(task_id)
        return _download_task_document(task, "admin_task_detail")

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/cancel")
    @admin_required
    def admin_cancel_task(task_id):
        task = _get_task_or_404(task_id)
        _cancel_task(task)
        flash("已提交取消请求。", "success")
        return redirect(_task_action_redirect("admin_tasks"))

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/delete")
    @admin_required
    def admin_delete_task(task_id):
        task = _get_task_or_404(task_id)
        if _delete_task(task):
            flash("任务已删除。", "success")
        return redirect(url_for(_task_list_endpoint(True, task["task_type"])))

    @app.route(f"{admin_prefix}/prompts", methods=["GET", "POST"])
    @admin_required
    def admin_prompts():
        return redirect(url_for("admin_settings"))

    @app.route(f"{admin_prefix}/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings():
        db = get_db()
        if request.method == "POST":
            action = request.form.get("action", "concurrency")
            if action == "concurrency":
                try:
                    global_concurrency = max(1, int(request.form.get("global_concurrency", "3")))
                    user_concurrency = max(1, int(request.form.get("user_concurrency", "1")))
                    check_item_concurrency = max(
                        1,
                        int(request.form.get("check_item_concurrency", str(CHECK_ITEM_CONCURRENCY_DEFAULT))),
                    )
                    image_page_check_max_pages = max(
                        1,
                        int(request.form.get("image_page_check_max_pages", str(DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES))),
                    )
                    issue_output_limit = max(
                        0,
                        int(request.form.get("issue_output_limit", str(ISSUE_OUTPUT_LIMIT_DEFAULT))),
                    )
                    report_retention_days = max(
                        0,
                        int(request.form.get("report_retention_days", str(REPORT_RETENTION_DAYS_DEFAULT))),
                    )
                except ValueError:
                    flash("任务设置必须是整数，报告保留天数和问题条数上限可为 0，其余必须为正整数。", "error")
                    return redirect(url_for("admin_settings"))
                set_setting("global_concurrency", global_concurrency)
                set_setting("user_concurrency", user_concurrency)
                set_setting("check_item_concurrency", check_item_concurrency)
                set_setting("image_page_check_max_pages", image_page_check_max_pages)
                set_setting("issue_output_limit", issue_output_limit)
                set_setting("report_retention_days", report_retention_days)
                flash("任务设置已保存。", "success")
                return redirect(url_for("admin_settings"))

            if action == "diagnostics":
                llm_stream_trace_enabled = request.form.get("llm_stream_trace_enabled") == "on"
                set_setting("llm_stream_trace_enabled", llm_stream_trace_enabled)
                if _wants_json_response():
                    return {"llm_stream_trace_enabled": llm_stream_trace_enabled}
                flash("定位日志设置已保存。", "success")
                return redirect(url_for("admin_settings"))

            if action == "network":
                proxy_mode = request.form.get("proxy_mode", "direct")
                proxy = request.form.get("proxy", "")
                if proxy_mode == "custom" and not str(proxy or "").strip():
                    flash("自定义代理模式需要填写代理地址。", "error")
                    return redirect(url_for("admin_settings"))
                network = save_network_config(
                    current_app.config["ROOT_DIR"],
                    {
                        "proxy_mode": proxy_mode,
                        "proxy": proxy,
                        "ssl_verify": request.form.get("ssl_verify") == "on",
                    },
                )
                current_app.config["NETWORK"] = network
                flash("系统出站网络配置已保存。", "success")
                return redirect(url_for("admin_settings"))

            if action == "ip_username":
                if not _ip_username_management_enabled():
                    abort(404)
                ip = request.form.get("ip", "").strip()
                username = request.form.get("username", "").strip()
                if not _valid_ip(ip):
                    if _wants_json_response():
                        return {"ok": False, "error": "请输入有效的 IP 地址。"}, 400
                    flash("请输入有效的 IP 地址。", "error")
                    return redirect(url_for("admin_settings", tab="ip_users"))
                set_ip_username(ip, username)
                if _wants_json_response():
                    return {"ok": True, "ip": ip, "username": username}
                flash("IP 用户名已保存。" if username else "IP 用户名已清除。", "success")
                return redirect(url_for("admin_settings", tab="ip_users"))

            if action == "create_check_item":
                task_type = _check_item_task_type(request.form.get("task_type"))
                name = request.form.get("name", "").strip()
                description = request.form.get("description", "").strip()
                prompt = request.form.get("prompt", "").strip()
                enabled = 1 if request.form.get("enabled") == "on" else 0
                if not name or not prompt:
                    flash("检查项名称和提示词不能为空。", "error")
                    return redirect(url_for("admin_settings"))
                now = now_text()
                db.execute(
                    """
                    INSERT INTO check_items(task_type, code, name, description, prompt, enabled, sort_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_type,
                        f"{_check_item_code_prefix(task_type)}-{uuid.uuid4().hex}",
                        name,
                        description,
                        prompt,
                        enabled,
                        _next_check_item_sort_order(db, task_type),
                        now,
                        now,
                    ),
                )
                db.commit()
                flash("扩展检查项已创建。", "success")
                return redirect(url_for("admin_settings"))

            if action == "reorder_check_items":
                task_type = _check_item_task_type(request.form.get("task_type"))
                item_ids = [int(value) for value in request.form.getlist("item_ids") if value.isdigit()]
                if not item_ids:
                    if request.headers.get("X-Requested-With") == "fetch":
                        return Response("检查项顺序不能为空。", status=400)
                    flash("检查项顺序不能为空。", "error")
                    return redirect(url_for("admin_settings"))
                _reorder_check_items(db, item_ids, task_type)
                db.commit()
                if request.headers.get("X-Requested-With") == "fetch":
                    return Response(status=204)
                flash("检查项顺序已保存。", "success")
                return redirect(url_for("admin_settings"))

            if action == "delete_check_item":
                item_id = request.form.get("item_id")
                if not item_id or not item_id.isdigit():
                    flash("检查项不存在，无法删除。", "error")
                    return redirect(url_for("admin_settings"))
                item = db.execute("SELECT code FROM check_items WHERE id = ?", (item_id,)).fetchone()
                if item is None:
                    flash("检查项不存在，无法删除。", "error")
                    return redirect(url_for("admin_settings"))
                if item["code"] in default_check_item_codes():
                    flash("内置检查项不能删除。", "error")
                    return redirect(url_for("admin_settings"))
                db.execute("DELETE FROM check_items WHERE id = ?", (item_id,))
                db.commit()
                flash("扩展检查项已删除。", "success")
                return redirect(url_for("admin_settings"))

            if action == "prompt" and request.form.get("reset_prompt") == "1":
                item_id = request.form.get("item_id")
                if not item_id or not item_id.isdigit():
                    flash("检查项不存在，无法重置。", "error")
                    return redirect(url_for("admin_settings"))
                if not reset_default_check_item_prompt(int(item_id)):
                    flash("该检查项没有默认提示词可重置。", "error")
                    return redirect(url_for("admin_settings"))
                flash("检查项提示词已重置为默认内容。", "success")
                return redirect(url_for("admin_settings"))

            if action != "prompt":
                flash("未知设置操作。", "error")
                return redirect(url_for("admin_settings"))

            item_id = request.form.get("item_id")
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            prompt = request.form.get("prompt", "").strip()
            enabled = 1 if request.form.get("enabled") == "on" else 0
            if not item_id or not item_id.isdigit() or not name or not prompt:
                flash("检查项名称和提示词不能为空。", "error")
                return redirect(url_for("admin_settings"))
            if db.execute("SELECT 1 FROM check_items WHERE id = ?", (item_id,)).fetchone() is None:
                flash("检查项不存在，无法保存。", "error")
                return redirect(url_for("admin_settings"))
            db.execute(
                """
                UPDATE check_items
                SET name = ?, description = ?, prompt = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, description, prompt, enabled, now_text(), item_id),
            )
            db.commit()
            flash("检查项提示词已保存。", "success")
            return redirect(url_for("admin_settings"))

        document_check_items = _check_items_for_task_type(db, DOCUMENT_TASK_TYPE)
        consistency_check_items = _check_items_for_task_type(db, CONSISTENCY_TASK_TYPE)
        language_consistency_check_items = _check_items_for_task_type(db, LANGUAGE_CONSISTENCY_TASK_TYPE)
        image_check_items = _check_items_for_task_type(db, IMAGE_TASK_TYPE)
        video_check_items = _check_items_for_task_type(db, VIDEO_TASK_TYPE)
        settings_tab = _settings_tab()
        return render_template(
            "admin_settings.html",
            check_item_groups=[
                {
                    "task_type": DOCUMENT_TASK_TYPE,
                    "title": "单文档检查-提示词设置",
                    "description": "内置检查项不可删除；扩展检查项可新增、停用或删除。",
                    "new_title": "新增单文档检查项",
                    "name_placeholder": "例如：术语一致性检查",
                    "description_placeholder": "用于向用户说明该检查项的范围",
                    "prompt_placeholder": "描述该检查项的审查角色、关注范围和输出要求",
                    "items": document_check_items,
                    "default_check_codes": default_check_item_codes(DOCUMENT_TASK_TYPE),
                },
                {
                    "task_type": CONSISTENCY_TASK_TYPE,
                    "title": "多文档对照检查-提示词设置",
                    "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交多文档对照任务时可多选。",
                    "new_title": "新增多文档对照项",
                    "name_placeholder": "例如：关键参数一致性检查",
                    "description_placeholder": "用于说明该多文档对照项的比对范围",
                    "prompt_placeholder": "描述素材与资料的比对规则、关注范围和输出要求",
                    "items": consistency_check_items,
                    "default_check_codes": default_check_item_codes(CONSISTENCY_TASK_TYPE),
                },
                {
                    "task_type": LANGUAGE_CONSISTENCY_TASK_TYPE,
                    "title": "跨语种文档一致性对比-提示词设置",
                    "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交跨语种对比任务时可多选。",
                    "new_title": "新增跨语种对比项",
                    "name_placeholder": "例如：翻译缺失与事实差异检查",
                    "description_placeholder": "用于说明该跨语种对比项的范围",
                    "prompt_placeholder": "描述两份不同语种文档的比对规则、关注范围和中文输出要求",
                    "items": language_consistency_check_items,
                    "default_check_codes": default_check_item_codes(LANGUAGE_CONSISTENCY_TASK_TYPE),
                },
                {
                    "task_type": IMAGE_TASK_TYPE,
                    "title": "图片检查-提示词设置",
                    "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交图片检查任务时可多选。",
                    "new_title": "新增图片检查项",
                    "name_placeholder": "例如：端子标识完整性检查",
                    "description_placeholder": "用于说明该图片检查项的范围",
                    "prompt_placeholder": "描述图片审查角色、关注范围、判断规则和输出要求",
                    "items": image_check_items,
                    "default_check_codes": default_check_item_codes(IMAGE_TASK_TYPE),
                },
                {
                    "task_type": VIDEO_TASK_TYPE,
                    "title": "视频检查-提示词设置",
                    "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交视频检查任务时可多选。",
                    "new_title": "新增视频检查项",
                    "name_placeholder": "例如：安装力矩与工具使用检查",
                    "description_placeholder": "用于说明该视频检查项的范围",
                    "prompt_placeholder": "描述视频质检角色、关注范围、判断规则和输出要求",
                    "items": video_check_items,
                    "default_check_codes": default_check_item_codes(VIDEO_TASK_TYPE),
                },
            ],
            global_concurrency=get_setting("global_concurrency", 3),
            user_concurrency=get_setting("user_concurrency", 1),
            check_item_concurrency=get_setting("check_item_concurrency", CHECK_ITEM_CONCURRENCY_DEFAULT),
            image_page_check_max_pages=get_setting("image_page_check_max_pages", DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES),
            issue_output_limit=get_setting("issue_output_limit", ISSUE_OUTPUT_LIMIT_DEFAULT),
            report_retention_days=get_setting("report_retention_days", REPORT_RETENTION_DAYS_DEFAULT),
            network=current_app.config["NETWORK"],
            llm_stream_trace_enabled=get_bool_setting("llm_stream_trace_enabled", False),
            settings_tab=settings_tab,
            ip_username_management_enabled=_ip_username_management_enabled(),
            ip_username_rows=_ip_username_rows() if _ip_username_management_enabled() else [],
        )


def _identity_label(identity: UserIdentity) -> str:
    if identity.display_name:
        return f"{identity.subject}-{identity.display_name}"
    return identity.label


def _wants_json_response() -> bool:
    return request.headers.get("X-Requested-With") == "fetch" or request.accept_mimetypes.best == "application/json"


def _form_bool(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _platform_enabled() -> bool:
    return bool(current_app.config.get("PLATFORM", True))


def _max_upload_mb() -> int:
    try:
        return max(1, int(current_app.config.get("MAX_UPLOAD_MB") or 1))
    except (TypeError, ValueError):
        return 1


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


def _auth_mode() -> str:
    auth_config = current_app.config.get("AUTH", {})
    if not isinstance(auth_config, dict):
        return "ip"
    return str(auth_config.get("mode") or "ip").strip().lower()


def _mode_subject_prefix() -> str:
    mode = _auth_mode()
    if mode == "trusted_header":
        return "trusted_header:"
    if mode == "saml":
        return "saml:"
    return "ip:"


def _owner_subject_expr(table_alias: str = "t") -> str:
    prefix = f"{table_alias}." if table_alias else ""
    return f"COALESCE({prefix}owner_subject, 'ip:' || {prefix}ip)"


def _mode_subject_filter(table_alias: str = "t") -> tuple[str, tuple[str]]:
    return f"instr({_owner_subject_expr(table_alias)}, ?) = 1", (_mode_subject_prefix(),)


def _ip_username_management_enabled() -> bool:
    return _auth_mode() == "ip"


def _settings_tab() -> str:
    tab = request.args.get("tab", "general").strip()
    if tab == "ip_users" and _ip_username_management_enabled():
        return tab
    return "general"


def _valid_ip(value: str) -> bool:
    try:
        ip_address(str(value or "").strip())
    except ValueError:
        return False
    return True


def _ip_username_rows():
    return get_db().execute(
        """
        WITH known_ips AS (
            SELECT ip
            FROM tasks
            WHERE ip IS NOT NULL
              AND ip != ''
              AND instr(COALESCE(owner_subject, 'ip:' || ip), 'ip:') = 1
            UNION
            SELECT ip FROM ip_usernames
        )
        SELECT
            k.ip,
            COALESCE(u.username, '') AS username,
            COUNT(t.id) AS task_count,
            MAX(t.created_at) AS last_task_at
        FROM known_ips k
        LEFT JOIN ip_usernames u ON u.ip = k.ip
        LEFT JOIN tasks t
            ON t.ip = k.ip
           AND instr(COALESCE(t.owner_subject, 'ip:' || t.ip), 'ip:') = 1
        GROUP BY k.ip, u.username
        ORDER BY COALESCE(MAX(t.created_at), '') DESC, k.ip ASC
        """
    ).fetchall()


def _saml_mode_enabled() -> bool:
    return _auth_mode() == "saml"


def _is_user_endpoint(endpoint: str | None) -> bool:
    return bool(endpoint and endpoint.startswith("user_"))


def _needs_saml_user_session(endpoint: str | None) -> bool:
    if _is_user_endpoint(endpoint):
        return True
    return bool(endpoint in CONSOLE_USER_ENDPOINTS and session.get("admin_logged_in"))


def _has_saml_user_session() -> bool:
    saml_user = session.get(SAML_USER_SESSION_KEY)
    return isinstance(saml_user, dict) and bool(str(saml_user.get("user_id") or "").strip())


def _current_relative_url() -> str:
    path = request.full_path if request.query_string else request.path
    return path.rstrip("?") or url_for("user_tasks")


def _safe_next_path(value) -> str:
    value = str(value or "").strip()
    if not value:
        return url_for("user_tasks")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return url_for("user_tasks")
    return value


def _saml_user_from_response(auth) -> tuple[str, str]:
    saml_config = current_app.config.get("AUTH", {}).get("saml", {})
    user_id_attribute = str(saml_config.get("user_id_attribute") or "").strip()
    username_attribute = str(saml_config.get("username_attribute") or "").strip()
    attributes = auth.get_attributes() or {}
    friendly_attributes = getattr(auth, "get_friendlyname_attributes", lambda: {})() or {}

    if user_id_attribute:
        user_id = _saml_attribute_value(attributes, user_id_attribute) or _saml_attribute_value(
            friendly_attributes, user_id_attribute
        )
    else:
        user_id = str(auth.get_nameid() or "").strip()
    username = ""
    if username_attribute:
        username = _saml_attribute_value(attributes, username_attribute) or _saml_attribute_value(
            friendly_attributes, username_attribute
        )
    return user_id, username or user_id


def _saml_attribute_value(attributes: dict, name: str) -> str:
    value = attributes.get(name) if isinstance(attributes, dict) else None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def _current_user_identity() -> UserIdentity:
    try:
        return current_identity(require_sso=True)
    except AuthenticationRequired:
        abort(401, description="未收到 SSO 用户信息，请通过公司统一入口访问。")


def _console_user_identity() -> UserIdentity:
    if _platform_enabled():
        return _current_user_identity()
    return current_identity()


def _owner_display(task) -> str:
    ip = str(_row_value(task, "ip") or "").strip()
    subject = (
        _row_value(task, "effective_owner_subject")
        or _row_value(task, "owner_subject")
        or owner_subject_from_ip(ip)
    )
    subject = str(subject)
    if subject.startswith("ip:"):
        current_ip_username = get_ip_username(ip or subject[3:])
        if current_ip_username:
            return current_ip_username
    current_owner_name = _row_value(task, "current_owner_name")
    if current_owner_name:
        return str(current_owner_name)
    owner_name_snapshot = _row_value(task, "owner_name_snapshot")
    if owner_name_snapshot:
        return str(owner_name_snapshot)
    username_snapshot = _row_value(task, "username_snapshot")
    if username_snapshot:
        return str(username_snapshot)
    return subject_label(subject)


def _owner_meta(task) -> str:
    ip = str(_row_value(task, "ip") or "").strip()
    subject = (
        _row_value(task, "effective_owner_subject")
        or _row_value(task, "owner_subject")
        or owner_subject_from_ip(ip)
    )
    subject = str(subject)
    if subject.startswith("ip:"):
        subject_ip = subject[3:].strip()
        display = _owner_display(task)
        if display and display not in {subject_ip, ip}:
            return f"IP {ip or subject_ip}"
        return ""
    if subject and ip:
        return f"{subject} · IP {ip}"
    if subject:
        return subject
    if ip:
        return f"IP {ip}"
    return ""


def _row_value(row, key: str, default=None):
    if row is None:
        return default
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def _render_admin_tasks_page():
    return _render_admin_task_list(
        task_type=DOCUMENT_TASK_TYPE,
        template_name="admin_tasks.html",
        totals_task_type=DOCUMENT_TASK_TYPE,
        check_items=get_enabled_check_items(),
    )


def _render_admin_consistency_page():
    return _render_admin_task_list(
        task_type=CONSISTENCY_TASK_TYPE,
        template_name="admin_consistency.html",
        totals_task_type=CONSISTENCY_TASK_TYPE,
        check_items=get_enabled_check_items(CONSISTENCY_TASK_TYPE),
    )


def _render_admin_language_consistency_page():
    return _render_admin_task_list(
        task_type=LANGUAGE_CONSISTENCY_TASK_TYPE,
        template_name="admin_language_consistency.html",
        totals_task_type=LANGUAGE_CONSISTENCY_TASK_TYPE,
        check_items=get_enabled_check_items(LANGUAGE_CONSISTENCY_TASK_TYPE),
    )


def _render_admin_images_page():
    return _render_admin_task_list(
        task_type=IMAGE_TASK_TYPE,
        template_name="admin_images.html",
        totals_task_type=IMAGE_TASK_TYPE,
        check_items=get_enabled_check_items(IMAGE_TASK_TYPE),
    )


def _render_admin_videos_page():
    return _render_admin_task_list(
        task_type=VIDEO_TASK_TYPE,
        template_name="admin_videos.html",
        totals_task_type=VIDEO_TASK_TYPE,
        check_items=get_enabled_check_items(VIDEO_TASK_TYPE),
    )


def _render_admin_task_list(*, task_type: str, template_name: str, totals_task_type: str, check_items):
    identity = _console_user_identity()
    status = request.args.get("status", "")
    owner = request.args.get("owner", request.args.get("ip", "")).strip()
    page = _page_arg()
    params = []
    clauses = []
    join_ip_usernames = _auth_mode() == "ip"
    ip_username_join = "LEFT JOIN ip_usernames iu ON iu.ip = t.ip" if join_ip_usernames else ""
    owner_name_expr = (
        "COALESCE(NULLIF(iu.username, ''), NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '')"
        if join_ip_usernames
        else "COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '')"
    )
    mode_clause, mode_params = _mode_subject_filter("t")
    clauses.append(mode_clause)
    params.extend(mode_params)
    if status:
        clauses.append("t.status = ?")
        params.append(status)
    if owner:
        owner_name_filter = "OR COALESCE(iu.username, '') LIKE ?" if join_ip_usernames else ""
        clauses.append(
            f"""
            (
                COALESCE(t.owner_subject, 'ip:' || t.ip) LIKE ?
                OR t.ip LIKE ?
                OR COALESCE(t.owner_name_snapshot, t.username_snapshot, '') LIKE ?
                {owner_name_filter}
            )
            """
        )
        owner_like = f"%{owner}%"
        params.extend([owner_like, owner_like, owner_like])
        if join_ip_usernames:
            params.append(owner_like)
    clauses.append("t.task_type = ?")
    params.append(task_type)
    where = f"WHERE {' AND '.join(clauses)}"
    total = get_db().execute(
        f"""
        SELECT COUNT(*) AS total
        FROM tasks t
        {ip_username_join}
        {where}
        """,
        tuple(params),
    ).fetchone()["total"]
    page = _bounded_page(page, total, TASKS_PER_PAGE)
    rows = get_db().execute(
        f"""
        SELECT t.*,
               {owner_name_expr} AS current_owner_name,
               {owner_name_expr} AS current_username,
               COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
        FROM tasks t
        {ip_username_join}
        {where}
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT ? OFFSET ?
        """,
        tuple(params + [TASKS_PER_PAGE, (page - 1) * TASKS_PER_PAGE]),
    ).fetchall()
    return render_template(
        template_name,
        tasks=rows,
        status=status,
        owner=owner,
        ip=owner,
        pagination=_pagination(page, total, TASKS_PER_PAGE),
        totals=_admin_totals(totals_task_type),
        global_concurrency=get_setting("global_concurrency", 3),
        user_concurrency=get_setting("user_concurrency", 1),
        check_items=check_items,
        models=get_enabled_models(identity.subject),
        active_nav=task_type,
    )


def _check_item_task_type(value: str | None) -> str:
    if value == CONSISTENCY_TASK_TYPE:
        return CONSISTENCY_TASK_TYPE
    if value == LANGUAGE_CONSISTENCY_TASK_TYPE:
        return LANGUAGE_CONSISTENCY_TASK_TYPE
    if value == IMAGE_TASK_TYPE:
        return IMAGE_TASK_TYPE
    if value == VIDEO_TASK_TYPE:
        return VIDEO_TASK_TYPE
    return DOCUMENT_TASK_TYPE


def _check_item_code_prefix(task_type: str) -> str:
    if task_type == CONSISTENCY_TASK_TYPE:
        return "custom-consistency"
    if task_type == LANGUAGE_CONSISTENCY_TASK_TYPE:
        return "custom-language-consistency"
    if task_type == IMAGE_TASK_TYPE:
        return "custom-image"
    if task_type == VIDEO_TASK_TYPE:
        return "custom-video"
    return "custom"


def _check_items_for_task_type(db, task_type: str):
    return db.execute(
        """
        SELECT *
        FROM check_items
        WHERE task_type = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (task_type,),
    ).fetchall()


def get_enabled_check_items(task_type: str = DOCUMENT_TASK_TYPE):
    return get_db().execute(
        """
        SELECT *
        FROM check_items
        WHERE task_type = ? AND enabled = 1
        ORDER BY sort_order ASC, id ASC
        """,
        (task_type,),
    ).fetchall()


def _enabled_check_item_snapshots(db, check_ids: list[int], task_type: str) -> list[dict]:
    unique_ids = []
    seen = set()
    for check_id in check_ids:
        if check_id not in seen:
            unique_ids.append(check_id)
            seen.add(check_id)
    if not unique_ids:
        return []

    placeholders = ",".join("?" for _ in unique_ids)
    rows = db.execute(
        f"""
        SELECT id, code, name, prompt
        FROM check_items
        WHERE id IN ({placeholders}) AND task_type = ? AND enabled = 1
        ORDER BY sort_order ASC, id ASC
        """,
        tuple(unique_ids + [task_type]),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "code": row["code"],
            "name": row["name"],
            "prompt": row["prompt"],
        }
        for row in rows
    ]


def _next_check_item_sort_order(db, task_type: str = DOCUMENT_TASK_TYPE) -> int:
    row = db.execute(
        "SELECT MIN(sort_order) AS value FROM check_items WHERE task_type = ?",
        (task_type,),
    ).fetchone()
    if row is None or row["value"] is None:
        return 10
    return int(row["value"]) - 10


def _reorder_check_items(db, item_ids: list[int], task_type: str = DOCUMENT_TASK_TYPE) -> list[int]:
    rows = db.execute(
        """
        SELECT id
        FROM check_items
        WHERE task_type = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (task_type,),
    ).fetchall()
    existing_ids = [int(row["id"]) for row in rows]
    existing_set = set(existing_ids)
    ordered_ids = []
    seen_ids = set()
    for item_id in item_ids:
        if item_id in existing_set and item_id not in seen_ids:
            ordered_ids.append(item_id)
            seen_ids.add(item_id)
    ordered_ids.extend(item_id for item_id in existing_ids if item_id not in seen_ids)

    updated_at = now_text()
    for index, item_id in enumerate(ordered_ids, start=1):
        db.execute(
            "UPDATE check_items SET sort_order = ?, updated_at = ? WHERE id = ?",
            (index * 10, updated_at, item_id),
        )
    return ordered_ids


def _model_page_identity() -> UserIdentity:
    if _platform_enabled():
        return _current_user_identity()
    return current_identity()


def _model_management_response(identity: UserIdentity, redirect_endpoint: str):
    if request.method == "POST":
        action = request.form.get("action", "save")
        provider_id = request.form.get("provider_id")
        if action == "delete" and provider_id:
            _delete_user_model_provider(identity.subject, provider_id)
            flash("模型提供商已删除。", "success")
            return redirect(url_for(redirect_endpoint))

        provider_data = _provider_form_data()
        if isinstance(provider_data, str):
            flash(provider_data, "error")
            return redirect(url_for(redirect_endpoint))

        if provider_id and not _user_provider_exists(identity.subject, provider_id):
            flash("模型提供商不存在。", "error")
            return redirect(url_for(redirect_endpoint))

        _save_user_model_provider(identity.subject, provider_id, provider_data)
        flash("模型提供商已保存。", "success")
        return redirect(url_for(redirect_endpoint))

    providers = _load_user_model_providers(identity.subject)
    models_by_provider = {
        provider["id"]: sorted(
            _provider_model_options(provider),
            key=lambda model: (model["model_name"], model["force_disable_thinking"]),
        )
        for provider in providers
    }
    return render_template(
        "user_models.html",
        providers=providers,
        models_by_provider=models_by_provider,
        active_nav="models",
    )


def _provider_form_data() -> dict | str:
    return _normalize_provider_input(
        {
            "name": request.form.get("name", ""),
            "api_base": request.form.get("api_base", ""),
            "api_key": request.form.get("api_key", ""),
            "request_timeout": request.form.get("request_timeout", str(PROVIDER_TIMEOUT_DEFAULT)),
            "max_input_chars": request.form.get("max_input_chars", str(PROVIDER_INPUT_LIMIT_DEFAULT)),
            "is_active": request.form.get("is_active") == "on",
            "models": _parse_model_configs(
                request.form.get("model_configs", ""),
                request.form.get("models", ""),
            ),
        },
        require_models=True,
    )


def _provider_query_data() -> dict | str:
    return _normalize_provider_input(
        {
            "name": "模型拉取",
            "api_base": request.args.get("api_base", ""),
            "api_key": request.args.get("api_key", ""),
            "request_timeout": request.args.get("request_timeout", str(PROVIDER_TIMEOUT_DEFAULT)),
            "max_input_chars": str(PROVIDER_INPUT_LIMIT_DEFAULT),
            "is_active": True,
            "models": [{"model_name": "placeholder", "force_disable_thinking": False}],
        },
        require_models=False,
    )


def _provider_payload_data(data: dict) -> dict | str:
    return _normalize_provider_input(
        {
            "name": "模型测试",
            "api_base": data.get("api_base", ""),
            "api_key": data.get("api_key", ""),
            "request_timeout": data.get("request_timeout", str(PROVIDER_TIMEOUT_DEFAULT)),
            "max_input_chars": str(PROVIDER_INPUT_LIMIT_DEFAULT),
            "is_active": True,
            "models": [{"model_name": "placeholder", "force_disable_thinking": False}],
        },
        require_models=False,
    )


def _normalize_provider_input(value: dict, *, require_models: bool) -> dict | str:
    name = str(value.get("name") or "").strip()
    api_base = str(value.get("api_base") or "").strip().rstrip("/")
    api_key = str(value.get("api_key") or "").strip()
    if not name or not api_base:
        return "提供商名称和 API 地址不能为空。"
    if not _is_chat_completions_endpoint(api_base):
        return "API 地址必须填写完整的 /chat/completions 请求地址。"
    try:
        request_timeout = int(value.get("request_timeout") or PROVIDER_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        return "超时时间必须是整数秒。"
    try:
        max_input_chars = int(value.get("max_input_chars") or PROVIDER_INPUT_LIMIT_DEFAULT)
    except (TypeError, ValueError):
        return "文本上限必须是整数。"
    if request_timeout < PROVIDER_TIMEOUT_MIN or request_timeout > PROVIDER_TIMEOUT_MAX:
        return f"超时时间需在 {PROVIDER_TIMEOUT_MIN}-{PROVIDER_TIMEOUT_MAX} 秒之间。"
    if max_input_chars < PROVIDER_INPUT_LIMIT_MIN or max_input_chars > PROVIDER_INPUT_LIMIT_MAX:
        return f"文本上限需在 {PROVIDER_INPUT_LIMIT_MIN}-{PROVIDER_INPUT_LIMIT_MAX} 字之间。"
    model_configs = value.get("models") or []
    if require_models and not model_configs:
        return "至少需要填写一个模型 ID。"
    return {
        "name": name,
        "api_base": api_base,
        "api_key": api_key,
        "request_timeout": request_timeout,
        "max_input_chars": max_input_chars,
        "is_active": bool(value.get("is_active")),
        "models": model_configs,
    }


def _load_user_model_providers(owner_subject: str) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT *
        FROM user_model_providers
        WHERE owner_subject = ?
        ORDER BY updated_at DESC, id DESC
        """,
        (owner_subject,),
    ).fetchall()
    return [_provider_from_row(row, _load_user_model_configs(row["id"])) for row in rows]


def _load_user_model_configs(provider_id: int) -> list[dict]:
    rows = get_db().execute(
        """
        SELECT model_name, force_disable_thinking
        FROM user_model_configs
        WHERE provider_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (provider_id,),
    ).fetchall()
    return [
        {
            "model_name": row["model_name"],
            "force_disable_thinking": bool(row["force_disable_thinking"]),
        }
        for row in rows
    ]


def _provider_from_row(row, models: list[dict]) -> dict:
    return {
        "id": row["id"],
        "owner_subject": row["owner_subject"],
        "name": row["name"],
        "api_base": row["api_base"],
        "api_key": row["api_key"] or "",
        "request_timeout": row["request_timeout"],
        "max_input_chars": row["max_input_chars"],
        "is_active": bool(row["is_active"]),
        "models": models,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _user_provider_exists(owner_subject: str, provider_id) -> bool:
    return (
        get_db()
        .execute(
            "SELECT 1 FROM user_model_providers WHERE id = ? AND owner_subject = ?",
            (provider_id, owner_subject),
        )
        .fetchone()
        is not None
    )


def _save_user_model_provider(owner_subject: str, provider_id, provider_data: dict):
    db = get_db()
    now = now_text()
    if provider_id:
        db.execute(
            """
            UPDATE user_model_providers
            SET name = ?, api_base = ?, api_key = ?,
                request_timeout = ?, max_input_chars = ?, is_active = ?, updated_at = ?
            WHERE id = ? AND owner_subject = ?
            """,
            (
                provider_data["name"],
                provider_data["api_base"],
                provider_data["api_key"],
                provider_data["request_timeout"],
                provider_data["max_input_chars"],
                1 if provider_data["is_active"] else 0,
                now,
                provider_id,
                owner_subject,
            ),
        )
        saved_provider_id = int(provider_id)
    else:
        cursor = db.execute(
            """
            INSERT INTO user_model_providers(
                owner_subject, name, api_base, api_key,
                request_timeout, max_input_chars, is_active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_subject,
                provider_data["name"],
                provider_data["api_base"],
                provider_data["api_key"],
                provider_data["request_timeout"],
                provider_data["max_input_chars"],
                1 if provider_data["is_active"] else 0,
                now,
                now,
            ),
        )
        saved_provider_id = cursor.lastrowid
        if saved_provider_id is None:
            raise RuntimeError("模型提供商保存失败，请稍后重试。")
    _replace_user_model_configs(saved_provider_id, provider_data["models"], now)
    db.commit()


def _replace_user_model_configs(provider_id: int, model_configs: list[dict], updated_at: str):
    db = get_db()
    db.execute("DELETE FROM user_model_configs WHERE provider_id = ?", (provider_id,))
    for index, model_config in enumerate(model_configs, start=1):
        db.execute(
            """
            INSERT INTO user_model_configs(
                provider_id, model_name, force_disable_thinking, sort_order, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                provider_id,
                model_config["model_name"],
                1 if model_config["force_disable_thinking"] else 0,
                index * 10,
                updated_at,
                updated_at,
            ),
        )


def _delete_user_model_provider(owner_subject: str, provider_id):
    get_db().execute(
        "DELETE FROM user_model_providers WHERE id = ? AND owner_subject = ?",
        (provider_id, owner_subject),
    )
    get_db().commit()


def _parse_model_configs(model_configs_json: str, models_text: str = "") -> list[dict]:
    configs = []
    try:
        value = json.loads(model_configs_json) if model_configs_json else []
    except json.JSONDecodeError:
        value = []

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                model_name = str(item.get("model_name") or item.get("id") or "").strip()
                force_disable_thinking = _form_bool(item.get("force_disable_thinking"))
            else:
                model_name = str(item or "").strip()
                force_disable_thinking = False
            configs.append(
                {
                    "model_name": model_name,
                    "force_disable_thinking": force_disable_thinking,
                }
            )

    if not configs:
        configs = [
            {
                "model_name": line.strip(),
                "force_disable_thinking": False,
            }
            for line in str(models_text or "").splitlines()
            if line.strip()
        ]

    result = []
    seen = set()
    for config in configs:
        model_name = str(config.get("model_name") or "").strip()
        force_disable_thinking = bool(config.get("force_disable_thinking"))
        key = (model_name, force_disable_thinking)
        if not model_name or key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "model_name": model_name,
                "force_disable_thinking": force_disable_thinking,
            }
        )
    return result


def _provider_model_options(provider: dict) -> list[dict]:
    return [
        {
            "model_name": _model_config_name(model_config),
            "force_disable_thinking": _model_config_force_disable_thinking(model_config),
            "enabled": True,
        }
        for model_config in provider["models"]
        if _model_config_name(model_config)
    ]


def _model_config_name(model_config) -> str:
    if isinstance(model_config, dict):
        return str(model_config.get("model_name") or model_config.get("id") or "").strip()
    return str(model_config or "").strip()


def _model_config_force_disable_thinking(model_config) -> bool:
    return bool(isinstance(model_config, dict) and model_config.get("force_disable_thinking"))


def get_enabled_models(owner_subject: str | None = None):
    if owner_subject is None:
        owner_subject = current_identity().subject
    models = []
    for provider in _load_user_model_providers(owner_subject):
        if not provider["is_active"]:
            continue
        for model_config in provider["models"]:
            models.append(_model_option(provider, model_config))
    return sorted(models, key=lambda model: (model["provider_name"], model["model_name"], model["force_disable_thinking"]))


def _model_option(provider: dict, model_name) -> dict:
    if isinstance(model_name, dict):
        model_config = model_name
        model_name = str(model_config.get("model_name") or model_config.get("id") or "").strip()
        force_disable_thinking = bool(model_config.get("force_disable_thinking"))
    else:
        model_name = str(model_name or "").strip()
        force_disable_thinking = False
    return {
        "id": f"{provider['id']}:{1 if force_disable_thinking else 0}:{model_name}",
        "provider_id": provider["id"],
        "provider_name": provider["name"],
        "model_name": model_name,
        "force_disable_thinking": force_disable_thinking,
        "api_base": provider["api_base"],
        "api_key": provider["api_key"],
        "request_timeout": provider["request_timeout"],
        "max_input_chars": provider["max_input_chars"],
    }


def _is_chat_completions_endpoint(value: str) -> bool:
    endpoint = str(value or "").strip().rstrip("/")
    return endpoint.startswith(("http://", "https://")) and endpoint.endswith("/chat/completions")


def _find_enabled_model(model_id: str, owner_subject: str | None = None) -> dict | None:
    if ":" not in model_id:
        return None
    if owner_subject is None:
        owner_subject = current_identity().subject
    force_disable_thinking = None
    parts = model_id.split(":", 2)
    if len(parts) == 3 and parts[1] in {"0", "1"}:
        provider_id, thinking_flag, model_name = parts
        force_disable_thinking = thinking_flag == "1"
    else:
        provider_id, model_name = model_id.split(":", 1)
    for provider in _load_user_model_providers(owner_subject):
        if str(provider["id"]) != str(provider_id) or not provider["is_active"]:
            continue
        for model_config in provider["models"]:
            option = _model_option(provider, model_config)
            if option["model_name"] == model_name and (
                force_disable_thinking is None or option["force_disable_thinking"] == force_disable_thinking
            ):
                return option
        return None
    return None


def _admin_overview_range() -> dict:
    today = date.today()
    default_start = today - timedelta(days=29)
    start_date = _date_arg("start_date", default_start)
    end_date = _date_arg("end_date", today)
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "start_at": f"{start_date.isoformat()} 00:00:00",
        "end_at": f"{(end_date + timedelta(days=1)).isoformat()} 00:00:00",
        "days": (end_date - start_date).days + 1,
    }


def _date_arg(name: str, default: date) -> date:
    value = request.args.get(name, "").strip()
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return default


def _admin_overview_data(start_at: str, end_at: str) -> dict:
    db = get_db()
    mode_clause, mode_params = _mode_subject_filter("")
    totals = db.execute(
        f"""
        SELECT
            COUNT(*) AS tasks,
            COUNT(DISTINCT COALESCE(owner_subject, 'ip:' || ip)) AS users,
            COALESCE(SUM(CASE WHEN task_type = ? THEN 1 ELSE 0 END), 0) AS document_tasks,
            COALESCE(SUM(CASE WHEN task_type = ? THEN 1 ELSE 0 END), 0) AS consistency_tasks,
            COALESCE(SUM(CASE WHEN task_type = ? THEN 1 ELSE 0 END), 0) AS language_consistency_tasks,
            COALESCE(SUM(CASE WHEN task_type = ? THEN 1 ELSE 0 END), 0) AS image_tasks,
            COALESCE(SUM(CASE WHEN task_type = ? THEN 1 ELSE 0 END), 0) AS video_tasks,
            COALESCE(SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END), 0) AS queued,
            COALESCE(SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END), 0) AS running,
            COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) AS completed,
            COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed,
            COALESCE(SUM(CASE WHEN status = 'canceled' THEN 1 ELSE 0 END), 0) AS canceled
        FROM tasks
        WHERE created_at >= ? AND created_at < ? AND {mode_clause}
        """,
        (
            DOCUMENT_TASK_TYPE,
            CONSISTENCY_TASK_TYPE,
            LANGUAGE_CONSISTENCY_TASK_TYPE,
            IMAGE_TASK_TYPE,
            VIDEO_TASK_TYPE,
            start_at,
            end_at,
            *mode_params,
        ),
    ).fetchone()
    totals = dict(totals or {})
    totals["report_items"] = _admin_report_item_totals_for_where(
        f"created_at >= ? AND created_at < ? AND {mode_clause}",
        (start_at, end_at, *mode_params),
    )
    mode_clause, mode_params = _mode_subject_filter("")
    daily_rows = db.execute(
        f"""
        SELECT
            substr(created_at, 1, 10) AS day,
            COUNT(DISTINCT COALESCE(owner_subject, 'ip:' || ip)) AS users,
            COUNT(*) AS tasks,
            COALESCE(SUM(CASE WHEN task_type = ? THEN 1 ELSE 0 END), 0) AS document_tasks,
            COALESCE(SUM(CASE WHEN task_type = ? THEN 1 ELSE 0 END), 0) AS consistency_tasks,
            COALESCE(SUM(CASE WHEN task_type = ? THEN 1 ELSE 0 END), 0) AS language_consistency_tasks,
            COALESCE(SUM(CASE WHEN task_type = ? THEN 1 ELSE 0 END), 0) AS image_tasks,
            COALESCE(SUM(CASE WHEN task_type = ? THEN 1 ELSE 0 END), 0) AS video_tasks
        FROM tasks
        WHERE created_at >= ? AND created_at < ? AND {mode_clause}
        GROUP BY day
        ORDER BY day DESC
        """,
        (
            DOCUMENT_TASK_TYPE,
            CONSISTENCY_TASK_TYPE,
            LANGUAGE_CONSISTENCY_TASK_TYPE,
            IMAGE_TASK_TYPE,
            VIDEO_TASK_TYPE,
            start_at,
            end_at,
            *mode_params,
        ),
    ).fetchall()
    join_ip_usernames = _auth_mode() == "ip"
    ip_username_join = "LEFT JOIN ip_usernames iu ON iu.ip = t.ip" if join_ip_usernames else ""
    username_expr = (
        "COALESCE(NULLIF(MAX(iu.username), ''), NULLIF(MAX(t.owner_name_snapshot), ''), NULLIF(MAX(t.username_snapshot), ''))"
        if join_ip_usernames
        else "COALESCE(NULLIF(MAX(t.owner_name_snapshot), ''), NULLIF(MAX(t.username_snapshot), ''))"
    )
    mode_clause, mode_params = _mode_subject_filter("t")
    user_rows = db.execute(
        f"""
        SELECT
            COALESCE(t.owner_subject, 'ip:' || t.ip) AS subject,
            MIN(t.ip) AS ip,
            {username_expr} AS username,
            COUNT(*) AS tasks,
            COALESCE(SUM(CASE WHEN t.task_type = ? THEN 1 ELSE 0 END), 0) AS document_tasks,
            COALESCE(SUM(CASE WHEN t.task_type = ? THEN 1 ELSE 0 END), 0) AS consistency_tasks,
            COALESCE(SUM(CASE WHEN t.task_type = ? THEN 1 ELSE 0 END), 0) AS language_consistency_tasks,
            COALESCE(SUM(CASE WHEN t.task_type = ? THEN 1 ELSE 0 END), 0) AS image_tasks,
            COALESCE(SUM(CASE WHEN t.task_type = ? THEN 1 ELSE 0 END), 0) AS video_tasks,
            MAX(t.created_at) AS last_task_at
        FROM tasks t
        {ip_username_join}
        WHERE t.created_at >= ? AND t.created_at < ? AND {mode_clause}
        GROUP BY COALESCE(t.owner_subject, 'ip:' || t.ip)
        ORDER BY tasks DESC, last_task_at DESC, COALESCE(t.owner_subject, 'ip:' || t.ip) ASC
        LIMIT 10
        """,
        (
            DOCUMENT_TASK_TYPE,
            CONSISTENCY_TASK_TYPE,
            LANGUAGE_CONSISTENCY_TASK_TYPE,
            IMAGE_TASK_TYPE,
            VIDEO_TASK_TYPE,
            start_at,
            end_at,
            *mode_params,
        ),
    ).fetchall()
    return {
        "totals": totals,
        "daily_rows": daily_rows,
        "user_rows": user_rows,
    }


def _admin_totals(task_type: str = DOCUMENT_TASK_TYPE) -> dict:
    db = get_db()
    mode_clause, mode_params = _mode_subject_filter("")
    totals = {
        "tasks": db.execute(
            f"SELECT COUNT(*) AS total FROM tasks WHERE task_type = ? AND {mode_clause}",
            (task_type, *mode_params),
        ).fetchone()["total"],
        "queued": db.execute(
            f"SELECT COUNT(*) AS total FROM tasks WHERE status = 'queued' AND task_type = ? AND {mode_clause}",
            (task_type, *mode_params),
        ).fetchone()["total"],
        "running": db.execute(
            f"SELECT COUNT(*) AS total FROM tasks WHERE status = 'running' AND task_type = ? AND {mode_clause}",
            (task_type, *mode_params),
        ).fetchone()["total"],
        "completed": db.execute(
            f"SELECT COUNT(*) AS total FROM tasks WHERE status = 'completed' AND task_type = ? AND {mode_clause}",
            (task_type, *mode_params),
        ).fetchone()["total"],
        "users": db.execute(
            f"""
            SELECT COUNT(DISTINCT COALESCE(owner_subject, 'ip:' || ip)) AS total
            FROM tasks
            WHERE task_type = ? AND {mode_clause}
            """,
            (task_type, *mode_params),
        ).fetchone()["total"],
        "ips": db.execute(
            f"""
            SELECT COUNT(DISTINCT COALESCE(owner_subject, 'ip:' || ip)) AS total
            FROM tasks
            WHERE task_type = ? AND {mode_clause}
            """,
            (task_type, *mode_params),
        ).fetchone()["total"],
    }
    totals["report_items"] = _admin_report_item_totals(task_type, mode_clause, mode_params)
    return totals


def _admin_report_item_totals(task_type: str, mode_clause: str, mode_params: tuple[str, ...]) -> dict:
    return _admin_report_item_totals_for_where(
        f"task_type = ? AND {mode_clause}",
        (task_type, *mode_params),
    )


def _admin_report_item_totals_for_where(where_clause: str, params: tuple) -> dict:
    totals = {key: 0 for key in REPORT_ITEM_TYPE_ORDER}
    totals.update(
        {
            "accepted_issue": 0,
            "rejected_issue": 0,
            "pending_issue_acceptance": 0,
        }
    )
    rows = get_db().execute(
        f"""
        SELECT result_json
        FROM tasks
        WHERE result_json IS NOT NULL
          AND TRIM(result_json) != ''
          AND {where_clause}
        """,
        params,
    ).fetchall()
    for row in rows:
        item_totals = _report_item_totals(_prepare_task_results(_parse_result_json(row["result_json"])))
        for key in tuple(REPORT_ITEM_TYPE_ORDER) + ("accepted_issue", "rejected_issue", "pending_issue_acceptance"):
            totals[key] += item_totals[key]
    return _finalize_report_counts(totals)


def create_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    db = get_db()
    uploads = _selected_uploads("document")
    if not uploads:
        flash("请选择要上传的文档。", "error")
        return _back_to_task_form(admin_created)
    for upload in uploads:
        if not allowed_file(upload.filename):
            flash(
                f"“{upload.filename}”不是支持的文件类型，仅支持 docx、pdf、txt、md、html、xlsx、xlsm、xls 文件。",
                "error",
            )
            return _back_to_task_form(admin_created)

    check_ids = [int(value) for value in request.form.getlist("checks") if value.isdigit()]
    if not check_ids:
        flash("请至少选择一个检查项。", "error")
        return _back_to_task_form(admin_created)
    check_snapshots = _enabled_check_item_snapshots(db, check_ids, DOCUMENT_TASK_TYPE)
    if len(check_snapshots) != len(set(check_ids)):
        flash("请选择当前可用的检查项。", "error")
        return _back_to_task_form(admin_created)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id, identity.subject)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created)

    saved_paths: list[Path] = []
    try:
        rows = [
            _prepare_document_task_row(upload, identity, model, check_ids, check_snapshots, saved_paths)
            for upload in uploads
        ]
    except DocumentReadError as exc:
        _remove_uploaded_files(saved_paths)
        flash(f"文档读取失败：{exc}", "error")
        return _back_to_task_form(admin_created)
    except RuntimeError as exc:
        _remove_uploaded_files(saved_paths)
        flash(str(exc), "error")
        return _back_to_task_form(admin_created)

    try:
        db.executemany(
            """
            INSERT INTO tasks(
                task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source,
                original_filename, stored_filename, file_type, file_size,
                document_text, checks_json, checks_snapshot_json, provider_name, model_name, api_base, api_key,
                request_timeout, max_input_chars, force_disable_thinking,
                status, progress, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
            """,
            rows,
        )
        db.commit()
    except Exception:
        db.rollback()
        _remove_uploaded_files(saved_paths)
        current_app.logger.exception("创建单文档检查任务失败")
        flash("创建任务失败，请稍后再试。", "error")
        return _back_to_task_form(admin_created)
    if admin_created:
        return redirect(url_for("admin_tasks"))
    return redirect(url_for("user_tasks"))


def _prepare_document_task_row(
    upload,
    identity: UserIdentity,
    model: dict,
    check_ids: list[int],
    check_snapshots: list[dict],
    saved_paths: list[Path],
):
    file_type = extension_of(upload.filename)
    original_filename = _clean_upload_filename(upload.filename, file_type)
    created_at = now_text()
    stored_filename, destination = _upload_destination(
        original_filename,
        identity.subject,
        created_at,
        file_type,
    )
    upload.save(destination)
    saved_paths.append(destination)
    file_size = os.path.getsize(destination)
    try:
        document_text = extract_text(destination, file_type).strip()
    except DocumentReadError as exc:
        raise DocumentReadError(f"“{original_filename}”：{exc}") from exc
    if not document_text:
        raise RuntimeError(f"“{original_filename}”未能提取到可检查文本。")
    prepared_document_text = format_document_text(original_filename, document_text)
    if len(prepared_document_text) > model["max_input_chars"]:
        raise RuntimeError(
            f"“{original_filename}”文档文本 {len(prepared_document_text)} 字，"
            f"超过当前模型文本上限 {model['max_input_chars']} 字。"
        )
    owner_name = identity.display_name or None
    return (
        DOCUMENT_TASK_TYPE,
        identity.ip,
        owner_name,
        identity.subject,
        owner_name,
        identity.source,
        original_filename,
        stored_filename,
        file_type,
        file_size,
        prepared_document_text,
        json.dumps(check_ids, ensure_ascii=False),
        json.dumps(check_snapshots, ensure_ascii=False),
        model["provider_name"],
        model["model_name"],
        model["api_base"],
        model["api_key"],
        model["request_timeout"],
        model["max_input_chars"],
        1 if model["force_disable_thinking"] else 0,
        created_at,
        created_at,
    )


def create_image_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    db = get_db()
    upload = request.files.get("document")
    if upload is None or not upload.filename:
        flash("请选择要提取图片的文档。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)
    file_type = extension_of(upload.filename)
    if file_type != "pdf":
        flash("图片检查仅支持 PDF 文件。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)

    check_ids = [int(value) for value in request.form.getlist("checks") if value.isdigit()]
    if not check_ids:
        flash("请至少选择一个图片检查项。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)
    check_snapshots = _enabled_check_item_snapshots(db, check_ids, IMAGE_TASK_TYPE)
    if len(check_snapshots) != len(set(check_ids)):
        flash("请选择当前可用的图片检查项。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id, identity.subject)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)

    original_filename = _clean_upload_filename(upload.filename, file_type)
    created_at = now_text()
    stored_filename, destination = _upload_destination(original_filename, identity.subject, created_at, file_type)
    image_dir = _image_output_dir_for_stored(stored_filename)
    upload.save(destination)
    file_size = os.path.getsize(destination)

    extracted_text = ""
    text_error = ""
    try:
        extracted_text = extract_text(destination, file_type).strip()
    except DocumentReadError as exc:
        text_error = str(exc)
        current_app.logger.warning(
            "图片检查任务未能提取文档文本 file=%s error=%s",
            original_filename,
            exc,
        )

    image_error = ""
    try:
        images = extract_images(destination, file_type, image_dir, source_filename=original_filename)
    except DocumentReadError as exc:
        images = []
        image_error = str(exc)
        current_app.logger.warning(
            "图片检查任务未能提取 PDF 内嵌图片 file=%s error=%s",
            original_filename,
            exc,
        )

    try:
        candidate_pages = candidate_pdf_pages_for_image_check(extracted_text, images)
        page_images, page_selection = render_pdf_page_images(
            destination,
            image_dir,
            source_filename=original_filename,
            max_pages=_image_page_check_max_pages(),
            candidate_pages=candidate_pages,
        )
    except DocumentReadError as exc:
        _remove_uploaded_file(destination)
        _remove_directory(image_dir)
        flash(f"PDF 页面截图生成失败：{exc}", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)
    if not images and not page_images:
        _remove_uploaded_file(destination)
        _remove_directory(image_dir)
        flash("未能从 PDF 中生成可检查页面截图或提取到可检查图片。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)

    prepared_document_text = format_image_document_text(
        original_filename,
        images,
        document_text=extracted_text,
        text_error=text_error,
        page_images=page_images,
        page_selection=page_selection,
    )

    document_meta = {
        "source_document": {
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "file_type": file_type,
            "file_size": file_size,
        },
        "image_extraction_error": image_error,
        "page_selection": page_selection,
        "images": [
            {
                **image,
                "relative_path": f"{image_dir.name}/{image['filename']}",
            }
            for image in images
        ],
        "page_images": [
            {
                **image,
                "relative_path": f"{image_dir.name}/{image['filename']}",
            }
            for image in page_images
        ],
    }
    owner_name = identity.display_name or None
    db.execute(
        """
        INSERT INTO tasks(
            task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source,
            original_filename, stored_filename, file_type, file_size,
            document_text, document_meta_json, checks_json, checks_snapshot_json, provider_name, model_name, api_base, api_key,
            request_timeout, max_input_chars, force_disable_thinking,
            status, progress, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
        """,
        (
            IMAGE_TASK_TYPE,
            identity.ip,
            owner_name,
            identity.subject,
            owner_name,
            identity.source,
            original_filename,
            stored_filename,
            file_type,
            file_size,
            prepared_document_text,
            json.dumps(document_meta, ensure_ascii=False),
            json.dumps(check_ids, ensure_ascii=False),
            json.dumps(check_snapshots, ensure_ascii=False),
            model["provider_name"],
            model["model_name"],
            model["api_base"],
            model["api_key"],
            model["request_timeout"],
            model["max_input_chars"],
            1 if model["force_disable_thinking"] else 0,
            created_at,
            created_at,
        ),
    )
    db.commit()
    return redirect(url_for(_task_list_endpoint(admin_created, IMAGE_TASK_TYPE)))


def create_video_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    db = get_db()
    upload = request.files.get("video")
    if upload is None or not upload.filename:
        flash("请选择要质检的视频。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)
    if not allowed_video_file(upload.filename):
        flash("视频检查仅支持 mp4、mov、mkv、webm、avi、m4v 文件。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)

    check_ids = [int(value) for value in request.form.getlist("checks") if value.isdigit()]
    if not check_ids:
        flash("请至少选择一个视频检查项。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)
    check_snapshots = _enabled_check_item_snapshots(db, check_ids, VIDEO_TASK_TYPE)
    if len(check_snapshots) != len(set(check_ids)):
        flash("请选择当前可用的视频检查项。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id, identity.subject)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)

    file_type = video_extension_of(upload.filename)
    original_filename = _clean_upload_filename(upload.filename, file_type)
    created_at = now_text()
    stored_filename, destination = _upload_destination(original_filename, identity.subject, created_at, file_type)
    frame_dir = _image_output_dir_for_stored(stored_filename)
    upload.save(destination)
    file_size = os.path.getsize(destination)

    try:
        frames, frame_selection = extract_video_frames(
            destination,
            frame_dir,
            source_filename=original_filename,
        )
    except DocumentReadError as exc:
        _remove_uploaded_file(destination)
        _remove_directory(frame_dir)
        flash(f"视频抽帧失败：{exc}", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)
    if not frames:
        _remove_uploaded_file(destination)
        _remove_directory(frame_dir)
        flash("未能从视频中抽取可检查画面。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)

    prepared_document_text = format_video_document_text(original_filename, frames, frame_selection)
    if len(prepared_document_text) > model["max_input_chars"]:
        _remove_uploaded_file(destination)
        _remove_directory(frame_dir)
        flash(f"视频帧上下文 {len(prepared_document_text)} 字，超过当前模型文本上限 {model['max_input_chars']} 字。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)

    document_meta = {
        "source_video": {
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "file_type": file_type,
            "file_size": file_size,
        },
        "frame_selection": frame_selection,
        "frames": [
            {
                **frame,
                "relative_path": f"{frame_dir.name}/{frame['filename']}",
            }
            for frame in frames
        ],
    }
    owner_name = identity.display_name or None
    db.execute(
        """
        INSERT INTO tasks(
            task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source,
            original_filename, stored_filename, file_type, file_size,
            document_text, document_meta_json, checks_json, checks_snapshot_json, provider_name, model_name, api_base, api_key,
            request_timeout, max_input_chars, force_disable_thinking,
            status, progress, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
        """,
        (
            VIDEO_TASK_TYPE,
            identity.ip,
            owner_name,
            identity.subject,
            owner_name,
            identity.source,
            original_filename,
            stored_filename,
            file_type,
            file_size,
            prepared_document_text,
            json.dumps(document_meta, ensure_ascii=False),
            json.dumps(check_ids, ensure_ascii=False),
            json.dumps(check_snapshots, ensure_ascii=False),
            model["provider_name"],
            model["model_name"],
            model["api_base"],
            model["api_key"],
            model["request_timeout"],
            model["max_input_chars"],
            1 if model["force_disable_thinking"] else 0,
            created_at,
            created_at,
        ),
    )
    db.commit()
    return redirect(url_for(_task_list_endpoint(admin_created, VIDEO_TASK_TYPE)))


def create_consistency_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    db = get_db()
    master_uploads = _selected_uploads("master_documents")
    related_uploads = _selected_uploads("related_documents")
    if not _validate_consistency_uploads(master_uploads, "素材文档", CONSISTENCY_MAX_MATERIAL_FILES):
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)
    if not _validate_consistency_uploads(related_uploads, "资料", CONSISTENCY_MAX_DATA_FILES):
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)

    check_ids = [int(value) for value in request.form.getlist("checks") if value.isdigit()]
    if not check_ids:
        flash("请至少选择一个多文档对照项。", "error")
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)
    check_snapshots = _enabled_check_item_snapshots(db, check_ids, CONSISTENCY_TASK_TYPE)
    if len(check_snapshots) != len(set(check_ids)):
        flash("请选择当前可用的多文档对照项。", "error")
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id, identity.subject)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)

    created_at = now_text()
    saved_paths = []
    try:
        master_files = _save_consistency_upload_group(master_uploads, identity.subject, created_at, "素材文档", saved_paths)
        related_files = _save_consistency_upload_group(related_uploads, identity.subject, created_at, "资料", saved_paths)
    except DocumentReadError as exc:
        _remove_uploaded_files(saved_paths)
        flash(f"文档读取失败：{exc}", "error")
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)

    validation_text = _compose_consistency_validation_text(
        [
            {"label": "素材文档", "files": master_files},
            {"label": "资料", "files": related_files},
        ]
    )
    if len(validation_text) > model["max_input_chars"]:
        _remove_uploaded_files(saved_paths)
        flash(f"文档文本 {len(validation_text)} 字，超过当前模型文本上限 {model['max_input_chars']} 字。", "error")
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)

    document_meta = {
        "groups": [
            {
                "role": "master",
                "label": "素材文档",
                "files": [_persisted_file_info(file_info) for file_info in master_files],
            },
            {
                "role": "related",
                "label": "资料",
                "files": [_persisted_file_info(file_info) for file_info in related_files],
            },
        ]
    }
    all_files = master_files + related_files
    first_file = all_files[0]
    file_size = sum(file_info["file_size"] for file_info in all_files)
    original_filename = f"多文档对照检查：素材{len(master_files)}个 / 资料{len(related_files)}个"
    owner_name = identity.display_name or None

    db.execute(
        """
        INSERT INTO tasks(
            task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source,
            original_filename, stored_filename, file_type, file_size,
            document_text, document_meta_json, checks_json, checks_snapshot_json, provider_name, model_name, api_base, api_key,
            request_timeout, max_input_chars, force_disable_thinking,
            status, progress, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
        """,
        (
            CONSISTENCY_TASK_TYPE,
            identity.ip,
            owner_name,
            identity.subject,
            owner_name,
            identity.source,
            original_filename,
            first_file["stored_filename"],
            "多文档",
            file_size,
            validation_text,
            json.dumps(document_meta, ensure_ascii=False),
            json.dumps(check_ids, ensure_ascii=False),
            json.dumps(check_snapshots, ensure_ascii=False),
            model["provider_name"],
            model["model_name"],
            model["api_base"],
            model["api_key"],
            model["request_timeout"],
            model["max_input_chars"],
            1 if model["force_disable_thinking"] else 0,
            created_at,
            created_at,
        ),
    )
    db.commit()
    return redirect(url_for(_task_list_endpoint(admin_created, CONSISTENCY_TASK_TYPE)))


def create_language_consistency_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    db = get_db()
    document_a = request.files.get("document_a")
    document_b = request.files.get("document_b")
    if not _validate_language_consistency_upload(document_a, "文档A"):
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)
    if not _validate_language_consistency_upload(document_b, "文档B"):
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)

    check_ids = [int(value) for value in request.form.getlist("checks") if value.isdigit()]
    if not check_ids:
        flash("请至少选择一个跨语种对比项。", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)
    check_snapshots = _enabled_check_item_snapshots(db, check_ids, LANGUAGE_CONSISTENCY_TASK_TYPE)
    if len(check_snapshots) != len(set(check_ids)):
        flash("请选择当前可用的跨语种对比项。", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id, identity.subject)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)

    created_at = now_text()
    saved_paths = []
    try:
        file_a = _save_consistency_upload_group([document_a], identity.subject, created_at, "文档A", saved_paths)[0]
        file_b = _save_consistency_upload_group([document_b], identity.subject, created_at, "文档B", saved_paths)[0]
    except DocumentReadError as exc:
        _remove_uploaded_files(saved_paths)
        flash(f"文档读取失败：{exc}", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)

    validation_text = _compose_language_consistency_validation_text(file_a, file_b)
    if len(validation_text) > model["max_input_chars"]:
        _remove_uploaded_files(saved_paths)
        flash(f"文档文本 {len(validation_text)} 字，超过当前模型文本上限 {model['max_input_chars']} 字。", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)

    document_meta = {
        "groups": [
            {
                "role": "document_a",
                "label": "文档A",
                "files": [_persisted_file_info(file_a)],
            },
            {
                "role": "document_b",
                "label": "文档B",
                "files": [_persisted_file_info(file_b)],
            },
        ],
        "static_precheck": _language_consistency_static_summary(file_a, file_b),
    }
    file_size = file_a["file_size"] + file_b["file_size"]
    original_filename = f"跨语种对比：{file_a['original_filename']} / {file_b['original_filename']}"
    owner_name = identity.display_name or None

    db.execute(
        """
        INSERT INTO tasks(
            task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source,
            original_filename, stored_filename, file_type, file_size,
            document_text, document_meta_json, checks_json, checks_snapshot_json, provider_name, model_name, api_base, api_key,
            request_timeout, max_input_chars, force_disable_thinking,
            status, progress, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
        """,
        (
            LANGUAGE_CONSISTENCY_TASK_TYPE,
            identity.ip,
            owner_name,
            identity.subject,
            owner_name,
            identity.source,
            original_filename,
            file_a["stored_filename"],
            "双文档",
            file_size,
            validation_text,
            json.dumps(document_meta, ensure_ascii=False),
            json.dumps(check_ids, ensure_ascii=False),
            json.dumps(check_snapshots, ensure_ascii=False),
            model["provider_name"],
            model["model_name"],
            model["api_base"],
            model["api_key"],
            model["request_timeout"],
            model["max_input_chars"],
            1 if model["force_disable_thinking"] else 0,
            created_at,
            created_at,
        ),
    )
    db.commit()
    return redirect(url_for(_task_list_endpoint(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)))


def _back_to_task_form(admin_created: bool, task_type: str = DOCUMENT_TASK_TYPE):
    return redirect(url_for(_task_list_endpoint(admin_created, task_type)))


def _task_list_endpoint(admin_created: bool, task_type: str | None = DOCUMENT_TASK_TYPE) -> str:
    if task_type == CONSISTENCY_TASK_TYPE:
        return "admin_consistency" if admin_created else "user_consistency"
    if task_type == LANGUAGE_CONSISTENCY_TASK_TYPE:
        return "admin_language_consistency" if admin_created else "user_language_consistency"
    if task_type == IMAGE_TASK_TYPE:
        return "admin_images" if admin_created else "user_images"
    if task_type == VIDEO_TASK_TYPE:
        return "admin_videos" if admin_created else "user_videos"
    return "admin_tasks" if admin_created else "user_tasks"


def _selected_uploads(field_name: str):
    return [upload for upload in request.files.getlist(field_name) if upload and upload.filename]


def _validate_consistency_uploads(uploads: list, label: str, max_files: int) -> bool:
    if not uploads:
        flash(f"请至少选择 1 个{label}。", "error")
        return False
    if len(uploads) > max_files:
        flash(f"{label}最多上传 {max_files} 个。", "error")
        return False
    for upload in uploads:
        if not allowed_file(upload.filename):
            flash(f"{label}仅支持 docx、pdf、txt、md、html、xlsx、xlsm、xls 文件。", "error")
            return False
    return True


def _validate_language_consistency_upload(upload, label: str) -> bool:
    if upload is None or not upload.filename:
        flash(f"请选择{label}。", "error")
        return False
    if not allowed_file(upload.filename):
        flash(f"{label}仅支持 docx、pdf、txt、md、html、xlsx、xlsm、xls 文件。", "error")
        return False
    return True


def _save_consistency_upload_group(uploads: list, ip: str, created_at: str, label: str, saved_paths: list[Path]) -> list[dict]:
    files = []
    for upload in uploads:
        file_type = extension_of(upload.filename)
        original_filename = _clean_upload_filename(upload.filename, file_type)
        stored_filename, destination = _upload_destination(original_filename, ip, created_at, file_type)
        upload.save(destination)
        saved_paths.append(destination)
        file_size = os.path.getsize(destination)
        try:
            text = extract_text(destination, file_type).strip()
        except DocumentReadError as exc:
            raise DocumentReadError(f"{label}“{original_filename}”：{exc}") from exc
        if not text:
            raise DocumentReadError(f"{label}“{original_filename}”未能提取到可检查文本")
        files.append(
            {
                "original_filename": original_filename,
                "stored_filename": stored_filename,
                "file_type": file_type,
                "file_size": file_size,
                "text": text,
            }
        )
    return files


def _compose_consistency_validation_text(groups: list[dict]) -> str:
    sections = []
    for group in groups:
        group_parts = [f"# {group['label']}"]
        for index, file_info in enumerate(group["files"], start=1):
            group_parts.append(f"## {group['label']}{index}：{file_info['original_filename']}\n{file_info['text']}")
        sections.append("\n\n".join(group_parts))
    return "\n\n".join(sections).strip()


def _compose_language_consistency_validation_text(file_a: dict, file_b: dict) -> str:
    return "\n\n".join(
        [
            "# 静态预检摘要\n"
            + _language_consistency_static_summary(file_a, file_b)
            + "\n\n说明：静态预检仅提供优先核对线索，最终差异判断需结合两份文档正文。",
            f"# 文档A：{file_a['original_filename']}\n{file_a['text']}",
            f"# 文档B：{file_b['original_filename']}\n{file_b['text']}",
        ]
    ).strip()


def _language_consistency_static_summary(file_a: dict, file_b: dict) -> str:
    profile_a = _document_static_profile(file_a)
    profile_b = _document_static_profile(file_b)
    only_a = _limited_sorted(profile_a["tokens"] - profile_b["tokens"], 40)
    only_b = _limited_sorted(profile_b["tokens"] - profile_a["tokens"], 40)
    ratio = _safe_ratio(profile_b["nonspace_chars"], profile_a["nonspace_chars"])
    lines = [
        (
            f"- 文档A：{file_a['original_filename']}；格式：{file_a['file_type']}；"
            f"语种估计：{profile_a['language']}；非空白字符：{profile_a['nonspace_chars']}；"
            f"段落：{profile_a['paragraphs']}；标题线索：{len(profile_a['headings'])}"
        ),
        (
            f"- 文档B：{file_b['original_filename']}；格式：{file_b['file_type']}；"
            f"语种估计：{profile_b['language']}；非空白字符：{profile_b['nonspace_chars']}；"
            f"段落：{profile_b['paragraphs']}；标题线索：{len(profile_b['headings'])}"
        ),
        f"- 长度比例：文档B / 文档A = {ratio}",
        f"- 文档A独有硬线索：{_format_preview_list(only_a)}",
        f"- 文档B独有硬线索：{_format_preview_list(only_b)}",
        f"- 文档A标题线索：{_format_preview_list(profile_a['headings'])}",
        f"- 文档B标题线索：{_format_preview_list(profile_b['headings'])}",
    ]
    return "\n".join(lines)


def _document_static_profile(file_info: dict) -> dict:
    text = str(file_info.get("text") or "")
    nonspace_text = re.sub(r"\s+", "", text)
    paragraphs = [part for part in re.split(r"\n\s*\n+", text.strip()) if part.strip()]
    tokens = {
        _normalize_static_token(match.group(0))
        for match in LANGUAGE_STATIC_TOKEN_RE.finditer(text)
    }
    tokens = {token for token in tokens if token}
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_chars = len(re.findall(r"[A-Za-z]", text))
    return {
        "language": _estimate_text_language(cjk_chars, latin_chars),
        "nonspace_chars": len(nonspace_text),
        "paragraphs": len(paragraphs),
        "tokens": tokens,
        "headings": _extract_static_headings(text, 12),
    }


def _extract_static_headings(text: str, limit: int) -> list[str]:
    headings = []
    seen = set()
    for line in text.splitlines():
        value = re.sub(r"\s+", " ", line).strip()
        if not value or len(value) > 120 or not LANGUAGE_HEADING_RE.match(value):
            continue
        if value in seen:
            continue
        seen.add(value)
        headings.append(value)
        if len(headings) >= limit:
            break
    return headings


def _estimate_text_language(cjk_chars: int, latin_chars: int) -> str:
    if cjk_chars >= 40 and latin_chars >= 80 and min(cjk_chars, latin_chars) / max(cjk_chars, latin_chars) >= 0.2:
        return "中英混合"
    if cjk_chars >= max(20, latin_chars):
        return "中文为主"
    if latin_chars >= max(40, cjk_chars):
        return "拉丁语系为主"
    if cjk_chars or latin_chars:
        return "语种特征较少，需人工确认"
    return "未识别"


def _normalize_static_token(value: str) -> str:
    return value.strip(" \t\r\n,.;:，。；：、()（）[]【】<>《》\"'“”‘’").lower()


def _limited_sorted(values: set[str] | list[str], limit: int) -> list[str]:
    return sorted(values, key=lambda value: (len(value), value))[:limit]


def _format_preview_list(values: list[str]) -> str:
    return "、".join(values) if values else "未发现"


def _safe_ratio(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "无法计算"
    return f"{numerator / denominator:.2f}"


def _persisted_file_info(file_info: dict) -> dict:
    return {
        "original_filename": file_info["original_filename"],
        "stored_filename": file_info["stored_filename"],
        "file_type": file_info["file_type"],
        "file_size": file_info["file_size"],
    }


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _platform_enabled():
            return view(*args, **kwargs)
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def _get_task_or_404(task_id: int):
    join_ip_usernames = _auth_mode() == "ip"
    ip_username_join = "LEFT JOIN ip_usernames iu ON iu.ip = t.ip" if join_ip_usernames else ""
    owner_name_expr = (
        "COALESCE(NULLIF(iu.username, ''), NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '')"
        if join_ip_usernames
        else "COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '')"
    )
    clauses = ["t.id = ?"]
    params: list[object] = [task_id]
    if _platform_enabled():
        mode_clause, mode_params = _mode_subject_filter("t")
        clauses.append(mode_clause)
        params.extend(mode_params)
    task = get_db().execute(
        f"""
        SELECT t.*,
               {owner_name_expr} AS current_owner_name,
               {owner_name_expr} AS current_username,
               COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
        FROM tasks t
        {ip_username_join}
        WHERE {' AND '.join(clauses)}
        """,
        tuple(params),
    ).fetchone()
    if task is None:
        abort(404)
    return task


def _get_user_task(task_id: int):
    identity = _current_user_identity()
    task = get_db().execute(
        """
        SELECT t.*,
               COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_owner_name,
               COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_username,
               COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
        FROM tasks t
        WHERE t.id = ? AND COALESCE(t.owner_subject, 'ip:' || t.ip) = ?
        """,
        (task_id, identity.subject),
    ).fetchone()
    if task is None:
        abort(404)
    return task


def _get_user_task_or_local_admin(task_id: int):
    if not _platform_enabled():
        return _get_task_or_404(task_id)
    return _get_user_task(task_id)


def _cancel_task(task):
    if task["status"] in {"completed", "failed", "canceled"}:
        return
    db = get_db()
    db.execute(
        """
        UPDATE tasks
        SET cancel_requested = 1,
            status = 'canceled',
            progress = 0,
            updated_at = ?,
            finished_at = ?
        WHERE id = ? AND status NOT IN ('completed', 'failed', 'canceled')
        """,
        (now_text(), now_text(), task["id"]),
    )
    db.commit()


def _delete_task(task):
    if task["status"] == "running":
        flash("运行中的任务不能直接删除，请先取消后再删除。", "error")
        return False
    db = get_db()
    paths = _task_upload_paths(task)
    image_dirs = {path.parent for path in paths if _image_folder() in path.parents}
    failures = _remove_uploaded_files(paths)
    if failures:
        current_app.logger.warning(
            "删除任务文件失败 task_id=%s failures=%s",
            task["id"],
            "; ".join(f"{path}: {error}" for path, error in failures),
        )
        flash(
            f"任务文件正被其他程序使用，暂时无法删除：{describe_failures(failures)}。"
            "请关闭正在下载、预览或扫描该文件的程序后稍后重试。",
            "error",
        )
        return False
    for image_dir in image_dirs:
        _remove_empty_directory(image_dir)
    db.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
    db.commit()
    return True


def _task_action_redirect(default_endpoint: str):
    next_url = request.form.get("next", "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return url_for(default_endpoint)


def _download_task_document(task, fallback_endpoint: str):
    if task["task_type"] in {CONSISTENCY_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE}:
        return _download_task_documents_zip(task, fallback_endpoint)

    upload_path = _task_upload_path(task)
    if not upload_path.is_file():
        flash("文档已删除，无法下载。", "error")
        return redirect(request.referrer or url_for(fallback_endpoint, task_id=task["id"]))
    return send_file(
        upload_path,
        as_attachment=True,
        download_name=task["original_filename"],
    )


def _remove_uploaded_file(path: Path):
    ok, error = remove_file(path)
    if not ok:
        current_app.logger.warning("删除文件失败 path=%s error=%s", path, error)
    return ok, error


def _remove_uploaded_files(paths: list[Path]):
    failures = []
    for path in paths:
        ok, error = _remove_uploaded_file(path)
        if not ok:
            failures.append((path, error))
    return failures


def _remove_directory(path: Path):
    ok, error = remove_directory_tree(path)
    if not ok:
        current_app.logger.warning("删除目录失败 path=%s error=%s", path, error)
    return ok


def _remove_empty_directory(path: Path):
    return cleanup_remove_empty_directory(path)


def _task_upload_path(task) -> Path:
    return Path(current_app.config["UPLOAD_FOLDER"]) / Path(task["stored_filename"]).name


def _task_upload_paths(task) -> list[Path]:
    groups = _task_document_groups(task)
    upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
    paths = []
    if groups:
        for group in groups:
            for file_info in group["files"]:
                stored_filename = Path(str(file_info.get("stored_filename") or "")).name
                if stored_filename:
                    paths.append(upload_folder / stored_filename)
    else:
        paths.append(_task_upload_path(task))
    for image in _task_image_items(task):
        paths.append(image_path_from_item(_image_folder(), image))
    return paths


def _task_document_groups(task) -> list[dict]:
    return document_groups_from_meta(task["document_meta_json"])


def _task_image_items(task) -> list[dict]:
    raw = task["document_meta_json"]
    return image_items_from_meta(raw) + image_items_from_meta(raw, "page_images") + image_items_from_meta(raw, "frames")


def _image_folder() -> Path:
    configured = current_app.config.get("IMAGE_FOLDER")
    if configured:
        return Path(configured)
    return default_image_folder(current_app.config["UPLOAD_FOLDER"])


def _image_output_dir_for_stored(stored_filename: str) -> Path:
    folder = _image_folder()
    stem = Path(stored_filename).stem
    return folder / _safe_filename_part(stem, "task-images")


def _image_page_check_max_pages() -> int:
    return max(1, _int_setting("image_page_check_max_pages", DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES))


def _int_setting(key: str, default: int) -> int:
    value = get_setting(key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _download_task_documents_zip(task, fallback_endpoint: str):
    groups = _task_document_groups(task)
    if not groups:
        flash("文档信息缺失，无法下载。", "error")
        return redirect(request.referrer or url_for(fallback_endpoint, task_id=task["id"]))

    upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
    buffer = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        used_names = set()
        for group in groups:
            for file_info in group["files"]:
                stored_filename = Path(str(file_info.get("stored_filename") or "")).name
                if not stored_filename:
                    continue
                upload_path = upload_folder / stored_filename
                if not upload_path.is_file():
                    continue
                archive_name = _unique_archive_name(
                    used_names,
                    f"{group['label']}/{Path(str(file_info.get('original_filename') or stored_filename)).name}",
                )
                archive.write(upload_path, archive_name)
                added += 1
    if added == 0:
        flash("文档已删除，无法下载。", "error")
        return redirect(request.referrer or url_for(fallback_endpoint, task_id=task["id"]))

    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{task['task_type'] or 'document-check'}-{task['id']}-documents.zip",
    )


def _unique_archive_name(used_names: set[str], archive_name: str) -> str:
    archive_name = archive_name.strip("/\\") or "document"
    if archive_name not in used_names:
        used_names.add(archive_name)
        return archive_name
    path = Path(archive_name)
    parent = str(path.parent)
    stem = path.stem or "document"
    suffix = path.suffix
    for index in range(2, 1000):
        candidate_name = f"{stem}-{index}{suffix}"
        candidate = f"{parent}/{candidate_name}" if parent and parent != "." else candidate_name
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
    raise RuntimeError("压缩包内文件名过多，无法生成唯一名称")


def _clean_upload_filename(filename: str, file_type: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    name = _safe_filename_part(name, f"document.{file_type}")
    if "." not in name:
        name = f"{name}.{file_type}"
    return name


def _upload_destination(original_filename: str, ip: str, created_at: str, file_type: str) -> tuple[str, Path]:
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    timestamp = re.sub(r"\D", "", created_at) or now_text().replace("-", "").replace(":", "").replace(" ", "")
    stem = _limit_utf8_bytes(_safe_filename_part(Path(original_filename).stem, "document"), 140)
    ip_part = _limit_utf8_bytes(_safe_filename_part(ip, "0.0.0.0"), 80)
    token = uuid.uuid4().hex[:12]
    stored_filename = f"{stem}_{ip_part}_{timestamp}_{token}.{file_type}"
    destination = upload_dir / stored_filename
    if not destination.exists():
        return stored_filename, destination

    for index in range(2, 1000):
        candidate = f"{stem}_{ip_part}_{timestamp}_{token}-{index}.{file_type}"
        destination = upload_dir / candidate
        if not destination.exists():
            return candidate, destination
    raise RuntimeError("无法保存上传文档，请稍后再试")


def _safe_filename_part(value: str, fallback: str) -> str:
    value = INVALID_FILENAME_CHARS.sub("_", value).strip(" ._")
    value = re.sub(r"_+", "_", value)
    return value or fallback


def _limit_utf8_bytes(value: str, max_bytes: int) -> str:
    while len(value.encode("utf-8")) > max_bytes:
        value = value[:-1]
    return value or "document"


def _task_results(task):
    return _prepare_task_results(_raw_task_results(task))


def _raw_task_results(task) -> list[dict]:
    return _parse_result_json(task["result_json"])


def _parse_result_json(result_json) -> list[dict]:
    if not result_json:
        return []
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _prepare_task_results(results: list[dict]) -> list[dict]:
    prepared = []
    for result in results:
        item = dict(result)
        structured_report = _result_structured_report(item)
        report_items = _result_report_items(item, structured_report)
        classifications = item.get("item_classifications")
        if not isinstance(classifications, dict):
            classifications = {}
        acceptances = item.get("item_acceptances")
        if not isinstance(acceptances, dict):
            acceptances = {}
        for report_item in report_items:
            saved_type = classifications.get(report_item["id"])
            report_item["type"] = _normalize_report_item_type(saved_type) or report_item["type"]
            report_item["type_label"] = REPORT_ITEM_TYPES[report_item["type"]]
            acceptance = _normalize_report_acceptance(acceptances.get(report_item["id"]))
            report_item.update(acceptance)
        item["result_summary"] = _result_report_summary(item, structured_report)
        item["report_items"] = report_items
        item["report_counts"] = _count_report_items(report_items)
        prepared.append(item)
    return prepared


def _result_report_items(result: dict, structured_report: dict | None = None) -> list[dict]:
    code = str(result.get("code") or "")
    if structured_report is None:
        structured_report = _result_structured_report(result)
    if structured_report is not None:
        return _structured_report_items(code, structured_report)

    text = str(result.get("result") or "").strip()
    if not text:
        return []
    chunks = _extract_report_item_chunks(text) or [text]
    items = []
    for index, chunk in enumerate(chunks, start=1):
        item_text = chunk.strip()
        if not item_text:
            continue
        fields = _legacy_report_item_fields(item_text)
        items.append(
            {
                "id": _report_item_id(code, index, item_text),
                "index": index,
                "text": item_text,
                **fields,
                "type": _infer_report_item_type(item_text),
            }
        )
    return items


def _result_structured_report(result: dict) -> dict | None:
    for key in ("structured_report", "report_json"):
        structured = _normalize_structured_report_payload(result.get(key))
        if structured is not None:
            return structured

    structured_items = result.get("structured_items")
    if isinstance(structured_items, list):
        summary = _first_report_field(result, REPORT_JSON_SUMMARY_KEYS)
        return {"summary": summary, "items": structured_items}

    return _normalize_structured_report_payload(result.get("result"))


def _result_report_summary(result: dict, structured_report: dict | None) -> str:
    if structured_report is not None:
        return str(structured_report.get("summary") or "").strip()
    return ""


def _normalize_structured_report_payload(value) -> dict | None:
    payload = value
    if isinstance(value, str):
        payload = _parse_structured_report_json(value)
    if isinstance(payload, list):
        return {"summary": "", "items": payload}
    if not isinstance(payload, dict):
        return None

    items = None
    for key in REPORT_JSON_ITEM_KEYS:
        candidate = payload.get(key)
        if isinstance(candidate, list):
            items = candidate
            break
        if isinstance(candidate, str):
            parsed_items = _parse_structured_report_json(candidate)
            if isinstance(parsed_items, list):
                items = parsed_items
                break
    summary = _first_report_field(payload, REPORT_JSON_SUMMARY_KEYS)
    if items is None:
        if any(_first_report_field(payload, aliases) for aliases in REPORT_FIELD_ALIASES.values()):
            items = [payload]
        elif summary:
            items = []
        else:
            return None
    return {"summary": summary, "items": items}


def _parse_structured_report_json(text: str, depth: int = 0):
    text = str(text or "").strip()
    if not text:
        return None
    for candidate in _structured_json_candidates(text):
        parsed = _load_structured_json_candidate(candidate, depth)
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _load_structured_json_candidate(candidate: str, depth: int):
    if depth > 3:
        return None
    for variant in _json_candidate_variants(candidate):
        try:
            parsed = json.loads(variant)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, str):
            nested = _parse_structured_report_json(parsed, depth + 1)
            if nested is not None:
                return nested
            continue
        return parsed
    return None


def _json_candidate_variants(candidate: str) -> list[str]:
    raw = str(candidate or "").strip()
    if not raw:
        return []
    variants = [raw]
    repaired = _escape_json_string_control_chars(raw)
    if repaired != raw:
        variants.append(repaired)
    return variants


def _escape_json_string_control_chars(text: str) -> str:
    result = []
    in_string = False
    escaped = False
    for char in str(text or ""):
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            result.append(char)
            continue
        if in_string and char in {"\n", "\r", "\t"}:
            result.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
            continue
        result.append(char)
    return "".join(result)


def _structured_json_candidates(text: str) -> list[str]:
    candidates = [text]
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL):
        candidates.append(match.group(1).strip())
    object_start = text.find("{")
    object_end = text.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(text[object_start : object_end + 1])
    array_start = text.find("[")
    array_end = text.rfind("]")
    if array_start >= 0 and array_end > array_start:
        candidates.append(text[array_start : array_end + 1])

    seen = set()
    unique = []
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _structured_report_items(result_code: str, structured_report: dict) -> list[dict]:
    raw_items = structured_report.get("items")
    if not isinstance(raw_items, list):
        return []

    items = []
    for raw_item in raw_items:
        fields = _normalize_structured_report_item(raw_item)
        if not fields:
            continue
        index = len(items) + 1
        item_text = _structured_report_item_text(fields)
        item_type = fields.pop("type", "") or _infer_report_item_type(item_text)
        items.append(
            {
                "id": _report_item_id(result_code, index, item_text),
                "index": index,
                "text": item_text,
                **fields,
                "type": item_type,
            }
        )
    return items


def _normalize_structured_report_item(raw_item) -> dict:
    if isinstance(raw_item, str):
        text = raw_item.strip()
        parsed = _parse_structured_report_json(text)
        if isinstance(parsed, dict):
            return _normalize_structured_report_item(parsed)
        return {
            "category": "",
            "location": "",
            "excerpt": "",
            "description": text,
            "impact": "",
            "suggestion": "",
            "type": _infer_report_item_type(text),
        } if text else {}
    if not isinstance(raw_item, dict):
        return {}

    status = _normalize_report_item_status(_first_report_field(raw_item, REPORT_STATUS_KEYS))
    fields = {
        field: _first_report_field(raw_item, aliases)
        for field, aliases in REPORT_FIELD_ALIASES.items()
    }
    if _looks_like_status_only(fields["category"]):
        status = status or _normalize_report_item_status(fields["category"])
        fields["category"] = ""
    if not any(fields.values()):
        fields["description"] = _report_field_text(raw_item)
    item_text = _structured_report_item_text(fields)
    fields["type"] = "non_issue" if _is_no_action_report_item(fields) else status or _infer_report_item_type(item_text)
    return fields


def _first_report_field(source: dict, aliases: tuple[str, ...]) -> str:
    if not isinstance(source, dict):
        return ""
    for key in aliases:
        if key in source:
            text = _report_field_text(source.get(key))
            if text:
                return text
    return ""


def _report_field_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _report_field_text(item)
            if text:
                parts.append(text)
        return "；".join(parts)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _structured_report_item_text(fields: dict) -> str:
    lines = []
    for key, label in REPORT_ITEM_FIELDS:
        value = str(fields.get(key) or "").strip()
        if value:
            lines.append(f"{label}：{value}")
    return "\n".join(lines).strip()


def _is_no_action_report_item(fields: dict) -> bool:
    impact = _compact_report_text(fields.get("impact"))
    suggestion = _compact_report_text(fields.get("suggestion"))
    return _has_report_marker(impact, REPORT_NO_ACTION_IMPACT_MARKERS) and _has_report_marker(
        suggestion,
        REPORT_NO_ACTION_SUGGESTION_MARKERS,
    )


def _compact_report_text(value) -> str:
    return re.sub(r"[\s，。；、,.!！?？:：;；/\\|()（）【】\\[\\]\"'“”‘’_-]+", "", str(value or "")).lower()


def _has_report_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _legacy_report_item_fields(text: str) -> dict:
    fields = {key: "" for key, _ in REPORT_ITEM_FIELDS}
    body_lines = []
    for raw_line in str(text or "").splitlines():
        line = _clean_report_item_line(raw_line)
        if not line:
            continue
        match = re.match(r"^([^:：]{1,28})[:：]\s*(.*)$", line)
        if match:
            label = match.group(1).strip()
            value = match.group(2).strip()
            field = REPORT_LEGACY_LABEL_FIELDS.get(label)
            if field and value:
                fields[field] = _append_report_field(fields[field], value)
                continue
        body_lines.append(line)
    if not fields["description"]:
        fields["description"] = "\n".join(body_lines).strip() or fields["suggestion"] or str(text or "").strip()
    return fields


def _clean_report_item_line(line: str) -> str:
    stripped = REPORT_ITEM_PREFIX_RE.sub("", str(line or "").strip()).strip()
    stripped = re.sub(r"^(?:\d{1,3}[.、)]|\(\d{1,3}\)|（\d{1,3}）)\s*", "", stripped)
    return stripped.strip("*_`~ ")


def _append_report_field(current: str, value: str) -> str:
    current = str(current or "").strip()
    value = str(value or "").strip()
    if not current:
        return value
    if not value or value in current.split("；"):
        return current
    return f"{current}；{value}"


def _looks_like_status_only(value: str) -> bool:
    compact = re.sub(r"\s+", "", str(value or "")).strip()
    return compact.lower() in {"issue", "problem", "suggestion", "advice", "non_issue", "nonissue", "not_issue"} or compact in {
        "问题",
        "明确问题",
        "建议",
        "需人工确认",
        "非问题",
        "不是问题",
    }


def _extract_report_item_chunks(text: str) -> list[str]:
    chunks = []
    current = []
    for line in str(text or "").splitlines():
        if _is_report_item_start(line):
            if current:
                chunks.append("\n".join(current).strip())
            current = [line]
            continue
        if current:
            current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def _is_report_item_start(line: str) -> bool:
    stripped = str(line or "").strip()
    if not stripped:
        return False
    if stripped.startswith(("|", "```", ">")):
        return False
    match_text = REPORT_ITEM_PREFIX_RE.sub("", stripped).strip()
    return bool(REPORT_ITEM_START_RE.match(match_text))


def _report_item_id(result_code: str, index: int, text: str) -> str:
    source = f"{result_code}\n{index}\n{text}"
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def _infer_report_item_type(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or ""))
    if any(marker in compact for marker in ("非问题", "未发现", "无明显", "未见明显", "无需修改", "未见异常", "无异常")):
        return "non_issue"
    if any(marker in compact for marker in ("需人工确认", "人工确认", "疑似", "不确定", "证据不足", "建议核实", "建议复核", "看不清", "无法确认")):
        return "suggestion"
    issue_markers = (
        "问题",
        "错误",
        "不一致",
        "矛盾",
        "冲突",
        "缺失",
        "风险",
        "不规范",
        "不匹配",
        "异常",
    )
    if "建议" in compact and not any(marker in compact for marker in issue_markers):
        return "suggestion"
    if any(marker in compact for marker in issue_markers):
        return "issue"
    return "suggestion"


def _normalize_report_item_status(value) -> str | None:
    exact = _normalize_report_item_type(value)
    if exact:
        return exact
    compact = re.sub(r"\s+", "", str(value or "")).strip().lower()
    if not compact:
        return None
    if any(marker in compact for marker in ("non_issue", "nonissue", "not_issue", "非问题", "不是问题", "无需修改", "无问题")):
        return "non_issue"
    if any(marker in compact for marker in ("suggestion", "advise", "manual", "uncertain", "建议", "需人工确认", "人工确认", "疑似", "不确定", "证据不足")):
        return "suggestion"
    if any(marker in compact for marker in ("issue", "problem", "明确问题", "问题", "错误", "不一致", "缺失", "冲突")):
        return "issue"
    return None


def _normalize_report_item_type(value) -> str | None:
    value = str(value or "").strip()
    return value if value in REPORT_ITEM_TYPES else None


def _normalize_report_acceptance_status(value) -> str | None:
    value = str(value or "").strip()
    return value if value in REPORT_ACCEPTANCE_STATUSES else None


def _normalize_report_rejection_reason(value) -> str:
    value = str(value or "").strip()
    return value if value in REPORT_REJECTION_REASONS else ""


def _normalize_report_acceptance(value) -> dict:
    if not isinstance(value, dict):
        status = _normalize_report_acceptance_status(value) or "pending"
        return {
            "acceptance_status": status,
            "acceptance_label": REPORT_ACCEPTANCE_STATUSES[status],
            "rejection_reason": "",
            "rejection_reason_label": "",
            "rejection_note": "",
        }

    status = _normalize_report_acceptance_status(value.get("status")) or "pending"
    reason = _normalize_report_rejection_reason(value.get("rejection_reason")) if status == "rejected" else ""
    note = str(value.get("rejection_note") or "").strip() if status == "rejected" else ""
    return {
        "acceptance_status": status,
        "acceptance_label": REPORT_ACCEPTANCE_STATUSES[status],
        "rejection_reason": reason,
        "rejection_reason_label": REPORT_REJECTION_REASONS.get(reason, ""),
        "rejection_note": note,
    }


def _report_rate_label(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "-"
    return f"{numerator / denominator * 100:.1f}%"


def _finalize_report_counts(counts: dict) -> dict:
    counts["total"] = sum(int(counts.get(key) or 0) for key in REPORT_ITEM_TYPE_ORDER)
    confirmed_issues = int(counts.get("accepted_issue") or 0) + int(counts.get("rejected_issue") or 0)
    counts["issue_detection_rate"] = _report_rate_label(int(counts.get("issue") or 0), counts["total"])
    counts["issue_acceptance_rate"] = _report_rate_label(int(counts.get("accepted_issue") or 0), confirmed_issues)
    return counts


def _count_report_items(items: list[dict]) -> dict:
    counts = {key: 0 for key in REPORT_ITEM_TYPE_ORDER}
    counts.update(
        {
            "accepted_issue": 0,
            "rejected_issue": 0,
            "pending_issue_acceptance": 0,
        }
    )
    for item in items:
        item_type = _normalize_report_item_type(item.get("type")) or "issue"
        counts[item_type] += 1
        if item_type == "issue":
            acceptance_status = _normalize_report_acceptance_status(item.get("acceptance_status")) or "pending"
            if acceptance_status == "accepted":
                counts["accepted_issue"] += 1
            elif acceptance_status == "rejected":
                counts["rejected_issue"] += 1
            else:
                counts["pending_issue_acceptance"] += 1
    return _finalize_report_counts(counts)


def _report_item_totals(results: list[dict]) -> dict:
    totals = {key: 0 for key in REPORT_ITEM_TYPE_ORDER}
    totals.update(
        {
            "accepted_issue": 0,
            "rejected_issue": 0,
            "pending_issue_acceptance": 0,
        }
    )
    for result in results:
        counts = result.get("report_counts") or {}
        for key in tuple(REPORT_ITEM_TYPE_ORDER) + ("accepted_issue", "rejected_issue", "pending_issue_acceptance"):
            totals[key] += int(counts.get(key) or 0)
    return _finalize_report_counts(totals)


def _update_report_item_type(task):
    if task["status"] in {"queued", "running"}:
        return {"ok": False, "error": "任务尚未完成，暂不能修改报告条目判定。"}, 409
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict):
        data = request.form
    result_code = str(data.get("result_code") or "").strip()
    item_id = str(data.get("item_id") or "").strip()
    item_type = _normalize_report_item_type(data.get("item_type"))
    if not result_code or not item_id or not item_type:
        return {"ok": False, "error": "报告条目判定数据无效。"}, 400
    acceptance_supplied = "acceptance_status" in data
    acceptance_status = None
    rejection_reason = ""
    rejection_note = ""
    if acceptance_supplied:
        acceptance_status = _normalize_report_acceptance_status(data.get("acceptance_status"))
        rejection_reason = _normalize_report_rejection_reason(data.get("rejection_reason"))
        rejection_note = str(data.get("rejection_note") or "").strip()
        if acceptance_status is None:
            return {"ok": False, "error": "接纳状态数据无效。"}, 400
        if acceptance_status == "rejected":
            if not rejection_reason and not rejection_note:
                return {"ok": False, "error": "不接纳时必须选择或填写原因。"}, 400
            if rejection_reason == "other" and not rejection_note:
                return {"ok": False, "error": "选择其他原因时必须填写具体原因。"}, 400

    results = _raw_task_results(task)
    target = None
    valid_item_ids = set()
    for result in results:
        if str(result.get("code") or "") != result_code:
            continue
        target = result
        valid_item_ids = {item["id"] for item in _result_report_items(result)}
        break
    if target is None or item_id not in valid_item_ids:
        return {"ok": False, "error": "报告条目不存在。"}, 404

    classifications = target.get("item_classifications")
    if not isinstance(classifications, dict):
        classifications = {}
    classifications[item_id] = item_type
    target["item_classifications"] = classifications
    if acceptance_supplied:
        acceptances = target.get("item_acceptances")
        if not isinstance(acceptances, dict):
            acceptances = {}
        if acceptance_status == "pending":
            acceptances.pop(item_id, None)
        else:
            record = {"status": acceptance_status}
            if acceptance_status == "rejected":
                record["rejection_reason"] = rejection_reason
                record["rejection_note"] = rejection_note
            acceptances[item_id] = record
        target["item_acceptances"] = acceptances

    db = get_db()
    db.execute(
        "UPDATE tasks SET result_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(results, ensure_ascii=False), now_text(), task["id"]),
    )
    db.commit()

    prepared = _prepare_task_results(results)
    updated_result = next((item for item in prepared if str(item.get("code") or "") == result_code), None)
    updated_report_item = None
    if updated_result:
        updated_report_item = next(
            (item for item in updated_result.get("report_items", []) if item.get("id") == item_id),
            None,
        )
    return {
        "ok": True,
        "item_id": item_id,
        "item_type": item_type,
        "item_type_label": REPORT_ITEM_TYPES[item_type],
        "acceptance_status": (updated_report_item or {}).get("acceptance_status", "pending"),
        "acceptance_label": (updated_report_item or {}).get("acceptance_label", REPORT_ACCEPTANCE_STATUSES["pending"]),
        "rejection_reason": (updated_report_item or {}).get("rejection_reason", ""),
        "rejection_reason_label": (updated_report_item or {}).get("rejection_reason_label", ""),
        "rejection_note": (updated_report_item or {}).get("rejection_note", ""),
        "result_counts": (updated_result or {}).get("report_counts", {}),
        "totals": _report_item_totals(prepared),
    }


def _export_task_report(task):
    static_folder = current_app.static_folder
    if not static_folder:
        raise RuntimeError("静态资源目录未配置，无法导出报告。")
    app_css = (Path(static_folder) / "app.css").read_text(encoding="utf-8")
    results = _task_results(task)
    html = render_template(
        "task_report_export.html",
        task=task,
        results=results,
        report_totals=_report_item_totals(results),
        report_item_types=REPORT_ITEM_TYPES,
        document_groups=_task_document_groups(task),
        app_css=app_css,
    )
    filename = f"document-check-report-{task['id']}.html"
    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _export_task_report_excel(task):
    results = _task_results(task)
    report_totals = _report_item_totals(results)
    document_groups = _task_document_groups(task)
    workbook = Workbook()
    report_sheet = workbook.active
    report_sheet.title = "报告条目"

    _fill_report_items_sheet(report_sheet, task, results, document_groups)
    _fill_report_totals_sheet(workbook.create_sheet("统计"), report_totals)

    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    output.seek(0)
    filename = f"document-check-report-{task['id']}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype=REPORT_EXPORT_MIMETYPE,
    )


def _fill_report_items_sheet(sheet, task, results: list[dict], document_groups: list[dict]) -> None:
    headers = [
        "任务ID",
        "任务类型",
        "文件名称",
        "检查项",
        "条目",
        *[label for _, label in REPORT_ITEM_FIELDS],
        REPORT_ITEM_TYPE_LABEL,
        "是否接纳",
        "不接纳原因",
        "人工原因",
    ]
    sheet.append(headers)
    context = _excel_task_context(task, document_groups)
    for result in results:
        report_items = result.get("report_items") or []
        if not report_items:
            sheet.append(
                [
                    *context,
                    _excel_cell_text(result.get("name")),
                    "",
                    *["" for _ in REPORT_ITEM_FIELDS],
                    "未拆分",
                    "",
                    "",
                    "",
                ]
            )
            continue
        for item in report_items:
            sheet.append(
                [
                    *context,
                    _excel_cell_text(result.get("name")),
                    f"条目 {item.get('index')}",
                    *[_excel_cell_text(item.get(field)) for field, _ in REPORT_ITEM_FIELDS],
                    _excel_cell_text(item.get("type_label")),
                    _excel_cell_text(item.get("acceptance_label")),
                    _excel_cell_text(item.get("rejection_reason_label")) if item.get("acceptance_status") == "rejected" else "",
                    _excel_cell_text(item.get("rejection_note")) if item.get("acceptance_status") == "rejected" else "",
                ]
            )
    _style_excel_sheet(sheet)


def _fill_report_totals_sheet(sheet, report_totals: dict) -> None:
    sheet.append(["指标", "值"])
    for label, key in REPORT_TOTAL_EXPORT_ROWS:
        sheet.append([label, report_totals.get(key, 0)])
    _style_excel_sheet(sheet)


def _excel_task_context(task, document_groups: list[dict]) -> list:
    return [
        _row_value(task, "id", ""),
        task_type_label(_row_value(task, "task_type", DOCUMENT_TASK_TYPE)),
        _excel_document_names(task, document_groups),
    ]


def _excel_document_names(task, document_groups: list[dict]) -> str:
    names = []
    for group in document_groups:
        for file in group.get("files", []):
            name = str(file.get("original_filename") or "").strip()
            if name:
                names.append(name)
    if names:
        return "\n".join(names)
    return _excel_cell_text(_row_value(task, "original_filename", ""))


def _excel_cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return value
    return str(value).strip()


def _style_excel_sheet(sheet) -> None:
    sheet.freeze_panes = "A2"
    for cell in sheet[1]:
        cell.font = REPORT_EXPORT_HEADER_FONT
        cell.fill = REPORT_EXPORT_HEADER_FILL
        cell.alignment = Alignment(vertical="top", wrap_text=True)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for column_cells in sheet.columns:
        width = max(len(str(cell.value or "")) for cell in column_cells[:200]) + 2
        sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max(width, 10), 60)


def _page_arg() -> int:
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return 1
    return max(1, page)


def _bounded_page(page: int, total: int, per_page: int) -> int:
    pages = max(1, (total + per_page - 1) // per_page)
    return min(max(1, page), pages)


def _pagination(page: int, total: int, per_page: int) -> dict:
    pages = max(1, (total + per_page - 1) // per_page)
    return {
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < pages,
        "prev_page": max(1, page - 1),
        "next_page": min(pages, page + 1),
        "start": 0 if total == 0 else (page - 1) * per_page + 1,
        "end": min(total, page * per_page),
    }


def _task_stats_for_where(where: str, params: tuple) -> dict:
    stats = {"total": 0, "queued": 0, "running": 0, "completed": 0, "failed": 0, "canceled": 0}
    rows = get_db().execute(
        f"SELECT status, COUNT(*) AS total FROM tasks WHERE {where} GROUP BY status",
        params,
    ).fetchall()
    for row in rows:
        count = row["total"]
        stats["total"] += count
        if row["status"] in stats:
            stats[row["status"]] = count
    return stats
