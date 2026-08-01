import json
import re
from dataclasses import dataclass


LONG_DOCUMENT_DIRECT_MAX_CHARS = 120_000
LONG_DOCUMENT_CHUNK_MAX_CHARS = 40_000
LONG_DOCUMENT_CHUNK_OVERLAP_CHARS = 1_200
LONG_DOCUMENT_FACT_LIMIT_PER_CHUNK = 120
LONG_DOCUMENT_CONSISTENCY_CANDIDATE_LIMIT = 80

_PAGE_MARKER_RE = re.compile(r"(?m)^\[第(\d+)页\]\s*$")
_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+|第[一二三四五六七八九十百千\d]+[章节篇部分]\s*|"
    r"\d+(?:\.\d+){0,5}[.、]?\s+|[A-Z](?:\.\d+)*[.、]?\s+)"
)
_REPORT_FIELDS = (
    "status",
    "severity",
    "confidence",
    "category",
    "location",
    "excerpt",
    "description",
    "impact",
    "suggestion",
)
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
_STATUS_ORDER = {"issue": 0, "suggestion": 1, "non_issue": 2}


@dataclass(frozen=True)
class DocumentChunk:
    index: int
    total: int
    label: str
    text: str


def long_document_limits(max_input_chars: int) -> tuple[int, int]:
    configured = max(5_000, int(max_input_chars or 0))
    direct_limit = min(LONG_DOCUMENT_DIRECT_MAX_CHARS, max(3_000, int(configured * 0.6)))
    chunk_limit = min(LONG_DOCUMENT_CHUNK_MAX_CHARS, max(2_500, int(configured * 0.5)))
    return direct_limit, min(chunk_limit, direct_limit)


