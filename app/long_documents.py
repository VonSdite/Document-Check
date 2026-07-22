import json
import re
from dataclasses import dataclass


LONG_DOCUMENT_DIRECT_MAX_CHARS = 120_000
LONG_DOCUMENT_CHUNK_MAX_CHARS = 40_000
LONG_DOCUMENT_CHUNK_OVERLAP_CHARS = 1_200
LONG_DOCUMENT_FACT_LIMIT_PER_CHUNK = 120
LONG_DOCUMENT_CONSISTENCY_CANDIDATE_LIMIT = 80
GROUPED_DOCUMENT_CHUNK_MAX_CHARS = 28_000
GROUPED_DOCUMENT_REQUEST_MAX_CHARS = 60_000
GROUPED_DOCUMENT_RETRIEVAL_TOP_K = 3

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
_CONSISTENCY_GROUP_RE = re.compile(r"^#\s+(素材文档|资料)\s*$")
_CONSISTENCY_FILE_RE = re.compile(r"^##\s+(素材文档|资料)(\d+)[：:]\s*(.+?)\s*$")
_LANGUAGE_DOCUMENT_RE = re.compile(r"^#\s+文档([AB])[：:]\s*(.+?)\s*$", re.MULTILINE)
_LANGUAGE_GROUP_RE = re.compile(r"^#\s+(文档[AB])\s*$")
_LANGUAGE_FILE_RE = re.compile(r"^##\s+(文档[AB])(\d+)[：:]\s*(.+?)\s*$")
_LATIN_TERM_RE = re.compile(r"(?i)[a-z][a-z0-9_-]{2,}")
_HARD_ANCHOR_RE = re.compile(
    r"(?i)https?://[^\s，。；、]+|[\w.+-]+@[\w.-]+\.[a-z]{2,}|"
    r"(?<!\w)(?:\d{1,3}\.){3}\d{1,3}(?!\w)|"
    r"[a-z][a-z0-9._/-]*\d[a-z0-9._/-]*|"
    r"\d+(?:\.\d+)*(?:\s*(?:%|‰|°c|℃|[a-z]{1,8}|毫米|厘米|米|千克|克|伏|安|瓦|赫兹|秒|分钟|小时))?"
)
_SECTION_IDENTIFIER_RE = re.compile(
    r"^\s*(?:第([一二三四五六七八九十百千零〇两\d]+)[章节篇部分]|"
    r"(\d+(?:\.\d+){0,5})(?:[.、\s]|$)|"
    r"([A-Z](?:\.\d+)+)(?:[.、\s]|$))"
)


@dataclass(frozen=True)
class DocumentChunk:
    index: int
    total: int
    label: str
    text: str


@dataclass(frozen=True)
class GroupedDocument:
    role: str
    label: str
    index: int
    filename: str
    text: str


@dataclass(frozen=True)
class EvidenceChunk:
    role: str
    document_label: str
    filename: str
    index: int
    total: int
    label: str
    text: str
    position: float


@dataclass(frozen=True)
class ChunkSearchIndex:
    chunk: EvidenceChunk
    hard_anchors: frozenset[str]
    latin_terms: frozenset[str]
    cjk_bigrams: frozenset[str]
    section_ids: frozenset[str]


@dataclass(frozen=True)
class LanguageChunkAlignment:
    left: EvidenceChunk
    right: EvidenceChunk
    score: float


def long_document_limits(max_input_chars: int) -> tuple[int, int]:
    configured = max(5_000, int(max_input_chars or 0))
    direct_limit = min(LONG_DOCUMENT_DIRECT_MAX_CHARS, max(3_000, int(configured * 0.6)))
    chunk_limit = min(LONG_DOCUMENT_CHUNK_MAX_CHARS, max(2_500, int(configured * 0.5)))
    return direct_limit, min(chunk_limit, direct_limit)


