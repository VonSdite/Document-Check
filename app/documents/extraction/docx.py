import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
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
    path: Path, hyperlinks: list[dict] | None = None, *, blocks=None, cancel_event=None
) -> str:
    document = Document(str(path))
    internal_targets = {
        str(node.get(qn("w:name")) or "").strip()
        for node in document.element.iter(qn("w:bookmarkStart"))
        if str(node.get(qn("w:name")) or "").strip()
    }
    parts = []
    paragraph_index, table_index, section = 0, 0, ""

    def append(text, kind, location):
        if text:
            parts.append(text)
            if blocks is not None:
                blocks.append(
                    {
                        "text": text,
                        "kind": kind,
                        "section": section,
                        "location": location,
                    }
                )

    def cell_text(cell, location):
        values = []
        for content in cell.iter_inner_content():
            check_extraction_canceled(cancel_event)
            if isinstance(content, Paragraph):
                values.append(
                    _docx_paragraph_text(
                        content,
                        hyperlinks,
                        location=location,
                        internal_targets=internal_targets,
                    )
                )
            else:
                for nested_row in content.rows:
                    values.append(
                        " | ".join(cell_text(c, location) for c in nested_row.cells)
                    )
        return "\n".join(value for value in values if value).strip()

    def append_table(table, prefix=""):
        nonlocal table_index
        table_index += 1
        for row_index, row in enumerate(table.rows, start=1):
            check_extraction_canceled(cancel_event)
            location = f"{prefix}表格{table_index} > 第{row_index}行"
            cells = [
                cell_text(cell, f"{location} > 第{column_index}列")
                for column_index, cell in enumerate(row.cells, start=1)
            ]
            if any(cells):
                append(" | ".join(cells), "table", location)

    for content in document.iter_inner_content():
        check_extraction_canceled(cancel_event)
        if isinstance(content, Paragraph):
            paragraph_index += 1
            location = f"第{paragraph_index}段"
            text = _docx_paragraph_text(
                content,
                hyperlinks,
                location=location,
                internal_targets=internal_targets,
            )
            heading = (
                str(content.style.name or "").lower().startswith(("heading", "标题"))
            )
            if heading and text:
                section = text
            append(text, "heading" if heading else "paragraph", location)
        elif isinstance(content, Table):
            append_table(content)
    if blocks is not None:
        unresolved = [document.element]
        for section_number, document_section in enumerate(document.sections, start=1):
            for label, container in (
                ("页眉", document_section.header),
                ("页脚", document_section.footer),
                ("首页页眉", document_section.first_page_header),
                ("首页页脚", document_section.first_page_footer),
                ("偶数页页眉", document_section.even_page_header),
                ("偶数页页脚", document_section.even_page_footer),
            ):
                if (
                    label.startswith("首页")
                    and not document_section.different_first_page_header_footer
                ):
                    continue
                if (
                    label.startswith("偶数")
                    and not document.settings.odd_and_even_pages_header_footer
                ):
                    continue
                if section_number > 1 and container.is_linked_to_previous:
                    continue
                section = f"第{section_number}节 / {label}"
                unresolved.append(container._element)
                for content in container.iter_inner_content():
                    check_extraction_canceled(cancel_event)
                    if isinstance(content, Paragraph):
                        text = _docx_paragraph_text(
                            content,
                            hyperlinks,
                            location=section,
                            internal_targets=internal_targets,
                        )
                        append(text, "paragraph", section)
                    elif isinstance(content, Table):
                        append_table(content, section + " / ")
        if any(
            any(element.iter(tag))
            for element in unresolved
            for tag in (qn("w:txbxContent"), qn("w:sdt"))
        ):
            blocks.append(
                {
                    "text": "",
                    "kind": "unresolved",
                    "location": "文本框或内容控件",
                    "section": "",
                }
            )
    return "\n".join(parts)


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
