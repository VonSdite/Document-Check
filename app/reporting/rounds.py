"""按原文证据、位置和相近描述归并检查轮次，保留每条发现的来源。"""

import hashlib
import json
import re
from copy import deepcopy
from difflib import SequenceMatcher

from app.reporting.constants import REPORT_SEVERITY_ORDER
from app.reporting.service import (
    _merge_report_evidence_refs,
    _merge_report_field_values,
    _normalize_report_description,
    _report_description_similarity,
    _result_report_items,
)


def _normalized(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _same_finding(left: dict, right: dict) -> bool:
    left_fingerprint = left.get("evidence_fingerprint")
    right_fingerprint = right.get("evidence_fingerprint")
    if left_fingerprint and right_fingerprint and left_fingerprint != right_fingerprint:
        return False
    if all(
        _normalized(left.get(field)) == _normalized(right.get(field))
        for field in ("category", "location", "excerpt", "description")
    ) and _normalized(left.get("description")):
        return True
    excerpt = _normalized(left.get("excerpt"))
    if not excerpt or excerpt != _normalized(right.get("excerpt")):
        return False
    if _normalized(left.get("category")) != _normalized(right.get("category")):
        return False
    left_description = _normalize_report_description(left.get("description"))
    right_description = _normalize_report_description(right.get("description"))
    if not left_description or not right_description:
        return False
    if left_description == right_description:
        return True
    location = _normalized(left.get("location"))
    if not location or location != _normalized(right.get("location")):
        return False
    if re.findall(r"\d+(?:\.\d+)?", left_description) != re.findall(
        r"\d+(?:\.\d+)?", right_description
    ):
        return False
    if any(
        operation == "replace"
        for operation, *_ in SequenceMatcher(
            None, left_description, right_description
        ).get_opcodes()
    ):
        return False
    return _report_description_similarity(left_description, right_description) >= 0.78


def _merge_findings(code: str, rounds: list[dict]) -> list[dict]:
    findings = []
    for round_result in rounds:
        if round_result.get("status") != "completed" and not (
            round_result.get("agent") and round_result.get("report")
        ):
            continue
        round_number = round_result["round"]
        for item in _result_report_items(
            {"code": code, "structured_report": round_result["report"]}
        ):
            if item.get("type") == "non_issue":
                continue
            existing = next(
                (finding for finding in findings if _same_finding(finding, item)),
                None,
            )
            if existing is None:
                identity = json.dumps(
                    [
                        code,
                        *[
                            item.get(field, "")
                            for field in (
                                "category",
                                "location",
                                "excerpt",
                                "description",
                            )
                        ],
                    ],
                    ensure_ascii=False,
                )
                existing = {
                    **{
                        field: item.get(field, "")
                        for field in (
                            "category",
                            "location",
                            "excerpt",
                            "description",
                            "impact",
                            "suggestion",
                            "severity",
                            "confidence",
                            "evidence_fingerprint",
                        )
                    },
                    "id": "round-"
                    + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
                    "status": item["type"],
                    "evidence_refs": item.get("evidence_refs", []),
                    "source_rounds": [],
                    "source_items": [],
                    "round_conflict": False,
                }
                findings.append(existing)
            elif existing["status"] != item["type"]:
                existing["round_conflict"] = True
            previous_numbers = set(
                re.findall(r"\d+(?:\.\d+)?", existing.get("suggestion", ""))
            )
            current_numbers = set(
                re.findall(r"\d+(?:\.\d+)?", item.get("suggestion", ""))
            )
            if (
                previous_numbers
                and current_numbers
                and previous_numbers != current_numbers
            ):
                existing["round_conflict"] = True
            if REPORT_SEVERITY_ORDER.get(
                item.get("severity"), 99
            ) < REPORT_SEVERITY_ORDER.get(existing.get("severity"), 99):
                existing["severity"] = item["severity"]
            for field in ("location", "impact", "suggestion"):
                existing[field] = _merge_report_field_values(
                    existing.get(field), item.get(field)
                )
            existing["evidence_refs"] = _merge_report_evidence_refs(
                existing.get("evidence_refs"), item.get("evidence_refs")
            )
            if round_number not in existing["source_rounds"]:
                existing["source_rounds"].append(round_number)
            existing["source_items"].append(
                {"round": round_number, "item": item["index"]}
            )
    for finding in findings:
        if finding["round_conflict"]:
            finding["status"] = "suggestion"
            finding["confidence"] = "low"
    return findings


def _merge_agent_rounds(report: dict, rounds: list[dict]) -> dict | None:
    agent_rounds = [entry for entry in rounds if entry.get("agent")]
    if not agent_rounds:
        return None
    for field in ("pending_items", "withdrawn_items"):
        records = {}
        for entry in agent_rounds:
            for candidate in entry.get("report", {}).get(field, []):
                value = {key: value for key, value in candidate.items() if key != "id"}
                identity = hashlib.sha256(
                    json.dumps(
                        {
                            key: item
                            for key, item in value.items()
                            if key != "validation"
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()[:24]
                record = records.setdefault(
                    identity,
                    {**value, "id": f"round-{field}-{identity}", "source_rounds": []},
                )
                record["source_rounds"].append(entry["round"])
        report[field] = list(records.values())
    metas = [entry["agent"] for entry in agent_rounds]
    metadata = deepcopy(metas[-1])
    for field in (
        "model_calls",
        "known_input_tokens",
        "known_output_tokens",
        "elapsed_seconds",
    ):
        metadata[field] = sum(meta[field] for meta in metas)
    for field in ("requests", "nodes", "diagnostics", "tool_trace"):
        metadata[field] = [value for meta in metas for value in meta.get(field, [])]
    for field in ("review_reused", "reference_retry_batches", "saturated_batches"):
        metadata[field] = sum(meta.get(field, 0) for meta in metas)
    metadata["plans_reused"] = sum(bool(meta.get("plan_reused")) for meta in metas)
    metadata["round_focuses"] = [
        {"round": entry["round"], "focus": entry["agent"].get("round_focus", "")}
        for entry in agent_rounds
    ]
    metadata["decision_audit"] = {
        f"{entry['round']}:{candidate_id}": {**audit, "round": entry["round"]}
        for entry in agent_rounds
        for candidate_id, audit in entry["agent"].get("decision_audit", {}).items()
    }
    metadata["followup_rounds"] = [
        {"round": entry["round"], **entry["agent"]["followup"]}
        for entry in agent_rounds
        if entry["agent"].get("followup")
    ]
    metadata["usage_complete"] = all(meta["usage_complete"] for meta in metas)
    metadata["cost"] = (
        sum(meta["cost"] for meta in metas)
        if all(meta.get("cost") is not None for meta in metas)
        else None
    )
    coverage = metadata["coverage"]
    coverage["checked_blocks"] = min(
        meta["coverage"]["checked_blocks"] for meta in metas
    )
    for field in ("all_checked", "text_complete"):
        coverage[field] = all(meta["coverage"][field] for meta in metas)
    return metadata


def build_round_result(
    code: str, planned: int, rounds: list[dict], *, previous_items=()
) -> dict:
    rounds = sorted(deepcopy(rounds), key=lambda entry: entry["round"])
    completed = sum(entry.get("status") == "completed" for entry in rounds)
    failed = sum(entry.get("status") == "failed" for entry in rounds)
    items = _merge_findings(code, rounds)
    used_ids = set()
    for item in items:
        origins = {(entry["round"], entry["item"]) for entry in item["source_items"]}
        for previous in previous_items:
            previous_id = previous.get("id")
            if not previous_id or previous_id in used_ids:
                continue
            previous_origins = {
                (entry["round"], entry["item"])
                for entry in previous.get("source_items", [])
            }
            if origins & previous_origins:
                item["id"] = previous_id
                used_ids.add(previous_id)
                break
    summary = f"计划 {planned} 轮，已完成 {completed} 轮，失败 {failed} 轮，合并后共 {len(items)} 条。"
    report = {"summary": summary, "items": items}
    agent_metadata = _merge_agent_rounds(report, rounds)
    result = {
        "result": json.dumps(report, ensure_ascii=False),
        "structured_report": report,
        "round_results": rounds,
        "round_summary": {"planned": planned, "completed": completed, "failed": failed},
    }
    if agent_metadata is not None:
        result["agent"] = agent_metadata
    if failed:
        result["error"] = (
            f"{failed}/{planned} 轮检查失败，已保留 {completed} 轮成功结果，可重试未完成轮次。"
        )
    return result
