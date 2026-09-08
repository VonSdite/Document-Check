import html
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup
from docx import Document
from docx.oxml.ns import qn
import fitz
import mistune
from openpyxl import load_workbook
from openpyxl.utils.cell import get_column_letter, range_boundaries
from pypdf import PdfReader
import xlrd


ALLOWED_EXTENSIONS = {"docx", "pdf", "txt", "md", "html", "xlsx", "xlsm", "xls"}
_HYPERLINK_FIELD_PATTERN = re.compile(
    r"\bHYPERLINK\s+(?:(\\l)\s+)?(?:\"([^\"]+)\"|(\S+))",
    re.IGNORECASE,
)
_SPREADSHEETML_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_RELATIONSHIP_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PDF_TABLE_COORDINATE_TOLERANCE = 2.0
_PDF_TABLE_MIN_TEXT_COVERAGE = 0.85

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class DocumentReadError(Exception):
    pass


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[1].lower()


def extract_text(path: Path, file_type: str) -> str:
    text, _hyperlinks = extract_document(path, file_type)
    return text


def extract_document(path: Path, file_type: str) -> tuple[str, list[dict]]:
    hyperlinks = []
    try:
        if file_type == "docx":
            text = _extract_docx(path, hyperlinks)
            return text, hyperlinks
        if file_type == "pdf":
            text = _extract_pdf(path, hyperlinks)
            return text, hyperlinks
        if file_type == "txt":
            return _read_text(path), hyperlinks
        if file_type == "md":
            text = _read_text(path)
            _extract_markdown_hyperlinks(text, hyperlinks)
            return text, hyperlinks
        if file_type == "html":
            text = _extract_html(path, hyperlinks)
            return text, hyperlinks
        if file_type in {"xlsx", "xlsm"}:
            text = _extract_openpyxl_workbook(path, hyperlinks)
            return text, hyperlinks
        if file_type == "xls":
            text = _extract_xls(path, hyperlinks)
            return text, hyperlinks
    except Exception as exc:
        raise DocumentReadError(str(exc)) from exc
    raise DocumentReadError(f"不支持的文件类型：{file_type}")


