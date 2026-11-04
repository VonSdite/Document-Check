"""在检查项并发额度内顺序执行独立轮次，并复用已成功的轮次。"""

from app.models.client import LLMError, LLMIncompleteResponseError
from app.reporting.rounds import build_round_result
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
            report = parse_check_report(sanitize(content))
        except (LLMError, RuntimeError) as exc:
            ensure_active()
            if isinstance(exc, LLMIncompleteResponseError):
                content = exc.partial_content or content
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
