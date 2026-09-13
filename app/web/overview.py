from datetime import date, datetime, timedelta

from flask import redirect, render_template, request, url_for

from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.persistence.connection import get_db
from app.reporting.statistics import _admin_report_item_totals_for_where
from app.web.auth import (
    _auth_mode,
    _mode_subject_filter,
    _platform_enabled,
    admin_required,
)


def register_overview_routes(app):
    admin_prefix = app.config["ADMIN_URL"]

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
        WHERE task_type IN ('document_check', 'consistency_check', 'language_consistency_check', 'image_check', 'video_check') AND created_at >= ? AND created_at < ? AND {mode_clause}
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
        f"t.task_type IN ('document_check', 'consistency_check', 'language_consistency_check', 'image_check', 'video_check') AND t.created_at >= ? AND t.created_at < ? AND {mode_clause}",
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
        WHERE task_type IN ('document_check', 'consistency_check', 'language_consistency_check', 'image_check', 'video_check') AND created_at >= ? AND created_at < ? AND {mode_clause}
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
        WHERE t.task_type IN ('document_check', 'consistency_check', 'language_consistency_check', 'image_check', 'video_check') AND t.created_at >= ? AND t.created_at < ? AND {mode_clause}
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
