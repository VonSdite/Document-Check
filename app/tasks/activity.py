"""任务的轻量运行状态；按任务主键读写，并通过执行令牌隔离每次运行。"""

import json

from app.persistence.connection import get_db, now_text
from app.tasks.selection import selected_check_items

ACTIVITY_KEY_PREFIX = "task_activity:"
PHASE_LABELS = {
    "preparing": "解析",
    "pending": "待执行",
    "checking": "检查",
    "waiting": "等待",
    "thinking": "思考",
    "output": "输出",
    "retrying": "重试",
    "finalizing": "整理",
    "canceling": "取消中",
}
TERMINAL_PHASES = {"completed", "failed", "canceled"}
CANCELABLE_PHASES = {"pending", "checking", "waiting", "thinking", "output", "retrying"}


def activity_key(task_id: int) -> str:
    return f"{ACTIVITY_KEY_PREFIX}{task_id}"


def _change_activity(task_id, claim_token, change, *, allow_queued=False):
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute(
            "SELECT status, claim_token FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if (
            task is None
            or task["status"]
            not in ({"queued", "running"} if allow_queued else {"running"})
            or task["claim_token"] != claim_token
        ):
            db.rollback()
            return None
        row = db.execute(
            "SELECT value FROM settings WHERE key = ?", (activity_key(task_id),)
        ).fetchone()
        state = json.loads(row["value"]) if row else {}
        if state.get("claim_token") != task["claim_token"]:
            state = {}
        state.setdefault("claim_token", task["claim_token"])
        state.setdefault("checks", {})
        result = change(state)
        db.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (activity_key(task_id), json.dumps(state, ensure_ascii=False), now_text()),
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def initialize_activity(task_id, claim_token, *, phase="preparing", checks=()):
    def change(state):
        state["phase"] = phase
        for item in checks:
            state["checks"].setdefault(
                item["code"], {"name": item["name"], "phase": "pending"}
            )

    _change_activity(task_id, claim_token, change)


def update_check_activity(task_id, claim_token, codes, phase, attempt=None):
    def change(state):
        for code in codes:
            item = state["checks"].get(code)
            if item is None or item.get("phase") in TERMINAL_PHASES:
                continue
            if item.get("cancel_requested"):
                continue
            item["phase"] = phase
            if attempt is not None:
                item["attempt"] = attempt

    _change_activity(task_id, claim_token, change)


def finish_check_activity(task_id, claim_token, code, *, failed=False):
    def change(state):
        item = state["checks"].get(code)
        if item is None:
            return False
        canceled = bool(item.get("cancel_requested"))
        item["phase"] = "canceled" if canceled else "failed" if failed else "completed"
        return canceled

    return bool(_change_activity(task_id, claim_token, change))


def start_check_activity(task_id, claim_token, code):
    """在检查项开始前原子确认取消意图，并进入执行阶段。"""

    def change(state):
        item = state["checks"].get(code)
        if (
            item is None
            or item.get("cancel_requested")
            or item.get("phase") in TERMINAL_PHASES
        ):
            return False
        item["phase"] = "checking"
        return True

    return bool(_change_activity(task_id, claim_token, change))


def request_check_cancellation(task_id, claim_token, code):
    def change(state):
        item = state["checks"].get(code)
        if item is None:
            db = get_db()
            task = db.execute(
                "SELECT task_type, checks_json, checks_snapshot_json, retry_check_codes_json "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            try:
                selected = selected_check_items(db, task)
            except RuntimeError:
                return False
            match = next((entry for entry in selected if entry["code"] == code), None)
            if match is None:
                return False
            item = state["checks"][code] = {"name": match["name"], "phase": "pending"}
        if item is None or item.get("phase") not in CANCELABLE_PHASES | {"canceling"}:
            return False
        item.update(cancel_requested=True, phase="canceling")
        return True

    return bool(_change_activity(task_id, claim_token, change, allow_queued=True))


def task_activities(task_ids):
    task_ids = list(dict.fromkeys(task_ids))
    if not task_ids:
        return {}
    placeholders = ",".join("?" for _ in task_ids)
    rows = get_db().execute(
        f"SELECT t.id, t.claim_token, s.value FROM tasks t "
        f"JOIN settings s ON s.key = ? || t.id "
        f"WHERE t.id IN ({placeholders}) AND t.status IN ('queued', 'running', 'canceling')",
        (ACTIVITY_KEY_PREFIX, *task_ids),
    )
    activities = {}
    for row in rows:
        state = json.loads(row["value"])
        if state.get("claim_token") == row["claim_token"]:
            activities[row["id"]] = state
    return activities


def activity_label(state):
    return "解析" if state and state.get("phase") == "preparing" else "检查"


def clear_activity(task_id, claim_token):
    db = get_db()
    db.execute(
        "DELETE FROM settings WHERE key = ? "
        "AND json_extract(value, '$.claim_token') IS ?",
        (activity_key(task_id), claim_token),
    )
    db.commit()
