import re

from .text_language import estimate_text_language, text_language_label


LANGUAGE_STATIC_TOKEN_RE = re.compile(
    r"https?://[^\s<>\]\)\"']+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b"
    r"|\bv?\d+(?:\.\d+){1,4}\b"
    r"|\b\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?\b"
    r"|\b\d+(?:[.,]\d+)*(?:\s?(?:%|ms|s|m|mm|cm|km|kg|g|KB|MB|GB|TB|V|A|W|Hz|kHz|MHz|GHz|°C|℃))?\b",
    re.IGNORECASE,
)
LANGUAGE_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|第[一二三四五六七八九十百千万\d]+[章节篇部]\s*|"
    r"(?:\d+|[A-Z])(?:[.\-、)]\d*){0,5}[.\-、)]?\s+|[一二三四五六七八九十]+[、.]\s*)\S"
)


def compose_language_consistency_text(file_a: dict, file_b: dict) -> tuple[str, str]:
    static_summary = language_consistency_static_summary(file_a, file_b)
    document_text = "\n\n".join(
        [
            "# 静态预检摘要\n"
            + static_summary
            + "\n\n说明：静态预检仅提供优先核对线索，最终差异判断需结合两份文档正文。",
            f"# 文档A：{file_a['original_filename']}\n{file_a['text']}",
            f"# 文档B：{file_b['original_filename']}\n{file_b['text']}",
        ]
    ).strip()
    return document_text, static_summary


def language_consistency_static_summary(file_a: dict, file_b: dict) -> str:
    profile_a = _document_static_profile(file_a)
    profile_b = _document_static_profile(file_b)
    only_a = _limited_sorted(profile_a["tokens"] - profile_b["tokens"], 40)
    only_b = _limited_sorted(profile_b["tokens"] - profile_a["tokens"], 40)
    ratio = _safe_ratio(profile_b["nonspace_chars"], profile_a["nonspace_chars"])
    lines = [
        (
            f"- 文档A：{file_a['original_filename']}；格式：{file_a['file_type']}；"
            f"语种估计：{profile_a['language']}；非空白字符：{profile_a['nonspace_chars']}；"
            f"段落：{profile_a['paragraphs']}；标题线索：{len(profile_a['headings'])}"
        ),
        (
            f"- 文档B：{file_b['original_filename']}；格式：{file_b['file_type']}；"
            f"语种估计：{profile_b['language']}；非空白字符：{profile_b['nonspace_chars']}；"
            f"段落：{profile_b['paragraphs']}；标题线索：{len(profile_b['headings'])}"
        ),
        f"- 长度比例：文档B / 文档A = {ratio}",
        f"- 文档A独有硬线索：{_format_preview_list(only_a)}",
        f"- 文档B独有硬线索：{_format_preview_list(only_b)}",
        f"- 文档A标题线索：{_format_preview_list(profile_a['headings'])}",
        f"- 文档B标题线索：{_format_preview_list(profile_b['headings'])}",
    ]
    return "\n".join(lines)


def _document_static_profile(file_info: dict) -> dict:
    text = str(file_info.get("text") or "")
    nonspace_text = re.sub(r"\s+", "", text)
    paragraphs = [part for part in re.split(r"\n\s*\n+", text.strip()) if part.strip()]
    tokens = {
        _normalize_static_token(match.group(0))
        for match in LANGUAGE_STATIC_TOKEN_RE.finditer(text)
    }
    tokens = {token for token in tokens if token}
    return {
        "language": text_language_label(estimate_text_language(text)),
        "nonspace_chars": len(nonspace_text),
        "paragraphs": len(paragraphs),
        "tokens": tokens,
        "headings": _extract_static_headings(text, 12),
    }


def _extract_static_headings(text: str, limit: int) -> list[str]:
    headings = []
    seen = set()
    for line in text.splitlines():
        value = re.sub(r"\s+", " ", line).strip()
        if not value or len(value) > 120 or not LANGUAGE_HEADING_RE.match(value):
            continue
        if value in seen:
            continue
        seen.add(value)
        headings.append(value)
        if len(headings) >= limit:
            break
    return headings


def _normalize_static_token(value: str) -> str:
    return value.strip(" \t\r\n,.;:，。；：、()（）[]【】<>《》\"'“”‘’").lower()


def _limited_sorted(values: set[str] | list[str], limit: int) -> list[str]:
    return sorted(values, key=lambda value: (len(value), value))[:limit]


def _format_preview_list(values: list[str]) -> str:
    return "、".join(values) if values else "未发现"


def _safe_ratio(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "无法计算"
    return f"{numerator / denominator:.2f}"
