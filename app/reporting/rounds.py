"""按原文证据、位置和相近描述归并独立检查轮次，保留每条发现的来源。"""

import hashlib
import json
import re
import unicodedata
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
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).replace("−", "-")


def _compatible_numbers(left, right) -> bool:
    # 数值保留小数点、正负号与单位大小写；上下文可以补充编号或例子。
    numbers = []
    for value in (left, right):
        text = _normalized(value)
        tokens = set()
        for match in re.finditer(r"([+-]?\d+(?:\.\d+)*)([a-zA-ZµΩ%]+)?", text):
            # 字母开头的标识符按编号处理，例如设备型号和标准编号。
            in_identifier = match.start() > 0 and re.fullmatch(
                r"[a-zA-Z]", text[match.start() - 1]
            )
            tokens.add(match.group(1) if in_identifier else match.group())
        numbers.append(tokens)
    return numbers[0] <= numbers[1] or numbers[1] <= numbers[0]


def _compatible_claims(left: dict, right: dict) -> bool:
    if not _compatible_numbers(left.get("description"), right.get("description")):
        return False
    raw_descriptions = [_normalized(item.get("description")) for item in (left, right)]
    if re.findall(
        r"(?:[<>]=?|[≤≥≠])[+-]?\d+(?:\.\d+)*", raw_descriptions[0]
    ) != re.findall(r"(?:[<>]=?|[≤≥≠])[+-]?\d+(?:\.\d+)*", raw_descriptions[1]):
        return False
    if (
        raw_descriptions[0] != raw_descriptions[1]
        and raw_descriptions[0].casefold() == raw_descriptions[1].casefold()
    ):
        return False
    descriptions = [
        _normalize_report_description(item.get("description")) for item in (left, right)
    ]
    edits = [
        (
            operation,
            descriptions[0][left_start:left_end],
            descriptions[1][right_start:right_end],
        )
        for operation, left_start, left_end, right_start, right_end in SequenceMatcher(
            None, *descriptions, autojunk=False
        ).get_opcodes()
        if operation != "equal"
    ]
    # 仅替换关键短语的近似句分别保留，覆盖对象、方向和单位差异。
    replaced_length = sum(
        max(len(left_text), len(right_text))
        for operation, left_text, right_text in edits
        if operation == "replace"
    )
    if 0 < replaced_length <= 3:
        return False
    return not any(
        operation == "replace" and left_text.isdecimal() and right_text.isdecimal()
        for operation, left_text, right_text in edits
    )


def _shared_evidence(left, right) -> bool:
    if not _compatible_numbers(left, right):
        return False
    excerpts = [
        re.sub(r"[^\w.+%/<>=×*-]+", "", _normalized(value)) for value in (left, right)
    ]
    shorter, longer = sorted(excerpts, key=len)
    if not shorter:
        return False
    if shorter == longer:
        return True
    if len(shorter) < 6:
        return False
    if shorter in longer:
        return True
    matcher = SequenceMatcher(None, *excerpts, autojunk=False)
    longest = matcher.find_longest_match().size
    # 同一句证据可以带不同上下文，也可以按不同顺序引用多个片段。
    if longest >= max(6, len(shorter) * 0.6):
        return True
    bigrams = [
        {excerpt[index : index + 2] for index in range(len(excerpt) - 1)}
        for excerpt in excerpts
    ]
    overlap = 2 * len(bigrams[0] & bigrams[1]) / sum(map(len, bigrams))
    return longest >= 6 and (matcher.ratio() >= 0.85 or overlap >= 0.85)


def _same_finding(left: dict, right: dict) -> bool:
    if all(
        _normalized(left.get(field)) == _normalized(right.get(field))
        for field in ("category", "location", "excerpt", "description")
    ) and _normalized(left.get("description")):
        return True
    if not _shared_evidence(left.get("excerpt"), right.get("excerpt")):
        return False
    left_description = _normalize_report_description(left.get("description"))
    right_description = _normalize_report_description(right.get("description"))
    if not left_description or not right_description:
        return False
    if not _compatible_claims(left, right):
        return False
    # 描述改写较多时，以相近的修改建议补充判定。
    similarity = _report_description_similarity(left_description, right_description)
    return similarity >= 0.6 or (
        similarity >= 0.45
        and _report_description_similarity(
            left.get("suggestion"), right.get("suggestion")
        )
        >= 0.65
    )


def _merge_findings(code: str, rounds: list[dict]) -> list[dict]:
    groups = []
    for round_result in rounds:
        if round_result.get("status") != "completed":
            continue
        round_number = round_result["round"]
        for item in _result_report_items(
            {"code": code, "structured_report": round_result["report"]}
        ):
            if item.get("type") == "non_issue":
                continue
            group = next(
                (
                    (finding, sources)
                    for finding, sources in groups
                    if any(_same_finding(source, item) for source in sources)
                    and all(_compatible_claims(source, item) for source in sources)
                ),
                None,
            )
            if group is None:
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
                sources = []
                groups.append((existing, sources))
            else:
                existing, sources = group
                if existing["status"] != item["type"]:
                    existing["round_conflict"] = True
            if any(
                not _compatible_numbers(
                    source.get("suggestion"), item.get("suggestion")
                )
                for source in sources
            ):
                existing["round_conflict"] = True
            sources.append(item)
            if REPORT_SEVERITY_ORDER.get(
                item.get("severity"), 99
            ) < REPORT_SEVERITY_ORDER.get(existing.get("severity"), 99):
                existing["severity"] = item["severity"]
            for field in ("location", "excerpt", "description", "impact", "suggestion"):
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
    for finding, _ in groups:
        if finding["round_conflict"]:
            finding["status"] = "suggestion"
            finding["confidence"] = "low"
    return [finding for finding, _ in groups]


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
    result = {
        "result": json.dumps(report, ensure_ascii=False),
        "structured_report": report,
        "round_results": rounds,
        "round_summary": {"planned": planned, "completed": completed, "failed": failed},
    }
    if failed:
        result["error"] = (
            f"{failed}/{planned} 轮检查失败，已保留 {completed} 轮成功结果，可重试未完成轮次。"
        )
    return result
