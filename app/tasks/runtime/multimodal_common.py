import re
from pathlib import Path

from app.documents.images import (
    image_path_from_item,
    image_to_data_url,
    page_numbers_from_image_item,
    page_sections_from_document_text,
)
from app.tasks.runtime.common import _int_setting

DEFAULT_IMAGE_CHECK_BATCH_SIZE = 4
MAX_IMAGE_CHECK_BATCH_SIZE = 4
MULTIMODAL_CHECK_GROUP_SIZE = 3
IMAGE_CONTEXT_NEIGHBOR_PAGES = 1
IMAGE_DOCUMENT_CONTEXT_MAX_CHARS = 20000


def _image_check_batch_size() -> int:
    return max(
        1,
        min(
            MAX_IMAGE_CHECK_BATCH_SIZE,
            _int_setting("image_check_batch_size", DEFAULT_IMAGE_CHECK_BATCH_SIZE),
        ),
    )


def _image_batches(image_items: list[dict], batch_size: int) -> list[list[dict]]:
    return [
        image_items[index : index + batch_size]
        for index in range(0, len(image_items), batch_size)
    ]


def _check_item_groups(check_items: list[dict]) -> list[list[dict]]:
    return [
        check_items[index : index + MULTIMODAL_CHECK_GROUP_SIZE]
        for index in range(0, len(check_items), MULTIMODAL_CHECK_GROUP_SIZE)
    ]


def _document_text_for_image_batch(document_text: str, image_items: list[dict]) -> str:
    text = str(document_text or "").strip()
    if not text:
        return ""

    pages = sorted(
        {page for image in image_items for page in page_numbers_from_image_item(image)}
    )
    if not pages:
        return _trim_document_context(text, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)

    page_sections = page_sections_from_document_text(text)
    if not page_sections:
        return _trim_document_context(text, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)

    wanted_pages = set()
    for page in pages:
        for value in range(
            page - IMAGE_CONTEXT_NEIGHBOR_PAGES, page + IMAGE_CONTEXT_NEIGHBOR_PAGES + 1
        ):
            if value > 0:
                wanted_pages.add(value)
    selected = [
        (page, section) for page, section in page_sections if page in wanted_pages
    ]
    if not selected:
        return _trim_document_context(text, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)

    image_lines = []
    for image in image_items:
        filename = str(image.get("filename") or image.get("id") or "图片")
        position = str(image.get("position") or "未标注")
        image_lines.append(f"- {filename}（位置：{position}）")

    page_text = "\n\n".join(
        section.strip() for _, section in selected if section.strip()
    )
    header = _document_header(text)
    scoped = (
        f"{header}\n\n"
        f"document_text_scope: 仅提供当前图片所在页及前后 {IMAGE_CONTEXT_NEIGHBOR_PAGES} 页的文本，避免跨页误配。\n"
        f"current_batch_images:\n{chr(10).join(image_lines)}\n\n"
        f"相关文档文本：\n{page_text}"
    ).strip()
    return _trim_document_context(scoped, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)


def _document_header(document_text: str) -> str:
    lines = []
    for line in str(document_text or "").splitlines():
        if line.startswith("file:"):
            lines.append(line.strip())
            continue
        if lines:
            break
    return "\n".join(lines) if lines else "file: document"


