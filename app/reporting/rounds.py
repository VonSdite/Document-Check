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
    return re.sub(r"\s+", "", text).replace("−", "-").replace("–", "-")


_REFERENCE_NUMBER = r"\d+(?:[.-]\d+)*"
_REFERENCE_LIST = rf"{_REFERENCE_NUMBER}(?:(?:、|和|与|及|/){_REFERENCE_NUMBER})*"
_SECTION_NUMBER = r"\d+(?:\.\d+)+"
_SECTION_LIST = rf"{_SECTION_NUMBER}(?:(?:、|和|与|及|/){_SECTION_NUMBER})*"
_REFERENCE_BOUNDARY = r"(?<![\da-zA-Z.+<>=≤≥为是至达于-])"
_QUOTE = r'[“"‘`]([^”"’`\n]+)[”"’`]'
_CHANGE_TO = r"(?:统一)?(?:更正|修正|修改|替换|调整|改)为"
_COUNTS = str.maketrans("零一二两三四五六七八九", "01223456789")


def _without_references(value, location="") -> str:
    text = _normalized(value)
    # 根据引用标记、章节语境和条目位置识别编号，保留参数陈述中的数值。
    text = re.sub(
        rf"(?:表|图|步骤){_REFERENCE_LIST}(?![\d.a-zA-Z%µΩ])"
        rf"|第?{_REFERENCE_LIST}(?:章|节|页)",
        "",
        text,
    )
    text = re.sub(
        rf"{_REFERENCE_BOUNDARY}{_SECTION_LIST}"
        r"(?=规定|要求|未|写|说明|须知|步骤|标题|正文|同类表述|均使用)",
        "",
        text,
    )
    text = re.sub(rf"(?:同章|正文|参见|见){_SECTION_LIST}(?![\d.a-zA-Z%µΩ])", "", text)
    location = _normalized(location)
    for number in set(re.findall(r"\d+(?:\.\d+)+", location)):
        text = re.sub(
            rf"{_REFERENCE_BOUNDARY}{re.escape(number)}(?=中|的|、|和|与|及|/)",
            "",
            text,
        )
        for heading in re.findall(
            re.escape(number) + r"([\u4e00-\u9fff]{2,})", location
        ):
            text = re.sub(
                rf"{_REFERENCE_BOUNDARY}{re.escape(number)}(?={re.escape(heading)})",
                "",
                text,
            )
    return text


def _corrections(item: dict) -> list[tuple[str, str]]:
    """提取能在原文中定位的明确替换或补字指令。"""
    suggestion = _normalized(item.get("suggestion"))
    excerpt = _normalized(item.get("excerpt"))
    corrections = re.findall(_QUOTE + _CHANGE_TO + _QUOTE, suggestion)
    for anchor, side, addition in re.findall(
        _QUOTE + r"(前|后)(?:补充|补上|添加|加上|插入)" + _QUOTE, suggestion
    ):
        corrections.append(
            (anchor, anchor + addition if side == "后" else addition + anchor)
        )
    return [
        (before, after)
        for before, after in corrections
        if before != after and before in excerpt
    ]


def _correction_edits(item: dict) -> list[tuple[str, str, str, str]]:
    corrections = _corrections(item)
    explicit = bool(corrections)
    if not explicit:
        excerpt = _normalized(item.get("excerpt"))
        for target in re.findall(
            _CHANGE_TO + _QUOTE, _normalized(item.get("suggestion"))
        ):
            if SequenceMatcher(None, excerpt, target, autojunk=False).ratio() >= 0.6:
                corrections.append((excerpt, target))
    edits = []
    quoted_description = re.findall(_QUOTE, _normalized(item.get("description")))
    for before, after in corrections:
        for operation, start, end, new_start, new_end in SequenceMatcher(
            None, before, after, autojunk=False
        ).get_opcodes():
            if operation == "equal":
                continue
            removed, added = before[start:end], after[new_start:new_end]
            if not explicit and not any(
                part and part in quote
                for part in (removed, added)
                for quote in quoted_description
            ):
                continue
            edits.append(
                (
                    removed,
                    added,
                    before[max(0, start - 6) : start],
                    before[end : end + 6],
                )
            )
    return edits


def _same_correction(left: dict, right: dict) -> bool:
    for removed, added, prefix, suffix in _correction_edits(left):
        for other_removed, other_added, other_prefix, other_suffix in _correction_edits(
            right
        ):
            if (removed, added) != (other_removed, other_added):
                continue
            shared_prefix = min(len(prefix), len(other_prefix))
            shared_suffix = min(len(suffix), len(other_suffix))
            if (
                len(removed) + shared_prefix + shared_suffix >= 2
                and (
                    not shared_prefix
                    or prefix[-shared_prefix:] == other_prefix[-shared_prefix:]
                )
                and suffix[:shared_suffix] == other_suffix[:shared_suffix]
            ):
                return True
    return False


