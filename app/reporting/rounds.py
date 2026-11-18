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
_SPACING_INTENT = re.compile(r"(?:写法|混用|并存|统一|不一致|不统一)")
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
    anchored_action = (
        _QUOTE
        + r"(前|后)(?:(?:补充|补上|添加|加上|插入|增加)(?:介词|助词|文字|词语|字)?"
        + r"|(?:缺少|漏写|漏掉)(?:介词|助词|文字|词语|字)?)"
        + _QUOTE
    )
    for text in (suggestion, _normalized(item.get("description"))):
        for anchor, side, addition in re.findall(anchored_action, text):
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
    if _same_spacing_consistency(left, right):
        return True
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


def _spacing_consistency_terms(item: dict) -> set[str]:
    description = str(item.get("description") or "")
    if not _SPACING_INTENT.search(_normalized(description)):
        return set()
    excerpt = _normalized(item.get("excerpt"))
    variants = {}
    for value in re.findall(_QUOTE, description):
        value = unicodedata.normalize("NFKC", value)
        compact = re.sub(r"\s+", "", value)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", compact):
            continue
        spaced, unspaced = variants.setdefault(compact, [False, False])
        variants[compact] = [
            spaced or bool(re.search(r"\s", value)),
            unspaced or not bool(re.search(r"\s", value)),
        ]
    return {
        term
        for term, (spaced, unspaced) in variants.items()
        if spaced and unspaced and term in excerpt
    }


def _same_spacing_consistency(left: dict, right: dict) -> bool:
    return bool(_spacing_consistency_terms(left) & _spacing_consistency_terms(right))


def _shared_evidence_text(left, right) -> bool:
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


def _shared_evidence(left, right) -> bool:
    return _compatible_numbers(left, right) and _shared_evidence_text(left, right)


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
    if _same_spacing_consistency(left, right):
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


def _suggestion_targets(item: dict) -> set[str]:
    suggestion = _normalized(item.get("suggestion"))
    targets = set(re.findall(_CHANGE_TO + _QUOTE, suggestion))
    targets.update(
        re.findall(r"(?:全文|全篇)?统一(?:为|使用|写作)" + _QUOTE, suggestion)
    )
    return {target.strip("，。；、：: ") for target in targets if target.strip()}


def _shared_suggestion_target(left: dict, right: dict) -> bool:
    targets = [_suggestion_targets(item) for item in (left, right)]
    descriptions = [_normalized(item.get("description")) for item in (left, right)]
    for first in targets[0]:
        for second in targets[1]:
            shorter, longer = sorted((first, second), key=len)
            if len(shorter) < 4 or not (
                shorter == longer
                or shorter in longer
                or SequenceMatcher(None, first, second, autojunk=False).ratio() >= 0.9
            ):
                continue
            if all(shorter in description for description in descriptions):
                return True
    return False


def _same_named_object_conflict(left: dict, right: dict) -> bool:
    descriptions = [_normalized(item.get("description")) for item in (left, right)]
    for description in descriptions:
        has_identity = any(
            marker in description for marker in ("同一", "相同", "等同", "即指")
        )
        has_naming = any(
            marker in description
            for marker in ("称", "写作", "叫法", "命名", "名称", "术语")
        )
        has_conflict = any(
            marker in description
            for marker in (
                "不一致",
                "不统一",
                "两种",
                "多称",
                "混用",
                "混淆",
                "是否即指",
            )
        )
        if not (has_identity and has_naming and has_conflict):
            return False

    quoted_terms = [
        {term for term in re.findall(_QUOTE, description) if len(term.strip()) >= 2}
        for description in descriptions
    ]
    shared_terms = quoted_terms[0] & quoted_terms[1]
    if len(shared_terms) < 2:
        return False

    section_refs = [
        set(
            re.findall(r"(?<!\d)\d+(?:\.\d+)+(?!\d)", _normalized(item.get("location")))
        )
        for item in (left, right)
    ]
    return bool(section_refs[0] & section_refs[1])


