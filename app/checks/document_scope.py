"""以第一章正文为单文档内容检查范围，保留原文定位信息。"""

import re
from dataclasses import dataclass

from app.checks.revision_history import (
    REVISION_SCOPE_INSTRUCTION,
    REVISION_SCOPE_NOTE,
    exclude_revision_history,
)

_PAGE = re.compile(r"\[第(\d+)页\]")
_FILE = re.compile(r"(?m)^file:[^\r\n]*")
_METADATA = re.compile(r"(?m)^file:[^\r\n]*|\[第\d+页\]")
_FIRST_CHAPTER = re.compile(
    r"^(?:第\s*[一1１]\s*章\s*[:：、.]?\s*|Chapter\s+(?:1|One)\b\s*[:：.\-]?\s*|"
    r"1(?:[.、]\s*|[ \t]+)(?!\d))(?P<title>.*)$",
    re.I,
)
_CONTENTS = re.compile(r"^(?:目\s*录|(?:Table\s+of\s+)?Contents)$", re.I)
_TOC_ENTRY = re.compile(r"(?:\.{2,}|…+|\t|[ \t])\s*(?:\d+|[ivxlcdm]+)\s*$", re.I)
_NUMBERED_HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*[.、]?\s+|第[^\s]{1,8}章|Chapter\s+\d+\b)", re.I
)

FRONT_MATTER_INSTRUCTION = (
    "检查范围：从文档第一章正文开始。第一章之前的封面、版权声明、前言、目录、"
    "修订记录等模板内容已排除，不参与表述、术语、编号引用、完整性和正文一致性检查。"
    "这些内容属于检查范围之外，不能据此报告缺失或要求补写。"
    "修订记录中的历史变更说明不代表当前正文要求。"
    "封面与最新修订条目的版本号和发布日期由独立本地规则核对。"
)


@dataclass(frozen=True)
class FirstChapter:
    start: int
    title: str
    page: int | None


def _visible_heading(value: str) -> str:
    value = re.sub(r"</?h[1-6][^>]*>", "", value.strip(), flags=re.I)
    return re.sub(r"^#{1,6}\s+", "", value).strip(" *")


def first_chapter(text: str) -> FirstChapter | None:
    if re.search(r"(?m)^#\s*工作表：", text):
        return None
    lines = list(re.finditer(r"[^\n]+(?:\n|$)", text))
    page = None
    page_start = 0
    fenced = in_table = annotations = False
    contents_page = template_page = False
    for index, line in enumerate(lines):
        raw = line.group().strip()
        marker = _PAGE.fullmatch(raw)
        if marker:
            page, page_start = int(marker[1]), line.end()
            annotations = contents_page = template_page = False
            continue
        if raw.startswith(("```", "~~~")):
            fenced = not fenced
        if re.search(r"<table\b", raw, re.I):
            in_table = True
        table_line = in_table
        if re.search(r"</table>", raw, re.I):
            in_table = False
        if raw.startswith("[超链接]"):
            annotations = True
        value = _visible_heading(raw)
        if _CONTENTS.fullmatch(value):
            contents_page = True
        if re.fullmatch(
            r"前言|修订记录|修改记录|版权声明|Preface|Change History|Revision History",
            value,
            re.I,
        ):
            template_page = True
        if fenced or table_line or annotations or "|" in value:
            continue
        match = _FIRST_CHAPTER.fullmatch(value)
        if not match:
            continue
        title = match["title"].strip()
        following = [
            m.group().strip()
            for m in lines[index + 1 : index + 13]
            if m.group().strip()
        ]
        if not title and following:
            title = _visible_heading(following.pop(0))
        if (
            not title
            or len(title) > 100
            or not re.search(r"[A-Za-z\u3400-\u9fff]", title)
            or re.search(r"[。！？!?；;]$|\.$", title)
            or (match["title"].strip() and _TOC_ENTRY.search(value))
            or _TOC_ENTRY.search(title)
            or (contents_page and page is not None)
        ):
            continue
        context = []
        for part in following:
            if _PAGE.fullmatch(part) or part.startswith("[超链接]"):
                break
            context.append(part)
        if not context or re.fullmatch(r"\d+|[ivxlcdm]+", context[0], re.I):
            continue
        if sum(bool(_TOC_ENTRY.search(part)) for part in context[:4]) >= 2:
            continue
        if len(context) >= 2 and all(
            _NUMBERED_HEADING.match(part) for part in context[:2]
        ):
            continue
        # 普通数字标题以页首、明确前置章节或后续章节结构作为依据。
        explicit = bool(re.match(r"^(?:第|Chapter\b|#{1,6}\s|<h[1-6]\b)", raw, re.I))
        if not explicit and (
            (template_page and page is not None)
            or re.match(
                r"^1[.、]?\s*(?:请|点击|选择|阅读|打开|不得|禁止|Please\b|Read\b|Click\b|Select\b|Open\b|Ensure\b)",
                value,
                re.I,
            )
        ):
            continue
        near_page_start = (
            page is not None and text[page_start : line.start()].count("\n") <= 4
        )
        front_heading = re.search(
            r"(?mi)^(?:前言|概述|版权声明|修订记录|修改记录|文档版本|发布日期|Preface|Copyright\b.*|Change History|Revision History|Contents|Table of Contents)\s*$",
            text[: line.start()],
        )
        next_section = any(re.match(r"^1\.\d+\s", part) for part in context)
        if (
            explicit
            or near_page_start
            or next_section
            or (front_heading and not re.match(r"^1[.、]", value))
        ):
            return FirstChapter(
                line.start(),
                value if match["title"].strip() else value + " " + title,
                page,
            )
    return None


