"""以封面字段和修订记录最新版本为证据核对文档发布信息。"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from app.checks.revision_history import readable_release_text, revision_history_sections
from app.checks.term_locations import DocumentLocationIndex

DOCUMENT_RELEASE_CHECK_CODE = "document-release-consistency"
_VERSION_LABEL = (
    r"(?:文档版本|版本号?|修订版本|(?:Document\s+)?(?:Version|Issue|Revision)|Rev\.?)"
)
_DATE_LABEL = r"(?:发布日期|发行日期|发布日|(?:Release|Publication|Issue)\s+Date|Date)"
_VERSION = r"[vV]?\d+(?:\.\d+)*(?:[-_][A-Za-z0-9]+)?"
_LABELED_VERSION = re.compile(
    rf"^{_VERSION_LABEL}\s*[:：]?\s*({_VERSION})(?![\w.])(.*)$", re.I
)
_BARE_VERSION = re.compile(rf"^({_VERSION})(?![\w.])\s*(.*)$")
_DATE_START = re.compile(r"\d{1,4}[-/.年]|\d{1,2}\s+[A-Za-z]|[A-Za-z]+\s+\d")
_MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        1,
    )
}
_MONTHS.update({name[:3]: number for name, number in list(_MONTHS.items())})


@dataclass(frozen=True)
class Release:
    version: str
    date_text: str
    excerpt: str
    location: str


def _version_key(value: str) -> tuple[int, ...] | None:
    value = unicodedata.normalize("NFKC", value).strip().lstrip("vV")
    if re.fullmatch(r"\d+(?:\.\d+)*", value):
        return tuple(int(part) for part in value.split("."))
    return None


def _date_value(value: str) -> date | None:
    value = unicodedata.normalize("NFKC", value).strip(" ()|。")
    numeric = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", value)
    if numeric:
        parts = tuple(map(int, numeric.groups()))
    else:
        english = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", value)
        if english:
            month, day, year = english.groups()
        else:
            english = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})", value)
            if not english:
                return None
            day, month, year = english.groups()
        month_number = _MONTHS.get(month.lower())
        if not month_number:
            return None
        parts = (int(year), month_number, int(day))
    try:
        return date(*parts)
    except ValueError:
        return None


def _lines(text: str) -> list[str]:
    return [
        re.sub(r"^#{1,6}\s+", "", line).strip(" *")
        for raw in readable_release_text(text).splitlines()
        if (line := raw.strip()) and not line.startswith(("[超链接]", "file:"))
    ]


def _located_lines(text: str, start: int, end: int) -> list[tuple[str, int]]:
    """为普通行和结构化表格行保留原文位置。"""
    chunks = []
    offset = start
    for table in re.finditer(
        r"<table\b[^>]*>.*?</table>", text[start:end], re.I | re.S
    ):
        table_start, table_end = start + table.start(), start + table.end()
        chunks.append((text[offset:table_start], offset))
        for row in re.finditer(r"<tr\b[^>]*>.*?</tr>", table.group(), re.I | re.S):
            chunks.append(
                ("<table>" + row.group() + "</table>", table_start + row.start())
            )
        offset = table_end
    chunks.append((text[offset:end], offset))
    lines = []
    for chunk, position in chunks:
        for line in _lines(chunk):
            found = chunk.find(line)
            lines.append((line, position + max(0, found)))
    return lines


def _field(lines: list[str], label: str) -> str:
    values = set()
    pattern = re.compile(rf"^{label}\s*[:：|]?\s*(.*)$", re.I)
    for index, line in enumerate(lines):
        match = pattern.fullmatch(line)
        if match:
            value = match[1].strip()
            if not value and index + 1 < len(lines):
                value = lines[index + 1]
            if value:
                values.add(value)
    return values.pop() if len(values) == 1 else ""


def _cover(text: str, history_start: int) -> Release:
    pages = list(re.finditer(r"\[第\d+页\]", text))
    start = pages[0].end() if pages else 0
    end = min(history_start, pages[1].start() if len(pages) > 1 else history_start)
    lines = _lines(text[start:end])
    version_field = _field(lines, _VERSION_LABEL)
    version_match = _BARE_VERSION.fullmatch(version_field)
    version = version_match[1] if version_match else ""
    date_text = _field(lines, _DATE_LABEL)
    if not date_text and version_match:
        date_text = version_match[2].strip(" ()（）")
    return Release(
        version,
        date_text,
        "\n".join(lines[:40]),
        "封面；" + DocumentLocationIndex(text).location_for(start, ""),
    )


def _history_records(text: str) -> tuple[list[Release], list[str]]:
    records = []
    warnings = []
    locations = DocumentLocationIndex(text)
    for section in revision_history_sections(text):
        body = text[section.start : section.end]
        if re.search(r'data-confidence="(?:low|medium)"', body):
            warnings.append("修订记录表格的行列对应关系需要确认")
            continue
        located_lines = _located_lines(text, section.start, section.end)
        lines = [line for line, _ in located_lines]
        columns = None
        for index, (line, position) in enumerate(located_lines):
            cells = [value.strip() for value in line.split("|")]
            if len(cells) > 2 and not cells[0] and not cells[-1]:
                cells = cells[1:-1]
            if len(cells) == 1 and "\t" in line:
                cells = [value.strip() for value in line.split("\t")]
            version_column = next(
                (
                    n
                    for n, cell in enumerate(cells)
                    if re.fullmatch(_VERSION_LABEL, cell, re.I)
                ),
                None,
            )
            date_column = next(
                (
                    n
                    for n, cell in enumerate(cells)
                    if re.fullmatch(_DATE_LABEL, cell, re.I)
                ),
                None,
            )
            if version_column is not None and date_column is not None:
                columns = (version_column, date_column)
                continue
            version = date_text = ""
            if (
                columns
                and len(cells) <= max(columns)
                and any(_DATE_START.match(cell) for cell in cells)
            ):
                warnings.append("修订记录中存在字段不完整的日期行")
                continue
            if columns and len(cells) > max(columns):
                candidate = cells[columns[0]]
                if re.fullmatch(_VERSION, candidate):
                    version, date_text = candidate, cells[columns[1]]
                elif (candidate and not re.fullmatch(r"[-: ]+", candidate)) or (
                    not candidate and cells[columns[1]]
                ):
                    warnings.append("修订记录中存在无法排序的版本号")
            else:
                match = _LABELED_VERSION.fullmatch(line)
                if not match:
                    match = _BARE_VERSION.fullmatch(line.strip("| "))
                    if match and not (
                        match[2].strip().startswith(("(", "（"))
                        or _DATE_START.match(match[2].strip())
                        or (
                            not match[2].strip()
                            and index + 1 < len(lines)
                            and _DATE_START.match(lines[index + 1])
                        )
                    ):
                        match = None
                if match:
                    version = match[1]
                    date_text = match[2].strip(" ()（）|")
                    if not date_text and index + 1 < len(lines):
                        date_text = lines[index + 1]
            if not version:
                if not re.fullmatch(_VERSION_LABEL, line, re.I) and re.match(
                    rf"^{_VERSION_LABEL}\s*[:：]?\s*\S", line, re.I
                ):
                    warnings.append("修订记录中存在无法识别的版本条目")
                continue
            location = section.title + "；" + locations.location_for(position, version)
            records.append(
                Release(
                    version,
                    date_text,
                    line + ("\n" + date_text if date_text not in line else ""),
                    location,
                )
            )
    return records, warnings


def build_document_release_report(text: str) -> dict:
    sections = revision_history_sections(text)
    cover = _cover(text, sections[0].start if sections else len(text))
    records, warnings = _history_records(text)
    latest = None
    if len(sections) > 1:
        warnings.append("文档包含多个独立修订记录章节，需要确认与封面的对应关系")
    if not sections:
        warnings.append("未识别到具有日期记录的修订记录章节")
    elif not records:
        warnings.append("未可靠提取修订记录中的版本和日期")
    elif any(_version_key(record.version) is None for record in records):
        warnings.append("修订记录的版本号不能按数字版本排序")
    elif not warnings:
        key = max(_version_key(record.version) for record in records)
        candidates = [
            record for record in records if _version_key(record.version) == key
        ]
        latest = candidates[0]
        if (
            len(
                {
                    _date_value(record.date_text) or record.date_text
                    for record in candidates
                }
            )
            > 1
        ):
            warnings.append("最新版本在修订记录中对应多个不同日期")
            latest = Release(latest.version, "", latest.excerpt, latest.location)

    items = []
    comparisons = {}
    for field, label, normalizer in (
        ("version", "版本号", _version_key),
        ("date_text", "发布日期", _date_value),
    ):
        cover_value = getattr(cover, field)
        history_value = getattr(latest, field) if latest else ""
        left, right = normalizer(cover_value), normalizer(history_value)
        status = (
            "unverified"
            if left is None or right is None
            else "match"
            if left == right
            else "mismatch"
        )
        comparisons["version" if field == "version" else "date"] = {
            "status": status,
            "cover": cover_value,
            "history": history_value,
        }
        if status == "unverified":
            warnings.append(
                f"封面或最新修订条目的{label}缺失、格式不明确或无法唯一确定"
            )
        elif status == "mismatch":
            items.append(
                {
                    "status": "issue",
                    "severity": "medium",
                    "confidence": "high",
                    "category": f"文档{label}不一致",
                    "location": cover.location + "；" + latest.location,
                    "excerpt": f"封面：\n{cover.excerpt}\n最新修订条目：\n{latest.excerpt}",
                    "description": f"封面{label}为“{cover_value}”，修订记录最新文档版本“{latest.version}”的{label}为“{history_value}”，两者不一致。",
                    "impact": "文档发布标识与修订依据不一致，影响版本追溯。",
                    "suggestion": f"确认本次正式发布信息后，统一封面与最新修订条目的{label}。",
                }
            )
    if warnings:
        items.append(
            {
                "status": "suggestion",
                "severity": "low",
                "confidence": "low",
                "category": "文档发布信息待确认",
                "location": cover.location
                + ("；" + latest.location if latest else "；修订记录"),
                "excerpt": cover.excerpt + ("\n" + latest.excerpt if latest else ""),
                "description": "；".join(dict.fromkeys(warnings)) + "。",
                "impact": "当前文本证据不足以完成全部发布信息核对。",
                "suggestion": "核对原文封面和修订记录的版本、日期及行列对应关系。",
            }
        )
    summary = (
        "；".join(
            f"{label}一致（{comparison['cover']}）"
            if comparison["status"] == "match"
            else f"{label}不一致（封面 {comparison['cover']}，最新修订 {comparison['history']}）"
            if comparison["status"] == "mismatch"
            else f"{label}待确认"
            for label, comparison in zip(("版本号", "发布日期"), comparisons.values())
        )
        + "。"
    )
    return {"summary": summary, "items": items, "comparisons": comparisons}
