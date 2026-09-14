import json
import logging
import threading
import time
from datetime import datetime, timedelta

from app.persistence.connection import get_db, now_text
from app.tasks.activity import ACTIVITY_KEY_PREFIX

logger = logging.getLogger(__name__)


TASK_LEASE_SECONDS = 90
TASK_LEASE_RENEW_INTERVAL_SECONDS = 10
TASK_CANCEL_POLL_INTERVAL_SECONDS = 1


class TaskCanceled(Exception):
    pass


def _task_value(task, key: str):
    if hasattr(task, "keys") and key in task.keys():
        return task[key]
    if isinstance(task, dict):
        return task.get(key)
    return None


def _task_flag(task, key: str) -> bool:
    if hasattr(task, "keys") and key in task.keys():
        return bool(task[key])
    if isinstance(task, dict):
        return bool(task.get(key))
    return False


def _task_claim_token(task) -> str | None:
    value = _task_value(task, "claim_token")
    return str(value).strip() if value else None


def _task_lease_deadline_text() -> str:
    deadline = datetime.now() + timedelta(seconds=TASK_LEASE_SECONDS)
    return deadline.strftime("%Y-%m-%d %H:%M:%S")


def _start_task_cancel_watcher(
    app,
    task_id: int,
    claim_token: str | None,
    cancel_event: threading.Event,
    check_events: dict[str, threading.Event] | None = None,
):
    if not claim_token and check_events is None:
        return None, None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_watch_task_cancellation,
        args=(app, task_id, claim_token, stop_event, cancel_event, check_events),
        daemon=True,
        name=f"task-cancel-{task_id}",
    )
    thread.start()
    return stop_event, thread


def _watch_task_cancellation(
    app,
    task_id: int,
    claim_token: str,
    stop_event: threading.Event,
    cancel_event: threading.Event,
    check_events: dict[str, threading.Event] | None = None,
):
    while not stop_event.wait(TASK_CANCEL_POLL_INTERVAL_SECONDS):
        if cancel_event.is_set():
            for event in list((check_events or {}).values()):
                event.set()
            return
        try:
            with app.app_context():
                db = get_db()
                task = db.execute(
                    "SELECT t.status, t.cancel_requested, t.claim_token, s.value AS activity "
                    "FROM tasks t LEFT JOIN settings s ON s.key = ? || t.id WHERE t.id = ?",
                    (ACTIVITY_KEY_PREFIX, task_id),
                ).fetchone()
                if (
                    task is None
                    or (claim_token is not None and task["claim_token"] != claim_token)
                    or task["status"] not in {"running", "canceling"}
                ):
                    cancel_event.set()
                    for event in list((check_events or {}).values()):
                        event.set()
                    return
                if task["cancel_requested"] or task["status"] == "canceling":
                    cancel_event.set()
                    for event in list((check_events or {}).values()):
                        event.set()
                    return
                if check_events is not None:
                    activity = json.loads(task["activity"]) if task["activity"] else {}
                    checks = (
                        activity.get("checks", {})
                        if activity.get("claim_token") == task["claim_token"]
                        else {}
                    )
                    for code, event in list(check_events.items()):
                        if checks.get(code, {}).get("cancel_requested"):
                            event.set()
        except Exception:
            logger.exception("任务取消状态读取失败 task_id=%s", task_id)