def format_document_text(filename: str, text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    name = Path(str(filename or "")).name.strip()
    if not name:
        return text
    return f"file: {name}\n\n{text}"


def _extract_docx(path: Path, hyperlinks: list[dict] | None = None) -> str:
    document = Document(str(path))
    internal_targets = {
        str(node.get(qn("w:name")) or "").strip()
        for node in document.element.iter(qn("w:bookmarkStart"))
        if str(node.get(qn("w:name")) or "").strip()
    }
    parts = []
    for paragraph_index, paragraph in enumerate(document.paragraphs, start=1):
        text = _docx_paragraph_text(
            paragraph,
            hyperlinks,
            location=f"第{paragraph_index}段",
            internal_targets=internal_targets,
        )
        if text:
            parts.append(text)
    for table_index, table in enumerate(document.tables, start=1):
        for row_index, row in enumerate(table.rows, start=1):
            cells = []
            for column_index, cell in enumerate(row.cells, start=1):
                location = f"表格{table_index} > 第{row_index}行 > 第{column_index}列"
                cell_text = "\n".join(
                    text
                    for paragraph in cell.paragraphs
                    if (
                        text := _docx_paragraph_text(
                            paragraph,
                            hyperlinks,
                            location=location,
                            internal_targets=internal_targets,
                        )
                    )
                ).strip()
                if cell_text:
                    cells.append(cell_text)
            if cells:
                parts.append(" | ".join(cells))
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
                target.lstrip("#") in internal_targets if target.startswith("#") else None
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
                target.lstrip("#") in internal_targets if target.startswith("#") else None
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
    instruction = "".join(
        node.text or "" for node in element.iter(qn("w:instrText"))
    )
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


def _format_hyperlink_text(label, target) -> str:
    label = str(label or "").strip()
    target = _clean_hyperlink_target(target)
    if not target:
        return label
    if label == target:
        return label
    if not label:
        return f"超链接：{target}"
    return f"{label}（超链接：{target}）"


def _clean_hyperlink_target(target) -> str:
    if isinstance(target, bytes):
        target = target.decode("utf-8", errors="replace")
    value = re.sub(r"[\r\n\t]+", " ", str(target or "")).strip()
    return value[:4096]


def _record_hyperlink(
    hyperlinks: list[dict] | None,
    display_text,
    target,
    location: str,
    *,
    internal_target_exists: bool | None = None,
) -> None:
    if hyperlinks is None:
        return
    item = {
        "display_text": str(display_text or "").strip(),
        "target": _clean_hyperlink_target(target),
        "location": str(location or "").strip(),
    }
    if isinstance(internal_target_exists, bool):
        item["internal_target_exists"] = internal_target_exists
    hyperlinks.append(item)


def _extract_pdf(path: Path, extracted_hyperlinks: list[dict] | None = None) -> str:
    reader = PdfReader(str(path))
    try:
        layout_document = fitz.open(str(path))
    except Exception:
        layout_document = None
    try:
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            pypdf_text = page.extract_text() or ""
            if layout_document is not None and index <= layout_document.page_count:
                layout_page = layout_document[index - 1]
                pymupdf_text, raw_page = _extract_pymupdf_page_text(layout_page)
                pypdf_text = _normalize_pdf_overlapping_spaces(pypdf_text, raw_page)
                pymupdf_text = _normalize_pdf_overlapping_spaces(pymupdf_text, raw_page)
                text = _select_pdf_page_text(pypdf_text, pymupdf_text)
                if text == pymupdf_text:
                    structured_text = _extract_pymupdf_page_with_tables(
                        layout_page,
                        index,
                        raw_page,
                        pymupdf_text,
                    )
                    if structured_text:
                        text = structured_text
                hyperlinks = _extract_pymupdf_page_hyperlinks(layout_page)
            else:
                text = pypdf_text
                hyperlinks = _extract_pypdf_page_hyperlinks(page)
            if hyperlinks:
                text = _append_hyperlinks(text, hyperlinks)
                for label, target, internal_target_exists in hyperlinks:
                    _record_hyperlink(
                        extracted_hyperlinks,
                        label,
                        target,
                        f"第{index}页",
                        internal_target_exists=internal_target_exists,
                    )
            if text.strip():
                pages.append(f"[第{index}页]\n{text.strip()}")
        return "\n\n".join(pages)
    finally:
        if layout_document is not None:
            layout_document.close()


def _extract_pymupdf_page_text(page) -> tuple[str, dict]:
    try:
        text_page = page.get_textpage()
        text = page.get_text("text", textpage=text_page, sort=False) or ""
        raw_page = page.get_text("rawdict", textpage=text_page)
        return text, raw_page
    except Exception:
        return "", {}


def _extract_pymupdf_page_with_tables(
    page,
    page_number: int,
    raw_page: dict,
    source_text: str,
) -> str:
    try:
        table_finder = page.find_tables(strategy="lines_strict")
        candidate_tables = list(table_finder.tables)
        if not candidate_tables:
            table_finder = page.find_tables(strategy="lines")
            candidate_tables = list(table_finder.tables)
        drawings = page.get_drawings()
        image_bboxes = [
            tuple(float(value) for value in image.get("bbox"))
            for image in page.get_image_info()
            if image.get("bbox")
        ]
    except Exception as exc:
        logger.debug("PDF 表格检测失败 page=%s error=%s", page_number, exc)
        return ""

    table_models = []
    for table_index, table in enumerate(candidate_tables, start=1):
        try:
            model = _pdf_table_model(
                page,
                table,
                page_number=page_number,
                table_index=table_index,
                drawings=drawings,
                image_bboxes=image_bboxes,
            )
        except Exception as exc:
            logger.debug(
                "PDF 单个表格结构化失败 page=%s table=%s error=%s",
                page_number,
                table_index,
                exc,
            )
            continue
        if model is not None:
            table_models.append(model)
    if not table_models:
        return ""

    table_rects = [model["bbox"] for model in table_models]
    elements = _pdf_page_text_elements(raw_page, table_rects)
    for model in table_models:
        elements.append(
            {
                "bbox": model["bbox"],
                "content": _format_pdf_table_html(model),
                "plain_text": "\n".join(cell["text"] for cell in model["cells"] if cell["text"]),
                "kind": "table",
                "source_order": model["table_index"],
            }
        )
    elements.sort(key=_pdf_page_element_sort_key)
    plain_text = "\n".join(element["plain_text"] for element in elements if element["plain_text"])
    source_chars = _pdf_text_quality(source_text)["useful_chars"]
    structured_chars = _pdf_text_quality(plain_text)["useful_chars"]
    if source_chars and structured_chars < source_chars * _PDF_TABLE_MIN_TEXT_COVERAGE:
        logger.debug(
            "PDF 表格结构化结果文本覆盖不足 page=%s source_chars=%s structured_chars=%s",
            page_number,
            source_chars,
            structured_chars,
        )
        return ""
    return "\n".join(element["content"] for element in elements if element["content"]).strip()


def _pdf_table_model(
    page,
    table,
    *,
    page_number: int,
    table_index: int,
    drawings: list[dict],
    image_bboxes: list[tuple[float, ...]],
) -> dict | None:
    try:
        row_count = int(table.row_count)
        column_count = int(table.col_count)
        rows = list(table.rows)
        extracted_rows = table.extract()
        table_bbox = tuple(float(value) for value in table.bbox)
    except Exception:
        return None
    if row_count < 2 or column_count < 2 or len(rows) != row_count:
        return None

    row_boundaries = _cluster_pdf_coordinates(
        [
            coordinate
            for row in rows
            for coordinate in (float(row.bbox[1]), float(row.bbox[3]))
        ]
    )
    column_boundaries = _cluster_pdf_coordinates(
        [
            coordinate
            for row in rows
            for cell_bbox in row.cells
            if cell_bbox is not None
            for coordinate in (float(cell_bbox[0]), float(cell_bbox[2]))
        ]
    )
    if len(row_boundaries) != row_count + 1 or len(column_boundaries) != column_count + 1:
        return None

    cells = []
    covered_positions = set()
    for row_index, row in enumerate(rows):
        if len(row.cells) != column_count:
            return None
        for column_index, cell_bbox in enumerate(row.cells):
            if cell_bbox is None:
                continue
            bbox = tuple(float(value) for value in cell_bbox)
            start_row = _pdf_coordinate_index(row_boundaries, bbox[1])
            end_row = _pdf_coordinate_index(row_boundaries, bbox[3])
            start_column = _pdf_coordinate_index(column_boundaries, bbox[0])
            end_column = _pdf_coordinate_index(column_boundaries, bbox[2])
            if (
                start_row != row_index
                or start_column != column_index
                or end_row is None
                or end_column is None
                or end_row <= row_index
                or end_column <= column_index
            ):
                return None
            rowspan = end_row - row_index
            colspan = end_column - column_index
            for covered_row in range(row_index, end_row):
                for covered_column in range(column_index, end_column):
                    covered_positions.add((covered_row, covered_column))
            cell_text = ""
            extracted_row = (
                extracted_rows[row_index]
                if isinstance(extracted_rows, (list, tuple)) and row_index < len(extracted_rows)
                else ()
            )
            if isinstance(extracted_row, (list, tuple)) and column_index < len(extracted_row):
                cell_text = str(extracted_row[column_index] or "").strip()
            if not cell_text:
                try:
                    cell_text = str(page.get_textbox(fitz.Rect(bbox)) or "").strip()
                except Exception:
                    cell_text = ""
            has_nontext_content = _pdf_cell_has_nontext_content(
                bbox,
                image_bboxes=image_bboxes,
                drawings=drawings,
            )
            cells.append(
                {
                    "row": row_index,
                    "column": column_index,
                    "rowspan": rowspan,
                    "colspan": colspan,
                    "bbox": bbox,
                    "text": cell_text,
                    "has_nontext_content": has_nontext_content,
                }
            )

    unresolved_positions = [
        (row_index, column_index)
        for row_index, row in enumerate(rows)
        for column_index, cell_bbox in enumerate(row.cells)
        if cell_bbox is None and (row_index, column_index) not in covered_positions
    ]
    if not cells or not any(cell["text"] for cell in cells):
        return None
    vector_edge_count = _pdf_table_vector_edge_count(drawings, table_bbox)
    if unresolved_positions:
        confidence = "low"
    elif vector_edge_count >= max(4, row_count + column_count):
        confidence = "high"
    else:
        confidence = "medium"
    return {
        "id": f"page{page_number:03d}-table{table_index:03d}",
        "page_number": page_number,
        "table_index": table_index,
        "bbox": table_bbox,
        "row_count": row_count,
        "column_count": column_count,
        "cells": cells,
        "confidence": confidence,
        "unresolved_positions": unresolved_positions,
        "vector_edge_count": vector_edge_count,
    }


def _cluster_pdf_coordinates(values, tolerance: float = _PDF_TABLE_COORDINATE_TOLERANCE) -> list[float]:
    groups = []
    for value in sorted(float(item) for item in values):
        if not groups or abs(value - sum(groups[-1]) / len(groups[-1])) > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _pdf_coordinate_index(boundaries: list[float], value: float) -> int | None:
    if not boundaries:
        return None
    index = min(range(len(boundaries)), key=lambda candidate: abs(boundaries[candidate] - value))
    if abs(boundaries[index] - value) > _PDF_TABLE_COORDINATE_TOLERANCE:
        return None
    return index


def _pdf_table_vector_edge_count(drawings: list[dict], table_bbox: tuple[float, ...]) -> int:
    table_rect = fitz.Rect(table_bbox)
    count = 0
    for drawing in drawings:
        drawing_rect = drawing.get("rect")
        if drawing_rect is not None and not fitz.Rect(drawing_rect).intersects(table_rect):
            continue
        for item in drawing.get("items", []):
            if not item:
                continue
            if item[0] == "l":
                count += 1
            elif item[0] == "re":
                count += 4
    return count


def _pdf_cell_has_nontext_content(
    cell_bbox: tuple[float, ...],
    *,
    image_bboxes: list[tuple[float, ...]],
    drawings: list[dict],
) -> bool:
    cell_rect = fitz.Rect(cell_bbox)
    for image_bbox in image_bboxes:
        image_rect = fitz.Rect(image_bbox)
        if _pdf_rect_contains_center(cell_bbox, image_bbox) and image_rect.get_area() >= 4:
            return True
    inset = max(1.0, min(cell_rect.width, cell_rect.height) * 0.04)
    interior = fitz.Rect(
        cell_rect.x0 + inset,
        cell_rect.y0 + inset,
        cell_rect.x1 - inset,
        cell_rect.y1 - inset,
    )
    if interior.is_empty:
        return False
    for drawing in drawings:
        drawing_rect_value = drawing.get("rect")
        if drawing_rect_value is None:
            continue
        drawing_rect = fitz.Rect(drawing_rect_value)
        if (
            drawing_rect.width >= 2
            and drawing_rect.height >= 2
            and _pdf_rect_contains_center(tuple(interior), tuple(drawing_rect))
        ):
            return True
    return False


def _pdf_page_text_elements(raw_page: dict, table_rects: list[tuple[float, ...]]) -> list[dict]:
    elements = []
    source_order = 0
    for block in raw_page.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            source_order += 1
            _source, text = _pdf_line_text_variants(line)
            text = text.strip()
            bbox = line.get("bbox")
            if not text or not bbox:
                continue
            normalized_bbox = tuple(float(value) for value in bbox)
            if any(_pdf_rect_contains_center(table_rect, normalized_bbox) for table_rect in table_rects):
                continue
            elements.append(
                {
                    "bbox": normalized_bbox,
                    "content": text,
                    "plain_text": text,
                    "kind": "text",
                    "source_order": source_order,
                }
            )
    return elements


def _pdf_rect_contains_center(container_bbox: tuple[float, ...], item_bbox: tuple[float, ...]) -> bool:
    center_x = (item_bbox[0] + item_bbox[2]) / 2
    center_y = (item_bbox[1] + item_bbox[3]) / 2
    return (
        container_bbox[0] - _PDF_TABLE_COORDINATE_TOLERANCE
        <= center_x
        <= container_bbox[2] + _PDF_TABLE_COORDINATE_TOLERANCE
        and container_bbox[1] - _PDF_TABLE_COORDINATE_TOLERANCE
        <= center_y
        <= container_bbox[3] + _PDF_TABLE_COORDINATE_TOLERANCE
    )


def _pdf_page_element_sort_key(element: dict) -> tuple:
    bbox = element["bbox"]
    return (
        round(float(bbox[1]), 1),
        round(float(bbox[0]), 1),
        1 if element["kind"] == "table" else 0,
        int(element["source_order"]),
    )


def _format_pdf_table_html(model: dict) -> str:
    normalized_cells = {}
    for cell in model["cells"]:
        anchor = _pdf_cell_anchor_reference(cell)
        original_range = _pdf_cell_reference(cell)
        for row_index in range(cell["row"], cell["row"] + cell["rowspan"]):
            for column_index in range(cell["column"], cell["column"] + cell["colspan"]):
                inherited = (row_index, column_index) != (cell["row"], cell["column"])
                normalized_cells[(row_index, column_index)] = {
                    "cell": cell,
                    "anchor": anchor,
                    "original_range": original_range,
                    "inherited": inherited,
                }
    lines = [
        (
            f'[PDF结构化表格 {model["id"]} 开始；'
            f'{model["row_count"]}行×{model["column_count"]}列；'
            f'置信度={model["confidence"]}；视图=归一化；'
            f'合并继承位置通过 data-inherited-from 标记]'
        ),
        (
            f'<table id="{model["id"]}" data-confidence="{model["confidence"]}" '
            'data-view="normalized">'
        ),
    ]
    for row_index in range(model["row_count"]):
        lines.append("  <tr>")
        for column_index in range(model["column_count"]):
            cell_ref = _pdf_position_reference(row_index, column_index)
            normalized = normalized_cells.get((row_index, column_index))
            if normalized is None:
                lines.append(
                    f'    <td data-cell="{cell_ref}" data-unresolved="true">'
                    "[未能可靠还原的单元格]</td>"
                )
                continue
            cell = normalized["cell"]
            attributes = [f'data-cell="{cell_ref}"']
            if cell["rowspan"] > 1 or cell["colspan"] > 1:
                attributes.append(f'data-original-range="{normalized["original_range"]}"')
            if normalized["inherited"]:
                attributes.append(f'data-inherited-from="{normalized["anchor"]}"')
                if cell["text"]:
                    value = html.escape(cell["text"])
                elif cell["has_nontext_content"]:
                    value = f'[合并覆盖，继承自{normalized["anchor"]}的非文本内容]'
                else:
                    value = f'[合并覆盖，继承自{normalized["anchor"]}]'
            else:
                if cell["rowspan"] > 1:
                    attributes.append(f'data-original-rowspan="{cell["rowspan"]}"')
                if cell["colspan"] > 1:
                    attributes.append(f'data-original-colspan="{cell["colspan"]}"')
                value = html.escape(cell["text"])
                if not value:
                    if cell["has_nontext_content"]:
                        attributes.append('data-non-text="true"')
                        value = "[非文本图形或图标]"
                    else:
                        attributes.append('data-empty="true"')
                        value = "[空单元格]"
            lines.append(f"    <td {' '.join(attributes)}>{value}</td>")
        lines.append("  </tr>")
    if model["unresolved_positions"]:
        positions = ",".join(
            f"{get_column_letter(column + 1)}{row + 1}"
            for row, column in model["unresolved_positions"]
        )
        lines.append(f"  <!-- 未能可靠还原的网格位置：{positions} -->")
    lines.extend(
        [
            "</table>",
            f'[PDF结构化表格 {model["id"]} 结束]',
        ]
    )
    return "\n".join(lines)


def _pdf_cell_reference(cell: dict) -> str:
    start = _pdf_cell_anchor_reference(cell)
    end = _pdf_position_reference(
        cell["row"] + cell["rowspan"] - 1,
        cell["column"] + cell["colspan"] - 1,
    )
    return start if start == end else f"{start}:{end}"


def _pdf_cell_anchor_reference(cell: dict) -> str:
    return _pdf_position_reference(cell["row"], cell["column"])


def _pdf_position_reference(row: int, column: int) -> str:
    return f"{get_column_letter(column + 1)}{row + 1}"


def _extract_pymupdf_page_hyperlinks(page) -> list[tuple[str, str, bool | None]]:
    hyperlinks = []
    try:
        links = page.get_links()
    except Exception:
        return hyperlinks
    for link in links:
        target = _clean_hyperlink_target(link.get("uri") or link.get("file"))
        internal_target_exists = None
        if not target and link.get("kind") == fitz.LINK_GOTO:
            page_index = link.get("page")
            if isinstance(page_index, int):
                target = f"#page={page_index + 1}"
                internal_target_exists = 0 <= page_index < page.parent.page_count
        if not target and link.get("nameddest"):
            target = f"#{_clean_hyperlink_target(link.get('nameddest')).lstrip('#')}"
        label = ""
        link_rect = link.get("from")
        if link_rect:
            try:
                label = page.get_textbox(link_rect).strip()
            except Exception:
                pass
        item = (label, target, internal_target_exists)
        if item not in hyperlinks:
            hyperlinks.append(item)
    return hyperlinks


def _extract_pypdf_page_hyperlinks(page) -> list[tuple[str, str, bool | None]]:
    hyperlinks = []
    try:
        annotations = page.get("/Annots") or []
    except Exception:
        return hyperlinks
    for annotation_ref in annotations:
        try:
            annotation = annotation_ref.get_object()
            if annotation.get("/Subtype") != "/Link":
                continue
            action = annotation.get("/A") or {}
            target = _clean_hyperlink_target(action.get("/URI"))
        except Exception:
            continue
        item = ("", target, None)
        if target and item not in hyperlinks:
            hyperlinks.append(item)
    return hyperlinks


def _append_hyperlinks(
    text: str,
    hyperlinks: list[tuple[str, str, bool | None]],
) -> str:
    annotations = [
        f"[超链接] {_format_hyperlink_text(label, target)}"
        for label, target, _internal_target_exists in hyperlinks
    ]
    return "\n".join(part for part in (str(text or "").strip(), *annotations) if part)


def _select_pdf_page_text(pypdf_text: str, pymupdf_text: str) -> str:
    pypdf_text = str(pypdf_text or "")
    pymupdf_text = str(pymupdf_text or "")
    if not pymupdf_text.strip():
        return pypdf_text
    if not pypdf_text.strip():
        return pymupdf_text

    pypdf_quality = _pdf_text_quality(pypdf_text)
    pymupdf_quality = _pdf_text_quality(pymupdf_text)
    pypdf_useful = pypdf_quality["useful_chars"]
    pymupdf_useful = pymupdf_quality["useful_chars"]

    if (
        pymupdf_quality["bad_char_ratio"] >= 0.01
        and pypdf_quality["bad_char_ratio"] <= pymupdf_quality["bad_char_ratio"] * 0.5
        and pypdf_useful >= pymupdf_useful * 0.7
    ):
        return pypdf_text

    if pypdf_quality["bad_char_ratio"] > max(0.01, pymupdf_quality["bad_char_ratio"] * 2):
        return pymupdf_text

    pymupdf_tokens = pymupdf_quality["tokens"]
    pypdf_tokens = pypdf_quality["tokens"]
    pymupdf_covered_by_pypdf = _shared_pdf_token_ratio(pymupdf_tokens, pypdf_tokens)
    pypdf_covered_by_pymupdf = _shared_pdf_token_ratio(pypdf_tokens, pymupdf_tokens)
    if (
        pymupdf_tokens
        and pypdf_tokens
        and pymupdf_covered_by_pypdf < 0.3
        and pypdf_covered_by_pymupdf < 0.3
    ):
        return pypdf_text

    extra_pypdf_chars = pypdf_useful - pymupdf_useful
    pypdf_relative_gain = pypdf_useful / max(pymupdf_useful, 1)
    pypdf_is_materially_more_complete = extra_pypdf_chars >= max(20, int(pymupdf_useful * 0.15))
    if (
        pypdf_is_materially_more_complete
        and pypdf_relative_gain >= 1.15
        and pymupdf_covered_by_pypdf >= 0.8
    ):
        return pypdf_text
    return pymupdf_text


def _pdf_text_quality(text: str) -> dict:
    visible_chars = [char for char in text if not char.isspace()]
    bad_chars = [char for char in visible_chars if _is_bad_pdf_text_char(char)]
    useful_chars = len(visible_chars) - len(bad_chars)
    tokens = set(re.findall(r"\w+", text.casefold(), flags=re.UNICODE))
    return {
        "useful_chars": useful_chars,
        "bad_char_ratio": len(bad_chars) / max(len(visible_chars), 1),
        "tokens": tokens,
    }


def _is_bad_pdf_text_char(char: str) -> bool:
    if char == "\ufffd":
        return True
    return unicodedata.category(char) in {"Cc", "Cs", "Co", "Cn"}


def _shared_pdf_token_ratio(primary_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not primary_tokens:
        return 1.0
    return len(primary_tokens & candidate_tokens) / len(primary_tokens)


def _normalize_pdf_overlapping_spaces(text: str, raw_page: dict) -> str:
    """Remove encoded spaces that have no visible advance on the PDF page."""
    if not text or not raw_page:
        return text

    source_counts = {}
    replacements = {}
    for block in raw_page.get("blocks", []):
        for line in block.get("lines", []):
            source, normalized = _pdf_line_text_variants(line)
            if not source:
                continue
            occurrence = source_counts.get(source, 0) + 1
            source_counts[source] = occurrence
            if normalized != source:
                replacements[(source, occurrence)] = normalized

    if not replacements:
        return text

    text_counts = {}
    normalized_lines = []
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        line_ending = line[len(content) :]
        occurrence = text_counts.get(content, 0) + 1
        text_counts[content] = occurrence
        normalized_lines.append(replacements.get((content, occurrence), content) + line_ending)
    return "".join(normalized_lines)


def _pdf_line_text_variants(line: dict) -> tuple[str, str]:
    chars = []
    for span in line.get("spans", []):
        size = float(span.get("size") or 0)
        for char in span.get("chars", []):
            value = str(char.get("c") or "")
            if value:
                chars.append({"value": value, "origin": char.get("origin"), "size": size})

    source = "".join(char["value"] for char in chars)
    if " " not in source or len(chars) < 3:
        return source, source

    direction = line.get("dir") or (1.0, 0.0)
    try:
        direction_x = float(direction[0])
        direction_y = float(direction[1])
        direction_length = (direction_x**2 + direction_y**2) ** 0.5
    except (TypeError, ValueError, IndexError):
        return source, source
    if direction_length <= 0:
        return source, source
    direction_x /= direction_length
    direction_y /= direction_length

    remove_indexes = set()
    for index, char in enumerate(chars):
        if char["value"] != " " or not any(item["value"].strip() for item in chars[:index]):
            continue
        next_char = next((item for item in chars[index + 1 :] if item["value"].strip()), None)
        if next_char is None:
            continue
        origin = char.get("origin")
        next_origin = next_char.get("origin")
        if not origin or not next_origin:
            continue
        try:
            advance = (
                (float(next_origin[0]) - float(origin[0])) * direction_x
                + (float(next_origin[1]) - float(origin[1])) * direction_y
            )
        except (TypeError, ValueError, IndexError):
            continue
        tolerance = max(0.25, float(char.get("size") or 0) * 0.03)
        if abs(advance) <= tolerance:
            remove_indexes.add(index)

    normalized = "".join(char["value"] for index, char in enumerate(chars) if index not in remove_indexes)
    return source, normalized


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentReadError("无法识别文本编码，请使用 UTF-8 文档")


def _extract_markdown_hyperlinks(text: str, hyperlinks: list[dict]) -> None:
    rendered = mistune.html(text)
    soup = BeautifulSoup(rendered, "html.parser")
    for index, anchor in enumerate(soup.find_all("a"), start=1):
        if not anchor.has_attr("href"):
            continue
        target = _clean_hyperlink_target(anchor.get("href"))
        _record_hyperlink(
            hyperlinks,
            anchor.get_text(" ", strip=True),
            target,
            f"Markdown 链接{index}",
        )


def _extract_html(path: Path, hyperlinks: list[dict] | None = None) -> str:
    html = _read_text(path)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    internal_targets = {
        str(tag.get("id") or tag.get("name") or "").strip()
        for tag in soup.find_all(attrs={"id": True}) + soup.find_all(attrs={"name": True})
        if str(tag.get("id") or tag.get("name") or "").strip()
    }
    for index, anchor in enumerate(soup.find_all("a"), start=1):
        if not anchor.has_attr("href"):
            continue
        label = anchor.get_text(" ", strip=True)
        target = _clean_hyperlink_target(anchor.get("href"))
        _record_hyperlink(
            hyperlinks,
            label,
            target,
            f"HTML 链接{index}",
            internal_target_exists=(
                target == "#" or target.lstrip("#") in internal_targets
                if target.startswith("#")
                else None
            ),
        )
        anchor.replace_with(_format_hyperlink_text(label, target))
    return soup.get_text("\n", strip=True)


def _extract_openpyxl_workbook(
    path: Path,
    extracted_hyperlinks: list[dict] | None = None,
) -> str:
    value_workbook = load_workbook(path, read_only=True, data_only=True)
    formula_workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        parts = []
        formula_sheets = {sheet.title: sheet for sheet in formula_workbook.worksheets}
        try:
            hyperlinks = _extract_xlsx_hyperlinks(path, formula_workbook)
        except (ET.ParseError, KeyError, OSError, zipfile.BadZipFile):
            hyperlinks = {}
        for sheet in value_workbook.worksheets:
            formula_sheet = formula_sheets.get(sheet.title)
            rows = _openpyxl_sheet_rows_text(
                sheet,
                formula_sheet,
                hyperlinks.get(sheet.title, []),
                extracted_hyperlinks,
                set(formula_sheets),
            )
            if rows:
                parts.append(f"# 工作表：{sheet.title}\n" + "\n".join(rows))
        return "\n\n".join(parts)
    finally:
        value_workbook.close()
        formula_workbook.close()


def _extract_xls(path: Path, hyperlinks: list[dict] | None = None) -> str:
    workbook = xlrd.open_workbook(str(path), on_demand=True)
    try:
        parts = []
        for sheet in workbook.sheets():
            rows = []
            for row_index in range(sheet.nrows):
                values = []
                for column_index in range(sheet.ncols):
                    value = sheet.cell_value(row_index, column_index)
                    hyperlink = sheet.hyperlink_map.get((row_index, column_index))
                    if hyperlink is not None:
                        target = _clean_hyperlink_target(hyperlink.url_or_path)
                        textmark = _clean_hyperlink_target(hyperlink.textmark)
                        if textmark:
                            target = f"{target}#{textmark}" if target else f"#{textmark}"
                        label = value or hyperlink.desc
                        value = _format_hyperlink_text(label, target)
                        _record_hyperlink(
                            hyperlinks,
                            label,
                            target,
                            (
                                f"工作表“{sheet.name}”!"
                                f"{get_column_letter(column_index + 1)}{row_index + 1}"
                            ),
                            internal_target_exists=(
                                _spreadsheet_internal_target_exists(
                                    target,
                                    set(workbook.sheet_names()),
                                )
                                if target.startswith("#")
                                else None
                            ),
                        )
                    values.append(value)
                row_text = _spreadsheet_row_text(values)
                if row_text:
                    rows.append(row_text)
            if rows:
                parts.append(f"# 工作表：{sheet.name}\n" + "\n".join(rows))
        return "\n\n".join(parts)
    finally:
        workbook.release_resources()


def _openpyxl_sheet_rows_text(
    sheet,
    formula_sheet,
    hyperlinks,
    extracted_hyperlinks: list[dict] | None = None,
    workbook_titles: set[str] | None = None,
) -> list[str]:
    rows = []
    max_row = max(sheet.max_row or 0, getattr(formula_sheet, "max_row", 0) or 0)
    max_column = max(sheet.max_column or 0, getattr(formula_sheet, "max_column", 0) or 0)
    for row_index in range(1, max_row + 1):
        values = []
        for column_index in range(1, max_column + 1):
            value = sheet.cell(row_index, column_index).value
            formula_value = None
            if value is None and formula_sheet is not None:
                formula_value = formula_sheet.cell(row_index, column_index).value
                if isinstance(formula_value, str) and formula_value.startswith("="):
                    value = formula_value
            elif formula_sheet is not None:
                formula_value = formula_sheet.cell(row_index, column_index).value
            hyperlink = _xlsx_hyperlink_at(hyperlinks, row_index, column_index)
            formula_target = _xlsx_formula_hyperlink_target(formula_value)
            target = formula_target or (hyperlink[0] if hyperlink else "")
            if target:
                display = hyperlink[1] if hyperlink else ""
                label = value or display
                value = _format_hyperlink_text(label, target)
                _record_hyperlink(
                    extracted_hyperlinks,
                    label,
                    target,
                    (
                        f"工作表“{sheet.title}”!"
                        f"{get_column_letter(column_index)}{row_index}"
                    ),
                    internal_target_exists=(
                        _spreadsheet_internal_target_exists(
                            target,
                            workbook_titles or set(),
                        )
                        if target.startswith("#")
                        else None
                    ),
                )
            values.append(value)
        row_text = _spreadsheet_row_text(values)
        if row_text:
            rows.append(row_text)
    return rows


def _extract_xlsx_hyperlinks(path: Path, workbook) -> dict[str, list[tuple]]:
    result = {}
    with zipfile.ZipFile(path) as archive:
        archive_names = set(archive.namelist())
        for sheet in workbook.worksheets:
            sheet_path = str(getattr(sheet, "_worksheet_path", "")).replace("\\", "/")
            if not sheet_path or sheet_path not in archive_names:
                continue
            relationships = _xlsx_sheet_relationships(
                archive,
                archive_names,
                sheet_path,
            )
            items = []
            with archive.open(sheet_path) as source:
                for _event, element in ET.iterparse(source, events=("end",)):
                    if element.tag != f"{{{_SPREADSHEETML_NAMESPACE}}}hyperlink":
                        element.clear()
                        continue
                    cell_range = str(element.get("ref") or "").strip()
                    relationship_id = element.get(
                        f"{{{_OFFICE_RELATIONSHIP_NAMESPACE}}}id"
                    )
                    target = _clean_hyperlink_target(
                        relationships.get(relationship_id, "")
                    )
                    if not target:
                        location = _clean_hyperlink_target(element.get("location"))
                        target = f"#{location}" if location else ""
                    try:
                        bounds = range_boundaries(cell_range)
                    except ValueError:
                        bounds = None
                    if target and bounds:
                        items.append((*bounds, target, element.get("display") or ""))
                    element.clear()
            if items:
                result[sheet.title] = items
    return result


def _xlsx_sheet_relationships(
    archive: zipfile.ZipFile,
    archive_names: set[str],
    sheet_path: str,
) -> dict[str, str]:
    sheet_name = sheet_path.rsplit("/", 1)[-1]
    sheet_dir = sheet_path.rsplit("/", 1)[0]
    relationship_path = f"{sheet_dir}/_rels/{sheet_name}.rels"
    if relationship_path not in archive_names:
        return {}
    relationships = {}
    with archive.open(relationship_path) as source:
        for _event, element in ET.iterparse(source, events=("end",)):
            if element.tag.rsplit("}", 1)[-1] == "Relationship":
                relationship_id = element.get("Id")
                target = _clean_hyperlink_target(element.get("Target"))
                if relationship_id and target:
                    relationships[relationship_id] = target
            element.clear()
    return relationships


def _xlsx_hyperlink_at(hyperlinks, row_index: int, column_index: int):
    for min_column, min_row, max_column, max_row, target, display in hyperlinks:
        if min_row <= row_index <= max_row and min_column <= column_index <= max_column:
            return target, display
    return None


def _xlsx_formula_hyperlink_target(formula) -> str:
    match = re.match(
        r'^=HYPERLINK\(\s*"((?:[^"]|"")*)"',
        str(formula or ""),
        re.IGNORECASE,
    )
    if not match:
        return ""
    return _clean_hyperlink_target(match.group(1).replace('""', '"'))


def _spreadsheet_internal_target_exists(target: str, workbook_titles: set[str]) -> bool:
    location = str(target or "").lstrip("#").strip()
    if not location:
        return False
    sheet_name = location.split("!", 1)[0].strip().strip("'").replace("''", "'")
    if "!" not in location:
        return True
    return sheet_name in workbook_titles


def _spreadsheet_rows_text(rows) -> list[str]:
    result = []
    for row in rows:
        row_text = _spreadsheet_row_text(row)
        if row_text:
            result.append(row_text)
    return result


def _spreadsheet_row_text(values) -> str:
    cells = [_spreadsheet_cell_text(value) for value in values]
    while cells and not cells[-1]:
        cells.pop()
    return " | ".join(cells)


def _spreadsheet_cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()
