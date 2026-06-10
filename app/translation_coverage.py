from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass


TRANSLATION_COVERAGE_CHECK_CODE = "consistency-translation-coverage"
LOCAL_CONSISTENCY_CHECK_CODES = frozenset({TRANSLATION_COVERAGE_CHECK_CODE})
STRUCTURAL_ENTRY_KINDS = frozenset({"heading", "list", "table"})

_MAX_REPORTED_ITEMS = 50
_MAX_SNIPPET_CHARS = 160

_GROUP_HEADING_RE = re.compile(r"^# (?P<label>素材文档|资料)\s*$")
_FILE_HEADING_RE = re.compile(r"^## (?P<label>素材文档|资料)(?P<index>\d+)：(?P<name>.+?)\s*$")
_MARKDOWN_HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<body>.+?)\s*$")
_SHEET_RE = re.compile(r"^# 工作表：(?P<name>.+?)\s*$")
_PAGE_RE = re.compile(r"^\[第(?P<page>\d+)页\]\s*$")
_PURE_PAGE_NUMBER_RE = re.compile(r"^(?:第\s*)?\d{1,4}\s*(?:页)?$")
_PAGE_FURNITURE_MARKERS = (
    "文档版本",
    "版权所有",
    "版权",
    "©",
    "copyright",
    "all rights reserved",
    "document version",
    "issue ",
)
_EMBEDDED_PAGE_FURNITURE_RE = re.compile(
    r"\s*(?:文档版本|版权所有|版权|©|Copyright|All rights reserved|Document version|Issue\s+\d+).*$",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(
    r"^(?P<marker>(?:\d+(?:[.．]\d+)+|第[一二三四五六七八九十百千\d]+[章节]))"
    r"[\s、.．]*(?P<body>.+?)\s*$"
)
_LIST_RE = re.compile(
    r"^\s*(?P<marker>(?:\d+(?:[.)、．]|(?:[.．]\d+)+[.)、．]?)|[A-Za-z][.)、．]|"
    r"[（(][一二三四五六七八九十百千\dA-Za-z]+[）)]|[-*+•●]))\s+"
    r"(?P<body>.+?)\s*$"
)
_DATE_RE = re.compile(r"\b\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\b")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_VERSION_RE = re.compile(r"\b[vV]?\d+(?:\.\d+){1,4}\b")
_NUMBER_UNIT_RE = re.compile(
    r"(?<![\w.])\d+(?:[.,]\d+)?\s*(?:"
    r"mA|A|kA|V|kV|W|kW|MW|Wh|kWh|Hz|kHz|MHz|GHz|"
    r"MB|GB|TB|KB|Mbps|Gbps|bps|ms|s|min|h|mm|cm|m|km|"
    r"kg|g|mg|℃|°C|%|Ω|ohm|Ah|mAh|Pa|kPa|MPa|N|"
    r"毫安|安|伏|千伏|瓦|千瓦|赫兹|毫米|厘米|米|千克|克|秒|分钟|小时|个|台|次"
    r")\b",
    re.IGNORECASE,
)
_MODEL_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*(?:[-_/][A-Z0-9]+)+\b")
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w.])")


@dataclass(frozen=True)
class DocumentBlock:
    group_label: str
    file_label: str
    file_name: str
    text: str


@dataclass(frozen=True)
class Entry:
    kind: str
    text: str
    location: str
    key: str | None
    bucket: str
    identifiers: tuple[str, ...]


@dataclass(frozen=True)
class Issue:
    title: str
    location: str
    material_text: str
    related_text: str
    suggestion: str
    manual: bool = False


def run_translation_coverage_check(document_text: str) -> str:
    blocks = _parse_document_blocks(document_text)
    material_entries = _entries_for_groups(blocks, "素材文档")
    related_entries = _entries_for_groups(blocks, "资料")

    if not material_entries or not related_entries:
        return (
            "## 总体结论\n"
            "需人工确认：未能从素材文档或资料中抽取到可对齐的标题、段落、列表或表格行。"
        )

    issues, manual_notes = _compare_entries(material_entries, related_entries)
    return _format_report(material_entries, related_entries, issues, manual_notes)


