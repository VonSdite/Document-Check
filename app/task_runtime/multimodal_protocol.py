import json
import re

from ..llm import run_multimodal_document_check
from .multimodal_common import (
    _structured_section_items,
    _summary_line_is_negative,
    _summary_line_is_normal,
)


def _run_combined_multimodal_check_with_repair(
    app,
    *,
    check_items: list[dict],
    prompt_builder,
    check_name: str,
    error_label: str,
    run_kwargs: dict,
    preserve_structured: bool = False,
) -> dict[str, str] | dict[str, dict]:
    initial_kwargs = dict(run_kwargs)
    initial_kwargs.update(
        check_name=f"{check_name}（{len(check_items)}项）",
        prompt=prompt_builder(check_items),
    )
    content = run_multimodal_document_check(**initial_kwargs)
    sections = (
        _split_combined_structured_output(content, check_items)
        if preserve_structured
        else _split_combined_check_output(content, check_items, fill_missing=False)
    )
    missing_items = _missing_check_items(check_items, sections)
    if missing_items:
        app.logger.warning(
            "模型多检查项结果缺失，准备补偿请求 task_id=%s target=%s returned=%s missing=%s output_chars=%s",
            run_kwargs.get("task_id") or "-",
            error_label,
            ",".join(sections) or "-",
            ",".join(str(item.get("code") or "") for item in missing_items),
            len(str(content or "")),
        )
        repair_kwargs = dict(run_kwargs)
        repair_kwargs.pop("on_content", None)
        repair_kwargs.update(
            check_name=f"{check_name}补偿检查（{len(missing_items)}项）",
            prompt=(
                "上一次响应缺少以下检查项。只返回本次列出的缺失 code，不要重复其他检查项。\n\n"
                + prompt_builder(missing_items)
            ),
        )
        repair_content = run_multimodal_document_check(**repair_kwargs)
        repair_sections = (
            _split_combined_structured_output(
                repair_content,
                missing_items,
                allow_single_plain_text=False,
            )
            if preserve_structured
            else _split_combined_check_output(
                repair_content,
                missing_items,
                fill_missing=False,
                allow_single_plain_text=False,
            )
        )
        sections.update(repair_sections)
        missing_items = _missing_check_items(check_items, sections)
        app.logger.info(
            "模型多检查项补偿请求完成 task_id=%s target=%s repaired=%s remaining=%s output_chars=%s",
            run_kwargs.get("task_id") or "-",
            error_label,
            ",".join(repair_sections) or "-",
            ",".join(str(item.get("code") or "") for item in missing_items) or "-",
            len(str(repair_content or "")),
        )

    if not sections:
        raise RuntimeError(f"模型连续两次未返回可识别的{error_label}多检查项结果")
    if preserve_structured:
        _fill_missing_structured_check_sections(sections, check_items)
    else:
        _fill_missing_check_sections(sections, check_items)
    return sections


def _missing_check_items(
    check_items: list[dict], sections: dict[str, str]
) -> list[dict]:
    return [item for item in check_items if str(item.get("code") or "") not in sections]


def _unresolved_check_codes(sections: dict, check_items: list[dict]) -> set[str]:
    marker = "模型未按要求返回该检查项的独立结果。"
    unresolved = set()
    for item in check_items:
        code = str(item.get("code") or "")
        section = sections.get(code)
        if isinstance(section, dict):
            if section.get("incomplete"):
                unresolved.add(code)
            continue
        if marker in str(section or ""):
            unresolved.add(code)
    return unresolved


def _split_combined_check_output(
    content: str,
    check_items: list[dict],
    *,
    fill_missing: bool = True,
    allow_single_plain_text: bool = True,
) -> dict[str, str]:
    text = str(content or "").strip()
    if not text:
        if not fill_missing:
            return {}
        return {
            item["code"]: _missing_check_section(item, "模型未返回该检查项内容。")
            for item in check_items
        }

    sections = _split_combined_json_output(text, check_items)
    if sections:
        if fill_missing:
            _fill_missing_check_sections(sections, check_items)
        return sections

    if allow_single_plain_text and len(check_items) == 1 and "### 检查项：" not in text:
        return {check_items[0]["code"]: text}

    headers = list(re.finditer(r"(?m)^###\s*检查项[:：]\s*(.+?)\s*$", text))
    sections = {}
    for index, header in enumerate(headers):
        header_line = header.group(1).strip()
        start = header.start()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        matched_code = _match_check_code_from_header(header_line, check_items)
        if matched_code:
            sections[matched_code] = text[start:end].strip()

    if fill_missing:
        _fill_missing_check_sections(sections, check_items)
    return sections


