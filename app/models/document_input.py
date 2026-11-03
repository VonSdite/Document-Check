"""为模型生成保留表格语义的精简文档视图。"""

import html
import json
import re
from dataclasses import dataclass

PDF_TABLE_INPUT_INSTRUCTION = (
    "输入中的“PDF结构化表格”由文字坐标和矢量边框还原，保留表格 ID、行列数和置信度。"
    "精简表格以“[表 表格ID 行数×列数 H/M/L”开头，以独立一行的“]”结束；"
    "H、M、L 分别表示 high、medium、low 置信度。"
    "表格内每行以“坐标或范围=值”记录一个原始单元格；"
    '例如 A1:C1="组合柜" 表示横向合并，A2:A4 表示纵向合并，范围左上角是锚点，'
    "覆盖位置共同继承该值，不是独立的数据或空位，合并内容只记录一次。"
    '双引号中的值是 JSON 字符串，\\n、\\t、\\"、\\\\ 分别还原为换行、制表符、引号和反斜杠；'
    "只有未加引号的 [空单元格] 表示真正的空值；"
    "未加引号的 [非文本图形或图标]、[未能可靠还原的单元格] 都不是数据缺失。"
    "归一化 HTML 视图中，data-cell 是坐标，data-original-range 记录原始合并范围，"
    "data-inherited-from 表示该位置的值继承自原始合并单元格，"
    'data-empty="true" 表示真正的空值；data-non-text、data-unresolved 和合并覆盖位置都不是数据缺失。'
    "只有基于文本标记明确确认为真正的空值、表格置信度为 high，"
    "并且能够根据行标题、列标题或原文要求证明该位置必须填写时，才允许据此报告表格数据缺失；"
    "location 必须包含表格 ID 和原始单元格的锚点坐标。"
    "置信度为 medium 或 low 时不得报告数据缺失。"
)

_PDF_TABLE_PATTERN = re.compile(
    r"\[PDF结构化表格 (?P<id>page\d+-table\d+) 开始；"
    r"(?P<rows>[1-9]\d*)行×(?P<columns>[1-9]\d*)列；"
    r"置信度=(?P<confidence>high|medium|low)；视图=归一化；"
    r"合并继承位置通过 data-inherited-from 标记\]\n"
    r'<table id="(?P=id)" data-confidence="(?P=confidence)" '
    r'data-view="normalized">\n(?P<body>.*?)\n</table>\n'
    r"\[PDF结构化表格 (?P=id) 结束\]",
    re.DOTALL,
)
_ROW_PATTERN = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
_CELL_PATTERN = re.compile(r"<td (?P<attributes>[^<>]*)>(?P<text>[^<]*)</td>")
_ATTRIBUTE_PATTERN = re.compile(r'([\w-]+)="([^"<>]*)"')
_REFERENCE_PATTERN = re.compile(r"([A-Z]+)([1-9]\d*)")
_UNRESOLVED_COMMENT_PATTERN = re.compile(
    r"\s*<!-- 未能可靠还原的网格位置：(?P<positions>[A-Z0-9,]+) -->\s*\Z"
)
_CELL_ATTRIBUTES = {
    "data-cell",
    "data-original-range",
    "data-original-rowspan",
    "data-original-colspan",
    "data-inherited-from",
    "data-empty",
    "data-non-text",
    "data-unresolved",
}
_CELL_STATES = {
    "data-empty": "[空单元格]",
    "data-non-text": "[非文本图形或图标]",
    "data-unresolved": "[未能可靠还原的单元格]",
}
_CONFIDENCE_CODES = {"high": "H", "medium": "M", "low": "L"}


@dataclass(frozen=True)
class _NormalizedCell:
    attributes: dict[str, str]
    text: str


def compact_model_document_text(document_text: str) -> str:
    """将归一化表格转换为坐标表示，保留正文和未识别的结构。"""
    if 'data-view="normalized"' not in document_text:
        return document_text
    return _PDF_TABLE_PATTERN.sub(_compact_pdf_table, document_text)