def _trim_document_context(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[文档上下文已按当前批次截断]"


def _split_checkable_image_items(
    image_items: list[dict],
) -> tuple[list[dict], list[dict]]:
    checkable = []
    skipped = []
    for index, image in enumerate(image_items, start=1):
        item = dict(image)
        item["_image_index"] = index
        mime_type = str(item.get("mime_type") or "")
        if mime_type.startswith("image/"):
            checkable.append(item)
            continue
        item["skip_reason"] = "不是可识别的图片格式"
        skipped.append(item)
    return checkable, skipped


def _multimodal_image_inputs(image_folder: Path, image_items: list[dict]) -> list[dict]:
    inputs = []
    for fallback_index, image in enumerate(image_items, start=1):
        image_index = int(image.get("_image_index") or fallback_index)
        image_path = image_path_from_item(image_folder, image)
        if image_path is None or not image_path.is_file():
            raise RuntimeError(
                f"提取图片“{image.get('filename') or image_index}”已删除，无法检查"
            )
        mime_type = str(image.get("mime_type") or "")
        if not mime_type.startswith("image/"):
            raise RuntimeError(
                f"提取图片“{image.get('filename') or image_index}”不是可识别的图片格式"
            )
        inputs.append(
            {
                "index": image_index,
                "name": str(image.get("filename") or f"image-{image_index:04d}"),
                "position": _image_location_label(image),
                "mime_type": mime_type,
                "data_url": image_to_data_url(image_path, mime_type),
            }
        )
    return inputs


def _image_pdf_page_label(image: dict) -> str:
    try:
        page_number = int(image.get("page_number") or 0)
    except (TypeError, ValueError):
        page_number = 0
    if page_number <= 0:
        pages = page_numbers_from_image_item(image)
        page_number = pages[0] if pages else 0
    if page_number <= 0:
        return ""
    return f"PDF第{page_number}页"


def _image_location_label(image: dict) -> str:
    page_label = _image_pdf_page_label(image)
    position = str(image.get("position") or "").strip()
    if page_label and position:
        return f"{page_label}（{position}）"
    if page_label:
        return page_label
    return position or "未标注"


def _format_multimodal_image_check_result(
    batch_results: list[dict],
    *,
    current_batch: dict | None = None,
    current_content: str = "",
    skipped_images: list[dict] | None = None,
    manual_notes: list[str] | None = None,
) -> str:
    parts = []
    for item in batch_results:
        parts.append(_format_image_batch_result(item, item["content"]))
    if current_batch is not None and current_content:
        parts.append(_format_image_batch_result(current_batch, current_content))
    if skipped_images:
        parts.append(_format_skipped_image_result(skipped_images))
    if manual_notes:
        parts.append(_format_manual_notes(manual_notes))
    if current_batch is None:
        summary = _format_image_check_issue_summary(
            batch_results, skipped_images or [], manual_notes or []
        )
        if summary:
            parts.append(summary)
    return "\n\n".join(parts).strip()


def _format_image_batch_result(batch: dict, content: str) -> str:
    batch_index = int(batch.get("batch_index") or 1)
    batch_count = int(batch.get("batch_count") or 1)
    images = batch.get("images") or []
    target_label = str(batch.get("target_label") or "图文联合检查")
    media_label = "视频帧" if batch.get("target_kind") == "video_frame" else "图片"
    title = (
        f"### {target_label}结果"
        if batch_count <= 1
        else f"### {target_label}结果（批次 {batch_index}/{batch_count}）"
    )
    image_lines = []
    for image in images:
        filename = str(image.get("filename") or image.get("id") or media_label)
        location = _image_location_label(image)
        image_lines.append(f"- {location}：{filename}")
    image_list = "\n".join(image_lines) if image_lines else f"- 未记录{media_label}"
    return (
        f"{title}\n\n覆盖{media_label}：\n{image_list}\n\n{str(content or '').strip()}"
    )


def _format_skipped_image_result(skipped_images: list[dict]) -> str:
    image_lines = []
    for image in skipped_images:
        filename = str(image.get("filename") or image.get("id") or "图片")
        position = str(image.get("position") or "未标注")
        mime_type = str(image.get("mime_type") or "未知格式")
        reason = str(image.get("skip_reason") or "已跳过")
        image_lines.append(
            f"- {filename}（位置：{position}，格式：{mime_type}，原因：{reason}）"
        )
    return (
        "### 已跳过的图片\n\n以下提取图片不是可识别图片格式，已跳过，不影响其他图片继续检查：\n"
        + "\n".join(image_lines)
    )


def _format_manual_notes(manual_notes: list[str]) -> str:
    lines = [
        f"- {note}"
        for note in _dedupe_limited([str(note).strip() for note in manual_notes], 30)
        if note
    ]
    if not lines:
        return ""
    return "### 系统需人工确认\n\n" + "\n".join(lines)


def _format_image_check_issue_summary(
    batch_results: list[dict],
    skipped_images: list[dict],
    manual_notes: list[str] | None = None,
) -> str:
    issues = []
    manual = []
    for batch in batch_results:
        batch_label = _image_batch_label(batch)
        structured_issues, structured_manual = _structured_summary_items(
            str(batch.get("content") or "")
        )
        for line in structured_issues:
            issues.append(f"{batch_label} {line}")
        for line in structured_manual:
            manual.append(f"{batch_label} {line}")
        if structured_issues or structured_manual:
            continue
        for line in _summary_candidate_lines(str(batch.get("content") or "")):
            normalized = _normalize_summary_line(line)
            if (
                not normalized
                or _summary_line_is_negative(normalized)
                or _summary_line_is_normal(normalized)
            ):
                continue
            entry = f"{batch_label} {normalized}"
            if _summary_line_needs_manual(normalized):
                manual.append(entry)
            elif _summary_line_is_issue(normalized):
                issues.append(entry)
    for image in skipped_images:
        filename = str(image.get("filename") or image.get("id") or "图片")
        location = _image_location_label(image)
        manual.append(
            f"{location}：{filename} 提取后不是可识别图片格式，已跳过，需人工确认是否影响检查。"
        )
    for note in manual_notes or []:
        manual.append(str(note).strip())

    issues = _dedupe_limited(issues, 30)
    manual = _dedupe_limited(manual, 30)
    if not issues and not manual:
        return "### 检查汇总\n\n#### 明确问题\n- 未发现明确问题。\n\n#### 需人工确认\n- 未发现需人工确认项。"

    issue_text = (
        "\n".join(f"- {item}" for item in issues) if issues else "- 未发现明确问题。"
    )
    manual_text = (
        "\n".join(f"- {item}" for item in manual)
        if manual
        else "- 未发现需人工确认项。"
    )
    return (
        f"### 检查汇总\n\n#### 明确问题\n{issue_text}\n\n#### 需人工确认\n{manual_text}"
    )


def _image_batch_label(batch: dict) -> str:
    batch_index = int(batch.get("batch_index") or 1)
    batch_count = int(batch.get("batch_count") or 1)
    page_label = (
        _video_batch_time_label(batch)
        if batch.get("target_kind") == "video_frame"
        else _image_batch_pages_label(batch)
    )
    if batch_count > 1:
        prefix = f"批次 {batch_index}/{batch_count}"
    else:
        prefix = "批次 1"
    if page_label:
        return f"{prefix}（{page_label}）："
    return f"{prefix}："


def _video_batch_time_label(batch: dict) -> str:
    positions = []
    seen = set()
    for image in batch.get("images") or []:
        position = str(image.get("position") or "").strip()
        if not position or position in seen:
            continue
        seen.add(position)
        positions.append(position)
    if not positions:
        return ""
    if len(positions) == 1:
        return f"视频时间 {positions[0]}"
    return f"视频时间 {positions[0]}-{positions[-1]}"


def _image_batch_pages_label(batch: dict) -> str:
    pages = []
    seen = set()
    for image in batch.get("images") or []:
        for page in page_numbers_from_image_item(image):
            if page in seen:
                continue
            seen.add(page)
            pages.append(page)
    if not pages:
        return ""
    pages = sorted(pages)
    if len(pages) == 1:
        return f"PDF第{pages[0]}页"
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f"PDF第{pages[0]}-{pages[-1]}页"
    return "PDF第" + "、".join(str(page) for page in pages) + "页"


def _summary_candidate_lines(content: str) -> list[str]:
    lines = []
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(
            r"^[-*]?\s*(覆盖图片|覆盖视频帧|图片名称|图片位置|输出要求|详细问题列表)[:：]?$",
            line,
        ):
            continue
        lines.append(line)
    return lines


def _structured_summary_items(content: str) -> tuple[list[str], list[str]]:
    issues = []
    manual = []
    for item in _structured_section_items(content, "明确问题"):
        if _summary_line_is_negative(item) or _summary_line_is_normal(item):
            continue
        if _summary_line_needs_manual(item):
            manual.append(item)
        elif _summary_line_is_issue(item):
            issues.append(item)
    for item in _structured_section_items(content, "需人工确认"):
        if _summary_line_is_negative(item) or _summary_line_is_normal(item):
            continue
        manual.append(item)
    return issues, manual


def _structured_section_items(content: str, section_title: str) -> list[str]:
    items = []
    current_section = ""
    for raw_line in str(content or "").splitlines():
        heading, inline_text = _summary_section_heading(raw_line)
        if heading:
            current_section = heading
            if heading == section_title and inline_text:
                line = _normalize_summary_line(inline_text)
                if line:
                    items.append(line)
            continue
        if current_section != section_title:
            continue
        line = _normalize_summary_line(raw_line)
        if line:
            items.append(line)
    return items


def _summary_section_heading(line: str) -> tuple[str, str]:
    value = str(line or "").strip()
    if not value:
        return "", ""
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value).strip()
    value = re.sub(r"^\s*[-*]\s*", "", value).strip()
    value = value.strip("*_` \t")
    value = re.sub(r"^\d+[.)、]\s*", "", value).strip()

    title_aliases = (
        ("总体判断", "总体判断"),
        ("明确问题", "明确问题"),
        ("发现问题", "明确问题"),
        ("发现明确问题", "明确问题"),
        ("明确冲突", "明确问题"),
        ("需人工确认", "需人工确认"),
        ("需要人工确认", "需人工确认"),
        ("未发现问题", "未发现问题"),
    )
    for title, normalized_title in title_aliases:
        if value == title:
            return normalized_title, ""
        match = re.match(rf"^{re.escape(title)}\s*[:：]\s*(.+)$", value)
        if match:
            return normalized_title, match.group(1).strip()
    return "", ""