def _parse_document_blocks(document_text: str) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    current_group = ""
    current_file_label = ""
    current_file_name = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if current_group and text:
            blocks.append(
                DocumentBlock(
                    group_label=current_group,
                    file_label=current_file_label or current_group,
                    file_name=current_file_name or current_group,
                    text=text,
                )
            )
        buffer = []

    for raw_line in str(document_text or "").splitlines():
        group_match = _GROUP_HEADING_RE.match(raw_line.strip())
        if group_match:
            flush()
            current_group = group_match.group("label")
            current_file_label = ""
            current_file_name = ""
            continue

        file_match = _FILE_HEADING_RE.match(raw_line.strip())
        if file_match and current_group == file_match.group("label"):
            flush()
            current_file_label = f"{file_match.group('label')}{file_match.group('index')}"
            current_file_name = file_match.group("name").strip()
            continue

        if current_group:
            buffer.append(raw_line)

    flush()
    return blocks


def _entries_for_groups(blocks: list[DocumentBlock], group_label: str) -> list[Entry]:
    entries: list[Entry] = []
    for block in blocks:
        if block.group_label != group_label:
            continue
        entries.extend(_extract_entries(block))
    return entries


def _extract_entries(block: DocumentBlock) -> list[Entry]:
    entries: list[Entry] = []
    paragraph_buffer: list[str] = []
    page_furniture_lines = _repeated_page_furniture_lines(block.text)
    section_key = "root"
    section_index = 0
    sheet_name = ""
    page_label = ""
    counters: defaultdict[str, int] = defaultdict(int)

    def location(kind: str) -> str:
        parts = [block.file_label]
        if block.file_name:
            parts.append(block.file_name)
        if page_label:
            parts.append(page_label)
        if sheet_name:
            parts.append(f"工作表：{sheet_name}")
        parts.append(kind)
        return " / ".join(parts)

    def add_entry(kind: str, text: str, key: str | None, bucket: str) -> None:
        text = _clean_entry_text(text)
        if not text:
            return
        counters[bucket] += 1
        sequence_key = key or f"{bucket}:{counters[bucket]}"
        entries.append(
            Entry(
                kind=kind,
                text=text,
                location=location(_kind_label(kind)),
                key=sequence_key,
                bucket=bucket,
                identifiers=_identifier_tokens(text, kind),
            )
        )

    def flush_paragraph() -> None:
        nonlocal paragraph_buffer
        if not paragraph_buffer:
            return
        text = _compact_text(" ".join(paragraph_buffer))
        paragraph_buffer = []
        if not text:
            return
        bucket = f"paragraph:{section_key}"
        add_entry("paragraph", text, None, bucket)

    for raw_line in block.text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue

        page_match = _PAGE_RE.match(line)
        if page_match:
            flush_paragraph()
            page_label = f"第{page_match.group('page')}页"
            continue

        sheet_match = _SHEET_RE.match(line)
        if sheet_match:
            flush_paragraph()
            sheet_name = sheet_match.group("name").strip()
            continue

        if _is_page_furniture_line(line, page_furniture_lines):
            flush_paragraph()
            continue

        heading_match = _MARKDOWN_HEADING_RE.match(line)
        if heading_match and not _SHEET_RE.match(line):
            flush_paragraph()
            body = _compact_text(heading_match.group("body"))
            marker = _section_marker(body)
            if marker:
                section_key = _normalize_marker(marker)
            else:
                section_index += 1
                section_key = f"section-{section_index}"
            bucket = "heading"
            add_entry("heading", body, f"heading:{section_key}", bucket)
            continue

        section_match = _SECTION_RE.match(line)
        if section_match:
            flush_paragraph()
            marker = _normalize_marker(section_match.group("marker"))
            body = _compact_text(section_match.group("body"))
            section_key = marker
            bucket = "heading"
            add_entry("heading", f"{section_match.group('marker')} {body}", f"heading:{marker}", bucket)
            continue

        list_match = _LIST_RE.match(line)
        if list_match:
            flush_paragraph()
            marker = _normalize_marker(list_match.group("marker"))
            body = _compact_text(list_match.group("body"))
            bucket = f"list:{section_key}"
            key = None if _is_bullet_marker(marker) else f"{bucket}:{marker}"
            add_entry("list", f"{list_match.group('marker')} {body}", key, bucket)
            continue

        if " | " in line:
            flush_paragraph()
            bucket = f"table:{section_key}:{sheet_name or block.file_name}"
            add_entry("table", line, None, bucket)
            continue

        paragraph_buffer.append(line)

    flush_paragraph()
    return entries


