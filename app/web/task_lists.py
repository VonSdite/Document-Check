import uuid

from flask import render_template, request, url_for

from app.checks.catalog import get_enabled_check_items
from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.identity.service import UserIdentity
from app.models.service import get_enabled_models
from app.persistence.connection import get_db
from app.persistence.settings import get_setting
from app.reporting.constants import (
    REPORT_ITEM_TYPE_ORDER,
    REPORT_REVIEW_FILTERS,
    REPORT_REVIEW_STATUSES,
)
from app.reporting.service import (
    _empty_report_suppression_version,
    _report_suppression_versions,
)
from app.reporting.statistics import _task_report_stat_rows_for_where
from app.web.auth import _auth_mode, _console_user_identity, _mode_subject_filter
from app.web.common import _row_value
from app.web.constants import (
    DEFAULT_TASKS_PER_PAGE,
    STATUS_LABELS,
    TASKS_PER_PAGE_OPTIONS,
)
from app.web.overview import _admin_totals


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
    stats = _task_stats_for_where("owner_subject = ? AND task_type = ?", params)
    total = stats["total"]
    page = _bounded_page(page, total, per_page)
    rows = (
        get_db()
        .execute(
            f"""
        SELECT t.id, t.task_type, t.ip,
               t.original_filename, t.stored_filename, t.file_type, t.file_size,
               t.provider_name, t.model_name, t.status, t.progress,
               t.created_at,
               CASE WHEN t.task_type IN ('consistency_check', 'language_consistency_check')
                    THEN t.document_meta_json END AS document_meta_json,
               t.source_files_cleaned_at
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
    # 有效统计直接用于轮询，仅为过期且已结束的任务检查原始报告是否存在。
    rows = [dict(row) for row in rows]
    stale_ids = [
        row["id"]
        for row in rows
        if row["status"] not in {"queued", "running", "canceling"}
        and (
            row["source_updated_at"] != row["updated_at"]
            or row["suppression_version"] != suppression_version
        )
    ]
    has_results = {}
    if stale_ids:
        placeholders = ",".join("?" for _ in stale_ids)
        has_results = {
            row["id"]: bool(row["has_result"])
            for row in get_db().execute(
                f"SELECT id, (result_json IS NOT NULL AND result_json != '') AS has_result "
                f"FROM tasks WHERE id IN ({placeholders})",
                tuple(stale_ids),
            )
        }
    for row in rows:
        row["has_result"] = has_results.get(row["id"], True)
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
    report_stats_join = (
        "LEFT JOIN task_report_stats trs ON trs.task_id = t.id" if review_status else ""
    )
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
        {ip_username_join if keyword else ""}
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
               t.created_at,
               CASE WHEN t.task_type IN ('consistency_check', 'language_consistency_check')
                    THEN t.document_meta_json END AS document_meta_json,
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
        identity=identity,
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
