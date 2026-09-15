from flask import flash, redirect, render_template, request, url_for

from app.contracts.task_types import DOCUMENT_TASK_TYPE, VIDEO_TASK_TYPE
from app.reporting.constants import REPORT_ITEM_TYPES
from app.reporting.service import (
    _report_item_fields_for_task,
    _report_item_totals,
    _task_results,
    _uses_compact_media_report,
)
from app.tasks.files import _task_document_groups
from app.web.auth import _current_user_identity, admin_required
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
    _get_task_or_404,
    _retry_task,
    _task_action_redirect,
)
from app.web.task_activity import (
    cancel_check,
    detail_progress,
    model_output_response,
    present_check_activity,
    retry_check,
)
from app.web.task_lists import (
    _render_admin_consistency_page,
    _render_admin_images_page,
    _render_admin_language_consistency_page,
    _render_admin_tasks_page,
    _render_admin_videos_page,
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


def register_admin_tasks_routes(app):
    admin_prefix = app.config["ADMIN_URL"]

    @app.route(f"{admin_prefix}/tasks", methods=["GET", "POST"])
    @admin_required
    def admin_tasks():
        if request.method == "POST":
            return create_task_for_identity(
                _current_user_identity(), admin_created=True
            )
        return _render_admin_tasks_page()

    @app.route(f"{admin_prefix}/tasks/new", methods=["GET", "POST"])
    @admin_required
    def admin_new_task():
        if request.method == "POST":
            return create_task_for_identity(
                _current_user_identity(), admin_created=True
            )
        return redirect(url_for("admin_tasks"))

    @app.get(f"{admin_prefix}/task-statuses")
    @admin_required
    def admin_task_statuses():
        task_type = _validated_task_status_type()
        if task_type is None:
            return {"error": "任务类型无效。"}, 400
        return _task_status_payload(task_type, owner_clause="1=1", owner_params=())

    @app.route(f"{admin_prefix}/consistency", methods=["GET", "POST"])
    @admin_required
    def admin_consistency():
        if request.method == "POST":
            return create_consistency_task_for_identity(
                _current_user_identity(), admin_created=True
            )
        return _render_admin_consistency_page()

    @app.route(f"{admin_prefix}/language-consistency", methods=["GET", "POST"])
    @admin_required
    def admin_language_consistency():
        if request.method == "POST":
            return create_language_consistency_task_for_identity(
                _current_user_identity(), admin_created=True
            )
        return _render_admin_language_consistency_page()

    @app.route(f"{admin_prefix}/images", methods=["GET", "POST"])
    @admin_required
    def admin_images():
        if request.method == "POST":
            return create_image_task_for_identity(
                _current_user_identity(), admin_created=True
            )
        return _render_admin_images_page()

    @app.route(f"{admin_prefix}/videos", methods=["GET", "POST"])
    @admin_required
    def admin_videos():
        if request.method == "POST":
            return create_video_task_for_identity(
                _current_user_identity(), admin_created=True
            )
        return _render_admin_videos_page()

    @app.get(f"{admin_prefix}/tasks/<int:task_id>")
    @admin_required
    def admin_task_detail(task_id):
        polling = request.args.get("_poll") == "1"
        task = _get_task_or_404(task_id, lightweight=polling)
        progress = detail_progress(task)
        if polling and request.args.get("revision") == progress["revision"]:
            return progress
        if polling:
            task = _get_task_or_404(task_id)
        results = present_check_activity(task, _task_results(task), progress)
        _attach_report_media_urls(results, task, "admin_task_media")
        back_endpoint = _task_list_endpoint(True, task["task_type"])
        html = render_template(
            "_task_detail_content.html" if polling else "task_detail.html",
            task_progress=progress,
            task_detail_url=url_for(
                request.endpoint, task_id=task_id, next=request.args.get("next")
            ),
            cancel_check_url=url_for("admin_cancel_check", task_id=task_id),
            retry_check_url=url_for("admin_retry_check", task_id=task_id),
            model_output_url=url_for("admin_model_output", task_id=task_id),
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
        if polling:
            progress["html"] = html
            return progress
        return html

    @app.get(f"{admin_prefix}/tasks/<int:task_id>/model-output")
    @admin_required
    def admin_model_output(task_id):
        task = _get_task_or_404(task_id, lightweight=True, include_revision=False)
        return model_output_response(task)

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/retry-check")
    @admin_required
    def admin_retry_check(task_id):
        task = _get_task_or_404(task_id, lightweight=True)
        return retry_check(task)

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/cancel-check")
    @admin_required
    def admin_cancel_check(task_id):
        task = _get_task_or_404(task_id, lightweight=True)
        return cancel_check(task)

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
