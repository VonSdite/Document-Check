"""误报入库的终审依据、有效性校验与过滤规则启用。"""

import json
from contextlib import contextmanager

from app.persistence.connection import get_db, now_text

REVIEW_MIGRATION_KEY = "report_suppression_final_review_v1"
VERIFIED_RULE_SQL = """EXISTS (
    SELECT 1 FROM report_suppression_reviews evidence
    WHERE evidence.rule_id = r.id AND evidence.is_current = 1
      AND evidence.needs_validation = 0
)"""


@contextmanager
def _write_scope():
    db = get_db()
    db.execute("SAVEPOINT suppression_reviews")
    try:
        db.execute(
            "UPDATE report_suppression_reviews SET is_current = is_current WHERE 0"
        )
        yield db
    except Exception:
        db.execute("ROLLBACK TO suppression_reviews")
        raise
    finally:
        db.execute("RELEASE suppression_reviews")


def record_confirmed_suppression(task, result, item, state):
    """将当前终审确认的不认可条目及两级审核快照一并入库。"""
    from app.reporting.service import _maybe_create_report_suppression_candidate

    initial, final = state["initial"], state["final"]
    if state["status"] != "confirmed" or not initial or not final:
        return False
    acceptance = initial["acceptance"]
    if acceptance["acceptance_status"] != "rejected":
        return False
    db = get_db()
    if db.execute(
        "SELECT 1 FROM report_suppression_reviews WHERE final_review_id = ?",
        (final["id"],),
    ).fetchone():
        return False
    rule_id = _maybe_create_report_suppression_candidate(
        db,
        task=task,
        result_code=str(result.get("code") or ""),
        result=result,
        item_id=item["id"],
        acceptance_status=acceptance["acceptance_status"],
        rejection_reason=acceptance["rejection_reason"],
        rejection_note=acceptance["rejection_note"],
    )
    if not rule_id:
        return False

    def snapshot(event):
        return {
            key: event[key]
            for key in (
                "id",
                "actor_subject",
                "actor_name",
                "actor_source",
                "created_at",
                "channel",
                "decision",
                "note",
                "item_hash",
                "item",
                "acceptance",
            )
        }

    evidence = {
        "filename": task["original_filename"],
        "check_name": result.get("name") or result.get("code"),
        "model_name": task["model_name"],
        "initial": snapshot(initial),
        "final": snapshot(final),
    }
    db.execute(
        "INSERT INTO report_suppression_reviews "
        "(final_review_id, rule_id, source_task_id, source_result_code, source_item_id, evidence_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            final["id"],
            rule_id,
            task["id"],
            str(result.get("code") or ""),
            item["id"],
            json.dumps(evidence, ensure_ascii=False),
        ),
    )
    return True


def refresh_suppression_reviews():
    """补齐当前有效的终审依据，按变更来源校验样本并停用失效规则。"""
    from app.reporting.reviews import review_context

    db = get_db()
    migrated = db.execute(
        "SELECT 1 FROM settings WHERE key = ?", (REVIEW_MIGRATION_KEY,)
    ).fetchone()
    dirty = db.execute(
        "SELECT 1 FROM report_suppression_reviews WHERE needs_validation = 1 LIMIT 1"
    ).fetchone()
    if migrated and not dirty:
        return
    with _write_scope():
        if not db.execute(
            "SELECT 1 FROM settings WHERE key = ?", (REVIEW_MIGRATION_KEY,)
        ).fetchone():
            tasks = db.execute(
                "SELECT * FROM tasks WHERE id IN (SELECT task_id FROM report_review_events "
                "WHERE stage = 'final' AND decision = 'confirmed')"
            ).fetchall()
            for task in tasks:
                for result in review_context(task)["results"]:
                    for item in result["report_items"]:
                        record_confirmed_suppression(task, result, item, item["review"])
            db.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, 'true', ?)",
                (REVIEW_MIGRATION_KEY, now_text()),
            )
        task_ids = [
            row[0]
            for row in db.execute(
                "SELECT DISTINCT source_task_id FROM report_suppression_reviews WHERE needs_validation = 1"
            )
        ]
        for task_id in task_ids:
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            current_ids = set()
            if task is not None:
                for result in review_context(task)["results"]:
                    for item in result["report_items"]:
                        state = item["review"]
                        if state["status"] == "confirmed":
                            current_ids.add(state["final"]["id"])
            for evidence in db.execute(
                "SELECT final_review_id FROM report_suppression_reviews "
                "WHERE source_task_id = ? AND needs_validation = 1",
                (task_id,),
            ).fetchall():
                event_id = evidence["final_review_id"]
                db.execute(
                    "UPDATE report_suppression_reviews SET is_current = ?, needs_validation = 0 "
                    "WHERE final_review_id = ?",
                    (int(event_id in current_ids), event_id),
                )
        db.execute(
            f"UPDATE report_suppression_rules AS r SET enabled = 0, updated_at = ? "
            f"WHERE enabled = 1 AND NOT {VERIFIED_RULE_SQL}",
            (now_text(),),
        )


def enable_suppression_rule(rule_id):
    with _write_scope() as db:
        refresh_suppression_reviews()
        return (
            db.execute(
                f"UPDATE report_suppression_rules AS r SET enabled = 1, updated_at = ? "
                f"WHERE id = ? AND {VERIFIED_RULE_SQL}",
                (now_text(), rule_id),
            ).rowcount
            > 0
        )


def suppression_evidence(rule_ids):
    """按规则批量读取可追溯的审核快照，保留失效依据供查看。"""
    if not rule_ids:
        return {}
    placeholders = ",".join("?" for _ in rule_ids)
    rows = get_db().execute(
        f"SELECT e.*, t.id AS existing_task_id FROM report_suppression_reviews e "
        f"LEFT JOIN tasks t ON t.id = e.source_task_id WHERE rule_id IN ({placeholders}) "
        "ORDER BY is_current DESC, final_review_id DESC",
        rule_ids,
    )
    result = {}
    for row in rows:
        result.setdefault(row["rule_id"], []).append(
            {
                **json.loads(row["evidence_json"]),
                "task_id": row["existing_task_id"],
                "source_task_id": row["source_task_id"],
                "result_code": row["source_result_code"],
                "item_id": row["source_item_id"],
                "is_current": bool(row["is_current"] and not row["needs_validation"]),
            }
        )
    return result
