"""任务与单项手动重试：保留模型快照、结果和任务并发边界。"""

import json

from app.contracts.task_types import IMAGE_TASK_TYPE, VIDEO_TASK_TYPE
from app.persistence.connection import get_db, now_text
from app.tasks.activity import RETRYABLE_PHASES, load_activity, save_activity
from app.tasks.runtime.common import _check_results_from_json
from app.tasks.runtime.state import _check_result_failed
from app.tasks.selection import _check_items_from_snapshot, _stored_retry_check_codes


class TaskRetryError(RuntimeError):
    pass


class CheckRetryError(TaskRetryError):
    pass


def retry_check_codes_for_task(task) -> list[str]:
    task = dict(task)
    status = task.get("status")
    if status not in {"failed", "partial", "canceled"}:
        raise TaskRetryError("仅失败、部分完成或已取消任务可重试。")
    snapshot_raw = task.get("checks_snapshot_json")
    try:
        snapshot_value = json.loads(snapshot_raw) if snapshot_raw else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise TaskRetryError("原任务缺少有效的检查项快照，无法重试。") from exc
    snapshot = _check_items_from_snapshot(snapshot_raw)
    if (
        not isinstance(snapshot_value, list)
        or not snapshot_value
        or len(snapshot) != len(snapshot_value)
    ):
        raise TaskRetryError("原任务缺少有效的检查项快照，无法重试。")
    results = {
        str(item.get("code") or "").strip(): item
        for item in _check_results_from_json(task.get("result_json"))
    }
    try:
        stored_codes = _stored_retry_check_codes(task)
    except RuntimeError as exc:
        raise TaskRetryError(str(exc)) from exc
    missing_scope = set(stored_codes) if stored_codes is not None else None
    codes = []
    for item in snapshot:
        code = item["code"]
        previous = results.get(code)
        if previous is not None:
            if previous.get("canceled") or _check_result_failed(previous):
                codes.append(code)
        elif status in {"failed", "canceled"} and (
            missing_scope is None or code in missing_scope
        ):
            codes.append(code)
    if not codes:
        raise TaskRetryError("任务没有可重试的未完成检查项。")
    return codes


