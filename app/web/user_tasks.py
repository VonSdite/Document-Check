from flask import flash, redirect, render_template, request, url_for

from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.identity.service import current_identity
from app.reporting.constants import REPORT_ITEM_TYPES
from app.reporting.service import (
    _report_item_fields_for_task,
    _report_item_totals,
    _task_results,
    _uses_compact_media_report,
)
from app.tasks.files import _task_document_groups
from app.web.auth import _current_user_identity, _platform_enabled
from app.web.common import _safe_next_path
from app.web.reports import (
    _export_task_report,
    _export_task_report_excel,
    _import_task_report_excel,
    _update_report_item_type,
)
from app.web.submission import (
    _task_list_endpoint,
    create_consistency_task_for_identity,
    create_image_task_for_identity,
    create_language_consistency_task_for_identity,
    create_task_for_identity,
    create_video_task_for_identity,
)
from app.web.task_actions import (
    _bulk_delete_tasks,
    _cancel_task,
    _delete_task,
    _get_user_task,
    _get_user_task_or_local_admin,
    _retry_task,
    _task_action_redirect,
)
from app.web.task_activity import (
    cancel_check,
    detail_progress,
    model_output_response,
    present_check_activity,
)
from app.web.task_lists import (
    _render_admin_consistency_page,
    _render_admin_images_page,
    _render_admin_language_consistency_page,
    _render_admin_tasks_page,
    _render_admin_videos_page,
    _render_user_task_list,
    _task_status_payload,
    _validated_task_status_type,
)
from app.web.task_media import (
    _attach_report_media_urls,
    _download_task_document,
    _send_task_media,
    _stream_task_video,
    _task_video_stream_url,
)


def register_user_tasks_routes(app):
    @app.route("/", methods=["GET", "POST"])
    def user_tasks():
        if not _platform_enabled():
            if request.method == "POST":
                return create_task_for_identity(current_identity(), admin_created=True)
            return _render_admin_tasks_page()

        identity = _current_user_identity()
        if request.method == "POST":
            return create_task_for_identity(identity, admin_created=False)
        return _render_user_task_list(identity, DOCUMENT_TASK_TYPE, "user_tasks.html")

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

        return _render_user_task_list(
            identity, CONSISTENCY_TASK_TYPE, "user_consistency.html"
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

        return _render_user_task_list(
            identity, LANGUAGE_CONSISTENCY_TASK_TYPE, "user_language_consistency.html"
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

        return _render_user_task_list(identity, IMAGE_TASK_TYPE, "user_images.html")

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

        return _render_user_task_list(identity, VIDEO_TASK_TYPE, "user_videos.html")

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
        polling = request.args.get("_poll") == "1"
        task = _get_user_task_or_local_admin(task_id, lightweight=polling)
        progress = detail_progress(task)
        if polling and request.args.get("revision") == progress["revision"]:
            return progress
        if polling:
            task = _get_user_task_or_local_admin(task_id)
        results = present_check_activity(task, _task_results(task), progress)
        _attach_report_media_urls(results, task, "user_task_media")
        back_endpoint = _task_list_endpoint(not _platform_enabled(), task["task_type"])
        html = render_template(
            "_task_detail_content.html" if polling else "task_detail.html",
            task_progress=progress,
            task_detail_url=url_for(
                request.endpoint, task_id=task_id, next=request.args.get("next")
            ),
            cancel_check_url=url_for("user_cancel_check", task_id=task_id),
            model_output_url=url_for("user_model_output", task_id=task_id),
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
        if polling:
            progress["html"] = html
            return progress
        return html

    @app.get("/tasks/<int:task_id>/model-output")
    def user_model_output(task_id):
        task = _get_user_task_or_local_admin(
            task_id, lightweight=True, include_revision=False
        )
        return model_output_response(task)

    @app.post("/tasks/<int:task_id>/cancel-check")
    def user_cancel_check(task_id):
        task = _get_user_task_or_local_admin(task_id, lightweight=True)
        return cancel_check(task)

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
