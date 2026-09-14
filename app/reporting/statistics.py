from flask import current_app

from app.contracts.task_types import (
    DOCUMENT_TASK_TYPE,
)
from app.persistence.connection import get_db, now_text
from app.reporting.constants import (
    REPORT_COUNT_KEYS,
    REPORT_STATS_BACKGROUND_BATCH_SIZE,
    REPORT_STATS_INLINE_REBUILD_LIMIT,
)
from app.reporting.service import (
    _empty_report_suppression_version,
    _enabled_report_suppression_rules,
    _finalize_report_counts,
    _parse_result_json,
    _prepare_task_results,
    _report_item_totals,
    _report_suppression_versions,
    _write_task_report_stat_rows,
)

REPORT_STATS_SCAN_BATCH_SIZE = 512

REPORT_STAT_COLUMNS = {
    "issue": "issue_count",
    "suggestion": "suggestion_count",
    "non_issue": "non_issue_count",
    "accepted_issue": "accepted_issue_count",
    "rejected_issue": "rejected_issue_count",
    "pending_issue_acceptance": "pending_issue_acceptance_count",
    "suppressed": "suppressed_count",
    "reviewed": "reviewed_item_count",
    "pending_review": "pending_review_item_count",
}


def _admin_report_item_totals_for_where(where_clause: str, params: tuple) -> dict:
    # 页面只同步准备最近一批报告，历史统计由监督器分批刷新。
    recent = (
        get_db()
        .execute(
            f"SELECT t.id FROM tasks t WHERE {where_clause} "
            "ORDER BY t.created_at DESC, t.id DESC LIMIT ?",
            (*params, REPORT_STATS_INLINE_REBUILD_LIMIT),
        )
        .fetchall()
    )
    if recent:
        ids = tuple(row["id"] for row in recent)
        placeholders = ",".join("?" for _ in ids)
        _task_report_stat_rows_for_where(f"t.id IN ({placeholders})", ids)

    columns = ", ".join(
        f"COALESCE(SUM(s.{column}), 0) AS {key}"
        for key, column in REPORT_STAT_COLUMNS.items()
    )
    totals = (
        get_db()
        .execute(
            f"SELECT {columns} FROM tasks t "
            "JOIN task_report_stats s ON s.task_id = t.id "
            f"WHERE {where_clause}",
            params,
        )
        .fetchone()
    )
    return _finalize_report_counts(dict(totals))


def _task_report_stat_rows_for_where(where_clause: str, params: tuple) -> list:
    rows = _select_task_report_stat_rows(where_clause, params)
    versions = _report_suppression_versions(
        {str(row["task_type"] or DOCUMENT_TASK_TYPE) for row in rows}
    )
    stale = _stale_stat_ids(rows, versions)[:REPORT_STATS_INLINE_REBUILD_LIMIT]
    if stale:
        _refresh_stat_ids(stale, versions)
        return _select_task_report_stat_rows(where_clause, params)
    return rows


def refresh_stale_report_stats_batch(
    limit: int = REPORT_STATS_BACKGROUND_BATCH_SIZE,
) -> int:
    """按主键游标检查有界窗口，并刷新其中的过期报告统计。"""
    try:
        batch_size = min(REPORT_STATS_BACKGROUND_BATCH_SIZE, max(1, int(limit)))
    except (TypeError, ValueError):
        batch_size = REPORT_STATS_BACKGROUND_BATCH_SIZE
    cursor = current_app.extensions.get("report_stats_scan_cursor", 0)
    ids = tuple(
        row["id"]
        for row in get_db().execute(
            "SELECT id FROM tasks WHERE id > ? ORDER BY id LIMIT ?",
            (cursor, REPORT_STATS_SCAN_BATCH_SIZE),
        )
    )
    if not ids:
        current_app.extensions["report_stats_scan_cursor"] = 0
        return 0
    placeholders = ",".join("?" for _ in ids)
    rows = _select_task_report_stat_rows(f"t.id IN ({placeholders})", ids)
    versions = _report_suppression_versions(
        {str(row["task_type"] or DOCUMENT_TASK_TYPE) for row in rows}
    )
    stale = sorted(_stale_stat_ids(rows, versions))
    selected = stale[:batch_size]
    _refresh_stat_ids(selected, versions)
    current_app.extensions["report_stats_scan_cursor"] = (
        selected[-1] if len(stale) > batch_size else ids[-1]
    )
    return len(selected)


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
        WHERE (s.task_id IS NOT NULL OR (t.result_json IS NOT NULL AND t.result_json != ''))
          AND {where_clause}
        """,
            params,
        )
        .fetchall()
    )


def _stale_stat_ids(rows, versions: dict[str, str]) -> list[int]:
    empty_version = _empty_report_suppression_version()
    return [
        int(row["id"])
        for row in rows
        if row["source_updated_at"] != row["updated_at"]
        or row["suppression_version"]
        != versions.get(str(row["task_type"] or DOCUMENT_TASK_TYPE), empty_version)
    ]


def _refresh_stat_ids(task_ids: list[int], versions: dict[str, str]) -> None:
    if not task_ids:
        return
    placeholders = ",".join("?" for _ in task_ids)
    rows = (
        get_db()
        .execute(
            f"SELECT id, task_type, updated_at, result_json FROM tasks WHERE id IN ({placeholders})",
            tuple(task_ids),
        )
        .fetchall()
    )
    rules_by_type = {
        task_type: _enabled_report_suppression_rules(task_type)
        for task_type in {str(row["task_type"] or DOCUMENT_TASK_TYPE) for row in rows}
    }
    cache_rows = []
    for row in rows:
        task_type = str(row["task_type"] or DOCUMENT_TASK_TYPE)
        totals = _report_item_totals(
            _prepare_task_results(
                _parse_result_json(row["result_json"]),
                task_type=task_type,
                task_id=row["id"],
                suppression_rules=rules_by_type[task_type],
            )
        )
        cache_rows.append(
            (
                row["id"],
                row["updated_at"] or "",
                versions.get(task_type, _empty_report_suppression_version()),
                *[int(totals.get(key) or 0) for key in REPORT_COUNT_KEYS],
                now_text(),
            )
        )
    _write_task_report_stat_rows(cache_rows)