def request_task_retry(task_id: int) -> int:
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute(
            "SELECT status, checks_snapshot_json, result_json, retry_check_codes_json, "
            "provider_id, owner_subject FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise TaskRetryError("任务已不存在，无法重试。")
        codes = retry_check_codes_for_task(task)
        if task["provider_id"] is None or not task["owner_subject"]:
            raise TaskRetryError("原任务的模型提供商信息不完整，无法重试。")
        provider = db.execute(
            "SELECT api_key FROM user_model_providers WHERE id=? AND owner_subject=?",
            (task["provider_id"], task["owner_subject"]),
        ).fetchone()
        if provider is None:
            raise TaskRetryError("原任务使用的模型提供商已不存在，无法重试。")
        now = now_text()
        db.execute(
            "UPDATE tasks SET status='queued', progress=0, cancel_requested=0, "
            "retry_check_codes_json=?, api_key=?, claim_token=NULL, lease_expires_at=NULL, "
            "summary=?, error=NULL, updated_at=?, started_at=NULL, finished_at=NULL WHERE id=?",
            (
                json.dumps(codes),
                provider["api_key"],
                f"已进入重试队列，等待重跑 {len(codes)} 个未完成检查项。",
                now,
                task_id,
            ),
        )
        previous = {
            str(item.get("code") or "").strip(): item
            for item in _check_results_from_json(task["result_json"])
        }
        code_set = set(codes)
        save_activity(
            db,
            task_id,
            {
                "claim_token": None,
                "phase": "preparing",
                "checks": {
                    item["code"]: {
                        "name": item["name"],
                        "phase": "pending",
                        "execution": previous.get(item["code"], {}).get("execution", 0)
                        + 1,
                    }
                    for item in _check_items_from_snapshot(task["checks_snapshot_json"])
                    if item["code"] in code_set
                },
            },
        )
        db.execute("DELETE FROM task_live_results WHERE task_id=?", (task_id,))
        db.commit()
        return len(codes)
    except Exception:
        db.rollback()
        raise


def request_check_retry(task_id, code, execution):
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute(
            "SELECT status, task_type, claim_token, checks_snapshot_json, result_json, "
            "retry_check_codes_json, provider_id, owner_subject FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task is None or task["status"] == "canceling":
            raise CheckRetryError("任务当前无法重试，请刷新状态。")
        if task["task_type"] in {IMAGE_TASK_TYPE, VIDEO_TASK_TYPE} and task[
            "status"
        ] in {"queued", "running"}:
            raise CheckRetryError("图片和视频请在任务结束后重试单项。")
        snapshot = _check_items_from_snapshot(task["checks_snapshot_json"])
        check = next((item for item in snapshot if item["code"] == code), None)
        if check is None:
            raise CheckRetryError("任务快照中没有此检查项，无法重试。")
        state = load_activity(db, task_id, task["claim_token"])
        active = task["status"] in {"queued", "running"}
        item = state["checks"].get(code) if active else None
        results = json.loads(task["result_json"] or "[]")
        previous = next((item for item in results if item.get("code") == code), {})
        if item is None:
            phase = "completed"
            if (
                previous.get("canceled")
                or task["status"] == "canceled"
                and not previous
            ):
                phase = "canceled"
            elif previous.get("error") or task["status"] == "failed" and not previous:
                phase = "failed"
            item = {
                "name": check["name"],
                "phase": phase,
                "execution": previous.get("execution", 0),
            }
            if (
                active
                and task["retry_check_codes_json"] is not None
                and code in _stored_retry_check_codes(task)
            ):
                item["phase"] = "pending"
        if item.get("execution", 0) != execution:
            raise CheckRetryError("检查项状态已变化，请刷新后重试。")
        if item.get("retry_requested"):
            db.rollback()
            return {"status": "pending", "code": code}
        if item["phase"] not in RETRYABLE_PHASES:
            raise CheckRetryError("仅已取消或失败的检查项可重试。")
        if task["status"] == "running":
            if state.get("phase") == "finalizing":
                raise CheckRetryError("任务正在整理结果，请稍后重试。")
            item["retry_requested"] = True
            state["checks"][code] = item
            if task["retry_check_codes_json"] is not None:
                codes = list(dict.fromkeys([*_stored_retry_check_codes(task), code]))
                db.execute(
                    "UPDATE tasks SET retry_check_codes_json=? WHERE id=?",
                    (json.dumps(codes), task_id),
                )
        else:
            if task["status"] == "queued":
                if task["retry_check_codes_json"] is not None:
                    codes = list(
                        dict.fromkeys([*_stored_retry_check_codes(task), code])
                    )
                    db.execute(
                        "UPDATE tasks SET retry_check_codes_json=? WHERE id=?",
                        (json.dumps(codes), task_id),
                    )
            else:
                provider = db.execute(
                    "SELECT api_key FROM user_model_providers WHERE id=? AND owner_subject=?",
                    (task["provider_id"], task["owner_subject"]),
                ).fetchone()
                if provider is None:
                    raise CheckRetryError("原模型提供商的访问凭据已不可用，无法重试。")
                now = now_text()
                db.execute(
                    "UPDATE tasks SET status='queued', progress=0, cancel_requested=0, "
                    "retry_check_codes_json=?, api_key=?, claim_token=NULL, lease_expires_at=NULL, "
                    "summary=?, error=NULL, updated_at=?, started_at=NULL, finished_at=NULL WHERE id=?",
                    (
                        json.dumps([code]),
                        provider["api_key"],
                        f"等待重新执行：{check['name']}。",
                        now,
                        task_id,
                    ),
                )
                db.execute("DELETE FROM task_live_results WHERE task_id=?", (task_id,))
                state = {"claim_token": None, "phase": "preparing", "checks": {}}
            state["checks"][code] = {
                "name": check["name"],
                "phase": "pending",
                "execution": execution + 1,
            }
        save_activity(db, task_id, state)
        db.commit()
        return {"status": "pending", "code": code}
    except Exception:
        db.rollback()
        raise