def group_cross_check_report_items(results: list[dict]) -> list[dict]:
    """关联不同检查项中证据和问题目标均相符的报告条目。"""
    candidates = []
    order = 0
    for result in results:
        code = str(result.get("code") or "")
        if not code:
            continue
        for item in result.get("report_items") or []:
            item.pop("cross_check_group_id", None)
            item.pop("cross_check_anchor_id", None)
            candidates.append(
                {
                    "code": code,
                    "name": str(result.get("name") or code),
                    "item": item,
                    "order": order,
                }
            )
            order += 1

    def same_group_candidate(left: dict, right: dict) -> bool:
        if not _normalized(left["item"].get("excerpt")) or not _normalized(
            right["item"].get("excerpt")
        ):
            return False
        if not _compatible_claims(left["item"], right["item"]):
            return False
        if not _compatible_suggestions(left["item"], right["item"]):
            return False
        if _same_finding(left["item"], right["item"]):
            return True
        return (
            _shared_evidence_text(
                left["item"].get("excerpt"), right["item"].get("excerpt")
            )
            and _shared_suggestion_target(left["item"], right["item"])
        ) or _same_named_object_conflict(left["item"], right["item"])

    links = [set() for _ in candidates]
    for left_index, left in enumerate(candidates):
        for right_index in range(left_index + 1, len(candidates)):
            right = candidates[right_index]
            if same_group_candidate(left, right):
                links[left_index].add(right_index)
                links[right_index].add(left_index)

    components = []
    visited = set()
    for start in range(len(candidates)):
        if start in visited or not links[start]:
            continue
        component = []
        pending = [start]
        visited.add(start)
        while pending:
            index = pending.pop()
            component.append(candidates[index])
            for linked_index in links[index]:
                if linked_index not in visited:
                    visited.add(linked_index)
                    pending.append(linked_index)
        remaining = {member["order"] for member in component}
        while remaining:
            cliques = []
            for start in sorted(remaining):
                clique = [start]
                for candidate in sorted(remaining):
                    if candidate == start:
                        continue
                    if all(candidate in links[member] for member in clique):
                        clique.append(candidate)
                if len({candidates[index]["code"] for index in clique}) >= 2:
                    cliques.append(clique)
            if not cliques:
                break
            clique = max(
                cliques,
                key=lambda indexes: (
                    len(indexes),
                    -min(candidates[index]["order"] for index in indexes),
                ),
            )
            components.append([candidates[index] for index in clique])
            remaining.difference_update(clique)

    components.sort(key=lambda members: min(member["order"] for member in members))
    cross_check_groups = []
    for members in components:
        members.sort(key=lambda member: member["order"])
        group_id = len(cross_check_groups) + 1
        for member in members:
            member["item"]["cross_check_group_id"] = group_id
            identity = f"{member['code']}\0{member['item'].get('id', '')}"
            member["anchor_id"] = (
                "report-item-"
                + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
            )
            member["item"]["cross_check_anchor_id"] = member["anchor_id"]
        cross_check_groups.append(
            {
                "id": group_id,
                "item_count": len(members),
                "members": members,
            }
        )
    return cross_check_groups


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


def _new_group(code: str, item: dict) -> dict:
    identity = json.dumps(
        [
            code,
            *[
                item.get(field, "")
                for field in ("category", "location", "excerpt", "description")
            ],
        ],
        ensure_ascii=False,
    )
    return {
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
        "id": "round-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        "status": item["type"],
        "evidence_refs": item.get("evidence_refs", []),
        "source_rounds": [],
        "source_items": [],
        "round_conflict": False,
    }


def _append_source(existing: dict, sources: list[dict], item: dict) -> None:
    if existing["status"] != item["type"]:
        existing["round_conflict"] = True
    if any(not _compatible_suggestions(source, item) for source in sources):
        existing["round_conflict"] = True
    sources.append(item)
    if REPORT_SEVERITY_ORDER.get(item.get("severity"), 99) < REPORT_SEVERITY_ORDER.get(
        existing.get("severity"), 99
    ):
        existing["severity"] = item["severity"]
    for field in ("location", "excerpt", "description", "impact", "suggestion"):
        existing[field] = _merge_report_field_values(
            existing.get(field), item.get(field)
        )
    existing["evidence_refs"] = _merge_report_evidence_refs(
        existing.get("evidence_refs"), item.get("evidence_refs")
    )
    round_number = item["_round_number"]
    if round_number not in existing["source_rounds"]:
        existing["source_rounds"].append(round_number)
    existing["source_items"].append({"round": round_number, "item": item["index"]})


def _rebuild_group(
    code: str, sources: list[dict], group_id: str
) -> tuple[dict, list[dict]]:
    ordered_sources = sorted(
        sources, key=lambda item: (item["_round_number"], item["index"])
    )
    finding = _new_group(code, ordered_sources[0])
    finding["id"] = group_id
    rebuilt_sources = []
    for source in ordered_sources:
        _append_source(finding, rebuilt_sources, source)
    return finding, rebuilt_sources


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
            item["_round_number"] = round_number
            matching = [
                index
                for index, (_, sources) in enumerate(groups)
                if any(_same_finding(source, item) for source in sources)
                and all(_compatible_claims(source, item) for source in sources)
            ]
            if not matching:
                existing = _new_group(code, item)
                sources = []
                _append_source(existing, sources, item)
                groups.append((existing, sources))
                continue

            primary_index = matching[0]
            existing, sources = groups[primary_index]
            merged_sources = list(sources)
            merged_group_indexes = []
            for index in matching[1:]:
                _, candidate_sources = groups[index]
                if all(
                    _compatible_claims(source, candidate)
                    for source in merged_sources
                    for candidate in candidate_sources
                ):
                    merged_sources.extend(candidate_sources)
                    merged_group_indexes.append(index)
            if merged_group_indexes:
                merged_sources.append(item)
                groups[primary_index] = _rebuild_group(
                    code, merged_sources, existing["id"]
                )
                for index in reversed(merged_group_indexes):
                    del groups[index]
            else:
                _append_source(existing, sources, item)
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