@dataclass(frozen=True)
class DocumentCheckScope:
    source: str
    chapter: FirstChapter | None
    ai_text: str
    front_note: str
    ai_note: str
    instruction: str

    def prompt(self, prompt: str) -> str:
        return prompt + ("\n\n" + self.instruction if self.instruction else "")

    def term_text(self) -> str:
        """清空模板词项，并维持原始行号、页码和字符位置。"""
        if not self.front_note:
            return self.source
        prefix = self.source[: self.chapter.start]
        parts = []
        position = 0
        for marker in _METADATA.finditer(prefix):
            parts.append(re.sub(r"[^\r\n]", " ", prefix[position : marker.start()]))
            parts.append(marker.group())
            position = marker.end()
        parts.append(re.sub(r"[^\r\n]", " ", prefix[position:]))
        return "".join(parts) + self.source[self.chapter.start :]

    def hyperlinks(self, items: list[dict]) -> list[dict]:
        if not self.front_note:
            return items
        before, body = (
            self.source[: self.chapter.start],
            self.source[self.chapter.start :],
        )
        retained = []
        for item in items:
            location = re.fullmatch(r"第(\d+)页", item.get("location", "").strip())
            if location and self.chapter.page is not None:
                page = int(location[1])
                if page < self.chapter.page:
                    continue
                if page > self.chapter.page:
                    retained.append(item)
                    continue
            target = item.get("target", "")
            if target and target in before and target not in body:
                continue
            retained.append(item)
        return retained


def document_check_scope(text: str) -> DocumentCheckScope:
    chapter = first_chapter(text)
    body = text
    front_note = ""
    if chapter and _METADATA.sub("", text[: chapter.start]).strip():
        filename = _FILE.search(text)
        parts = [filename.group()] if filename else []
        if chapter.page is not None:
            parts.append(f"[第{chapter.page}页]")
        body = "\n\n".join(parts + [text[chapter.start :]])
        location = f"（第{chapter.page}页）" if chapter.page is not None else ""
        front_note = (
            f"检查范围：从“{chapter.title}”{location}开始，已排除第一章之前的模板内容。"
        )
    ai_text, history = exclude_revision_history(body)
    notes = [front_note] if front_note else []
    instructions = [FRONT_MATTER_INSTRUCTION] if front_note else []
    if history:
        notes.append(REVISION_SCOPE_NOTE)
        instructions.append(REVISION_SCOPE_INSTRUCTION)
    return DocumentCheckScope(
        text, chapter, ai_text, front_note, "\n".join(notes), "\n".join(instructions)
    )