def _compact_pdf_table(match: re.Match) -> str:
    original = match.group(0)
    try:
        cells = _normalized_cells(match)
        lines = [
            f"[表 {match['id']} {match['rows']}×{match['columns']} "
            f"{_CONFIDENCE_CODES[match['confidence']]}"
        ]
        covered = set()
        for position, cell in cells.items():
            attributes = cell.attributes
            if "data-inherited-from" in attributes:
                continue
            reference = attributes["data-cell"]
            cell_range = attributes.get("data-original-range", reference)
            boundaries = cell_range.split(":")
            if len(boundaries) not in {1, 2}:
                raise ValueError("单元格范围不完整")
            start = _cell_position(boundaries[0])
            end = _cell_position(boundaries[-1])
            if start != position or end[0] < start[0] or end[1] < start[1]:
                raise ValueError("合并范围与锚点不匹配")
            if end[0] >= int(match["rows"]) or end[1] >= int(match["columns"]):
                raise ValueError("合并范围超出表格")
            for name, span in (
                ("data-original-rowspan", end[0] - start[0] + 1),
                ("data-original-colspan", end[1] - start[1] + 1),
            ):
                if int(attributes.get(name, "1")) != span:
                    raise ValueError("合并跨度与范围不匹配")
            for row in range(start[0], end[0] + 1):
                for column in range(start[1], end[1] + 1):
                    inherited_position = (row, column)
                    if inherited_position in covered:
                        raise ValueError("合并范围重叠")
                    covered.add(inherited_position)
                    if inherited_position != start:
                        _validate_inherited_cell(
                            cells[inherited_position], cell, cell_range
                        )
            state = next(
                (value for name, value in _CELL_STATES.items() if name in attributes),
                None,
            )
            value = state or json.dumps(cell.text, ensure_ascii=False)
            lines.append(f"{cell_range}={value}")
        if len(covered) != len(cells):
            raise ValueError("存在没有锚点的合并继承位置")
        lines.append("]")
        compact = "\n".join(lines)
    except ValueError:
        return original
    return compact if len(compact) < len(original) else original


def _normalized_cells(match: re.Match) -> dict[tuple[int, int], _NormalizedCell]:
    body = match["body"]
    comment = _UNRESOLVED_COMMENT_PATTERN.search(body)
    if comment:
        body = body[: comment.start()]
    rows = _complete_matches(_ROW_PATTERN, body)
    if len(rows) != int(match["rows"]):
        raise ValueError("表格行数不匹配")
    cells = {}
    unresolved = []
    for row_index, row in enumerate(rows):
        row_cells = _complete_matches(_CELL_PATTERN, row[1])
        if len(row_cells) != int(match["columns"]):
            raise ValueError("表格列数不匹配")
        for column_index, cell in enumerate(row_cells):
            attribute_matches = _complete_matches(
                _ATTRIBUTE_PATTERN, cell["attributes"]
            )
            attributes = {item[1]: item[2] for item in attribute_matches}
            if len(attributes) != len(attribute_matches) or (
                attributes.keys() - _CELL_ATTRIBUTES
            ):
                raise ValueError("存在未知或重复的单元格属性")
            position = (row_index, column_index)
            if _cell_position(attributes.get("data-cell", "")) != position:
                raise ValueError("单元格坐标与网格不匹配")
            text = html.unescape(cell["text"])
            states = [name for name in _CELL_STATES if name in attributes]
            if len(states) > 1 or any(
                attributes[name] != "true" or text != _CELL_STATES[name]
                for name in states
            ):
                raise ValueError("单元格状态与内容不匹配")
            if "data-unresolved" in attributes:
                unresolved.append(attributes["data-cell"])
            cells[position] = _NormalizedCell(attributes, text)
    if (comment["positions"].split(",") if comment else []) != unresolved:
        raise ValueError("未还原位置与网格不匹配")
    return cells


def _validate_inherited_cell(
    inherited: _NormalizedCell, anchor: _NormalizedCell, cell_range: str
):
    reference = anchor.attributes["data-cell"]
    if (
        inherited.attributes.get("data-inherited-from") != reference
        or inherited.attributes.get("data-original-range") != cell_range
        or inherited.attributes.keys()
        - {"data-cell", "data-inherited-from", "data-original-range"}
    ):
        raise ValueError("合并继承关系不匹配")
    if "data-non-text" in anchor.attributes:
        expected_text = f"[合并覆盖，继承自{reference}的非文本内容]"
    elif "data-empty" in anchor.attributes:
        expected_text = f"[合并覆盖，继承自{reference}]"
    else:
        expected_text = anchor.text
    if inherited.text != expected_text:
        raise ValueError("合并继承内容不匹配")


def _complete_matches(pattern: re.Pattern, value: str) -> list[re.Match]:
    matches = []
    position = 0
    for match in pattern.finditer(value):
        if value[position : match.start()].strip():
            raise ValueError("表格包含未识别的内容")
        matches.append(match)
        position = match.end()
    if value[position:].strip():
        raise ValueError("表格包含未识别的内容")
    return matches


def _cell_position(reference: str) -> tuple[int, int]:
    match = _REFERENCE_PATTERN.fullmatch(reference)
    if match is None:
        raise ValueError("单元格坐标无效")
    column = 0
    for letter in match[1]:
        column = column * 26 + ord(letter) - ord("A") + 1
    return int(match[2]) - 1, column - 1