def _split_combined_structured_output(
    content: str,
    check_items: list[dict],
    *,
    allow_single_plain_text: bool = True,
) -> dict[str, dict]:
    text = str(content or "").strip()
    if not text:
        return {}

    payload = _load_combined_json_output(text)
    if isinstance(payload, dict):
        results = payload.get("results")
        if (
            results is None
            and len(check_items) == 1
            and isinstance(payload.get("items"), list)
        ):
            results = [{"code": check_items[0]["code"], **payload}]
    elif isinstance(payload, list):
        results = payload
    else:
        results = None

    items_by_code = {str(item.get("code") or ""): item for item in check_items}
    sections: dict[str, dict] = {}
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict):
                continue
            code = str(result.get("code") or "").strip()
            if code not in items_by_code or code in sections:
                continue
            sections[code] = _normalize_combined_structured_result(result)
    if sections:
        return sections

    legacy_sections = _split_combined_check_output(
        text,
        check_items,
        fill_missing=False,
        allow_single_plain_text=allow_single_plain_text,
    )
    return {
        code: _legacy_combined_section_report(section)
        for code, section in legacy_sections.items()
        if str(section or "").strip()
    }


def _normalize_combined_structured_result(result: dict) -> dict:
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    items = []
    for raw_item in raw_items:
        if isinstance(raw_item, dict):
            items.append(dict(raw_item))
        elif isinstance(raw_item, str) and raw_item.strip():
            items.append(
                {
                    "status": "suggestion",
                    "severity": "low",
                    "confidence": "low",
                    "category": "视频检查结果",
                    "location": "",
                    "excerpt": "",
                    "description": raw_item.strip(),
                    "impact": "",
                    "suggestion": "请人工复核该结论。",
                }
            )
    return {
        "summary": str(result.get("summary") or "").strip(),
        "items": items,
    }


def _legacy_combined_section_report(section: str) -> dict:
    text = str(section or "").strip()
    items = []
    for line in _structured_section_items(text, "明确问题"):
        if _summary_line_is_negative(line) or _summary_line_is_normal(line):
            continue
        items.append(_legacy_video_report_item(line, "issue"))
    for line in _structured_section_items(text, "需人工确认"):
        if _summary_line_is_negative(line) or _summary_line_is_normal(line):
            continue
        items.append(_legacy_video_report_item(line, "suggestion"))

    summary_match = re.search(
        r"(?ms)^#{0,6}\s*总体判断\s*$\s*(.*?)(?=^#{1,6}\s|\Z)",
        text,
    )
    summary = summary_match.group(1).strip() if summary_match else ""
    if (
        not items
        and text
        and not _summary_line_is_negative(text)
        and not _summary_line_is_normal(text)
    ):
        items.append(_legacy_video_report_item(text, "suggestion"))
    return {"summary": summary, "items": items}


def _legacy_video_report_item(line: str, status: str) -> dict:
    text = re.sub(r"^[-*]\s*", "", str(line or "").strip()).strip()
    location = ""
    description = text
    match = re.match(
        r"^((?:视频时间\s*)?(?:\d{1,2}:)?\d{2}:\d{2}(?:\.\d{1,3})?(?:\s*[-–—~至]\s*(?:\d{1,2}:)?\d{2}:\d{2}(?:\.\d{1,3})?)?|[^：:]{1,40}(?:帧|画面))[:：]\s*(.+)$",
        text,
    )
    if match:
        location = match.group(1).strip()
        description = match.group(2).strip()
    return {
        "status": status,
        "severity": "medium" if status == "issue" else "low",
        "confidence": "medium" if status == "issue" else "low",
        "category": "视频检查结果",
        "location": location,
        "excerpt": "",
        "description": description,
        "impact": "",
        "suggestion": "请人工复核并处理。" if status == "suggestion" else "",
    }


def _split_combined_json_output(
    content: str, check_items: list[dict]
) -> dict[str, str]:
    payload = _load_combined_json_output(content)
    if isinstance(payload, dict):
        results = payload.get("results")
        if (
            results is None
            and len(check_items) == 1
            and isinstance(payload.get("items"), list)
        ):
            results = [{"code": check_items[0]["code"], **payload}]
    elif isinstance(payload, list):
        results = payload
    else:
        results = None
    if not isinstance(results, list):
        return {}

    items_by_code = {str(item.get("code") or ""): item for item in check_items}
    sections = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        code = str(result.get("code") or "").strip()
        item = items_by_code.get(code)
        if item is None or code in sections:
            continue
        sections[code] = _format_combined_json_result(item, result)
    return sections


