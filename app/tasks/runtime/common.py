import json

from app.contracts.limits import (
    DEFAULT_ISSUE_OUTPUT_LIMIT,
    normalize_issue_output_limit,
)
from app.persistence.settings import get_setting

STREAM_SNAPSHOT_INTERVAL_SECONDS = 5.0
STREAM_SNAPSHOT_MIN_CHAR_GROWTH = 256


def _merge_check_results(base_results: list[dict], updates: list[dict]) -> list[dict]:
    updates_by_code = {
        str(result.get("code") or "").strip(): result
        for result in updates
        if isinstance(result, dict) and str(result.get("code") or "").strip()
    }
    merged = []
    placed_codes = set()
    for result in base_results:
        if not isinstance(result, dict):
            continue
        code = str(result.get("code") or "").strip()
        if code and code in updates_by_code:
            if code not in placed_codes:
                merged.append(dict(updates_by_code[code]))
                placed_codes.add(code)
            continue
        merged.append(dict(result))

    for result in updates:
        if not isinstance(result, dict):
            continue
        code = str(result.get("code") or "").strip()
        if code:
            if code in placed_codes:
                continue
            merged.append(dict(result))
            placed_codes.add(code)
        else:
            merged.append(dict(result))
    return merged


def _task_value(task, key: str):
    if hasattr(task, "keys") and key in task.keys():
        return task[key]
    if isinstance(task, dict):
        return task.get(key)
    return None


def _document_meta(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _issue_output_limit() -> int:
    return normalize_issue_output_limit(
        _int_setting("issue_output_limit", DEFAULT_ISSUE_OUTPUT_LIMIT)
    )


def _int_setting(key: str, default: int) -> int:
    value = get_setting(key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ordered_results(
    check_items: list[dict], completed: dict[str, dict], partial: dict[str, dict]
) -> list[dict]:
    results = []
    for item in check_items:
        result = completed.get(item["code"]) or partial.get(item["code"])
        if result:
            results.append(result)
    return results