def grouped_document_limits(max_input_chars: int) -> tuple[int, int]:
    configured = max(5_000, int(max_input_chars or 0))
    request_limit = min(GROUPED_DOCUMENT_REQUEST_MAX_CHARS, max(4_000, int(configured * 0.75)))
    chunk_limit = min(
        GROUPED_DOCUMENT_CHUNK_MAX_CHARS,
        max(1_500, (request_limit - 1_200) // 2),
    )
    return request_limit, chunk_limit


def parse_consistency_documents(document_text: str) -> dict[str, list[GroupedDocument]]:
    role_by_label = {"素材文档": "master", "资料": "related"}
    documents = _parse_grouped_documents(
        document_text,
        group_re=_CONSISTENCY_GROUP_RE,
        file_re=_CONSISTENCY_FILE_RE,
        role_by_label=role_by_label,
    )
    return {
        role: [document for document in documents if document.role == role]
        for role in ("master", "related")
    }


def parse_language_consistency_documents(
    document_text: str,
) -> tuple[str, GroupedDocument | None, GroupedDocument | None]:
    text = str(document_text or "").strip()
    markers = list(_LANGUAGE_DOCUMENT_RE.finditer(text))
    if len(markers) >= 2:
        by_role = {}
        first_marker = markers[0]
        static_precheck = text[: first_marker.start()].strip()
        static_precheck = re.sub(r"^#\s+静态预检摘要\s*", "", static_precheck).strip()
        for marker_index, marker in enumerate(markers):
            role_letter = marker.group(1)
            role = "document_a" if role_letter == "A" else "document_b"
            end = markers[marker_index + 1].start() if marker_index + 1 < len(markers) else len(text)
            body = text[marker.end() : end].strip()
            by_role[role] = GroupedDocument(
                role=role,
                label=f"文档{role_letter}",
                index=1,
                filename=marker.group(2).strip(),
                text=body,
            )
        return static_precheck, by_role.get("document_a"), by_role.get("document_b")

    documents = _parse_grouped_documents(
        text,
        group_re=_LANGUAGE_GROUP_RE,
        file_re=_LANGUAGE_FILE_RE,
        role_by_label={"文档A": "document_a", "文档B": "document_b"},
    )
    by_role = {document.role: document for document in documents}
    return "", by_role.get("document_a"), by_role.get("document_b")


def split_grouped_document(document: GroupedDocument, *, max_chars: int) -> list[EvidenceChunk]:
    source_text = f"file: {document.filename}\n\n{document.text}".strip()
    chunks = split_long_document(source_text, max_chars=max_chars)
    return [
        EvidenceChunk(
            role=document.role,
            document_label=f"{document.label}{document.index}",
            filename=document.filename,
            index=chunk.index,
            total=chunk.total,
            label=f"{document.label}{document.index}：{document.filename} / {chunk.label}",
            text=chunk.text,
            position=(chunk.index - 0.5) / max(1, chunk.total),
        )
        for chunk in chunks
    ]


def build_chunk_search_index(chunks: list[EvidenceChunk]) -> list[ChunkSearchIndex]:
    return [_index_chunk(chunk) for chunk in chunks]


def rank_relevant_chunks(
    query: EvidenceChunk,
    candidates: list[ChunkSearchIndex],
    *,
    top_k: int = GROUPED_DOCUMENT_RETRIEVAL_TOP_K,
) -> list[EvidenceChunk]:
    query_index = _index_chunk(query)
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            -_retrieval_score(query_index, candidate),
            abs(query.position - candidate.chunk.position),
            candidate.chunk.label,
        ),
    )
    return [candidate.chunk for candidate in ranked[: max(1, int(top_k or 1))]]


def align_language_chunks(
    left_chunks: list[EvidenceChunk],
    right_chunks: list[EvidenceChunk],
) -> tuple[list[LanguageChunkAlignment], list[EvidenceChunk], list[EvidenceChunk]]:
    left_indexes = build_chunk_search_index(left_chunks)
    right_indexes = build_chunk_search_index(right_chunks)
    candidates = []
    for left_index, left in enumerate(left_indexes):
        for right_index, right in enumerate(right_indexes):
            compatible, score = _language_alignment_score(left, right)
            if compatible:
                candidates.append((score, left_index, right_index))
    candidates.sort(
        key=lambda item: (
            -item[0],
            abs(left_chunks[item[1]].position - right_chunks[item[2]].position),
            item[1],
            item[2],
        )
    )

    used_left = set()
    used_right = set()
    alignments = []
    for score, left_index, right_index in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        alignments.append(
            LanguageChunkAlignment(
                left=left_chunks[left_index],
                right=right_chunks[right_index],
                score=score,
            )
        )
    alignments.sort(key=lambda item: (item.left.position, item.right.position))
    unmatched_left = [chunk for index, chunk in enumerate(left_chunks) if index not in used_left]
    unmatched_right = [chunk for index, chunk in enumerate(right_chunks) if index not in used_right]
    return alignments, unmatched_left, unmatched_right


