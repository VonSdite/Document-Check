"""任务详情的运行状态与检查项操作。"""

import json

from flask import current_app, jsonify, request

from app.checks.common_terms import COMMON_TERMS_CHECK_CODE
from app.checks.hyperlinks import HYPERLINK_CHECK_CODE
from app.checks.sensitive_terms import SENSITIVE_TERMS_CHECK_CODE
from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
)
from app.tasks.activity import (
    CANCELABLE_PHASES,
    CHECK_CANCELED_MESSAGE,
    PHASE_LABELS,
    RETRYABLE_PHASES,
    activity_label,
    request_check_cancellation,
    task_activities,
)
from app.tasks.model_output import read_model_output
from app.tasks.retries import CheckRetryError, request_check_retry
from app.web.constants import STATUS_LABELS

SINGLE_CANCEL_TASK_TYPES = {
    DOCUMENT_TASK_TYPE,
    CONSISTENCY_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
}


def detail_progress(task):
    activity = task_activities([task["id"]]).get(task["id"], {})
    status = task["status"]
    return {
        "status": status,
        "active": status in {"queued", "running", "canceling"},
        "status_label": activity_label(activity)
        if status == "running"
        else STATUS_LABELS[status],
        "progress": task["progress"],
        "checks": activity.get("checks", {}),
        "phase": activity.get("phase", ""),
        "revision": ":".join(
            str(task.get(key) or "")
            for key in (
                "status",
                "live_updated_at",
                "live_result_size",
                "finished_at",
                "result_size",
                "current_owner_name",
            )
        )
        + ":"
        + json.dumps(
            {
                code: [item.get("execution", 0), item.get("phase") == "canceled"]
                for code, item in activity.get("checks", {}).items()
                if item.get("execution") or item.get("phase") == "canceled"
            },
            sort_keys=True,
        ),
    }


def present_check_activity(task, results, progress):
    by_code = {item["code"]: item for item in results if item.get("code")}
    checks = progress["checks"]
    try:
        retry_codes = json.loads(task.get("retry_check_codes_json") or "null")
    except (ValueError, TypeError):
        retry_codes = []
    if progress["active"] or task["status"] in {"failed", "canceled"}:
        try:
            snapshot = json.loads(task.get("checks_snapshot_json") or "[]")
        except (ValueError, TypeError):
            snapshot = []
        ordered = snapshot if isinstance(snapshot, list) else []
        for code, item in checks.items():
            if not any(
                isinstance(entry, dict) and entry.get("code") == code
                for entry in ordered
            ):
                ordered.append({"code": code, "name": item["name"]})
        for item in ordered:
            if (
                isinstance(item, dict)
                and item.get("code")
                and item["code"] not in by_code
            ):
                placeholder = {
                    "code": item["code"],
                    "name": item.get("name", item["code"]),
                    "result": "",
                }
                results.append(placeholder)
                by_code[item["code"]] = placeholder
        order = {
            item.get("code"): index
            for index, item in enumerate(ordered)
            if isinstance(item, dict)
        }
        results.sort(key=lambda item: order.get(item.get("code"), len(order)))
    for result in results:
        result["uses_model"] = result.get("code") not in {
            COMMON_TERMS_CHECK_CODE,
            HYPERLINK_CHECK_CODE,
            SENSITIVE_TERMS_CHECK_CODE,
        }
        state = checks.get(result.get("code"), {})
        phase = state.get("phase")
        execution = state.get("execution", result.get("execution", 0))
        output_execution = execution + bool(state.get("retry_requested"))
        if progress["active"] and output_execution > result.get("execution", 0):
            code, name, uses_model = (
                result["code"],
                result["name"],
                result["uses_model"],
            )
            result.clear()
            result.update(code=code, name=name, uses_model=uses_model, result="")
        if (
            not phase
            and progress["active"]
            and (
                result.get("code") in retry_codes
                if isinstance(retry_codes, list)
                else not result.get("result") and not result.get("error")
            )
        ):
            phase = "pending"
        if not phase:
            phase = (
                "canceled"
                if result.get("canceled")
                else "failed"
                if result.get("error")
                else "completed"
            )
            if (
                not progress["active"]
                and not result.get("result")
                and task["status"] in {"failed", "canceled"}
            ):
                phase = task["status"]
        result["execution_phase"] = "pending" if state.get("retry_requested") else phase
        if result["execution_phase"] == "canceled":
            result.update(canceled=True, error=CHECK_CANCELED_MESSAGE)
        result["execution"] = execution
        result["output_execution"] = output_execution
        result["execution_label"] = (
            PHASE_LABELS.get(result["execution_phase"], "")
            if progress["active"]
            else ""
        )
        result["execution_attempt"] = state.get("attempt", 0)
        result["can_cancel"] = (
            task["status"] in {"queued", "running"}
            and task["task_type"] in SINGLE_CANCEL_TASK_TYPES
            and (phase in CANCELABLE_PHASES or state.get("retry_requested"))
        )
        result["can_retry"] = (
            (task["task_type"] in SINGLE_CANCEL_TASK_TYPES or not progress["active"])
            and task["status"] != "canceling"
            and progress["phase"] != "finalizing"
            and phase in RETRYABLE_PHASES
            and not state.get("retry_requested")
        )
    return results


def cancel_check(task):
    if task["task_type"] not in SINGLE_CANCEL_TASK_TYPES:
        return {"error": "图片和视频使用合并请求，请通过任务列表取消整个任务。"}, 400
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return {"error": "检查项取消请求格式无效。"}, 400
    code = str(data.get("code") or "")
    execution = data.get("execution")
    if execution is not None and (type(execution) is not int or execution < 0):
        return {"error": "检查项取消请求格式无效。"}, 400
    phase = request_check_cancellation(
        task["id"], task.get("claim_token"), code, execution=execution
    )
    if not phase:
        return {"error": "此检查项当前无法取消，请刷新状态。"}, 409
    return {"status": phase, "code": code}


def retry_check(task):
    if task["task_type"] not in SINGLE_CANCEL_TASK_TYPES and task["status"] in {
        "queued",
        "running",
        "canceling",
    }:
        return {"error": "图片和视频请在任务结束后重试单项。"}, 409
    data = request.get_json(silent=True)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("code"), str)
        or type(data.get("execution")) is not int
        or data["execution"] < 0
    ):
        return {"error": "检查项重试请求格式无效。"}, 400
    try:
        return request_check_retry(task["id"], data["code"], data["execution"])
    except CheckRetryError as exc:
        return {"error": str(exc)}, 409


def model_output_response(task):
    try:
        cursor = int(request.args.get("cursor", "0"))
    except ValueError:
        cursor = None
    if cursor is None or not 0 <= cursor <= 2**63 - 1:
        return {"error": "模型输出游标无效。"}, 400
    payload = (
        {"events": [], "cursor": cursor, "more": False, "reset": False}
        if request.args.get("state_only") == "1"
        else read_model_output(current_app, task["id"], cursor)
    )
    payload["active"] = task["status"] in {"queued", "running", "canceling"}
    payload["status"] = task["status"]
    activity = task_activities([task["id"]]).get(task["id"], {})
    payload["checks"] = activity.get("checks", {})
    payload["phase"] = activity.get("phase", "")
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response
