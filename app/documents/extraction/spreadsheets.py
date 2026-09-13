import itertools
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import xlrd
from openpyxl import load_workbook
from openpyxl.utils.cell import get_column_letter, range_boundaries

from app.documents.extraction.common import (
    _clean_hyperlink_target,
    _format_hyperlink_text,
    _record_hyperlink,
)

_SPREADSHEETML_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

_OFFICE_RELATIONSHIP_NAMESPACE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)


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
                            target = (
                                f"{target}#{textmark}" if target else f"#{textmark}"
                            )
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
    max_column = max(
        sheet.max_column or 0, getattr(formula_sheet, "max_column", 0) or 0
    )
    value_rows = sheet.iter_rows(
        min_row=1,
        max_row=max_row,
        min_col=1,
        max_col=max_column,
    )
    formula_rows = (
        formula_sheet.iter_rows(
            min_row=1,
            max_row=max_row,
            min_col=1,
            max_col=max_column,
        )
        if formula_sheet is not None
        else itertools.repeat(())
    )
    for row_index, value_row in enumerate(value_rows, start=1):
        formula_row = next(formula_rows, ())
        values = []
        for column_index, value_cell in enumerate(value_row, start=1):
            value = value_cell.value
            formula_value = (
                formula_row[column_index - 1].value
                if formula_sheet is not None and column_index <= len(formula_row)
                else None
            )
            if value is None and formula_sheet is not None:
                if isinstance(formula_value, str) and formula_value.startswith("="):
                    value = formula_value
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