def build_evidence_comparison_input(
    query: EvidenceChunk,
    evidence_chunks: list[EvidenceChunk],
    *,
    direction: str,
    max_chars: int,
) -> str:
    title = "资料对照素材" if direction == "related_to_master" else "素材反向覆盖"
    query_title = "当前资料分段" if direction == "related_to_master" else "当前素材分段"
    evidence_title = "检索到的素材证据" if direction == "related_to_master" else "检索到的资料证据"
    prefix = (
        f"comparison_direction: {direction}\n"
        f"# {title}\n"
        f"# {query_title}\n位置：{query.label}\n"
    )
    value = _append_with_limit("", prefix + query.text, max_chars)
    value = _append_with_limit(value, f"# {evidence_title}", max_chars)
    for index, chunk in enumerate(evidence_chunks, start=1):
        block = f"## 候选证据 {index}\n位置：{chunk.label}\n{chunk.text}"
        value = _append_with_limit(value, block, max_chars)
        if len(value) >= max_chars:
            break
    return value.strip()


def build_language_alignment_input(
    left: EvidenceChunk,
    right: EvidenceChunk | None,
    *,
    static_precheck: str,
    max_chars: int,
    coverage_side: str = "",
) -> str:
    prefix = "language_alignment_mode: paired" if right is not None and not coverage_side else "language_alignment_mode: unmatched_coverage"
    value = prefix
    if coverage_side:
        value += f"\nunmatched_side: {coverage_side}"
    if static_precheck:
        value = _append_with_limit(value, f"# 静态预检摘要\n{static_precheck[:4_000]}", max_chars)
    value = _append_with_limit(value, f"# 文档A分段\n位置：{left.label}\n{left.text}", max_chars)
    if right is None:
        value = _append_with_limit(value, "# 文档B分段\n未找到可靠对齐分段。", max_chars)
    else:
        value = _append_with_limit(value, f"# 文档B分段\n位置：{right.label}\n{right.text}", max_chars)
    return value.strip()


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


def _parse_grouped_documents(
    document_text: str,
    *,
    group_re: re.Pattern,
    file_re: re.Pattern,
    role_by_label: dict[str, str],
) -> list[GroupedDocument]:
    documents = []
    current_label = ""
    active_filename = ""
    active_index = 0
    active_lines = []
    loose_lines = []
    next_indexes = {label: 1 for label in role_by_label}

    def append_document(label: str, filename: str, index: int, lines: list[str]):
        body = "\n".join(lines).strip()
        role = role_by_label.get(label)
        if not role or not body:
            return
        resolved_index = index or next_indexes[label]
        next_indexes[label] = max(next_indexes[label], resolved_index + 1)
        documents.append(
            GroupedDocument(
                role=role,
                label=label,
                index=resolved_index,
                filename=filename or f"{label}{resolved_index}",
                text=body,
            )
        )

    def flush_active():
        nonlocal active_filename, active_index, active_lines
        if active_filename or active_lines:
            append_document(current_label, active_filename, active_index, active_lines)
        active_filename = ""
        active_index = 0
        active_lines = []

    def flush_loose():
        nonlocal loose_lines
        if loose_lines:
            append_document(current_label, "", 0, loose_lines)
        loose_lines = []

    for line in str(document_text or "").splitlines():
        group_match = group_re.match(line.strip())
        if group_match:
            flush_active()
            flush_loose()
            current_label = group_match.group(1)
            continue
        file_match = file_re.match(line.strip())
        if file_match:
            flush_active()
            flush_loose()
            current_label = file_match.group(1)
            active_index = int(file_match.group(2))
            active_filename = file_match.group(3).strip()
            continue
        if active_filename:
            active_lines.append(line)
        elif current_label:
            loose_lines.append(line)

    flush_active()
    flush_loose()
    return documents


