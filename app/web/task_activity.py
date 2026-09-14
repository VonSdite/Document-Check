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
    PHASE_LABELS,
    TERMINAL_PHASES,
    activity_label,
    request_check_cancellation,
    task_activities,
)
from app.tasks.model_output import read_model_output
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
        "revision": ":".join(
            str(task.get(key) or "")
            for key in (
                "status",
                "live_updated_at",
                "live_result_size",
                "finished_at",
                "result_size",
            )
        ),
    }


def present_check_activity(task, results, progress):
    by_code = {item["code"]: item for item in results if item.get("code")}
    checks = progress["checks"]
    if progress["active"]:
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
        if (
            not phase
            and progress["active"]
            and not result.get("result")
            and not result.get("error")
        ):
            phase = "pending"
        result["execution_label"] = (
            PHASE_LABELS.get(phase, "")
            if progress["active"] and phase not in TERMINAL_PHASES
            else ""
        )
        result["execution_attempt"] = state.get("attempt", 0)
        result["can_cancel"] = (
            task["status"] == "running"
            and task["task_type"] in SINGLE_CANCEL_TASK_TYPES
            and phase in CANCELABLE_PHASES
        )
    return results


def cancel_check(task):
    if task["task_type"] not in SINGLE_CANCEL_TASK_TYPES:
        return {"error": "图片和视频使用合并请求，请通过任务列表取消整个任务。"}, 400
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return {"error": "检查项取消请求格式无效。"}, 400
    code = str(data.get("code") or "")
    if not request_check_cancellation(task["id"], task.get("claim_token"), code):
        return {"error": "此检查项已结束或尚未进入模型请求阶段，请刷新状态。"}, 409
    return {"status": "canceling", "code": code}


def model_output_response(task):
    try:
        cursor = int(request.args.get("cursor", "0"))
    except ValueError:
        cursor = None
    if cursor is None or not 0 <= cursor <= 2**63 - 1:
        return {"error": "模型输出游标无效。"}, 400
    payload = read_model_output(current_app, task["id"], cursor)
    payload["active"] = task["status"] in {"queued", "running", "canceling"}
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response