def _claim_text(item: dict, *, same_correction=False) -> str:
    text = _without_references(item.get("description"), item.get("location"))
    text = re.sub(
        r"(?<![零一二两三四五六七八九十百千万])([零一二两三四五六七八九])(?=根|个|条|台|倍)",
        lambda match: match[1].translate(_COUNTS),
        text,
    )
    if same_correction:
        # 明确的相同修改允许使用不同举例，实际参数陈述继续参与校验。
        text = re.sub(r"\((?:如|例如|比如)[^()]*\)", "", text)
        text = re.sub(r'(?:[“"]\d+[×*]\d+[”"])+等', "", text)
    return text


def _compatible_numbers(left, right) -> bool:
    # 数值保留小数点、正负号与单位大小写；上下文可以补充编号或例子。
    numbers = []
    for value in (left, right):
        text = _normalized(value)
        values, identifiers = set(), set()
        for match in re.finditer(r"([+-]?\d+(?:\.\d+)*)([a-zA-ZµΩ%]+)?", text):
            # 字母开头的标识符按编号处理，例如设备型号和标准编号。
            in_identifier = match.start() > 0 and re.fullmatch(
                r"[a-zA-Z]", text[match.start() - 1]
            )
            (identifiers if in_identifier else values).add(match.group())
        numbers.append((values, identifiers))
    return all(a <= b or b <= a for a, b in zip(*numbers))


def _compatible_claims(left: dict, right: dict) -> bool:
    same_correction = _same_correction(left, right)
    raw_descriptions = [
        _claim_text(item, same_correction=same_correction) for item in (left, right)
    ]
    if not _compatible_numbers(*raw_descriptions):
        return False
    if re.findall(
        r"(?:[<>]=?|[≤≥≠])[+-]?\d+(?:\.\d+)*", raw_descriptions[0]
    ) != re.findall(r"(?:[<>]=?|[≤≥≠])[+-]?\d+(?:\.\d+)*", raw_descriptions[1]):
        return False
    if (
        raw_descriptions[0] != raw_descriptions[1]
        and raw_descriptions[0].casefold() == raw_descriptions[1].casefold()
    ):
        return False
    descriptions = [_normalize_report_description(text) for text in raw_descriptions]
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
    # 缺少明确修改依据时，关键短语替换按不同对象、方向或单位分别保留。
    replaced_length = sum(
        max(len(left_text), len(right_text))
        for operation, left_text, right_text in edits
        if operation == "replace"
    )
    if 0 < replaced_length <= 3 and not same_correction:
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
    location = f"{left.get('location', '')}；{right.get('location', '')}"
    if not _shared_evidence(
        _without_references(left.get("excerpt"), location),
        _without_references(right.get("excerpt"), location),
    ):
        return False
    left_description = _normalize_report_description(left.get("description"))
    right_description = _normalize_report_description(right.get("description"))
    if not left_description or not right_description:
        return False
    if not _compatible_claims(left, right):
        return False
    if _same_correction(left, right):
        return True
    # 描述改写较多时，以相近的修改建议补充判定。
    similarity = _report_description_similarity(left_description, right_description)
    return similarity >= 0.6 or (
        similarity >= 0.45
        and _report_description_similarity(
            left.get("suggestion"), right.get("suggestion")
        )
        >= 0.65
    )


def _compatible_suggestions(left: dict, right: dict) -> bool:
    targets = []
    for item in (left, right):
        text = _without_references(item.get("suggestion"), item.get("location"))
        # 同时保留带引号与不带引号的各个修改目标，避免原值影响冲突判定。
        changes = re.findall(_CHANGE_TO + rf"(?:{_QUOTE}|([^,;。]+))", text)
        if changes:
            text = "；".join(quoted or unquoted for quoted, unquoted in changes)
        targets.append(text)
    return (
        _compatible_numbers(*targets)
        and set(re.findall(r"(?:[<>]=?|[≤≥≠])[+-]?\d+(?:\.\d+)*", targets[0]))
        == set(re.findall(r"(?:[<>]=?|[≤≥≠])[+-]?\d+(?:\.\d+)*", targets[1]))
        and not (
            targets[0] != targets[1] and targets[0].casefold() == targets[1].casefold()
        )
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
            if any(not _compatible_suggestions(source, item) for source in sources):
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