def _normalize_summary_line(line: str) -> str:
    value = re.sub(r"^\s*[-*]\s*", "", str(line or "").strip())
    value = re.sub(r"^\s*\d+[.)、]\s*", "", value)
    value = re.sub(r"\s+", " ", value)
    return value[:240].strip()


def _summary_line_is_negative(line: str) -> bool:
    text = str(line or "")
    if any(
        marker in text
        for marker in (
            "未汇总到",
            "未发现明确问题",
            "没有明确问题",
            "无明确问题",
            "无问题",
            "没有问题",
            "未见问题",
            "无异常",
            "未见异常",
        )
    ):
        return True
    if any(
        marker in text
        for marker in (
            "未发现需人工确认",
            "没有需人工确认",
            "无需人工确认",
            "无须人工确认",
            "未见需人工确认",
        )
    ):
        return True
    return bool(
        re.search(
            r"(未发现|没有发现|未见|无明显|不存在明显).{0,12}(问题|异常|风险|冲突|错误|缺失|不一致|不匹配|不符合)",
            text,
        )
    )


def _summary_line_is_normal(line: str) -> bool:
    text = str(line or "")
    if _summary_line_needs_manual(text) or _summary_line_has_issue_marker(text):
        return False
    return any(
        marker in text
        for marker in (
            "正常",
            "符合要求",
            "符合规范",
            "一致",
            "匹配",
            "清晰",
            "完整",
            "可读",
            "无误",
        )
    )