def _compare_entries(material_entries: list[Entry], related_entries: list[Entry]) -> tuple[list[Issue], list[Issue]]:
    issues: list[Issue] = []
    manual_notes: list[Issue] = []
    related_by_key = {entry.key: entry for entry in related_entries if entry.key}
    material_by_key = {entry.key: entry for entry in material_entries if entry.key}

    for material in material_entries:
        related = related_by_key.get(material.key)
        if related is None:
            if not _reports_missing_entry(material):
                continue
            issues.append(
                Issue(
                    title="疑似漏翻译或条目缺失",
                    location=material.location,
                    material_text=material.text,
                    related_text="未找到相同结构位置的资料条目。",
                    suggestion="请确认资料中是否需要补充对应译文，或是否存在合并/调整顺序。",
                    manual=_needs_manual_for_missing(material),
                )
            )
            continue
        missing_ids = [item for item in material.identifiers if item not in related.identifiers]
        extra_ids = [item for item in related.identifiers if item not in material.identifiers]
        if missing_ids or extra_ids:
            issues.append(
                Issue(
                    title="数字、单位、日期或型号疑似不一致",
                    location=material.location,
                    material_text=material.text,
                    related_text=related.text,
                    suggestion=_identifier_suggestion(missing_ids, extra_ids),
                )
            )

    for related in related_entries:
        if related.key and related.key not in material_by_key:
            if not _reports_extra_entry(related):
                continue
            issues.append(
                Issue(
                    title="资料疑似新增条目",
                    location=related.location,
                    material_text="未找到相同结构位置的素材条目。",
                    related_text=related.text,
                    suggestion="请确认该内容是否有素材依据；如无依据，应删除或补充素材来源。",
                    manual=_needs_manual_for_missing(related),
                )
            )

    material_buckets = _bucket_counts(material_entries)
    related_buckets = _bucket_counts(related_entries)
    for bucket in sorted(set(material_buckets) | set(related_buckets)):
        if not _reports_bucket_mismatch(bucket):
            continue
        left = material_buckets.get(bucket, 0)
        right = related_buckets.get(bucket, 0)
        if left != right:
            manual_notes.append(
                Issue(
                    title="条目数量或拆分方式不一致",
                    location=_bucket_label(bucket),
                    material_text=f"素材条目数：{left}",
                    related_text=f"资料条目数：{right}",
                    suggestion="请人工确认是否存在漏翻译、新增内容，或只是译文合并/拆分段落。",
                    manual=True,
                )
            )

    return _deduplicate_issues(issues), _deduplicate_issues(manual_notes)


def _format_report(
    material_entries: list[Entry],
    related_entries: list[Entry],
    issues: list[Issue],
    manual_notes: list[Issue],
) -> str:
    hard_issues = [issue for issue in issues if not issue.manual]
    soft_issues = [issue for issue in issues if issue.manual]
    risk = _risk_level(len(hard_issues), len(soft_issues) + len(manual_notes), len(material_entries))
    parts = [
        "## 总体结论",
        (
            f"风险等级：{risk}。本地规则抽取素材条目 {len(material_entries)} 条、资料条目 "
            f"{len(related_entries)} 条；发现明确疑点 {len(hard_issues)} 条，需人工确认 "
            f"{len(soft_issues) + len(manual_notes)} 条。"
        ),
        "说明：本检查不调用大模型，主要发现标题、编号/项目符号列表、表格行等结构项的覆盖问题，并辅助检查数字、单位、日期、版本和型号；普通正文段落不会按英文比中文多出的碎片行逐条报警。译文语义准确性仍建议结合多文档对照检查复核。",
    ]

    if hard_issues:
        parts.extend(["", "## 明确问题"])
        parts.extend(_format_issue_list(hard_issues))

    manual_items = soft_issues + manual_notes
    if manual_items:
        parts.extend(["", "## 需人工确认"])
        parts.extend(_format_issue_list(manual_items))

    if not hard_issues and not manual_items:
        parts.extend(["", "未发现素材文档与资料在标题、列表、表格等结构项或关键数字单位上存在明显不一致。"])

    total_items = len(hard_issues) + len(manual_items)
    if total_items > _MAX_REPORTED_ITEMS:
        parts.append(f"\n报告仅展示前 {_MAX_REPORTED_ITEMS} 条疑点，其余请结合源文档人工复核。")

    return "\n".join(parts)


