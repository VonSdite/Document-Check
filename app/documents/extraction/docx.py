import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from app.documents.extraction.common import (
    _clean_hyperlink_target,
    _format_hyperlink_text,
    _record_hyperlink,
    check_extraction_canceled,
)

_HYPERLINK_FIELD_PATTERN = re.compile(
    r"\bHYPERLINK\s+(?:(\\l)\s+)?(?:\"([^\"]+)\"|(\S+))",
    re.IGNORECASE,
)


def _extract_docx(
    path: Path, hyperlinks: list[dict] | None = None, *, cancel_event=None
) -> str:
    document = Document(str(path))
    internal_targets = {
        str(node.get(qn("w:name")) or "").strip()
        for node in document.element.iter(qn("w:bookmarkStart"))
        if str(node.get(qn("w:name")) or "").strip()
    }
    return _docx_blocks_text(
        document, hyperlinks, internal_targets, cancel_event=cancel_event
    )


def _docx_blocks_text(
    container,
    hyperlinks: list[dict] | None,
    internal_targets: set[str],
    *,
    location: str = "",
    cancel_event=None,
) -> str:
    parts = []
    paragraph_index = table_index = 0
    for block in container.iter_inner_content():
        check_extraction_canceled(cancel_event)
        if isinstance(block, Paragraph):
            paragraph_index += 1
            text = _docx_paragraph_text(
                block,
                hyperlinks,
                location=location or f"第{paragraph_index}段",
                internal_targets=internal_targets,
            )
        else:
            table_index += 1
            table_location = f"{location} > " if location else ""
            text = _docx_table_text(
                block,
                hyperlinks,
                internal_targets,
                location=f"{table_location}表格{table_index}",
                cancel_event=cancel_event,
            )
        if text:
            parts.append(text)
    return "\n".join(parts)


def _docx_table_text(
    table,
    hyperlinks: list[dict] | None,
    internal_targets: set[str],
    *,
    location: str,
    cancel_event=None,
) -> str:
    rows = []
    anchors = {}
    for row_index, row in enumerate(table.rows, start=1):
        check_extraction_canceled(cancel_event)
        cells = [""] * row.grid_cols_before
        for column_index, cell in enumerate(row.cells, start=row.grid_cols_before + 1):
            check_extraction_canceled(cancel_event)
            if cell._tc in anchors:
                cells.append(f"[合并覆盖，继承自{anchors[cell._tc]}]")
                continue
            anchors[cell._tc] = f"第{row_index}行第{column_index}列"
            cells.append(
                _docx_blocks_text(
                    cell,
                    hyperlinks,
                    internal_targets,
                    location=f"{location} > 第{row_index}行 > 第{column_index}列",
                    cancel_event=cancel_event,
                )
            )
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _docx_paragraph_text(
    paragraph,
    hyperlinks: list[dict] | None = None,
    *,
    location: str = "",
    internal_targets: set[str] | None = None,
) -> str:
    text = _docx_element_text(
        paragraph._p,
        paragraph.part,
        hyperlinks,
        location,
        internal_targets or set(),
    ).strip()
    for target in _docx_complex_field_hyperlinks(paragraph._p):
        _record_hyperlink(
            hyperlinks,
            "",
            target,
            location,
            internal_target_exists=(
                target.lstrip("#") in (internal_targets or set())
                if target.startswith("#")
                else None
            ),
        )
        if target not in text:
            text += f"（超链接：{target}）"
    return text


def _docx_element_text(
    element,
    part,
    hyperlinks: list[dict] | None,
    location: str,
    internal_targets: set[str],
) -> str:
    if element.tag == qn("w:hyperlink"):
        label = _docx_plain_element_text(element)
        target = _docx_hyperlink_target(element, part)
        _record_hyperlink(
            hyperlinks,
            label,
            target,
            location,
            internal_target_exists=(
                target.lstrip("#") in internal_targets
                if target.startswith("#")
                else None
            ),
        )
        return _format_hyperlink_text(label, target)
    if element.tag == qn("w:fldSimple"):
        label = _docx_plain_element_text(element)
        target = _docx_field_hyperlink_target(element.get(qn("w:instr")))
        _record_hyperlink(
            hyperlinks,
            label,
            target,
            location,
            internal_target_exists=(
                target.lstrip("#") in internal_targets
                if target.startswith("#")
                else None
            ),
        )
        return _format_hyperlink_text(label, target)
    if element.tag == qn("w:t"):
        return element.text or ""
    if element.tag == qn("w:tab"):
        return "\t"
    if element.tag in {qn("w:br"), qn("w:cr")}:
        return "\n"
    if element.tag == qn("w:noBreakHyphen"):
        return "-"
    if element.tag in {qn("w:instrText"), qn("w:delText")}:
        return ""
    return "".join(
        _docx_element_text(child, part, hyperlinks, location, internal_targets)
        for child in element
    )


def _docx_plain_element_text(element) -> str:
    if element.tag == qn("w:t"):
        return element.text or ""
    if element.tag == qn("w:tab"):
        return "\t"
    if element.tag in {qn("w:br"), qn("w:cr")}:
        return "\n"
    if element.tag == qn("w:noBreakHyphen"):
        return "-"
    if element.tag in {qn("w:instrText"), qn("w:delText")}:
        return ""
    return "".join(_docx_plain_element_text(child) for child in element)


def _docx_hyperlink_target(element, part) -> str:
    relationship_id = element.get(qn("r:id"))
    if relationship_id:
        try:
            return _clean_hyperlink_target(part.rels[relationship_id].target_ref)
        except (AttributeError, KeyError):
            pass
    anchor = _clean_hyperlink_target(element.get(qn("w:anchor")))
    return f"#{anchor}" if anchor else ""


def _docx_complex_field_hyperlinks(element) -> list[str]:
    instruction = "".join(node.text or "" for node in element.iter(qn("w:instrText")))
    return _hyperlink_targets_from_field_instruction(instruction)


def _docx_field_hyperlink_target(instruction) -> str:
    targets = _hyperlink_targets_from_field_instruction(instruction)
    return targets[0] if targets else ""


def _hyperlink_targets_from_field_instruction(instruction) -> list[str]:
    targets = []
    for match in _HYPERLINK_FIELD_PATTERN.finditer(str(instruction or "")):
        target = _clean_hyperlink_target(match.group(2) or match.group(3))
        if match.group(1) and target:
            target = f"#{target.lstrip('#')}"
        if target and target not in targets:
            targets.append(target)
    return targets