def _summary_line_needs_manual(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            "需人工确认",
            "需要人工确认",
            "建议人工",
            "建议复核",
            "无法确认",
            "无法判断",
            "无法辨认",
            "难以辨认",
            "不确定",
            "可能",
            "疑似",
            "证据不足",
            "上下文不足",
            "看不清",
            "文字过小",
            "较小",
            "分辨率不足",
        )
    )


def _summary_line_is_issue(line: str) -> bool:
    if (
        _summary_line_is_negative(line)
        or _summary_line_is_normal(line)
        or _summary_line_needs_manual(line)
    ):
        return False
    return _summary_line_has_issue_marker(line)


def _summary_line_has_issue_marker(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            "问题",
            "风险",
            "不一致",
            "不匹配",
            "冲突",
            "错误",
            "异常",
            "不符合",
            "缺失",
            "缺少",
            "错位",
            "不对应",
            "不完整",
            "不清晰",
            "不规范",
            "模糊",
            "遮挡",
            "裁切",
            "乱码",
            "断裂",
            "变形",
            "过度拉伸",
            "漏标",
            "错标",
            "矛盾",
            "有误",
        )
    )


def _dedupe_limited(items: list[str], limit: int) -> list[str]:
    result = []
    seen = set()
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
        if len(result) >= limit:
            break
    return result