def _format_issue_list(issues: list[Issue]) -> list[str]:
    lines: list[str] = []
    for index, issue in enumerate(issues[:_MAX_REPORTED_ITEMS], start=1):
        lines.append(f"{index}. **{issue.title}**")
        lines.append(f"   - 位置：{issue.location}")
        lines.append(f"   - 素材：{_snippet(issue.material_text)}")
        lines.append(f"   - 资料：{_snippet(issue.related_text)}")
        lines.append(f"   - 建议：{issue.suggestion}")
    return lines


def _identifier_tokens(text: str, kind: str = "") -> tuple[str, ...]:
    normalized = _normalize_identifier_text(text)
    primary_tokens: list[tuple[str, tuple[int, int]]] = []
    for pattern in (_IP_RE, _DATE_RE, _NUMBER_UNIT_RE, _VERSION_RE, _MODEL_RE):
        primary_tokens.extend((match.group(0), match.span()) for match in pattern.finditer(normalized))
    normalized_primary = [
        _normalize_identifier_token(token)
        for token, _ in primary_tokens
        if token.strip()
    ]
    tokens = list(normalized_primary)
    for match in _NUMBER_RE.finditer(normalized):
        number = _normalize_identifier_token(match.group(0))
        if not _is_significant_bare_number(number, kind):
            continue
        if any(_spans_overlap(match.span(), span) for _, span in primary_tokens):
            continue
        if any(_token_covers_number(token, number) for token in normalized_primary):
            continue
        tokens.append(number)
    return tuple(dict.fromkeys(token for token in tokens if token))


def _token_covers_number(token: str, number: str) -> bool:
    return token != number and token.startswith(number) and any(char.isalpha() or char in "%℃°Ω" for char in token)


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _is_significant_bare_number(number: str, kind: str) -> bool:
    if "." in number:
        return True
    digits = re.sub(r"\D", "", number)
    if len(digits) >= 3:
        return True
    return kind == "table" and len(digits) >= 2 and not digits.startswith("0")


def _normalize_identifier_text(text: str) -> str:
    return (
        str(text or "")
        .replace("．", ".")
        .replace("，", ",")
        .replace("％", "%")
        .replace("＋", "+")
        .replace("－", "-")
    )


def _normalize_identifier_token(token: str) -> str:
    token = re.sub(r"\s+", "", token.strip())
    return token.upper().replace(",", ".")


def _identifier_suggestion(missing_ids: list[str], extra_ids: list[str]) -> str:
    parts = []
    if missing_ids:
        parts.append(f"资料中缺少素材标识：{', '.join(missing_ids)}")
    if extra_ids:
        parts.append(f"资料中出现素材未对应的标识：{', '.join(extra_ids)}")
    parts.append("请核对是否漏译、误译或单位换算错误。")
    return "；".join(parts)


def _reports_missing_entry(entry: Entry) -> bool:
    if entry.kind in STRUCTURAL_ENTRY_KINDS:
        return True
    return bool(entry.identifiers) and entry.kind == "paragraph"


def _reports_extra_entry(entry: Entry) -> bool:
    return entry.kind in STRUCTURAL_ENTRY_KINDS


def _reports_bucket_mismatch(bucket: str) -> bool:
    return not bucket.startswith("paragraph:")


