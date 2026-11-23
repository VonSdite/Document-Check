"""全量报告审核与终审待办页面。"""

from flask import abort, flash, redirect, render_template, request, url_for

from app.contracts.task_types import VIDEO_TASK_TYPE
from app.persistence.connection import get_db
from app.reporting.constants import REPORT_ITEM_FIELDS
from app.reporting.reviews import (
    ACTIVE_STATUSES,
    FINAL_DECISIONS,
    FINAL_STATUSES,
    ReviewConflict,
    final_review_block_reason,
    final_review_progress,
    review_context,
    save_final_review,
)
from app.reporting.service import update_report_item_review
from app.web.auth import (
    _check_write_origin,
    can_manage_task,
    has_permission,
    permission_required,
)
from app.web.reports import review_actor
from app.web.task_actions import _get_task_or_404, _get_user_task
from app.web.task_lists import _bounded_page, _page_arg, _pagination, _per_page_arg
from app.web.task_media import _attach_report_media_urls, _task_video_stream_url


def register_final_review_routes(app):
    def queue():
        status = str(request.args.get("final_status") or "")
        if status not in FINAL_STATUSES:
            status = ""
        keyword = str(request.args.get("keyword") or "").strip()[:200]
        db = get_db()
        # 按有界批次准备缓存，后台扫描持续覆盖历史任务。
        recent = [
            r[0]
            for r in db.execute(
                "SELECT id FROM tasks ORDER BY created_at DESC, id DESC LIMIT 100"
            )
        ]
        final_review_progress(recent)
        clauses = ["t.status NOT IN ('queued', 'running', 'canceling')"]
        params = []
        if status:
            clauses.append("s.status = ?")
            params.append(status)
        if keyword:
            clauses.append("t.original_filename LIKE ? ESCAPE '\\'")
            value = (
                keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            params.append(f"%{value}%")
        source = (
            "FROM tasks t LEFT JOIN task_final_review_stats s ON s.task_id = t.id WHERE "
            + " AND ".join(clauses)
        )
        total = db.execute("SELECT COUNT(*) " + source, params).fetchone()[0]
        per_page = _per_page_arg()
        page = _bounded_page(_page_arg(), total, per_page)
        rows = db.execute(
            "SELECT t.id, t.original_filename, t.task_type, t.status "
            + source
            + " ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page),
        ).fetchall()
        progress = final_review_progress([row["id"] for row in rows])
        return render_template(
            "final_review_queue.html",
            tasks=rows,
            progress=progress,
            pagination=_pagination(page, total, per_page),
            final_status=status,
            final_statuses=FINAL_STATUSES,
            keyword=keyword,
            active_nav="final_reviews",
        )

    def detail(task_id):
        from app.web.auth import is_superadmin

        task = _get_task_or_404(task_id) if is_superadmin() else _get_user_task(task_id)
        actor = review_actor()
        if request.method == "POST":
            _check_write_origin()
            data = request.get_json(silent=True) if request.is_json else request.form
            if not isinstance(data, dict) and not request.form:
                abort(400)
            try:
                if data.get("stage") == "initial":
                    if not can_manage_task(task):
                        abort(403)
                    if not data.get("review_token"):
                        raise ReviewConflict("页面审核版本缺失，请刷新后重试。")
                    response = update_report_item_review(task, data, actor=actor)
                    if isinstance(response, tuple):
                        raise ReviewConflict(response[0]["error"])
                else:
                    if not has_permission("reports.final_review"):
                        abort(403)
                    save_final_review(task_id, data, actor)
            except ReviewConflict as exc:
                if request.is_json:
                    return {"ok": False, "error": str(exc)}, 409
                flash(str(exc), "error")
            else:
                if request.is_json:
                    return {"ok": True}
                flash("审核意见已保存。", "success")
            return redirect(
                url_for(
                    request.endpoint,
                    task_id=task_id,
                    _anchor=f"review-{data.get('result_code', '')}-{data.get('item_id', '')}",
                )
            )
        context = review_context(task)
        for result in context["results"]:
            for item in result["report_items"]:
                item["review"]["block_reason"] = final_review_block_reason(
                    item["review"]["initial"], actor
                )
        media_endpoint = "admin_task_media" if is_superadmin() else "user_task_media"
        _attach_report_media_urls(context["results"], task, media_endpoint)
        return render_template(
            "final_review.html",
            task=task,
            **context,
            actor=actor,
            active_nav="final_reviews",
            final_decisions=FINAL_DECISIONS,
            report_item_fields=REPORT_ITEM_FIELDS,
            task_editable=can_manage_task(task),
            task_finished=task["status"] not in ACTIVE_STATUSES,
            video_report=task["task_type"] == VIDEO_TASK_TYPE,
            video_stream_url=_task_video_stream_url(
                task, "admin_task_video" if is_superadmin() else "user_task_video"
            ),
        )

    for prefix, endpoint_prefix in (("", "user_"), (app.config["ADMIN_URL"], "admin_")):
        app.add_url_rule(
            prefix + "/final-reviews",
            endpoint_prefix + "final_reviews",
            permission_required("reports.final_review")(queue),
        )
        app.add_url_rule(
            prefix + "/tasks/<int:task_id>/reviews",
            endpoint_prefix + "report_reviews",
            detail,
            methods=["GET", "POST"],
        )
