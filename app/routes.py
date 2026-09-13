import hmac
import json
import mimetypes
import uuid
from datetime import date, datetime, timedelta
from functools import wraps
from ipaddress import ip_address
from urllib.parse import urlsplit

from flask import (
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge

from .auth import (
    SAML_USER_SESSION_KEY,
    AuthenticationRequired,
    UserIdentity,
    current_identity,
    subject_label,
)
from .config import save_network_config
from .db import (
    default_check_item_codes,
    delete_task_record,
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
from .file_cleanup import (
    describe_failures,
)
from .images import (
    DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES,
    image_path_from_item,
)
from .limits import (
    DEFAULT_ISSUE_OUTPUT_LIMIT,
    MAX_ISSUE_OUTPUT_LIMIT,
    normalize_issue_output_limit,
)
from .llm import LLMError, normalize_reasoning_effort, test_model_connection
from .model_discovery import ModelDiscoveryError, fetch_models
from .model_service import _find_enabled_model as _find_enabled_model
from .model_service import (
    _model_management_response,
    _model_page_identity,
    _provider_connection_data,
    get_enabled_models,
)
from .network import outbound_network_config
from .reporting.constants import (
    REPORT_ACCEPTANCE_STATUSES,
    REPORT_COUNT_KEYS,
    REPORT_ITEM_FIELDS,
    REPORT_ITEM_TYPE_LABEL,
    REPORT_ITEM_TYPE_ORDER,
    REPORT_ITEM_TYPES,
    REPORT_REJECTION_REASON_HINTS,
    REPORT_REJECTION_REASONS,
    REPORT_REVIEW_FILTERS,
    REPORT_REVIEW_STATUSES,
    REPORT_STATS_BACKGROUND_BATCH_SIZE,
    REPORT_STATS_INLINE_REBUILD_LIMIT,
)
from .reporting.excel import (
    _export_task_report_excel as _export_task_report_excel,
)
from .reporting.excel import (
    _import_task_report_excel as _import_task_report_excel,
)
from .reporting.service import (
    _empty_report_suppression_version as _empty_report_suppression_version,
)
from .reporting.service import (
    _enabled_report_suppression_rules as _enabled_report_suppression_rules,
)
from .reporting.service import (
    _export_task_report as _export_task_report,
)
from .reporting.service import (
    _finalize_report_counts as _finalize_report_counts,
)
from .reporting.service import (
    _parse_result_json as _parse_result_json,
)
from .reporting.service import (
    _prepare_task_results as _prepare_task_results,
)
from .reporting.service import (
    _report_item_fields_for_task as _report_item_fields_for_task,
)
from .reporting.service import (
    _report_item_totals as _report_item_totals,
)
from .reporting.service import (
    _report_suppression_versions as _report_suppression_versions,
)
from .reporting.service import (
    _task_results as _task_results,
)
from .reporting.service import (
    _update_report_item_type as _update_report_item_type,
)
from .reporting.service import (
    _uses_compact_media_report as _uses_compact_media_report,
)
from .reporting.service import (
    _write_task_report_stat_rows as _write_task_report_stat_rows,
)
from .saml import SamlConfigError, create_saml_auth, saml_sp_metadata
from .task_files import (
    UPLOAD_PATH_SAFE_CHARS as UPLOAD_PATH_SAFE_CHARS,
)
from .task_files import (
    _download_task_documents_zip,
    _image_folder,
    _int_setting,
    _remove_empty_directory,
    _remove_uploaded_files,
    _task_document_groups,
    _task_image_items,
    _task_source_files_available,
    _task_upload_path,
    _task_upload_paths,
)
from .task_files import (
    _upload_destination as _upload_destination,
)
from .task_submission import (
    _consistency_task_title,
    _task_list_endpoint,
    create_consistency_task_for_identity,
    create_image_task_for_identity,
    create_language_consistency_task_for_identity,
    create_task_for_identity,
    create_video_task_for_identity,
)
from .task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
    task_type_label,
)
from .tasks import (
    cleanup_task_file_cache,
    retry_check_codes_for_task,
    task_file_cache_snapshot,
    task_file_cache_snapshot_async,
)

STATUS_LABELS = {
    "queued": "排队中",
    "running": "检查中",
    "canceling": "取消中",
    "completed": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "canceled": "已取消",
}
DELETABLE_TASK_STATUSES = {"completed", "partial", "failed", "canceled"}
BULK_DELETABLE_TASK_STATUSES = DELETABLE_TASK_STATUSES | {"queued"}
DEFAULT_TASKS_PER_PAGE = 20
TASKS_PER_PAGE_OPTIONS = (DEFAULT_TASKS_PER_PAGE, 50, 100)
MAX_BULK_DELETE_TASKS = max(TASKS_PER_PAGE_OPTIONS)
CHECK_ITEM_CONCURRENCY_DEFAULT = 1
TASK_FILE_RETENTION_DAYS_DEFAULT = 0
ISSUE_OUTPUT_LIMIT_DEFAULT = DEFAULT_ISSUE_OUTPUT_LIMIT
MODEL_TEST_TIMEOUT_MAX = 60
CONSOLE_USER_ENDPOINTS = {
    "admin_tasks",
    "admin_new_task",
    "admin_consistency",
    "admin_language_consistency",
    "admin_images",
    "admin_videos",
    "admin_models",
}


def register_routes(app):
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

    @app.before_request
    def require_saml_user_session():
        if (
            not _platform_enabled()
            or not _saml_mode_enabled()
            or not _needs_saml_user_session(request.endpoint)
        ):
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
            redirect_url = auth.login(
                return_to=_safe_next_path(request.args.get("next"))
            )
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
            current_app.logger.warning(
                "SAML 回调校验失败：%s", ", ".join(auth.get_errors())
            )
            abort(401, description="SAML 登录失败，请重新从公司统一入口访问。")

        user_id, username = _saml_user_from_response(auth)
        if not user_id:
            abort(
                401, description="SAML 响应缺少用户 ID，请联系管理员检查 SSO 属性映射。"
            )
        session[SAML_USER_SESSION_KEY] = {
            "user_id": user_id,
            "username": username or user_id,
        }
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
        page, per_page, total, rows, stats = _user_task_list_data(
            identity, DOCUMENT_TASK_TYPE
        )
        return render_template(
            "user_tasks.html",
            ip=identity.ip,
            identity=identity,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, per_page),
            check_items=get_enabled_check_items(),
            models=get_enabled_models(identity.subject),
            refresh_url=url_for("user_task_statuses", task_type=DOCUMENT_TASK_TYPE),
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
                return create_consistency_task_for_identity(
                    current_identity(), admin_created=True
                )
            return _render_admin_consistency_page()

        identity = _current_user_identity()
        if request.method == "POST":
            return create_consistency_task_for_identity(identity, admin_created=False)

        page, per_page, total, rows, stats = _user_task_list_data(
            identity, CONSISTENCY_TASK_TYPE
        )
        return render_template(
            "user_consistency.html",
            ip=identity.ip,
            identity=identity,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, per_page),
            check_items=get_enabled_check_items(CONSISTENCY_TASK_TYPE),
            models=get_enabled_models(identity.subject),
            refresh_url=url_for("user_task_statuses", task_type=CONSISTENCY_TASK_TYPE),
            active_nav=CONSISTENCY_TASK_TYPE,
        )

    @app.route("/language-consistency", methods=["GET", "POST"])
    def user_language_consistency():
        if not _platform_enabled():
            if request.method == "POST":
                return create_language_consistency_task_for_identity(
                    current_identity(), admin_created=True
                )
            return _render_admin_language_consistency_page()

        identity = _current_user_identity()
        if request.method == "POST":
            return create_language_consistency_task_for_identity(
                identity, admin_created=False
            )

        page, per_page, total, rows, stats = _user_task_list_data(
            identity, LANGUAGE_CONSISTENCY_TASK_TYPE
        )
        return render_template(
            "user_language_consistency.html",
            ip=identity.ip,
            identity=identity,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, per_page),
            check_items=get_enabled_check_items(LANGUAGE_CONSISTENCY_TASK_TYPE),
            models=get_enabled_models(identity.subject),
            submission_token=uuid.uuid4().hex,
            refresh_url=url_for(
                "user_task_statuses", task_type=LANGUAGE_CONSISTENCY_TASK_TYPE
            ),
            active_nav=LANGUAGE_CONSISTENCY_TASK_TYPE,
        )

    @app.route("/images", methods=["GET", "POST"])
    def user_images():
        if not _platform_enabled():
            if request.method == "POST":
                return create_image_task_for_identity(
                    current_identity(), admin_created=True
                )
            return _render_admin_images_page()

        identity = _current_user_identity()
        if request.method == "POST":
            return create_image_task_for_identity(identity, admin_created=False)

        page, per_page, total, rows, stats = _user_task_list_data(
            identity, IMAGE_TASK_TYPE
        )
        return render_template(
            "user_images.html",
            ip=identity.ip,
            identity=identity,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, per_page),
            check_items=get_enabled_check_items(IMAGE_TASK_TYPE),
            models=get_enabled_models(identity.subject),
            refresh_url=url_for("user_task_statuses", task_type=IMAGE_TASK_TYPE),
            active_nav=IMAGE_TASK_TYPE,
        )

    @app.route("/videos", methods=["GET", "POST"])
    def user_videos():
        if not _platform_enabled():
            if request.method == "POST":
                return create_video_task_for_identity(
                    current_identity(), admin_created=True
                )
            return _render_admin_videos_page()

        identity = _current_user_identity()
        if request.method == "POST":
            return create_video_task_for_identity(identity, admin_created=False)

        page, per_page, total, rows, stats = _user_task_list_data(
            identity, VIDEO_TASK_TYPE
        )
        return render_template(
            "user_videos.html",
            ip=identity.ip,
            identity=identity,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, per_page),
            check_items=get_enabled_check_items(VIDEO_TASK_TYPE),
            models=get_enabled_models(identity.subject),
            refresh_url=url_for("user_task_statuses", task_type=VIDEO_TASK_TYPE),
            active_nav=VIDEO_TASK_TYPE,
        )

    @app.get("/task-statuses")
    def user_task_statuses():
        identity = _current_user_identity()
        task_type = _validated_task_status_type()
        if task_type is None:
            return {"error": "任务类型无效。"}, 400
        return _task_status_payload(
            task_type,
            owner_clause="t.owner_subject = ?",
            owner_params=(identity.subject,),
        )

    @app.get("/tasks/<int:task_id>")
    def user_task_detail(task_id):
        task = _get_user_task_or_local_admin(task_id)
        results = _task_results(task)
        _attach_report_media_urls(results, task, "user_task_media")
        back_endpoint = _task_list_endpoint(not _platform_enabled(), task["task_type"])
        return render_template(
            "task_detail.html",
            mode="admin" if not _platform_enabled() else "user",
            task=task,
            results=results,
            report_totals=_report_item_totals(results),
            report_item_types=REPORT_ITEM_TYPES,
            report_item_fields=_report_item_fields_for_task(task["task_type"]),
            media_report=_uses_compact_media_report(task["task_type"]),
            video_report=(task["task_type"] or DOCUMENT_TASK_TYPE) == VIDEO_TASK_TYPE,
            video_stream_url=_task_video_stream_url(task, "user_task_video"),
            report_classification_url=url_for(
                "admin_update_report_item_type"
                if not _platform_enabled()
                else "user_update_report_item_type",
                task_id=task_id,
            ),
            document_groups=_task_document_groups(task),
            active_nav=task["task_type"] or DOCUMENT_TASK_TYPE,
            back_url=_safe_next_path(request.args.get("next"), url_for(back_endpoint)),
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

    @app.post("/tasks/<int:task_id>/import.xlsx")
    def user_import_task_excel(task_id):
        task = _get_user_task_or_local_admin(task_id)
        return _import_task_report_excel(task, "user_task_detail")

    @app.get("/tasks/<int:task_id>/document")
    def user_download_task_document(task_id):
        task = _get_user_task_or_local_admin(task_id)
        return _download_task_document(task, "user_task_detail")

    @app.get("/tasks/<int:task_id>/media/<media_id>")
    def user_task_media(task_id, media_id):
        task = _get_user_task_or_local_admin(task_id)
        return _send_task_media(task, media_id)

    @app.get("/tasks/<int:task_id>/video")
    def user_task_video(task_id):
        task = _get_user_task_or_local_admin(task_id)
        return _stream_task_video(task)

    @app.post("/tasks/<int:task_id>/cancel")
    def user_cancel_task(task_id):
        task = _get_user_task_or_local_admin(task_id)
        _cancel_task(task)
        flash("已提交取消请求。", "success")
        return redirect(_task_action_redirect("user_tasks"))

    @app.post("/tasks/<int:task_id>/retry")
    def user_retry_task(task_id):
        task = _get_user_task(task_id)
        _retry_task(task)
        return redirect(
            _task_action_redirect(_task_list_endpoint(False, task["task_type"]))
        )

    @app.post("/tasks/<int:task_id>/delete")
    def user_delete_task(task_id):
        task = _get_user_task_or_local_admin(task_id)
        if _delete_task(task):
            flash("任务已删除。", "success")
        return redirect(url_for(_task_list_endpoint(False, task["task_type"])))

    @app.post("/tasks/bulk-delete")
    def user_bulk_delete_tasks():
        return _bulk_delete_tasks(_get_user_task_or_local_admin, admin_created=False)

    @app.route("/models", methods=["GET", "POST"])
    def user_models():
        return _model_management_response(_model_page_identity(), "user_models")

    @app.post("/models/fetch")
    def user_fetch_models():
        _model_page_identity()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return {"error": "请求数据格式不正确。"}, 400
        provider_data = _provider_connection_data(data, "模型拉取")
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
        provider_data = _provider_connection_data(data, "模型测试")
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
                request_timeout=min(
                    provider_data["request_timeout"], MODEL_TEST_TIMEOUT_MAX
                ),
                model_name=model_name,
                reasoning_effort=normalize_reasoning_effort(
                    data.get("reasoning_effort")
                ),
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
            ok = hmac.compare_digest(
                username, current_app.config["ADMIN_USERNAME"]
            ) and hmac.compare_digest(password, current_app.config["ADMIN_PASSWORD"])
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
        overview = _admin_overview_data(
            selected_range["start_at"], selected_range["end_at"]
        )
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
            return create_task_for_identity(
                _console_user_identity(), admin_created=True
            )
        return _render_admin_tasks_page()

    @app.route(f"{admin_prefix}/tasks/new", methods=["GET", "POST"])
    @admin_required
    def admin_new_task():
        if request.method == "POST":
            return create_task_for_identity(
                _console_user_identity(), admin_created=True
            )
        return redirect(url_for("admin_tasks"))

    @app.get(f"{admin_prefix}/task-statuses")
    @admin_required
    def admin_task_statuses():
        task_type = _validated_task_status_type()
        if task_type is None:
            return {"error": "任务类型无效。"}, 400
        mode_clause, mode_params = _mode_subject_filter("t")
        return _task_status_payload(
            task_type, owner_clause=mode_clause, owner_params=mode_params
        )

    @app.route(f"{admin_prefix}/consistency", methods=["GET", "POST"])
    @admin_required
    def admin_consistency():
        if request.method == "POST":
            return create_consistency_task_for_identity(
                _console_user_identity(), admin_created=True
            )
        return _render_admin_consistency_page()

    @app.route(f"{admin_prefix}/language-consistency", methods=["GET", "POST"])
    @admin_required
    def admin_language_consistency():
        if request.method == "POST":
            return create_language_consistency_task_for_identity(
                _console_user_identity(), admin_created=True
            )
        return _render_admin_language_consistency_page()

    @app.route(f"{admin_prefix}/images", methods=["GET", "POST"])
    @admin_required
    def admin_images():
        if request.method == "POST":
            return create_image_task_for_identity(
                _console_user_identity(), admin_created=True
            )
        return _render_admin_images_page()

    @app.route(f"{admin_prefix}/videos", methods=["GET", "POST"])
    @admin_required
    def admin_videos():
        if request.method == "POST":
            return create_video_task_for_identity(
                _console_user_identity(), admin_created=True
            )
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
        _attach_report_media_urls(results, task, "admin_task_media")
        back_endpoint = _task_list_endpoint(True, task["task_type"])
        return render_template(
            "task_detail.html",
            mode="admin",
            task=task,
            results=results,
            report_totals=_report_item_totals(results),
            report_item_types=REPORT_ITEM_TYPES,
            report_item_fields=_report_item_fields_for_task(task["task_type"]),
            media_report=_uses_compact_media_report(task["task_type"]),
            video_report=(task["task_type"] or DOCUMENT_TASK_TYPE) == VIDEO_TASK_TYPE,
            video_stream_url=_task_video_stream_url(task, "admin_task_video"),
            report_classification_url=url_for(
                "admin_update_report_item_type", task_id=task_id
            ),
            document_groups=_task_document_groups(task),
            active_nav=task["task_type"] or DOCUMENT_TASK_TYPE,
            back_url=_safe_next_path(request.args.get("next"), url_for(back_endpoint)),
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

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/import.xlsx")
    @admin_required
    def admin_import_task_excel(task_id):
        task = _get_task_or_404(task_id)
        return _import_task_report_excel(task, "admin_task_detail")

    @app.get(f"{admin_prefix}/tasks/<int:task_id>/document")
    @admin_required
    def admin_download_task_document(task_id):
        task = _get_task_or_404(task_id)
        return _download_task_document(task, "admin_task_detail")

    @app.get(f"{admin_prefix}/tasks/<int:task_id>/media/<media_id>")
    @admin_required
    def admin_task_media(task_id, media_id):
        task = _get_task_or_404(task_id)
        return _send_task_media(task, media_id)

    @app.get(f"{admin_prefix}/tasks/<int:task_id>/video")
    @admin_required
    def admin_task_video(task_id):
        task = _get_task_or_404(task_id)
        return _stream_task_video(task)

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/cancel")
    @admin_required
    def admin_cancel_task(task_id):
        task = _get_task_or_404(task_id)
        _cancel_task(task)
        flash("已提交取消请求。", "success")
        return redirect(_task_action_redirect("admin_tasks"))

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/retry")
    @admin_required
    def admin_retry_task(task_id):
        task = _get_task_or_404(task_id)
        _retry_task(task)
        return redirect(
            _task_action_redirect(_task_list_endpoint(True, task["task_type"]))
        )

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/delete")
    @admin_required
    def admin_delete_task(task_id):
        task = _get_task_or_404(task_id)
        if _delete_task(task):
            flash("任务已删除。", "success")
        return redirect(url_for(_task_list_endpoint(True, task["task_type"])))

    @app.post(f"{admin_prefix}/tasks/bulk-delete")
    @admin_required
    def admin_bulk_delete_tasks():
        return _bulk_delete_tasks(_get_task_or_404, admin_created=True)

    @app.route(f"{admin_prefix}/prompts", methods=["GET", "POST"])
    @admin_required
    def admin_prompts():
        return redirect(url_for("admin_settings"))

    @app.get(f"{admin_prefix}/settings/task-cache")
    @admin_required
    def admin_task_file_cache():
        app = current_app._get_current_object()
        if request.args.get("background") == "1":
            snapshot, ready = task_file_cache_snapshot_async(app)
        else:
            snapshot = task_file_cache_snapshot(app)
            ready = True
        items = []
        for item in snapshot["items"]:
            row = dict(item)
            if row["task_type"] in {
                CONSISTENCY_TASK_TYPE,
                LANGUAGE_CONSISTENCY_TASK_TYPE,
            }:
                row["title"] = _consistency_task_title(row)
            else:
                row["title"] = str(row["original_filename"] or f"任务 #{row['id']}")
            row["task_type_label"] = task_type_label(row["task_type"])
            row["report_url"] = url_for("admin_task_detail", task_id=row["id"])
            row.pop("document_meta_json", None)
            items.append(row)
        snapshot["items"] = items
        snapshot["ready"] = ready
        return snapshot

    @app.post(f"{admin_prefix}/settings/task-cache/cleanup")
    @admin_required
    def admin_cleanup_task_file_cache():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("task_ids"), list):
            return {"ok": False, "error": "请选择需要清理的任务。"}, 400
        task_ids = []
        for value in data["task_ids"]:
            try:
                task_id = int(value)
            except (TypeError, ValueError):
                continue
            if task_id > 0 and task_id not in task_ids:
                task_ids.append(task_id)
        if not task_ids:
            return {"ok": False, "error": "请选择需要清理的任务。"}, 400
        return {"ok": True, **cleanup_task_file_cache(current_app, task_ids)}

    @app.route(f"{admin_prefix}/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings():
        db = get_db()
        if request.method == "POST":
            action = request.form.get("action", "concurrency")
            if action == "concurrency":
                try:
                    global_concurrency = int(
                        request.form.get("global_concurrency", "3")
                    )
                    user_concurrency = max(
                        1, int(request.form.get("user_concurrency", "1"))
                    )
                    check_item_concurrency = max(
                        1,
                        int(
                            request.form.get(
                                "check_item_concurrency",
                                str(CHECK_ITEM_CONCURRENCY_DEFAULT),
                            )
                        ),
                    )
                    image_page_check_max_pages = max(
                        1,
                        int(
                            request.form.get(
                                "image_page_check_max_pages",
                                str(DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES),
                            )
                        ),
                    )
                    issue_output_limit = normalize_issue_output_limit(
                        int(
                            request.form.get(
                                "issue_output_limit", str(ISSUE_OUTPUT_LIMIT_DEFAULT)
                            )
                        )
                    )
                    task_file_retention_days = max(
                        0,
                        int(
                            request.form.get(
                                "task_file_retention_days",
                                str(TASK_FILE_RETENTION_DAYS_DEFAULT),
                            )
                        ),
                    )
                except ValueError:
                    flash(
                        "任务设置必须是整数，任务文件保留天数可为 0，其余必须为正整数。",
                        "error",
                    )
                    return redirect(url_for("admin_settings"))
                max_task_processes = _max_task_processes()
                if not 1 <= global_concurrency <= max_task_processes:
                    flash(
                        f"系统同时执行任务数必须在 1 到 {max_task_processes} 之间。",
                        "error",
                    )
                    return redirect(url_for("admin_settings"))
                set_setting("global_concurrency", global_concurrency)
                set_setting("user_concurrency", user_concurrency)
                set_setting("check_item_concurrency", check_item_concurrency)
                set_setting("image_page_check_max_pages", image_page_check_max_pages)
                set_setting("issue_output_limit", issue_output_limit)
                set_setting("task_file_retention_days", task_file_retention_days)
                flash("任务设置已保存。", "success")
                return redirect(url_for("admin_settings"))

            if action == "diagnostics":
                llm_stream_trace_enabled = (
                    request.form.get("llm_stream_trace_enabled") == "on"
                )
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
                flash(
                    "IP 用户名已保存。" if username else "IP 用户名已清除。", "success"
                )
                return redirect(url_for("admin_settings", tab="ip_users"))

            if action == "report_suppression_rule":
                rule_id = request.form.get("rule_id", "")
                operation = request.form.get("operation", "")
                if not rule_id.isdigit():
                    flash("误报忽略规则不存在。", "error")
                    return _report_suppression_rules_redirect()
                if operation == "enable":
                    db.execute(
                        "UPDATE report_suppression_rules SET enabled = 1, updated_at = ? WHERE id = ?",
                        (now_text(), int(rule_id)),
                    )
                    db.commit()
                    flash("误报忽略规则已启用。", "success")
                    return _report_suppression_rules_redirect()
                if operation == "disable":
                    db.execute(
                        "UPDATE report_suppression_rules SET enabled = 0, updated_at = ? WHERE id = ?",
                        (now_text(), int(rule_id)),
                    )
                    db.commit()
                    flash("误报忽略规则已停用。", "success")
                    return _report_suppression_rules_redirect()
                if operation == "delete":
                    db.execute(
                        "DELETE FROM report_suppression_rules WHERE id = ?",
                        (int(rule_id),),
                    )
                    db.commit()
                    flash("误报忽略规则已删除。", "success")
                    return _report_suppression_rules_redirect()
                flash("未知误报忽略规则操作。", "error")
                return _report_suppression_rules_redirect()

            if action == "create_check_item":
                task_type = _check_item_task_type(request.form.get("task_type"))
                name = request.form.get("name", "").strip()
                description = request.form.get("description", "").strip()
                prompt = request.form.get("prompt", "").strip()
                enabled = 1 if request.form.get("enabled") == "on" else 0
                if not name or not prompt:
                    if _wants_json_response():
                        return {
                            "ok": False,
                            "error": "检查项名称和提示词不能为空。",
                        }, 400
                    flash("检查项名称和提示词不能为空。", "error")
                    return redirect(url_for("admin_settings"))
                now = now_text()
                cursor = db.execute(
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
                if _wants_json_response():
                    item = db.execute(
                        "SELECT * FROM check_items WHERE id = ?", (cursor.lastrowid,)
                    ).fetchone()
                    return {
                        "ok": True,
                        "message": "扩展检查项已创建，可继续添加。",
                        "item_id": int(cursor.lastrowid),
                        "html": render_template(
                            "_check_item_rows.html", item=item, is_builtin=False
                        ),
                    }
                flash("扩展检查项已创建。", "success")
                return redirect(url_for("admin_settings"))

            if action == "reorder_check_items":
                task_type = _check_item_task_type(request.form.get("task_type"))
                item_ids = [
                    int(value)
                    for value in request.form.getlist("item_ids")
                    if value.isdigit()
                ]
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
                    if _wants_json_response():
                        return {"ok": False, "error": "检查项不存在，无法删除。"}, 400
                    flash("检查项不存在，无法删除。", "error")
                    return redirect(url_for("admin_settings"))
                item = db.execute(
                    "SELECT code FROM check_items WHERE id = ?", (item_id,)
                ).fetchone()
                if item is None:
                    if _wants_json_response():
                        return {"ok": False, "error": "检查项不存在，无法删除。"}, 404
                    flash("检查项不存在，无法删除。", "error")
                    return redirect(url_for("admin_settings"))
                if item["code"] in default_check_item_codes():
                    if _wants_json_response():
                        return {"ok": False, "error": "内置检查项不能删除。"}, 400
                    flash("内置检查项不能删除。", "error")
                    return redirect(url_for("admin_settings"))
                db.execute("DELETE FROM check_items WHERE id = ?", (item_id,))
                db.commit()
                if _wants_json_response():
                    return {
                        "ok": True,
                        "message": "扩展检查项已删除。",
                        "item_id": int(item_id),
                    }
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
            if (
                db.execute(
                    "SELECT 1 FROM check_items WHERE id = ?", (item_id,)
                ).fetchone()
                is None
            ):
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
        language_consistency_check_items = _check_items_for_task_type(
            db, LANGUAGE_CONSISTENCY_TASK_TYPE
        )
        image_check_items = _check_items_for_task_type(db, IMAGE_TASK_TYPE)
        video_check_items = _check_items_for_task_type(db, VIDEO_TASK_TYPE)
        settings_tab = _settings_tab()
        report_suppression_keyword, report_suppression_status = (
            _report_suppression_filter_values(request.args)
        )
        return render_template(
            "admin_settings.html",
            check_item_groups=[
                {
                    "task_type": DOCUMENT_TASK_TYPE,
                    "tab_title": "单文档检查",
                    "title": "单文档检查提示词",
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
                    "tab_title": "多文档对照",
                    "title": "多文档对照提示词",
                    "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交多文档对照任务时可多选。",
                    "new_title": "新增多文档对照项",
                    "name_placeholder": "例如：关键参数一致性检查",
                    "description_placeholder": "用于说明该多文档对照项的比对范围",
                    "prompt_placeholder": "描述素材与资料的比对规则、关注范围和输出要求",
                    "items": consistency_check_items,
                    "default_check_codes": default_check_item_codes(
                        CONSISTENCY_TASK_TYPE
                    ),
                },
                {
                    "task_type": LANGUAGE_CONSISTENCY_TASK_TYPE,
                    "tab_title": "跨语种检查",
                    "title": "跨语种检查提示词",
                    "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交跨语种检查任务时可多选。",
                    "new_title": "新增跨语种检查项",
                    "name_placeholder": "例如：翻译缺失与事实差异检查",
                    "description_placeholder": "用于说明该跨语种检查项的范围",
                    "prompt_placeholder": "描述两份不同语种文档的比对规则、关注范围和中文输出要求",
                    "items": language_consistency_check_items,
                    "default_check_codes": default_check_item_codes(
                        LANGUAGE_CONSISTENCY_TASK_TYPE
                    ),
                },
                {
                    "task_type": IMAGE_TASK_TYPE,
                    "tab_title": "图片检查",
                    "title": "图片检查提示词",
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
                    "tab_title": "视频检查",
                    "title": "视频检查提示词",
                    "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交视频检查任务时可多选。",
                    "new_title": "新增视频检查项",
                    "name_placeholder": "例如：安装力矩与工具使用检查",
                    "description_placeholder": "用于说明该视频检查项的范围",
                    "prompt_placeholder": "描述视频质检角色、关注范围、判断规则和输出要求",
                    "items": video_check_items,
                    "default_check_codes": default_check_item_codes(VIDEO_TASK_TYPE),
                },
            ],
            global_concurrency=min(
                _max_task_processes(),
                max(1, _int_setting("global_concurrency", 3)),
            ),
            max_task_processes=_max_task_processes(),
            user_concurrency=get_setting("user_concurrency", 1),
            check_item_concurrency=get_setting(
                "check_item_concurrency", CHECK_ITEM_CONCURRENCY_DEFAULT
            ),
            image_page_check_max_pages=get_setting(
                "image_page_check_max_pages", DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES
            ),
            issue_output_limit=get_setting(
                "issue_output_limit", ISSUE_OUTPUT_LIMIT_DEFAULT
            ),
            max_issue_output_limit=MAX_ISSUE_OUTPUT_LIMIT,
            task_file_retention_days=get_setting(
                "task_file_retention_days", TASK_FILE_RETENTION_DAYS_DEFAULT
            ),
            network=current_app.config["NETWORK"],
            llm_stream_trace_enabled=get_bool_setting(
                "llm_stream_trace_enabled", False
            ),
            settings_tab=settings_tab,
            ip_username_management_enabled=_ip_username_management_enabled(),
            ip_username_rows=_ip_username_rows()
            if _ip_username_management_enabled()
            else [],
            report_suppression_rules=_report_suppression_rule_rows(),
            report_suppression_keyword=report_suppression_keyword,
            report_suppression_status=report_suppression_status,
        )


def _identity_label(identity: UserIdentity) -> str:
    if identity.display_name:
        return f"{identity.subject}-{identity.display_name}"
    return identity.label


def _wants_json_response() -> bool:
    return (
        request.headers.get("X-Requested-With") == "fetch"
        or request.accept_mimetypes.best == "application/json"
    )


def _report_suppression_filter_values(values) -> tuple[str, str]:
    keyword = str(values.get("rule_keyword") or "").strip()[:200]
    status = str(values.get("rule_status") or "").strip()
    if status not in {"candidate", "enabled"}:
        status = ""
    return keyword, status


def _report_suppression_rules_redirect():
    keyword, status = _report_suppression_filter_values(request.form)
    url_values = {"_anchor": "report-suppression-rules"}
    if keyword:
        url_values["rule_keyword"] = keyword
    if status:
        url_values["rule_status"] = status
    return redirect(url_for("admin_settings", **url_values))


def _report_suppression_rule_rows() -> list[dict]:
    rows = (
        get_db()
        .execute(
            """
        SELECT r.*, c.name AS check_name
        FROM report_suppression_rules r
        LEFT JOIN check_items c ON c.code = r.check_code
        ORDER BY r.enabled ASC, r.updated_at DESC, r.id DESC
        """
        )
        .fetchall()
    )
    result = []
    for row in rows:
        snapshot = _parse_report_suppression_item_json(row["item_json"])
        result.append(
            {
                "id": row["id"],
                "enabled": bool(row["enabled"]),
                "task_type": row["task_type"],
                "task_type_label": task_type_label(row["task_type"]),
                "check_code": row["check_code"],
                "check_name": row["check_name"] or row["check_code"],
                "reason": row["reason"] or "",
                "hit_count": row["hit_count"],
                "last_hit_at": row["last_hit_at"] or "",
                "source_task_id": row["source_task_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "item": snapshot,
            }
        )
    return result


def _parse_report_suppression_item_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _form_bool(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _platform_enabled() -> bool:
    return bool(current_app.config.get("PLATFORM", True))


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
    return f"instr({_owner_subject_expr(table_alias)}, ?) = 1", (
        _mode_subject_prefix(),
    )


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
    return (
        get_db()
        .execute(
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
        )
        .fetchall()
    )


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
    return isinstance(saml_user, dict) and bool(
        str(saml_user.get("user_id") or "").strip()
    )


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


def _saml_user_from_response(auth) -> tuple[str, str]:
    saml_config = current_app.config.get("AUTH", {}).get("saml", {})
    user_id_attribute = str(saml_config.get("user_id_attribute") or "").strip()
    username_attribute = str(saml_config.get("username_attribute") or "").strip()
    attributes = auth.get_attributes() or {}
    friendly_attributes = (
        getattr(auth, "get_friendlyname_attributes", lambda: {})() or {}
    )

    if user_id_attribute:
        user_id = _saml_attribute_value(
            attributes, user_id_attribute
        ) or _saml_attribute_value(friendly_attributes, user_id_attribute)
    else:
        user_id = str(auth.get_nameid() or "").strip()
    username = ""
    if username_attribute:
        username = _saml_attribute_value(
            attributes, username_attribute
        ) or _saml_attribute_value(friendly_attributes, username_attribute)
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
        current_ip_username = _row_value(task, "current_ip_username")
        if current_ip_username:
            return str(current_ip_username)
        if not _row_value(task, "ip_username_lookup_complete", False):
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


def _user_task_list_data(identity: UserIdentity, task_type: str):
    page = _page_arg()
    per_page = _per_page_arg()
    owner_clause = "t.owner_subject = ?"
    params = (identity.subject, task_type)
    total = (
        get_db()
        .execute(
            f"SELECT COUNT(*) AS total FROM tasks t WHERE {owner_clause} AND t.task_type = ?",
            params,
        )
        .fetchone()["total"]
    )
    page = _bounded_page(page, total, per_page)
    rows = (
        get_db()
        .execute(
            f"""
        SELECT t.id, t.task_type, t.ip,
               t.original_filename, t.stored_filename, t.file_type, t.file_size,
               t.provider_name, t.model_name, t.status, t.progress,
               t.created_at, t.document_meta_json, t.source_files_cleaned_at
        FROM tasks t
        WHERE {owner_clause} AND t.task_type = ?
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT ? OFFSET ?
        """,
            (*params, per_page, (page - 1) * per_page),
        )
        .fetchall()
    )
    rows = _task_rows_with_review_progress(rows)
    stats = _task_stats_for_where(
        "owner_subject = ? AND task_type = ?",
        params,
    )
    return page, per_page, total, rows, stats


def _validated_task_status_type() -> str | None:
    task_type = str(request.args.get("task_type") or DOCUMENT_TASK_TYPE)
    allowed_task_types = {
        DOCUMENT_TASK_TYPE,
        CONSISTENCY_TASK_TYPE,
        LANGUAGE_CONSISTENCY_TASK_TYPE,
        IMAGE_TASK_TYPE,
        VIDEO_TASK_TYPE,
    }
    return task_type if task_type in allowed_task_types else None


def _task_status_payload(task_type: str, *, owner_clause: str, owner_params: tuple):
    task_ids = []
    for value in str(request.args.get("ids") or "").split(","):
        value = value.strip()
        if value.isdigit():
            task_ids.append(int(value))
    task_ids = list(dict.fromkeys(task_ids))[:100]
    rows = []
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        rows = (
            get_db()
            .execute(
                f"""
            SELECT
                t.id, t.status, t.progress, t.updated_at,
                CASE WHEN t.result_json IS NOT NULL AND t.result_json != '' THEN 1 ELSE 0 END AS has_result,
                s.source_updated_at, s.suppression_version,
                s.issue_count AS issue,
                s.suggestion_count AS suggestion,
                s.non_issue_count AS non_issue,
                s.reviewed_item_count AS reviewed,
                s.pending_review_item_count AS pending_review
            FROM tasks t
            LEFT JOIN task_report_stats s ON s.task_id = t.id
            WHERE t.task_type = ? AND {owner_clause} AND t.id IN ({placeholders})
            """,
                (task_type, *owner_params, *task_ids),
            )
            .fetchall()
        )
    suppression_version = _report_suppression_versions({task_type}).get(
        task_type, _empty_report_suppression_version()
    )
    counts = (
        get_db()
        .execute(
            f"""
        SELECT
            COUNT(*) AS tasks,
            COALESCE(SUM(CASE WHEN t.status = 'queued' THEN 1 ELSE 0 END), 0) AS queued,
            COALESCE(SUM(CASE WHEN t.status IN ('running', 'canceling') THEN 1 ELSE 0 END), 0) AS running,
            COALESCE(SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END), 0) AS completed,
            COALESCE(SUM(CASE WHEN t.status = 'partial' THEN 1 ELSE 0 END), 0) AS partial
        FROM tasks t
        WHERE t.task_type = ? AND {owner_clause}
        """,
            (task_type, *owner_params),
        )
        .fetchone()
    )
    return {
        "active": bool(counts["queued"] or counts["running"]),
        "counts": {
            key: int(counts[key] or 0)
            for key in ("tasks", "queued", "running", "completed", "partial")
        },
        "tasks": [_task_status_payload_row(row, suppression_version) for row in rows],
    }


def _task_status_payload_row(row, suppression_version: str) -> dict:
    status = str(row["status"] or "")
    payload = {
        "id": row["id"],
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "progress": int(row["progress"] or 0),
    }
    if status in {"queued", "running", "canceling"}:
        review = _task_review_progress(status, 0, 0, 0)
    elif not row["has_result"]:
        review = _task_review_progress(status, 0, 0, 0)
    elif (
        row["source_updated_at"] != row["updated_at"]
        or row["suppression_version"] != suppression_version
    ):
        review = {"review_key": "stale"}
    else:
        review = _task_review_progress(
            status,
            sum(int(_row_value(row, key, 0) or 0) for key in REPORT_ITEM_TYPE_ORDER),
            int(_row_value(row, "reviewed", 0) or 0),
            int(_row_value(row, "pending_review", 0) or 0),
        )
    payload.update(review)
    return payload


def _task_rows_with_review_progress(rows: list) -> list[dict]:
    if not rows:
        return []
    task_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in task_ids)
    stat_rows = _task_report_stat_rows_for_where(
        f"t.id IN ({placeholders})",
        tuple(task_ids),
    )
    stats_by_task = {int(row["id"]): row for row in stat_rows}
    prepared_rows = []
    for row in rows:
        task = dict(row)
        stats = stats_by_task.get(int(row["id"]))
        total = sum(
            int(_row_value(stats, key, 0) or 0) for key in REPORT_ITEM_TYPE_ORDER
        )
        reviewed = int(_row_value(stats, "reviewed", 0) or 0)
        pending_review = int(_row_value(stats, "pending_review", 0) or 0)
        task.update(
            _task_review_progress(
                str(task.get("status") or ""), total, reviewed, pending_review
            )
        )
        prepared_rows.append(task)
    return prepared_rows


def _task_review_progress(
    task_status: str, total: int, reviewed: int, pending_review: int
) -> dict:
    total = max(0, int(total or 0))
    reviewed = min(total, max(0, int(reviewed or 0)))
    pending_review = min(total, max(0, int(pending_review or 0)))
    if task_status in {"queued", "running", "canceling"}:
        review_status = "unavailable"
    elif total <= 0:
        review_status = "empty"
    elif pending_review <= 0 or reviewed >= total:
        review_status = "completed"
    elif reviewed <= 0:
        review_status = "pending"
    else:
        review_status = "in_progress"
    return {
        "report_item_count": total,
        "reviewed_item_count": reviewed,
        "pending_review_item_count": pending_review,
        "review_status": review_status,
        "review_status_label": REPORT_REVIEW_STATUSES[review_status],
        "review_key": f"{review_status}:{reviewed}:{total}",
    }


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


def _render_admin_task_list(
    *, task_type: str, template_name: str, totals_task_type: str, check_items
):
    identity = _console_user_identity()
    status = request.args.get("status", "")
    review_status = str(request.args.get("review_status") or "").strip()
    if review_status not in REPORT_REVIEW_FILTERS:
        review_status = ""
    keyword = request.args.get("keyword")
    if keyword is None:
        keyword = request.args.get("owner", request.args.get("ip", ""))
    keyword = keyword.strip()
    page = _page_arg()
    per_page = _per_page_arg()
    params = []
    clauses = []
    totals = _admin_totals(totals_task_type)
    join_ip_usernames = _auth_mode() == "ip"
    ip_username_join = (
        "LEFT JOIN ip_usernames iu ON iu.ip = t.ip" if join_ip_usernames else ""
    )
    report_stats_join = "LEFT JOIN task_report_stats trs ON trs.task_id = t.id"
    owner_name_expr = (
        "COALESCE(NULLIF(iu.username, ''), NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '')"
        if join_ip_usernames
        else "COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '')"
    )
    current_ip_username_expr = (
        "COALESCE(iu.username, '')" if join_ip_usernames else "''"
    )
    mode_clause, mode_params = _mode_subject_filter("t")
    clauses.append(mode_clause)
    params.extend(mode_params)
    if status:
        clauses.append("t.status = ?")
        params.append(status)
    report_total_expr = "(COALESCE(trs.issue_count, 0) + COALESCE(trs.suggestion_count, 0) + COALESCE(trs.non_issue_count, 0))"
    if review_status == "pending":
        clauses.append(
            f"{report_total_expr} > 0 AND COALESCE(trs.reviewed_item_count, 0) = 0"
        )
    elif review_status == "in_progress":
        clauses.append(
            f"{report_total_expr} > 0 AND COALESCE(trs.reviewed_item_count, 0) > 0 "
            "AND COALESCE(trs.pending_review_item_count, 0) > 0"
        )
    elif review_status == "completed":
        clauses.append(
            f"{report_total_expr} > 0 AND COALESCE(trs.pending_review_item_count, 0) = 0"
        )
    elif review_status == "empty":
        clauses.append(
            f"t.status NOT IN ('queued', 'running', 'canceling') AND {report_total_expr} = 0"
        )
    if keyword:
        owner_name_filter = (
            "OR COALESCE(iu.username, '') LIKE ?" if join_ip_usernames else ""
        )
        clauses.append(
            f"""
            (
                COALESCE(t.original_filename, '') LIKE ?
                OR COALESCE(t.document_meta_json, '') LIKE ?
                OR COALESCE(t.owner_subject, 'ip:' || t.ip) LIKE ?
                OR t.ip LIKE ?
                OR COALESCE(t.owner_name_snapshot, t.username_snapshot, '') LIKE ?
                {owner_name_filter}
            )
            """
        )
        keyword_like = f"%{keyword}%"
        params.extend(
            [keyword_like, keyword_like, keyword_like, keyword_like, keyword_like]
        )
        if join_ip_usernames:
            params.append(keyword_like)
    clauses.append("t.task_type = ?")
    params.append(task_type)
    where = f"WHERE {' AND '.join(clauses)}"
    total = (
        get_db()
        .execute(
            f"""
        SELECT COUNT(*) AS total
        FROM tasks t
        {ip_username_join}
        {report_stats_join}
        {where}
        """,
            tuple(params),
        )
        .fetchone()["total"]
    )
    page = _bounded_page(page, total, per_page)
    rows = (
        get_db()
        .execute(
            f"""
        SELECT
               t.id, t.task_type, t.ip, t.username_snapshot,
               t.owner_subject, t.owner_name_snapshot, t.owner_source,
               t.original_filename, t.stored_filename, t.file_type, t.file_size,
               t.provider_name, t.model_name, t.status, t.progress,
               t.created_at, t.document_meta_json,
               {current_ip_username_expr} AS current_ip_username,
               {1 if join_ip_usernames else 0} AS ip_username_lookup_complete,
               {owner_name_expr} AS current_owner_name,
               {owner_name_expr} AS current_username,
               COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
        FROM tasks t
        {ip_username_join}
        {report_stats_join}
        {where}
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT ? OFFSET ?
        """,
            tuple(params + [per_page, (page - 1) * per_page]),
        )
        .fetchall()
    )
    rows = _task_rows_with_review_progress(rows)
    return render_template(
        template_name,
        tasks=rows,
        status=status,
        review_status=review_status,
        review_status_filters=REPORT_REVIEW_FILTERS,
        keyword=keyword,
        pagination=_pagination(page, total, per_page),
        totals=totals,
        global_concurrency=get_setting("global_concurrency", 3),
        user_concurrency=get_setting("user_concurrency", 1),
        check_items=check_items,
        models=get_enabled_models(identity.subject),
        submission_token=uuid.uuid4().hex,
        refresh_url=url_for("admin_task_statuses", task_type=task_type),
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
    return (
        get_db()
        .execute(
            """
        SELECT *
        FROM check_items
        WHERE task_type = ? AND enabled = 1
        ORDER BY sort_order ASC, id ASC
        """,
            (task_type,),
        )
        .fetchall()
    )


def _next_check_item_sort_order(db, task_type: str = DOCUMENT_TASK_TYPE) -> int:
    row = db.execute(
        "SELECT MIN(sort_order) AS value FROM check_items WHERE task_type = ?",
        (task_type,),
    ).fetchone()
    if row is None or row["value"] is None:
        return 10
    return int(row["value"]) - 10


def _reorder_check_items(
    db, item_ids: list[int], task_type: str = DOCUMENT_TASK_TYPE
) -> list[int]:
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


def _admin_overview_range() -> dict:
    today = date.today()
    default_start = today
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
        "today": today.isoformat(),
        "last_7_start": (today - timedelta(days=6)).isoformat(),
        "last_30_start": (today - timedelta(days=29)).isoformat(),
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
            COALESCE(SUM(CASE WHEN status IN ('running', 'canceling') THEN 1 ELSE 0 END), 0) AS running,
            COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) AS completed,
            COALESCE(SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END), 0) AS partial,
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
        LIMIT 30
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
    ip_username_join = (
        "LEFT JOIN ip_usernames iu ON iu.ip = t.ip" if join_ip_usernames else ""
    )
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
    row = db.execute(
        f"""
        SELECT
            COUNT(*) AS tasks,
            COALESCE(SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END), 0) AS queued,
            COALESCE(SUM(CASE WHEN status IN ('running', 'canceling') THEN 1 ELSE 0 END), 0) AS running,
            COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) AS completed,
            COALESCE(SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END), 0) AS partial,
            COUNT(DISTINCT COALESCE(owner_subject, 'ip:' || ip)) AS users
        FROM tasks
        WHERE task_type = ? AND {mode_clause}
        """,
        (task_type, *mode_params),
    ).fetchone()
    totals = dict(row or {})
    totals["ips"] = totals.get("users", 0)
    totals["report_items"] = _admin_report_item_totals(
        task_type, mode_clause, mode_params
    )
    return totals


def _admin_report_item_totals(
    task_type: str, mode_clause: str, mode_params: tuple[str, ...]
) -> dict:
    return _admin_report_item_totals_for_where(
        f"task_type = ? AND {mode_clause}",
        (task_type, *mode_params),
    )


def _admin_report_item_totals_for_where(where_clause: str, params: tuple) -> dict:
    totals = {key: 0 for key in REPORT_COUNT_KEYS}
    for row in _task_report_stat_rows_for_where(where_clause, params):
        _add_report_counts(totals, row)
    return _finalize_report_counts(totals)


def _task_report_stat_rows_for_where(where_clause: str, params: tuple) -> list:
    db = get_db()
    rows = _select_task_report_stat_rows(where_clause, params)
    task_types = {str(row["task_type"] or DOCUMENT_TASK_TYPE) for row in rows}
    suppression_versions = _report_suppression_versions(task_types)
    empty_version = _empty_report_suppression_version()
    stale_ids = [
        int(row["id"])
        for row in rows
        if not (
            row["source_updated_at"] == row["updated_at"]
            and row["suppression_version"]
            == suppression_versions.get(
                str(row["task_type"] or DOCUMENT_TASK_TYPE), empty_version
            )
        )
    ]
    if not stale_ids:
        return rows

    # 统计缓存按批次刷新，页面请求保持轻量。
    if len(stale_ids) > REPORT_STATS_INLINE_REBUILD_LIMIT:
        stale_ids = stale_ids[:REPORT_STATS_INLINE_REBUILD_LIMIT]

    rules_by_type = {
        task_type: _enabled_report_suppression_rules(task_type)
        for task_type in task_types
    }
    cache_rows = []
    for chunk_start in range(0, len(stale_ids), 500):
        chunk = stale_ids[chunk_start : chunk_start + 500]
        placeholders = ",".join("?" for _ in chunk)
        stale_rows = db.execute(
            f"SELECT id, task_type, updated_at, result_json FROM tasks WHERE id IN ({placeholders})",
            tuple(chunk),
        ).fetchall()
        for row in stale_rows:
            task_type = str(row["task_type"] or DOCUMENT_TASK_TYPE)
            item_totals = _report_item_totals(
                _prepare_task_results(
                    _parse_result_json(row["result_json"]),
                    task_type=task_type,
                    task_id=row["id"],
                    suppression_rules=rules_by_type.get(task_type, {}),
                )
            )
            cache_rows.append(
                (
                    row["id"],
                    row["updated_at"] or "",
                    suppression_versions.get(task_type, empty_version),
                    *[int(item_totals.get(key) or 0) for key in REPORT_COUNT_KEYS],
                    now_text(),
                )
            )
    _write_task_report_stat_rows(cache_rows)
    return _select_task_report_stat_rows(where_clause, params)


def refresh_stale_report_stats_batch(
    limit: int = REPORT_STATS_BACKGROUND_BATCH_SIZE,
) -> int:
    """在当前应用上下文中刷新一小批报告统计缓存。"""

    try:
        batch_size = max(1, int(limit))
    except (TypeError, ValueError):
        batch_size = REPORT_STATS_BACKGROUND_BATCH_SIZE

    task_types = {
        DOCUMENT_TASK_TYPE,
        CONSISTENCY_TASK_TYPE,
        LANGUAGE_CONSISTENCY_TASK_TYPE,
        IMAGE_TASK_TYPE,
        VIDEO_TASK_TYPE,
    }
    suppression_versions = _report_suppression_versions(task_types)
    stale_clauses = [
        "s.task_id IS NULL",
        "COALESCE(s.source_updated_at, '') != COALESCE(t.updated_at, '')",
    ]
    stale_params: list[str] = []
    for task_type in sorted(task_types):
        stale_clauses.append(
            "(COALESCE(t.task_type, ?) = ? AND COALESCE(s.suppression_version, '') != ?)"
        )
        stale_params.extend(
            [
                DOCUMENT_TASK_TYPE,
                task_type,
                suppression_versions.get(
                    task_type, _empty_report_suppression_version()
                ),
            ]
        )

    rows = (
        get_db()
        .execute(
            f"""
        SELECT t.id, t.task_type, t.updated_at, t.result_json
        FROM tasks t
        LEFT JOIN task_report_stats s ON s.task_id = t.id
        WHERE t.result_json IS NOT NULL
          AND t.result_json != ''
          AND ({" OR ".join(stale_clauses)})
        ORDER BY t.id ASC
        LIMIT ?
        """,
            (*stale_params, batch_size),
        )
        .fetchall()
    )
    if not rows:
        return 0

    rules_by_type = {
        task_type: _enabled_report_suppression_rules(task_type)
        for task_type in {str(row["task_type"] or DOCUMENT_TASK_TYPE) for row in rows}
    }
    cache_rows = []
    for row in rows:
        task_type = str(row["task_type"] or DOCUMENT_TASK_TYPE)
        prepared = _prepare_task_results(
            _parse_result_json(row["result_json"]),
            task_type=task_type,
            task_id=row["id"],
            suppression_rules=rules_by_type.get(task_type, {}),
        )
        item_totals = _report_item_totals(prepared)
        cache_rows.append(
            (
                row["id"],
                row["updated_at"] or "",
                suppression_versions.get(
                    task_type, _empty_report_suppression_version()
                ),
                *[int(item_totals.get(key) or 0) for key in REPORT_COUNT_KEYS],
                now_text(),
            )
        )
    _write_task_report_stat_rows(cache_rows)
    return len(cache_rows)


def _select_task_report_stat_rows(where_clause: str, params: tuple) -> list:
    return (
        get_db()
        .execute(
            f"""
        SELECT
            t.id,
            t.task_type,
            t.updated_at,
            s.source_updated_at,
            s.suppression_version,
            s.issue_count AS issue,
            s.suggestion_count AS suggestion,
            s.non_issue_count AS non_issue,
            s.accepted_issue_count AS accepted_issue,
            s.rejected_issue_count AS rejected_issue,
            s.pending_issue_acceptance_count AS pending_issue_acceptance,
            s.suppressed_count AS suppressed,
            s.reviewed_item_count AS reviewed,
            s.pending_review_item_count AS pending_review
        FROM tasks t
        LEFT JOIN task_report_stats s ON s.task_id = t.id
        WHERE t.result_json IS NOT NULL
          AND t.result_json != ''
          AND {where_clause}
        """,
            params,
        )
        .fetchall()
    )


def _add_report_counts(target: dict, source) -> None:
    for key in REPORT_COUNT_KEYS:
        target[key] += int(_row_value(source, key, 0) or 0)


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
    ip_username_join = (
        "LEFT JOIN ip_usernames iu ON iu.ip = t.ip" if join_ip_usernames else ""
    )
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
    task = (
        get_db()
        .execute(
            f"""
        SELECT t.*,
               live.result_json AS live_result_json,
               live.summary AS live_summary,
               {owner_name_expr} AS current_owner_name,
               {owner_name_expr} AS current_username,
               COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
        FROM tasks t
        LEFT JOIN task_live_results live ON live.task_id = t.id
        {ip_username_join}
        WHERE {" AND ".join(clauses)}
        """,
            tuple(params),
        )
        .fetchone()
    )
    if task is None:
        abort(404)
    return _task_with_live_result(task)


def _get_user_task(task_id: int):
    identity = _current_user_identity()
    task = (
        get_db()
        .execute(
            """
        SELECT t.*,
               live.result_json AS live_result_json,
               live.summary AS live_summary,
               COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_owner_name,
               COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_username,
               COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
        FROM tasks t
        LEFT JOIN task_live_results live ON live.task_id = t.id
        WHERE t.id = ? AND t.owner_subject = ?
        """,
            (task_id, identity.subject),
        )
        .fetchone()
    )
    if task is None:
        abort(404)
    return _task_with_live_result(task)


def _task_with_live_result(task) -> dict:
    value = dict(task)
    if value.get("status") in {"running", "canceling"}:
        if value.get("live_result_json") is not None:
            value["result_json"] = value["live_result_json"]
        if value.get("status") == "running" and value.get("live_summary") is not None:
            value["summary"] = value["live_summary"]
    value.pop("live_result_json", None)
    value.pop("live_summary", None)
    return value


def _get_user_task_or_local_admin(task_id: int):
    if not _platform_enabled():
        return _get_task_or_404(task_id)
    return _get_user_task(task_id)


def _cancel_task(task):
    if task["status"] in DELETABLE_TASK_STATUSES:
        return
    db = get_db()
    now = now_text()
    if task["status"] == "queued":
        canceled = db.execute(
            """
            UPDATE tasks
            SET cancel_requested = 1,
                status = 'canceled',
                progress = 0,
                api_key = NULL,
                retry_check_codes_json = NULL,
                claim_token = NULL,
                lease_expires_at = NULL,
                updated_at = ?,
                finished_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, now, task["id"]),
        )
        if canceled.rowcount == 1:
            db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task["id"],))
    else:
        db.execute(
            """
            UPDATE tasks
            SET cancel_requested = 1,
                status = 'canceling',
                summary = ?,
                updated_at = ?
            WHERE id = ? AND status IN ('running', 'canceling')
            """,
            ("正在取消任务，请等待当前请求退出。", now, task["id"]),
        )
    db.commit()


def _retry_task(task) -> bool:
    try:
        retry_check_codes = retry_check_codes_for_task(task)
    except RuntimeError as exc:
        flash(str(exc), "error")
        return False

    provider_id = _row_value(task, "provider_id")
    owner_subject = str(_row_value(task, "owner_subject") or "").strip()
    if provider_id is None or not owner_subject:
        flash("原任务的模型提供商信息不完整，无法重试。", "error")
        return False

    db = get_db()
    provider = db.execute(
        """
        SELECT api_key
        FROM user_model_providers
        WHERE id = ? AND owner_subject = ?
        """,
        (provider_id, owner_subject),
    ).fetchone()
    if provider is None:
        flash("原任务使用的模型提供商已不存在，无法重试。", "error")
        return False

    now = now_text()
    retried = db.execute(
        """
        UPDATE tasks
        SET status = 'queued',
            progress = 0,
            cancel_requested = 0,
            retry_check_codes_json = ?,
            api_key = ?,
            claim_token = NULL,
            lease_expires_at = NULL,
            summary = ?,
            error = NULL,
            updated_at = ?,
            started_at = NULL,
            finished_at = NULL
        WHERE id = ? AND status IN ('failed', 'partial')
        """,
        (
            json.dumps(retry_check_codes, ensure_ascii=False),
            provider["api_key"],
            f"已进入重试队列，等待重跑 {len(retry_check_codes)} 个失败检查项。",
            now,
            task["id"],
        ),
    )
    if retried.rowcount != 1:
        db.rollback()
        flash("任务状态已变化，无法重试。", "error")
        return False

    db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task["id"],))
    db.commit()
    flash(
        f"已重新加入队列，将只重跑 {len(retry_check_codes)} 个失败检查项。", "success"
    )
    return True


def _delete_task(task):
    if task["status"] not in DELETABLE_TASK_STATUSES:
        flash("排队中或运行中的任务不能直接删除，请先取消后再删除。", "error")
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
    delete_task_record(db, task["id"])
    db.commit()
    return True


def _delete_queued_task(task) -> bool | None:
    db = get_db()
    canceled = db.execute(
        """
        UPDATE tasks
        SET cancel_requested = 1,
            status = 'canceled',
            progress = 0,
            api_key = NULL,
            claim_token = NULL,
            lease_expires_at = NULL,
            updated_at = ?,
            finished_at = ?
        WHERE id = ? AND status = 'queued'
        """,
        (now_text(), now_text(), task["id"]),
    )
    if canceled.rowcount != 1:
        db.rollback()
        return None
    db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task["id"],))
    db.commit()

    canceled_task = dict(task)
    canceled_task["status"] = "canceled"
    return _delete_task(canceled_task)


def _bulk_delete_tasks(task_loader, *, admin_created: bool):
    raw_task_ids = request.form.getlist("task_ids")
    if len(raw_task_ids) > MAX_BULK_DELETE_TASKS:
        flash(f"每次最多批量删除 {MAX_BULK_DELETE_TASKS} 个任务。", "error")
        return redirect(
            _task_action_redirect("admin_tasks" if admin_created else "user_tasks")
        )

    task_ids = []
    for raw_task_id in raw_task_ids:
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            continue
        if task_id > 0 and task_id not in task_ids:
            task_ids.append(task_id)

    if not task_ids:
        flash("请先选择需要删除的任务。", "error")
        return redirect(
            _task_action_redirect("admin_tasks" if admin_created else "user_tasks")
        )

    tasks = [task_loader(task_id) for task_id in task_ids]
    fallback_endpoint = _task_list_endpoint(admin_created, tasks[0]["task_type"])
    redirect_url = _task_action_redirect(fallback_endpoint)
    deleted_count = 0
    queued_deleted_count = 0
    skipped_count = 0
    for task in tasks:
        if task["status"] not in BULK_DELETABLE_TASK_STATUSES:
            skipped_count += 1
            continue
        if task["status"] == "queued":
            deleted = _delete_queued_task(task)
            if deleted is None:
                skipped_count += 1
                continue
            if deleted:
                deleted_count += 1
                queued_deleted_count += 1
            continue
        if _delete_task(task):
            deleted_count += 1

    if deleted_count:
        queued_message = (
            f"，其中 {queued_deleted_count} 个排队任务已取消"
            if queued_deleted_count
            else ""
        )
        flash(f"已批量删除 {deleted_count} 个任务{queued_message}。", "success")
    if skipped_count:
        flash(
            f"已跳过 {skipped_count} 个状态已变化或正在运行的任务，请先取消后再删除。",
            "error",
        )
    return redirect(redirect_url)


def _task_action_redirect(default_endpoint: str):
    return _safe_next_path(request.form.get("next"), url_for(default_endpoint))


def _download_task_document(task, fallback_endpoint: str):
    if task["task_type"] in {CONSISTENCY_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE}:
        return _download_task_documents_zip(task, fallback_endpoint)

    upload_path = _task_upload_path(task)
    if not upload_path.is_file():
        flash("原文件已清理或缺失，无法下载。", "error")
        return redirect(
            request.referrer or url_for(fallback_endpoint, task_id=task["id"])
        )
    return send_file(
        upload_path,
        as_attachment=True,
        download_name=task["original_filename"],
    )


def _task_video_stream_url(task, endpoint: str) -> str:
    if (task["task_type"] or DOCUMENT_TASK_TYPE) != VIDEO_TASK_TYPE:
        return ""
    if not _task_source_files_available(task):
        return ""
    return url_for(endpoint, task_id=task["id"])


def _stream_task_video(task):
    if (task["task_type"] or DOCUMENT_TASK_TYPE) != VIDEO_TASK_TYPE:
        abort(404)
    upload_path = _task_upload_path(task)
    if not upload_path.is_file():
        abort(404)
    mimetype = (
        mimetypes.guess_type(task["original_filename"])[0] or "application/octet-stream"
    )
    return send_file(
        upload_path,
        mimetype=mimetype,
        as_attachment=False,
        download_name=task["original_filename"],
        conditional=True,
    )


def _send_task_media(task, media_id: str):
    normalized_id = str(media_id or "").strip()
    if not normalized_id:
        abort(404)
    media_item = next(
        (
            item
            for item in _task_image_items(task)
            if normalized_id
            in {
                str(item.get("id") or ""),
                str(item.get("filename") or ""),
                str(item.get("stored_filename") or ""),
            }
        ),
        None,
    )
    if media_item is None:
        abort(404)
    media_path = image_path_from_item(_image_folder(), media_item)
    if media_path is None or not media_path.is_file():
        abort(404)
    return send_file(
        media_path,
        mimetype=str(media_item.get("mime_type") or "application/octet-stream"),
        as_attachment=False,
        download_name=str(media_item.get("filename") or media_path.name),
        conditional=True,
    )


def _attach_report_media_urls(results: list[dict], task, endpoint: str) -> None:
    available_items = [
        item
        for item in _task_image_items(task)
        if (media_path := image_path_from_item(_image_folder(), item)) is not None
        and media_path.is_file()
    ]
    available_ids = {
        value
        for item in available_items
        for value in (
            str(item.get("id") or "").strip(),
            str(item.get("filename") or "").strip(),
            str(item.get("stored_filename") or "").strip(),
        )
        if value
    }
    if not available_ids:
        return
    for result in results:
        for report_item in list(result.get("report_items") or []) + list(
            result.get("suppressed_report_items") or []
        ):
            for ref in report_item.get("evidence_refs") or []:
                media_id = str(ref.get("id") or ref.get("filename") or "").strip()
                if media_id in available_ids:
                    ref["url"] = url_for(
                        endpoint, task_id=task["id"], media_id=media_id
                    )


def _page_arg() -> int:
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return 1
    return max(1, page)


def _per_page_arg() -> int:
    try:
        per_page = int(request.args.get("per_page", str(DEFAULT_TASKS_PER_PAGE)))
    except ValueError:
        return DEFAULT_TASKS_PER_PAGE
    return per_page if per_page in TASKS_PER_PAGE_OPTIONS else DEFAULT_TASKS_PER_PAGE


def _bounded_page(page: int, total: int, per_page: int) -> int:
    pages = max(1, (total + per_page - 1) // per_page)
    return min(max(1, page), pages)


def _pagination(page: int, total: int, per_page: int) -> dict:
    pages = max(1, (total + per_page - 1) // per_page)
    return {
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "per_page_options": TASKS_PER_PAGE_OPTIONS,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < pages,
        "prev_page": max(1, page - 1),
        "next_page": min(pages, page + 1),
        "start": 0 if total == 0 else (page - 1) * per_page + 1,
        "end": min(total, page * per_page),
    }


def _task_stats_for_where(where: str, params: tuple) -> dict:
    stats = {
        "total": 0,
        "queued": 0,
        "running": 0,
        "completed": 0,
        "partial": 0,
        "failed": 0,
        "canceled": 0,
    }
    rows = (
        get_db()
        .execute(
            f"SELECT status, COUNT(*) AS total FROM tasks WHERE {where} GROUP BY status",
            params,
        )
        .fetchall()
    )
    for row in rows:
        count = row["total"]
        stats["total"] += count
        if row["status"] == "canceling":
            stats["running"] += count
        elif row["status"] in stats:
            stats[row["status"]] += count
    return stats
