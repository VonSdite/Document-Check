"""识别修订记录章节，并为单文档 AI 检查生成正文视图。"""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

_PAGE = re.compile(r"\[第\d+页\]")
_TITLE = re.compile(
    r"(?:修订记录|修改记录|变更记录|修订历史|版本历史|版本修订记录|"
    r"(?:document\s+)?(?:change|revision)\s+(?:history|log|record)s?|changelog)",
    re.IGNORECASE,
)
_NUMBERED_HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+|第[^\s]{1,8}[章节]\s*|Appendix\s+[A-Z]\b)"
)
_KNOWN_HEADING = re.compile(
    r"(?:目\s*录|前言|概述|读者对象|符号约定|安全注意事项|概览|简介|"
    r"Contents|Table\s+of\s+Contents|Preface|Overview|Introduction|"
    r"About\s+(?:this\s+)?document|Safety\s+Precautions)",
    re.IGNORECASE,
)
_RECORD = re.compile(
    r"^(?:(?:文档版本|版本号?|修订版本|(?:Document\s+)?(?:Version|Issue|Revision)|Rev\.?)"
    r"\s*[:：]?\s*)?[vV]?\d+(?:\.\d+)*\s*[(（|\t ]\s*"
    r"(?:\d{4}[-/.年]|\d{1,2}(?:[-/.]|\s+[A-Za-z])|[A-Za-z]+\s+\d)",
    re.IGNORECASE,
)
_TABLE_HEADER = re.compile(
    r"^(?:Version|Issue|Revision|Rev\.?|文档版本|版本号?|修订版本)(?:\s|[|：:]|$)", re.I
)

_DATE_HEADER = re.compile(r"(?:发布日期|日期|(?:Release\s+)?Date)", re.I)

REVISION_SCOPE_INSTRUCTION = (
    "检查范围：修订记录、Change History、Revision History 属于历史变更说明。"
    "系统已按可确认的章节边界排除其内容；该范围不参与表述、术语、引用、完整性或正文一致性检查。"
    "历史版本中的软件版本、旧章节号和删除说明不代表当前正文要求。"
    "排除标记表示检查范围，不能作为内容缺失的证据。"
    "封面与最新修订条目的文档版本及发布日期由独立本地规则核对。"
)
REVISION_SCOPE_NOTE = "检查范围：已排除修订记录章节的历史变更说明。"


@dataclass(frozen=True)
class HistorySection:
    start: int
    end: int
    title: str


def readable_release_text(text: str) -> str:
    """保留表格行和单元格边界，读取发布字段的可见文字。"""
    if not re.search(r"<(?:table|p|h[1-6])\b", text, re.I):
        return text
    soup = BeautifulSoup(text, "html.parser")
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        row.replace_with(
            "\n" + " | ".join(cell.get_text(" ", strip=True) for cell in cells) + "\n"
        )
    return soup.get_text("\n")


def _heading_text(line: str) -> str:
    value = re.sub(r"</?h[1-6][^>]*>", "", line.strip(), flags=re.I)
    value = re.sub(r"^#{1,6}\s+", "", value).strip(" *：:")
    return _NUMBERED_HEADING.sub("", value).strip()


def _is_record(line: str) -> bool:
    return bool(_RECORD.match(line.strip(" |#*")))


def _boundary(line: str) -> bool:
    value = line.strip()
    if not value or _is_record(value) or _TITLE.fullmatch(_heading_text(value)):
        return False
    if re.match(
        r"^\d+[.)、]\s*(?:(?:更新|增加|新增|删除|修改|更正|优化)|"
        r"(?:Updated?|Added?|Removed?|Changed?|Revised?|Fixed?)\b)",
        value,
        re.I,
    ):
        return False
    return bool(
        re.match(r"^(?:#{1,6}\s+|<h[1-6]\b)", value, re.I)
        or _NUMBERED_HEADING.match(value)
        or _KNOWN_HEADING.fullmatch(_heading_text(value))
    )


def revision_history_sections(text: str) -> list[HistorySection]:
    lines = list(re.finditer(r"[^\n]+(?:\n|$)", text))
    sections = []
    consumed = -1
    fenced = False
    for index, line in enumerate(lines):
        value = line.group().strip()
        if value.startswith(("```", "~~~")):
            fenced = not fenced
        title = _heading_text(value)
        if fenced or line.start() < consumed or not _TITLE.fullmatch(title):
            continue
        end = len(text)
        for following in range(index + 1, len(lines)):
            candidate = lines[following]
            content = candidate.group().strip()
            if _boundary(content):
                next_line = (
                    lines[following + 1].group().strip()
                    if following + 1 < len(lines)
                    else ""
                )
                if re.fullmatch(
                    r"前言|目\s*录|Preface|Contents", content, re.I
                ) and re.fullmatch(r"[ivxlcdm]+", next_line, re.I):
                    continue
                end = candidate.start()
                break
            if _PAGE.fullmatch(content):
                # 跨页延续以重复标题、版本条目或表格列名为依据。
                upcoming = [
                    m.group().strip()
                    for m in lines[following + 1 : following + 7]
                    if m.group().strip()
                ]
                visible = [
                    part.strip()
                    for part in readable_release_text("\n".join(upcoming)).splitlines()
                    if part.strip()
                ]
                cells = [
                    cell.strip(" |") for part in visible[:3] for cell in part.split("|")
                ]
                continuation = any(
                    _TITLE.fullmatch(_heading_text(part))
                    or _is_record(part)
                    or _is_record(" ".join(visible[number : number + 2]))
                    for number, part in enumerate(visible[:3])
                ) or (
                    any(_TABLE_HEADER.fullmatch(cell) for cell in cells)
                    and any(_DATE_HEADER.fullmatch(cell) for cell in cells)
                )
                if not continuation:
                    end = candidate.start()
                    break
        body = text[line.end() : end]
        # 目录条目、正文提及和缺少记录的同名标题保持原文。
        if not re.search(
            r"\d{4}[-/.年]|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|"
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b",
            body,
            re.I,
        ):
            continue
        sections.append(HistorySection(line.start(), end, title))
        consumed = end
    return sections


def exclude_revision_history(text: str) -> tuple[str, list[HistorySection]]:
    sections = revision_history_sections(text)
    parts = []
    offset = 0
    for section in sections:
        parts.append(text[offset : section.start])
        parts.append(f"{section.title}\n[修订记录内容已按检查范围排除]\n")
        parts.extend(
            marker.group() + "\n"
            for marker in _PAGE.finditer(text, section.start, section.end)
        )
        offset = section.end
    parts.append(text[offset:])
    return "".join(parts), sections