def _load_combined_json_output(content: str, depth: int = 0):
    if depth > 2:
        return None
    text = str(content or "").strip()
    if not text:
        return None
    candidates = [text]
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL
    )
    if fenced:
        candidates.append(fenced.group(1).strip())
    object_start = text.find("{")
    object_end = text.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(text[object_start : object_end + 1])
    array_start = text.find("[")
    array_end = text.rfind("]")
    if array_start >= 0 and array_end > array_start:
        candidates.append(text[array_start : array_end + 1])
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, str):
            nested = _load_combined_json_output(parsed, depth + 1)
            if nested is not None:
                return nested
            continue
        return parsed
    return None


def _format_combined_json_result(check_item: dict, result: dict) -> str:
    issue_lines = []
    manual_lines = []
    normal_lines = []
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        line = _format_combined_json_item(raw_item)
        if not line:
            continue
        status = str(raw_item.get("status") or "").strip().lower()
        if status == "issue":
            issue_lines.append(line)
        elif status == "non_issue":
            normal_lines.append(line)
        else:
            manual_lines.append(line)

    summary = str(result.get("summary") or "").strip()
    if not summary:
        summary = "发现明确问题。" if issue_lines else "未发现明确问题。"
    issue_text = (
        "\n".join(f"- {line}" for line in issue_lines)
        if issue_lines
        else "- 未发现明确问题。"
    )
    manual_text = (
        "\n".join(f"- {line}" for line in manual_lines)
        if manual_lines
        else "- 未发现需人工确认项。"
    )
    normal_text = (
        "\n".join(f"- {line}" for line in normal_lines)
        if normal_lines
        else "- 未单独列出正常项。"
    )
    return (
        f"### 检查项：{check_item.get('code')}｜{check_item.get('name')}\n\n"
        f"#### 总体判断\n{summary}\n\n"
        f"#### 明确问题\n{issue_text}\n\n"
        f"#### 需人工确认\n{manual_text}\n\n"
        f"#### 未发现问题\n{normal_text}"
    )


def _format_combined_json_item(item: dict) -> str:
    location = str(item.get("location") or "").strip()
    description = str(
        item.get("description") or item.get("suggestion") or item.get("excerpt") or ""
    ).strip()
    if not description:
        return ""
    line = f"{location}：{description}" if location else description
    details = []
    excerpt = str(item.get("excerpt") or "").strip()
    impact = str(item.get("impact") or "").strip()
    suggestion = str(item.get("suggestion") or "").strip()
    if excerpt and excerpt not in line:
        details.append(f"证据：{excerpt}")
    if impact and impact not in line:
        details.append(f"影响：{impact}")
    if suggestion and suggestion not in line:
        details.append(f"建议：{suggestion}")
    if details:
        line = line.rstrip("；。") + "；" + "；".join(details)
    return line


def _fill_missing_check_sections(sections: dict[str, str], check_items: list[dict]):
    for item in check_items:
        sections.setdefault(
            item["code"],
            _missing_check_section(item, "模型未按要求返回该检查项的独立结果。"),
        )


def _fill_missing_structured_check_sections(
    sections: dict[str, dict], check_items: list[dict]
):
    for item in check_items:
        sections.setdefault(item["code"], _missing_structured_check_section())


def _missing_structured_check_section() -> dict:
    return {
        "summary": "模型未按要求返回该检查项的独立结果。",
        "incomplete": True,
        "items": [
            {
                "status": "suggestion",
                "severity": "low",
                "confidence": "low",
                "category": "检查结果完整性",
                "location": "",
                "excerpt": "",
                "description": "模型未按要求返回该检查项的独立结果。",
                "impact": "该检查项可能未被完整执行。",
                "suggestion": "请重新执行任务或人工复核。",
            }
        ],
    }


def _match_check_code_from_header(header_line: str, check_items: list[dict]) -> str:
    normalized = str(header_line or "").strip()
    for item in check_items:
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        if code and code in normalized:
            return code
        if name and name in normalized:
            return code
    return ""


def _missing_check_section(item: dict, reason: str) -> str:
    return (
        f"### 检查项：{item.get('code')}｜{item.get('name')}\n\n"
        "#### 总体判断\n"
        "需人工确认。\n\n"
        "#### 明确问题\n"
        "- 未汇总到明确问题。\n\n"
        "#### 需人工确认\n"
        f"- {reason}\n\n"
        "#### 未发现问题\n"
        "- 未形成独立结论。"
    )