def _repeated_page_furniture_lines(text: str) -> set[str]:
    pages: list[set[str]] = []
    current: set[str] = set()
    saw_page_marker = False
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if _PAGE_RE.match(line):
            if saw_page_marker:
                pages.append(current)
                current = set()
            saw_page_marker = True
            continue
        normalized = _normalize_page_furniture_line(line)
        if normalized:
            current.add(normalized)
    if saw_page_marker:
        pages.append(current)
    if len(pages) < 2:
        return set()

    counts: defaultdict[str, int] = defaultdict(int)
    for page in pages:
        for line in page:
            counts[line] += 1
    threshold = 2 if len(pages) <= 8 else max(2, int(len(pages) * 0.2))
    return {
        line
        for line, count in counts.items()
        if count >= threshold and _looks_like_repeated_page_furniture(line)
    }


def _is_page_furniture_line(line: str, repeated_lines: set[str]) -> bool:
    normalized = _normalize_page_furniture_line(line)
    if not normalized:
        return False
    if normalized in repeated_lines:
        return True
    lowered = normalized.lower()
    if _is_standalone_page_furniture_line(lowered):
        return True
    return bool(_PURE_PAGE_NUMBER_RE.match(normalized))


def _looks_like_repeated_page_furniture(line: str) -> bool:
    if len(line) > 180:
        return False
    lowered = line.lower()
    if any(marker in lowered for marker in _PAGE_FURNITURE_MARKERS):
        return True
    if _PURE_PAGE_NUMBER_RE.match(line):
        return True
    if any(marker in line for marker in ("安装指南", "用户手册", "操作指南", "维护指南", "User Manual", "Installation Guide")):
        return True
    return not line.endswith(("。", "；", "？", "！", ".", ";", "?", "!"))


def _is_standalone_page_furniture_line(lowered_line: str) -> bool:
    stripped = lowered_line.strip()
    if not stripped:
        return False
    if stripped.startswith(("文档版本", "document version", "issue ")):
        return True
    return stripped.startswith(("版权所有", "copyright", "©")) or stripped.startswith("all rights reserved")


def _normalize_page_furniture_line(line: str) -> str:
    return _compact_text(str(line or "").strip())


def _clean_entry_text(text: str) -> str:
    text = _compact_text(text)
    text = _EMBEDDED_PAGE_FURNITURE_RE.sub("", text).strip()
    return _compact_text(text)


def _bucket_counts(entries: list[Entry]) -> dict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    for entry in entries:
        counts[entry.bucket] += 1
    return dict(counts)


def _deduplicate_issues(issues: list[Issue]) -> list[Issue]:
    seen: set[tuple[str, str, str, str]] = set()
    result: list[Issue] = []
    for issue in issues:
        key = (issue.title, issue.location, issue.material_text, issue.related_text)
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result


def _risk_level(hard_count: int, manual_count: int, material_count: int) -> str:
    if hard_count >= 5 or (material_count and hard_count / material_count >= 0.1):
        return "高"
    if hard_count:
        return "中"
    if manual_count:
        return "低"
    return "低"


def _needs_manual_for_missing(entry: Entry) -> bool:
    return entry.kind in {"paragraph", "table"} and not entry.identifiers


def _section_marker(text: str) -> str:
    match = _SECTION_RE.match(text)
    return match.group("marker") if match else ""


def _normalize_marker(marker: str) -> str:
    marker = str(marker or "").strip()
    marker = marker.strip("()（）")
    marker = marker.rstrip(".)、．")
    marker = marker.replace("．", ".")
    return marker.lower()


def _is_bullet_marker(marker: str) -> bool:
    return marker in {"-", "*", "+", "•", "●"}


def _kind_label(kind: str) -> str:
    return {
        "heading": "标题",
        "list": "列表项",
        "table": "表格行",
        "paragraph": "段落",
    }.get(kind, "条目")


def _bucket_label(bucket: str) -> str:
    if bucket.startswith("paragraph:"):
        return "段落组"
    if bucket.startswith("list:"):
        return "列表组"
    if bucket.startswith("table:"):
        return "表格组"
    if bucket == "heading":
        return "标题组"
    return bucket


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _snippet(text: str) -> str:
    text = _compact_text(text)
    if len(text) <= _MAX_SNIPPET_CHARS:
        return text
    return text[: _MAX_SNIPPET_CHARS - 1].rstrip() + "…"
