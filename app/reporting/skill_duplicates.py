"""按相同原文证据与问题描述合并专项重复报告，保留条目来源。"""

import re
import unicodedata

from app.contracts.limits import normalize_task_report_limit

LOCAL_CHECK_CODES = {"common-terms", "sensitive-terms", "hyperlink-validity"}


def _evidence_key(item):
    values = []
    for key in ("excerpt", "description"):
        value = unicodedata.normalize("NFKC", str(item.get(key) or ""))
        value = re.sub(r"\s+", " ", value).strip().strip("。.;；")
        if not value:
            return None
        values.append(value)
    return (item.get("type"), *values)


def merge_skill_duplicates(results, priority_key):
    if not any(result.get("kind") == "specialist" for result in results):
        return
    grouped = {}
    for result in results:
        if result.get("code") in LOCAL_CHECK_CODES:
            continue
        for item in result.get("report_items", []):
            key = _evidence_key(item)
            if key:
                grouped.setdefault(key, []).append((result, item))
    for group in grouped.values():
        codes = {result.get("code") for result, _ in group}
        if len(codes) < 2 or not any(r.get("kind") == "specialist" for r, _ in group):
            continue
        feedback = {
            (
                item.get("acceptance_status"),
                item.get("rejection_reason"),
                item.get("rejection_note"),
            )
            for _, item in group
            if item.get("acceptance_status") not in {"pending", None}
        }
        if len(feedback) > 1:
            continue
        primary_result, primary = min(
            group,
            key=lambda pair: (
                pair[1].get("acceptance_status", "pending") == "pending",
                priority_key(pair[1]),
                pair[0].get("kind") != "specialist",
            ),
        )
        primary["sources"] = [
            {
                "code": result["code"],
                "name": result.get("name") or result["code"],
                "item_id": item["id"],
                "version": result.get("version"),
            }
            for result, item in group
        ]
        for result, item in group:
            if item is primary:
                continue
            location = str(item.get("location") or "").strip()
            current = str(primary.get("location") or "").strip()
            if location and location not in current:
                primary["location"] = "；".join(filter(None, (current, location)))
            item["merged_into"] = {
                "code": primary_result["code"],
                "name": primary_result.get("name"),
                "item_id": primary["id"],
            }
            result.setdefault("merged_report_items", []).append(item)
    for result in results:
        result["report_items"] = [
            item for item in result.get("report_items", []) if "merged_into" not in item
        ]
        for index, item in enumerate(result["report_items"], 1):
            item["index"] = index


def apply_task_report_limit(results, priority_key):
    limits = [
        normalize_task_report_limit(r.get("task_report_limit"))
        for r in results
        if r.get("kind") == "specialist"
    ]
    limit = max(limits, default=0)
    if not limit:
        return
    ranked = sorted(
        [(result, item) for result in results for item in result["report_items"]],
        key=lambda pair: priority_key(pair[1]),
    )
    for result, item in ranked[limit:]:
        result.setdefault("task_omitted_report_items", []).append(item)
        result["task_report_limit"] = limit
    for result in results:
        omitted_ids = {
            item["id"] for item in result.get("task_omitted_report_items", [])
        }
        result["report_items"] = [
            item for item in result["report_items"] if item["id"] not in omitted_ids
        ]
        for index, item in enumerate(result["report_items"], 1):
            item["index"] = index
