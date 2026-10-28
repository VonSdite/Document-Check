"""在检查项并发额度内顺序执行独立轮次，并复用已成功的轮次。"""

import json

from app.models.client import LLMError
from app.reporting.rounds import build_round_result


def _parse_round_report(content: str) -> dict:
    candidate = content.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines[-1].strip() == "```":
            candidate = "\n".join(lines[1:-1])
    try:
        report = json.loads(candidate)
    except (TypeError, ValueError) as exc:
        raise LLMError("本轮未返回完整的结构化报告，请重试该轮。") from exc
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("items"), list)
        or any(not isinstance(item, dict) for item in report["items"])
    ):
        raise LLMError("本轮报告缺少有效的问题列表，请重试该轮。")
    if any(
        not isinstance(item.get("description"), str)
        or not item["description"].strip()
        or item.get("status") not in {"issue", "suggestion"}
        for item in report["items"]
    ):
        raise LLMError("本轮报告的问题条目缺少描述或有效判定，请重试该轮。")
    return report


def run_document_rounds(
    *,
    code,
    planned,
    previous,
    previous_report=None,
    run_round,
    ensure_active,
    on_update,
    on_round_start,
    sanitize=lambda content: content,
):
    retained = {}
    for entry in previous or []:
        if (
            isinstance(entry, dict)
            and type(entry.get("round")) is int
            and 1 <= entry["round"] <= planned
            and entry.get("status") == "completed"
            and isinstance(entry.get("report"), dict)
        ):
            retained[entry["round"]] = entry
    rounds = list(retained.values())
    previous_items = (previous_report or {}).get("items", [])

    def snapshot():
        nonlocal previous_items
        result = build_round_result(
            code, planned, rounds, previous_items=previous_items
        )
        previous_items = result["structured_report"]["items"]
        return result

    on_update(snapshot())
    for number in range(1, planned + 1):
        if number in retained:
            continue
        ensure_active()
        on_round_start(
            number, sum(entry.get("status") == "completed" for entry in rounds)
        )
        content = ""
        try:
            content = run_round(number)
            report = _parse_round_report(sanitize(content))
        except (LLMError, RuntimeError) as exc:
            ensure_active()
            rounds.append(
                {
                    "round": number,
                    "status": "failed",
                    "error": str(exc),
                    "result": content,
                }
            )
        else:
            rounds.append(
                {
                    "round": number,
                    "status": "completed",
                    "result": content,
                    "report": report,
                }
            )
        on_update(snapshot())
    return snapshot()