def _index_chunk(chunk: EvidenceChunk) -> ChunkSearchIndex:
    text = _searchable_chunk_text(chunk.text)
    return ChunkSearchIndex(
        chunk=chunk,
        hard_anchors=frozenset(_normalized_matches(_HARD_ANCHOR_RE, text)),
        latin_terms=frozenset(
            term
            for term in _normalized_matches(_LATIN_TERM_RE, text)
            if term not in {"file", "long", "document", "chunk", "scope", "current"}
        ),
        cjk_bigrams=frozenset(_cjk_bigrams(text, limit=6_000)),
        section_ids=frozenset(_section_identifiers(text)),
    )


def _searchable_chunk_text(text: str) -> str:
    ignored_prefixes = (
        "file:",
        "long_document_chunk:",
        "chunk_scope:",
        "说明：这是长文档的一部分",
    )
    return "\n".join(
        line for line in str(text or "").splitlines()
        if not line.strip().startswith(ignored_prefixes)
    )


def _normalized_matches(pattern: re.Pattern, text: str) -> list[str]:
    values = []
    for match in pattern.finditer(text):
        value = re.sub(r"\s+", "", match.group(0)).strip(".,;:，。；：、").lower()
        if value:
            values.append(value)
    return values


def _cjk_bigrams(text: str, *, limit: int) -> list[str]:
    values = []
    seen = set()
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for index in range(len(sequence) - 1):
            value = sequence[index : index + 2]
            if value in seen:
                continue
            seen.add(value)
            values.append(value)
            if len(values) >= limit:
                return values
    return values


def _section_identifiers(text: str) -> list[str]:
    values = []
    for line in str(text or "").splitlines():
        match = _SECTION_IDENTIFIER_RE.match(line)
        if not match:
            continue
        value = next((part for part in match.groups() if part), "")
        value = value.strip(".、 ").lower()
        if value and value not in values:
            values.append(value)
    return values


def _retrieval_score(query: ChunkSearchIndex, candidate: ChunkSearchIndex) -> float:
    section_overlap = len(query.section_ids & candidate.section_ids)
    hard_overlap = len(query.hard_anchors & candidate.hard_anchors)
    latin_overlap = len(query.latin_terms & candidate.latin_terms)
    cjk_overlap = len(query.cjk_bigrams & candidate.cjk_bigrams)
    return section_overlap * 30 + hard_overlap * 14 + latin_overlap * 2 + min(cjk_overlap, 250) * 0.15


def _language_alignment_score(left: ChunkSearchIndex, right: ChunkSearchIndex) -> tuple[bool, float]:
    section_overlap = len(left.section_ids & right.section_ids)
    hard_overlap = len(left.hard_anchors & right.hard_anchors)
    conflicting_sections = bool(left.section_ids and right.section_ids and not section_overlap)
    if conflicting_sections and not hard_overlap:
        return False, 0.0
    if bool(left.section_ids) != bool(right.section_ids) and not hard_overlap:
        return False, 0.0
    position_score = max(0.0, 1.0 - abs(left.chunk.position - right.chunk.position))
    latin_overlap = len(left.latin_terms & right.latin_terms)
    score = section_overlap * 100 + hard_overlap * 18 + latin_overlap * 2 + position_score * 8
    return True, score


def _append_with_limit(current: str, block: str, max_chars: int) -> str:
    limit = max(1_000, int(max_chars or 0))
    separator = "\n\n" if current else ""
    remaining = limit - len(current) - len(separator)
    if remaining <= 0:
        return current
    block = str(block or "").strip()
    if len(block) > remaining:
        suffix = "\n[内容因单次请求预算截断]"
        if remaining <= len(suffix):
            return current
        block = block[: remaining - len(suffix)].rstrip() + suffix
    return f"{current}{separator}{block}".strip()


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
