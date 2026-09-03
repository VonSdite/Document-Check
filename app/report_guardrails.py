import html
import json
import re


TEXT_EXTRACTION_LIMITATION = (
    "输入内容是解析器从原文中抽取的文本，不代表原文的全部视觉内容。"
    "图片、图标、矢量图形、公式和表格版式可能未出现在抽取结果中。"
    "不得仅因抽取文本中未出现这些对象，就判断原文中的图片、图标或表格缺失。"
)

_VISUAL_DETAIL = (
    r"(?:(?:中|内)?(?:的)?(?:标题|编号|图题|表题|字段|列名|行名|参数|单位|"
    r"说明|注释|文字|文本|数据|数值|来源|条件|示例|结果|内容))"
)
_VISUAL_OBJECT = (
    r"(?:图片|图像|插图|截图|图标|图示|图形|流程图|示意图|结构图|接线图|"
    r"表格|数据表|参数表|图表|上图|下图|上表|下表|"
    r"图\s*[A-Za-z0-9一二三四五六七八九十]+|"
    r"表\s*[A-Za-z0-9一二三四五六七八九十]+)"
    rf"(?!{_VISUAL_DETAIL})"
)
_MISSING_ACTION = (
    r"(?:缺失|缺少|遗漏|漏放|漏插|不存在|"
    r"未(?:提供|插入|附上|附加|包含|展示|显示|找到|发现)|"
    r"没有(?:提供|插入|附上|附加|包含|展示|显示|找到|发现|出现))"
)
_MISSING_VISUAL_PATTERNS = (
    re.compile(rf"{_MISSING_ACTION}[^，。；：;:\n]{{0,20}}{_VISUAL_OBJECT}", re.IGNORECASE),
    re.compile(
        rf"{_VISUAL_OBJECT}(?:本身|对象|文件|整体)?(?:在文档中)?"
        rf"{_MISSING_ACTION}(?!{_VISUAL_DETAIL})",
        re.IGNORECASE,
    ),
)
_EXPLICIT_PLACEHOLDER_PATTERN = re.compile(
    r"(?:\bTODO\b|\bTBD\b|\bXXX\b|待补充|待插入|此处插入|后续提供|"
    r"(?:图片|图像|插图|截图|图标|图示|图形|表格|图表)占位|占位符)",
    re.IGNORECASE,
)
_REPORT_ITEM_KEYS = ("items", "issues", "report_items", "findings", "problems")
_EXCERPT_FIELDS = (
    "excerpt",
    "quote",
    "original",
    "evidence",
    "原文摘录",
    "原文",
    "证据",
    "文档线索",
)
_DECISION_FIELDS = (
    "category",
    "issue_type",
    "problem_type",
    "问题类型",
    "description",
    "issue",
    "problem",
    "finding",
    "问题描述",
    "impact",
    "risk",
    "影响",
    "suggestion",
    "recommendation",
    "fix",
    "修改建议",
)
_PDF_TABLE_PATTERN = re.compile(
    r'<table\s+id="(?P<id>page\d+-table\d+)"\s+data-confidence="(?P<confidence>[^"]+)">'
    r"(?P<body>.*?)</table>",
    re.IGNORECASE | re.DOTALL,
)
_PDF_TABLE_ROW_PATTERN = re.compile(r"<tr>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_PDF_TABLE_CELL_PATTERN = re.compile(r"<td(?P<attributes>[^>]*)>(?P<content>.*?)</td>", re.IGNORECASE | re.DOTALL)
_PDF_TABLE_ATTRIBUTE_PATTERN = re.compile(r'([\w-]+)="([^"]*)"')
_PDF_TABLE_ID_PATTERN = re.compile(r"\bpage\d+-table\d+\b", re.IGNORECASE)
_PDF_CELL_REFERENCE_PATTERN = re.compile(r"\b([A-Z]{1,3})([1-9]\d*)\b", re.IGNORECASE)
_TABLE_DATA_MISSING_PATTERNS = (
    re.compile(
        r"(?:数据|参数值?|数值|内容|字段值?|单元格)(?:存在)?"
        r"(?:缺失|缺少|遗漏|为空|空白|未填写|未提供)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:缺失|缺少|遗漏|未填写|未提供)[^，。；：;:\n]{0,16}"
        r"(?:数据|参数值?|数值|内容|字段值?|单元格)",
        re.IGNORECASE,
    ),
)


def is_unsupported_visual_missing_item(item: dict) -> bool:
    """判断文本检查是否在没有直接证据时声称视觉对象缺失。"""
    if not isinstance(item, dict):
        return False
    excerpt = "\n".join(str(item.get(field) or "") for field in _EXCERPT_FIELDS)
    if _EXPLICIT_PLACEHOLDER_PATTERN.search(excerpt):
        return False
    decision_text = "\n".join(str(item.get(field) or "") for field in _DECISION_FIELDS)
    return any(pattern.search(decision_text) for pattern in _MISSING_VISUAL_PATTERNS)


def filter_unsupported_visual_missing_items(
    items: list,
    *,
    pdf_table_evidence: dict | None = None,
) -> tuple[list, int]:
    filtered = []
    removed_count = 0
    for item in items:
        if isinstance(item, dict) and (
            is_unsupported_visual_missing_item(item)
            or is_unsupported_pdf_table_data_missing_item(item, pdf_table_evidence or {})
        ):
            removed_count += 1
            continue
        filtered.append(item)
    return filtered, removed_count


def sanitize_text_check_result(
    content: str,
    *,
    pdf_table_evidence: dict | None = None,
) -> tuple[str, int]:
    """在文本检查的最终 JSON 写入报告前过滤无依据的视觉对象缺失结论。"""
    text = str(content or "")
    payload = _load_report_payload(text)
    if not isinstance(payload, dict):
        return text, 0
    item_key = next((key for key in _REPORT_ITEM_KEYS if isinstance(payload.get(key), list)), "")
    if not item_key:
        return text, 0

    filtered_items, removed_count = filter_unsupported_visual_missing_items(
        payload[item_key],
        pdf_table_evidence=pdf_table_evidence,
    )
    if not removed_count:
        return text, 0
    payload[item_key] = filtered_items
    payload["summary"] = guarded_report_summary(filtered_items)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")), removed_count


def build_pdf_table_evidence_index(document_text: str) -> dict:
    text = str(document_text or "")
    if "<table" not in text or "page" not in text:
        return {}
    tables = {}
    for table_match in _PDF_TABLE_PATTERN.finditer(text):
        table_id = table_match.group("id").lower()
        cell_map = {}
        for row_index, row_match in enumerate(_PDF_TABLE_ROW_PATTERN.finditer(table_match.group("body")), start=1):
            column_index = 1
            for cell_match in _PDF_TABLE_CELL_PATTERN.finditer(row_match.group(1)):
                while _pdf_cell_name(row_index, column_index) in cell_map:
                    column_index += 1
                attributes = dict(_PDF_TABLE_ATTRIBUTE_PATTERN.findall(cell_match.group("attributes")))
                rowspan = _positive_int(attributes.get("rowspan"), 1)
                colspan = _positive_int(attributes.get("colspan"), 1)
                anchor = _pdf_cell_name(row_index, column_index)
                if "data-non-text" in attributes:
                    kind = "nontext"
                elif "data-empty" in attributes:
                    kind = "empty"
                else:
                    kind = "text"
                content = re.sub(r"<[^>]+>", "", cell_match.group("content"))
                for covered_row in range(row_index, row_index + rowspan):
                    for covered_column in range(column_index, column_index + colspan):
                        cell_name = _pdf_cell_name(covered_row, covered_column)
                        cell_map[cell_name] = {
                            "kind": kind if cell_name == anchor else "merged",
                            "anchor": anchor,
                            "text": html.unescape(content).strip() if cell_name == anchor else "",
                        }
                column_index += colspan
        tables[table_id] = {
            "confidence": table_match.group("confidence").strip().lower(),
            "cells": cell_map,
        }
    return tables


def is_unsupported_pdf_table_data_missing_item(item: dict, pdf_table_evidence: dict) -> bool:
    if not isinstance(item, dict) or not pdf_table_evidence:
        return False
    excerpt = "\n".join(str(item.get(field) or "") for field in _EXCERPT_FIELDS)
    if _EXPLICIT_PLACEHOLDER_PATTERN.search(excerpt):
        return False
    decision_text = "\n".join(str(item.get(field) or "") for field in _DECISION_FIELDS)
    evidence_text = "\n".join(
        (str(item.get("location") or ""), excerpt, decision_text)
    )
    location_text = str(item.get("location") or "")
    if not any(pattern.search(decision_text) for pattern in _TABLE_DATA_MISSING_PATTERNS):
        return False
    table_ids = {value.lower() for value in _PDF_TABLE_ID_PATTERN.findall(location_text)}
    if not table_ids and not re.search(r"表格|单元格|行|列", evidence_text):
        return False
    if len(table_ids) != 1:
        return True
    table = pdf_table_evidence.get(next(iter(table_ids)))
    if not table or table.get("confidence") != "high":
        return True
    cell_names = {
        f"{column.upper()}{row}"
        for column, row in _PDF_CELL_REFERENCE_PATTERN.findall(location_text)
    }
    if not cell_names:
        return True
    for cell_name in cell_names:
        cell = table["cells"].get(cell_name)
        if not cell or cell.get("kind") != "empty":
            return True
    return False


def guarded_report_summary(items: list) -> str:
    statuses = [
        str(item.get("status") or item.get("状态") or item.get("type") or "").strip().lower()
        if isinstance(item, dict)
        else ""
        for item in items
    ]
    issue_count = sum(status in {"issue", "问题", "明确问题"} for status in statuses)
    suggestion_count = sum(status in {"suggestion", "建议", "待确认建议"} for status in statuses)
    other_count = max(0, len(items) - issue_count - suggestion_count)
    if not items:
        return "未发现具有充分文本证据的问题。"
    parts = []
    if issue_count:
        parts.append(f"{issue_count} 条明确问题")
    if suggestion_count:
        parts.append(f"{suggestion_count} 条待确认建议")
    if other_count:
        parts.append(f"{other_count} 条检查结果")
    return "检查完成，保留 " + "、".join(parts) + "。"


def _load_report_payload(text: str):
    raw = str(text or "")
    candidates = [raw.strip()]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*(.*?)```", raw, re.IGNORECASE | re.DOTALL)
    )
    object_start = raw.find("{")
    object_end = raw.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(raw[object_start : object_end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
    return None


def _positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _pdf_cell_name(row: int, column: int) -> str:
    letters = []
    value = max(1, int(column))
    while value:
        value, remainder = divmod(value - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters)) + str(max(1, int(row)))
