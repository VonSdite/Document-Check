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