def _cancel_requested(db, task_id: int, claim_token: str | None = None) -> bool:
    row = db.execute(
        "SELECT status, cancel_requested, claim_token FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return True
    if claim_token is not None and (
        row["status"] not in {"running", "canceling"}
        or row["claim_token"] != claim_token
    ):
        return True
    return bool(row["cancel_requested"])


def _update_progress(db, task_id: int, progress: int, claim_token: str | None = None):
    db.execute(
        """
        UPDATE tasks
        SET progress = ?, updated_at = ?
        WHERE id = ? AND status = 'running'
          AND (? IS NULL OR claim_token = ?)
        """,
        (progress, now_text(), task_id, claim_token, claim_token),
    )
    db.commit()


def _save_intermediate_results(
    db,
    task_id: int,
    results: list[dict],
    summary: str,
    progress: int,
    claim_token: str | None = None,
):
    updated_at = now_text()
    db.execute(
        """
        UPDATE tasks
        SET summary = ?,
            progress = MAX(progress, ?),
            updated_at = ?
        WHERE id = ? AND status = 'running'
          AND (? IS NULL OR claim_token = ?)
        """,
        (
            summary,
            progress,
            updated_at,
            task_id,
            claim_token,
            claim_token,
        ),
    )
    db.execute(
        """
        INSERT INTO task_live_results(task_id, result_json, summary, progress, updated_at)
        SELECT ?, ?, ?, ?, ?
        WHERE EXISTS (
            SELECT 1
            FROM tasks
            WHERE id = ? AND status = 'running'
              AND (? IS NULL OR claim_token = ?)
        )
        ON CONFLICT(task_id) DO UPDATE SET
            result_json = excluded.result_json,
            summary = excluded.summary,
            progress = MAX(task_live_results.progress, excluded.progress),
            updated_at = excluded.updated_at
        """,
        (
            task_id,
            json.dumps(results, ensure_ascii=False),
            summary,
            progress,
            updated_at,
            task_id,
            claim_token,
            claim_token,
        ),
    )
    db.commit()


def _progress_heartbeat(
    app,
    task_id: int,
    stop_event: threading.Event,
    start: int,
    end: int,
    timeout_seconds: int,
    claim_token: str | None = None,
):
    if end <= start:
        return
    started_at = time.monotonic()
    climb_seconds = max(60, min(int(timeout_seconds or 300), 300))
    while not stop_event.wait(8):
        elapsed = time.monotonic() - started_at
        ratio = min(1, elapsed / climb_seconds)
        progress = start + int((end - start) * ratio)
        if progress <= start:
            continue
        with app.app_context():
            db = get_db()
            row = db.execute(
                "SELECT status, progress, claim_token FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] != "running":
                return
            if claim_token is not None and row["claim_token"] != claim_token:
                return
            if row["progress"] >= progress:
                continue
            _update_progress(db, task_id, progress, claim_token)


def _mark_canceled(db, task_id: int, claim_token: str | None = None):
    canceled = db.execute(
        """
        UPDATE tasks
        SET status = 'canceled',
            progress = 0,
            api_key = NULL,
            retry_check_codes_json = NULL,
            claim_token = NULL,
            lease_expires_at = NULL,
            updated_at = ?,
            finished_at = ?
        WHERE id = ? AND status IN ('running', 'canceling')
          AND (? IS NULL OR claim_token = ?)
        """,
        (now_text(), now_text(), task_id, claim_token, claim_token),
    )
    if canceled.rowcount == 1:
        db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task_id,))
        db.execute(
            "DELETE FROM settings WHERE key = ?", (f"{ACTIVITY_KEY_PREFIX}{task_id}",)
        )
    db.commit()


def _mark_failed(
    db,
    task_id: int,
    error: str,
    results: list[dict] | None = None,
    claim_token: str | None = None,
):
    existing = db.execute(
        """
        SELECT COALESCE(live.result_json, tasks.result_json) AS result_json,
               COALESCE(live.summary, tasks.summary) AS summary
        FROM tasks
        LEFT JOIN task_live_results live ON live.task_id = tasks.id
        WHERE tasks.id = ? AND (? IS NULL OR tasks.claim_token = ?)
        """,
        (task_id, claim_token, claim_token),
    ).fetchone()
    result_json = existing["result_json"] if existing else None
    summary = existing["summary"] if existing else None
    if results:
        result_json = json.dumps(results, ensure_ascii=False)
        summary = _build_summary(results)
    failed = db.execute(
        """
        UPDATE tasks
        SET status = 'failed',
            error = ?,
            result_json = ?,
            summary = ?,
            api_key = NULL,
            claim_token = NULL,
            lease_expires_at = NULL,
            updated_at = ?,
            finished_at = ?
        WHERE id = ? AND status = 'running'
          AND (? IS NULL OR claim_token = ?)
        """,
        (
            error,
            result_json,
            summary,
            now_text(),
            now_text(),
            task_id,
            claim_token,
            claim_token,
        ),
    )
    if failed.rowcount == 1:
        db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task_id,))
        db.execute(
            "DELETE FROM settings WHERE key = ?", (f"{ACTIVITY_KEY_PREFIX}{task_id}",)
        )
    db.commit()


def _build_summary(results: list[dict]) -> str:
    failed = _failed_check_results(results)
    succeeded = [result for result in results if not _check_result_failed(result)]
    canceled = [result for result in results if result.get("canceled")]
    if canceled:
        return f"已完成 {len(succeeded)}/{len(results)} 个检查项，失败 {len(failed)} 项，已取消 {len(canceled)} 项。"
    if failed and succeeded:
        failed_names = "、".join(
            str(item.get("name") or item.get("code") or "未命名检查项")
            for item in failed
        )
        return f"已完成 {len(succeeded)}/{len(results)} 个检查项，{len(failed)} 个检查项失败：{failed_names}"
    if failed:
        failed_names = "、".join(
            str(item.get("name") or item.get("code") or "未命名检查项")
            for item in failed
        )
        return f"{len(failed)} 个检查项全部失败：{failed_names}"
    names = "、".join(item["name"] for item in results)
    return f"已完成 {len(results)} 个检查项：{names}"


def _check_result_failed(result: dict) -> bool:
    return bool(str(result.get("error") or "").strip())


def _failed_check_results(results: list[dict]) -> list[dict]:
    return [
        result
        for result in results
        if _check_result_failed(result) and not result.get("canceled")
    ]


def _failed_check_items_error(results: list[dict]) -> str:
    parts = []
    for result in results:
        name = str(result.get("name") or result.get("code") or "未命名检查项")
        error = str(result.get("error") or "检查失败").strip()
        if len(error) > 300:
            error = f"{error[:297]}..."
        parts.append(f"{name}：{error}")
    return f"{len(parts)} 个检查项失败：" + "；".join(parts)
