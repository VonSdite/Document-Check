"""在检查项并发额度内顺序执行独立轮次，并复用已成功的轮次。"""

import json

from app.models.client import LLMError, LLMIncompleteResponseError
from app.reporting.rounds import build_round_result
from app.reporting.service import _result_report_items
from app.tasks.runtime.report_validation import parse_check_report


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
    original_reports = {}
    for entry in previous or []:
        if (
            isinstance(entry, dict)
            and type(entry.get("round")) is int
            and 1 <= entry["round"] <= planned
            and entry.get("status") == "completed"
            and isinstance(entry.get("report"), dict)
        ):
            original_reports[entry["round"]] = entry["report"]
            retained[entry["round"]] = {
                **entry,
                "report": json.loads(
                    sanitize(json.dumps(entry["report"], ensure_ascii=False))
                ),
            }
    rounds = list(retained.values())
    # 过滤会改变来源条目的序号，按原始内容重映射，保持复核标识对应同一发现。
    origins = {}
    for number, original_report in original_reports.items():
        filtered = iter(_report_items(code, retained[number]["report"]))
        current = next(filtered, None)
        for original in _report_items(code, original_report):
            if current and (original["text"], original["type"]) == (
                current["text"],
                current["type"],
            ):
                origins[number, original["index"]] = current["index"]
                current = next(filtered, None)
    previous_items = [
        {
            **item,
            "source_items": [
                {
                    "round": origin["round"],
                    "item": origins[origin["round"], origin["item"]],
                }
                for origin in item.get("source_items", [])
                if (origin["round"], origin["item"]) in origins
            ],
        }
        for item in (previous_report or {}).get("items", [])
    ]

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
            report = parse_check_report(sanitize(content))
        except (LLMError, RuntimeError) as exc:
            ensure_active()
            if isinstance(exc, LLMIncompleteResponseError):
                content = exc.partial_content or content
            failure = {
                "round": number,
                "status": "failed",
                "error": str(exc),
                "result": content,
            }
            if isinstance(exc, LLMError) and exc.authentication_failed:
                failure["authentication_failed"] = True
            rounds.append(failure)
            if isinstance(exc, LLMError) and not exc.retryable:
                on_update(snapshot())
                break
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


def _report_items(code, report):
    return _result_report_items({"code": code, "structured_report": report})