def split_long_document(
    document_text: str,
    *,
    max_chars: int,
    overlap_chars: int = LONG_DOCUMENT_CHUNK_OVERLAP_CHARS,
) -> list[DocumentChunk]:
    text = str(document_text or "").strip()
    if not text:
        return []
    max_chars = max(1_500, int(max_chars or LONG_DOCUMENT_CHUNK_MAX_CHARS))
    overlap_chars = max(0, min(int(overlap_chars or 0), max_chars // 5))
    header, body = _document_header_and_body(text)
    units = _document_units(body, max_chars=max_chars)
    bodies = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}".strip() if current else unit
        if current and len(candidate) > max_chars:
            bodies.append(current.strip())
            available_overlap = max(0, max_chars - len(unit) - 2)
            overlap = _text_overlap(current, min(overlap_chars, available_overlap))
            current = f"{overlap}\n\n{unit}".strip() if overlap else unit
        else:
            current = candidate
    if current.strip():
        bodies.append(current.strip())

    total = len(bodies)
    chunks = []
    for index, chunk_body in enumerate(bodies, start=1):
        label = _chunk_label(chunk_body, index, total)
        prefix = (
            f"{header}\n\n" if header else ""
        ) + (
            f"long_document_chunk: {index}/{total}\n"
            f"chunk_scope: {label}\n"
            "说明：这是长文档的一部分，只能依据本分段和提供的全文结构线索判断；位置必须引用本分段中的页码或章节。"
        )
        chunks.append(DocumentChunk(index=index, total=total, label=label, text=f"{prefix}\n\n{chunk_body}".strip()))
    return chunks


def build_document_outline(document_text: str, max_chars: int = 8_000) -> str:
    lines = []
    seen = set()
    for raw_line in str(document_text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if line.startswith("file:") or _PAGE_MARKER_RE.match(line) or _HEADING_RE.match(line):
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
        if sum(len(value) + 1 for value in lines) >= max_chars:
            break
    return "\n".join(lines).strip()


def parse_structured_report(content: str) -> dict | None:
    value = str(content or "").strip()
    if not value:
        return None
    candidates = [value]
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE).strip()
    if fenced != value:
        candidates.append(fenced)
    start = value.find("{")
    end = value.rfind("}")
    if 0 <= start < end:
        candidates.append(value[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        for _ in range(2):
            if not isinstance(parsed, str):
                break
            try:
                parsed = json.loads(parsed)
            except json.JSONDecodeError:
                break
        if not isinstance(parsed, dict):
            continue
        items = parsed.get("items")
        if not isinstance(items, list):
            continue
        normalized_items = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            item = {field: str(raw_item.get(field) or "").strip() for field in _REPORT_FIELDS}
            item["status"] = item["status"] if item["status"] in _STATUS_ORDER else "suggestion"
            item["severity"] = item["severity"] if item["severity"] in _SEVERITY_ORDER else "low"
            item["confidence"] = item["confidence"] if item["confidence"] in _CONFIDENCE_ORDER else "low"
            normalized_items.append(item)
        return {"summary": str(parsed.get("summary") or "").strip(), "items": normalized_items}
    return None


def merge_chunk_reports(
    reports: list[tuple[str, str]],
    *,
    issue_output_limit: int,
    summary_prefix: str = "长文档分段检查完成",
) -> str:
    items_by_key = {}
    parse_failures = []
    for label, content in reports:
        report = parse_structured_report(content)
        if report is None:
            parse_failures.append(label)
            continue
        for item in report["items"]:
            if item["status"] == "non_issue":
                continue
            if not item["location"]:
                item["location"] = label
            key = _report_item_key(item)
            existing = items_by_key.get(key)
            if existing is None:
                items_by_key[key] = dict(item)
                continue
            existing["location"] = _merge_locations(existing.get("location", ""), item.get("location", ""))
            if _report_priority(item) < _report_priority(existing):
                existing["severity"] = item["severity"]
                existing["confidence"] = item["confidence"]

    if parse_failures:
        warning = {
            "status": "suggestion",
            "severity": "low",
            "confidence": "high",
            "category": "长文档检查完整性",
            "location": "、".join(parse_failures[:12]),
            "excerpt": "",
            "description": "部分分段的模型输出无法解析，相关范围未能纳入最终汇总。",
            "impact": "这些分段可能存在未汇总的问题。",
            "suggestion": "建议重试任务或人工复核所列分段。",
        }
        items_by_key[_report_item_key(warning)] = warning

    items = sorted(items_by_key.values(), key=_report_priority)
    limit = max(0, int(issue_output_limit or 0))
    omitted = 0
    if limit and len(items) > limit:
        omitted = len(items) - limit
        items = items[:limit]
    summary = f"{summary_prefix}，汇总 {len(reports)} 个检查批次，发现 {len(items)} 条需关注内容"
    if omitted:
        summary += f"；另有 {omitted} 条低优先级内容因输出上限未展示"
    summary += "。"
    return json.dumps({"summary": summary, "items": items}, ensure_ascii=False)


def extract_consistency_facts(content: str, chunk_label: str) -> list[dict]:
    report = parse_structured_report(content)
    if report is None:
        return []
    facts = []
    for item in report["items"]:
        entity = item["category"]
        attribute = item["description"]
        value = item["impact"]
        excerpt = item["excerpt"]
        if not entity or not attribute or not value or not excerpt:
            continue
        facts.append(
            {
                "entity": entity,
                "attribute": attribute,
                "value": value,
                "condition": item["suggestion"],
                "location": item["location"] or chunk_label,
                "excerpt": excerpt,
                "chunk": chunk_label,
            }
        )
    return facts


def build_consistency_candidates(facts: list[dict], limit: int = LONG_DOCUMENT_CONSISTENCY_CANDIDATE_LIMIT) -> list[dict]:
    grouped = {}
    for fact in facts:
        key = (_normalize_fact_text(fact.get("entity")), _normalize_fact_text(fact.get("attribute")))
        if not all(key):
            continue
        grouped.setdefault(key, []).append(fact)

    candidates = []
    seen = set()
    for group in grouped.values():
        values = {}
        for fact in group:
            values.setdefault(_normalize_fact_text(fact.get("value")), fact)
        representatives = list(values.values())
        for left_index, left in enumerate(representatives):
            for right in representatives[left_index + 1 :]:
                if left.get("chunk") == right.get("chunk") and left.get("location") == right.get("location"):
                    continue
                pair_key = tuple(sorted((_fact_identity(left), _fact_identity(right))))
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                candidates.append({"left": left, "right": right})
                if len(candidates) >= max(1, int(limit or 1)):
                    return candidates
    return candidates


def consistency_candidate_batches(candidates: list[dict], max_chars: int) -> list[str]:
    max_chars = max(2_000, int(max_chars or 0))
    blocks = []
    for index, candidate in enumerate(candidates, start=1):
        left = candidate["left"]
        right = candidate["right"]
        blocks.append(
            "\n".join(
                [
                    f"## 候选 {index}",
                    f"对象：{left['entity']}",
                    f"属性：{left['attribute']}",
                    f"证据A位置：{left['location']}",
                    f"证据A原文：{left['excerpt']}",
                    f"证据A规范化值：{left['value']}",
                    f"证据A条件：{left.get('condition') or '未注明'}",
                    f"证据B位置：{right['location']}",
                    f"证据B原文：{right['excerpt']}",
                    f"证据B规范化值：{right['value']}",
                    f"证据B条件：{right.get('condition') or '未注明'}",
                ]
            )
        )
    batches = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}".strip() if current else block
        if current and len(candidate) > max_chars:
            batches.append(current)
            current = block
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def _document_header_and_body(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if lines and lines[0].startswith("file:"):
        return lines[0].strip(), "\n".join(lines[1:]).strip()
    return "", text


def _document_units(body: str, *, max_chars: int) -> list[str]:
    page_matches = list(_PAGE_MARKER_RE.finditer(body))
    if page_matches:
        units = []
        prefix = body[: page_matches[0].start()].strip()
        if prefix:
            units.extend(_split_oversized_unit(prefix, max_chars))
        for index, match in enumerate(page_matches):
            end = page_matches[index + 1].start() if index + 1 < len(page_matches) else len(body)
            units.extend(_split_oversized_unit(body[match.start() : end].strip(), max_chars))
        return units
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", body) if part.strip()]
    units = []
    for paragraph in paragraphs or [body]:
        units.extend(_split_oversized_unit(paragraph, max_chars))
    return units


def _split_oversized_unit(value: str, max_chars: int) -> list[str]:
    value = value.strip()
    if len(value) <= max_chars:
        return [value]
    lines = value.splitlines()
    parts = []
    current = ""
    for line in lines:
        line = line.rstrip()
        candidate = f"{current}\n{line}".strip() if current else line
        if current and len(candidate) > max_chars:
            parts.append(current)
            current = line
        elif len(line) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.extend(line[index : index + max_chars] for index in range(0, len(line), max_chars))
        else:
            current = candidate
    if current:
        parts.append(current)
    return [part for part in parts if part]


def _text_overlap(value: str, overlap_chars: int) -> str:
    if overlap_chars <= 0 or not value:
        return ""
    tail = value[-overlap_chars:]
    newline = tail.find("\n")
    if newline >= 0:
        tail = tail[newline + 1 :]
    return tail.strip()


def _chunk_label(body: str, index: int, total: int) -> str:
    pages = [int(match.group(1)) for match in _PAGE_MARKER_RE.finditer(body)]
    if pages:
        return f"第{min(pages)}-{max(pages)}页（分段 {index}/{total}）"
    heading = next((line.strip() for line in body.splitlines() if _HEADING_RE.match(line.strip())), "")
    return f"{heading or '未识别章节'}（分段 {index}/{total}）"


def _report_item_key(item: dict) -> str:
    values = [
        item.get("status"),
        item.get("category"),
        item.get("excerpt"),
        item.get("description"),
        item.get("suggestion"),
    ]
    return "|".join(_normalize_report_text(value) for value in values)


def _normalize_report_text(value) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _report_priority(item: dict) -> tuple[int, int, int]:
    return (
        _STATUS_ORDER.get(str(item.get("status") or ""), 9),
        _SEVERITY_ORDER.get(str(item.get("severity") or ""), 9),
        _CONFIDENCE_ORDER.get(str(item.get("confidence") or ""), 9),
    )


def _merge_locations(left: str, right: str) -> str:
    values = []
    for value in (left, right):
        for part in re.split(r"[；;]", str(value or "")):
            part = part.strip()
            if part and part not in values:
                values.append(part)
    return "；".join(values)


def _normalize_fact_text(value) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff%°℃]+", "", str(value or "").lower())


def _fact_identity(fact: dict) -> str:
    return "|".join(
        _normalize_fact_text(fact.get(key))
        for key in ("entity", "attribute", "value", "condition", "location", "excerpt")
    )
