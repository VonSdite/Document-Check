import hashlib
import json
import re
from difflib import SequenceMatcher

from app.checks.guardrails import (
    guarded_report_summary,
    is_unsupported_visual_missing_item,
)
from app.contracts.limits import normalize_issue_output_limit
from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
    document_groups_from_meta,
)
from app.persistence.connection import get_db, now_text
from app.reporting.constants import (
    MEDIA_REPORT_ITEM_DETAIL_FIELDS,
    MEDIA_REPORT_ITEM_FIELDS,
    REPORT_ACCEPTANCE_STATUSES,
    REPORT_CONFIDENCE_LABELS,
    REPORT_CONFIDENCE_ORDER,
    REPORT_COUNT_KEYS,
    REPORT_FIELD_ALIASES,
    REPORT_ITEM_FIELDS,
    REPORT_ITEM_PREFIX_RE,
    REPORT_ITEM_START_RE,
    REPORT_ITEM_TYPE_ORDER,
    REPORT_ITEM_TYPES,
    REPORT_JSON_ITEM_KEYS,
    REPORT_JSON_SUMMARY_KEYS,
    REPORT_LEGACY_LABEL_FIELDS,
    REPORT_NO_ACTION_IMPACT_MARKERS,
    REPORT_NO_ACTION_SUGGESTION_MARKERS,
    REPORT_REJECTION_REASONS,
    REPORT_SEVERITY_LABELS,
    REPORT_SEVERITY_ORDER,
    REPORT_STATS_PREPARATION_VERSION,
    REPORT_STATUS_KEYS,
    REPORT_SUPPRESSION_DESCRIPTION_REPLACEMENTS,
    REPORT_SUPPRESSION_DESCRIPTION_SIMILARITY_THRESHOLD,
    REPORT_SUPPRESSION_FIELDS,
    REPORT_SUPPRESSION_REJECTION_REASONS,
)


def _row_value(row, key: str, default=None):
    if row is None:
        return default
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def _task_document_groups(task) -> list[dict]:
    return document_groups_from_meta(task["document_meta_json"])


def _parse_report_suppression_item_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _write_task_report_stat_rows(cache_rows: list[tuple]) -> None:
    if not cache_rows:
        return
    db = get_db()
    db.executemany(
        """
            INSERT INTO task_report_stats(
                task_id, source_updated_at, suppression_version,
                issue_count, suggestion_count, non_issue_count,
                accepted_issue_count, rejected_issue_count,
                pending_issue_acceptance_count, suppressed_count,
                reviewed_item_count, pending_review_item_count, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                source_updated_at = excluded.source_updated_at,
                suppression_version = excluded.suppression_version,
                issue_count = excluded.issue_count,
                suggestion_count = excluded.suggestion_count,
                non_issue_count = excluded.non_issue_count,
                accepted_issue_count = excluded.accepted_issue_count,
                rejected_issue_count = excluded.rejected_issue_count,
                pending_issue_acceptance_count = excluded.pending_issue_acceptance_count,
                suppressed_count = excluded.suppressed_count,
                reviewed_item_count = excluded.reviewed_item_count,
                pending_review_item_count = excluded.pending_review_item_count,
                updated_at = excluded.updated_at
        """,
        cache_rows,
    )
    db.commit()


def _cache_prepared_task_report_stats(
    task, prepared_results: list[dict], source_updated_at: str
) -> None:
    task_type = str(task["task_type"] or DOCUMENT_TASK_TYPE)
    suppression_version = _report_suppression_versions({task_type}).get(
        task_type, _empty_report_suppression_version()
    )
    item_totals = _report_item_totals(prepared_results)
    _write_task_report_stat_rows(
        [
            (
                task["id"],
                source_updated_at,
                suppression_version,
                *[int(item_totals.get(key) or 0) for key in REPORT_COUNT_KEYS],
                now_text(),
            )
        ]
    )


def _report_suppression_versions(task_types: set[str]) -> dict[str, str]:
    versions = {
        task_type: _empty_report_suppression_version() for task_type in task_types
    }
    if not task_types:
        return versions
    placeholders = ",".join("?" for _ in task_types)
    rows = (
        get_db()
        .execute(
            f"""
        SELECT task_type, COUNT(*) AS total, COALESCE(SUM(id), 0) AS id_sum, MAX(updated_at) AS latest
        FROM report_suppression_rules
        WHERE enabled = 1 AND task_type IN ({placeholders})
        GROUP BY task_type
        """,
            tuple(sorted(task_types)),
        )
        .fetchall()
    )
    for row in rows:
        versions[str(row["task_type"])] = (
            f"{REPORT_STATS_PREPARATION_VERSION}|"
            f"{int(row['total'] or 0)}:{int(row['id_sum'] or 0)}:{row['latest'] or ''}"
        )
    return versions


def _empty_report_suppression_version() -> str:
    return f"{REPORT_STATS_PREPARATION_VERSION}|0:0:"


def _is_media_report_task_type(task_type: str | None) -> bool:
    return (task_type or DOCUMENT_TASK_TYPE) in {IMAGE_TASK_TYPE, VIDEO_TASK_TYPE}


def _uses_compact_media_report(task_type: str | None) -> bool:
    return (task_type or DOCUMENT_TASK_TYPE) == IMAGE_TASK_TYPE


def _report_item_fields_for_task(task_type: str | None) -> tuple[tuple[str, str], ...]:
    if _uses_compact_media_report(task_type):
        return MEDIA_REPORT_ITEM_FIELDS
    return REPORT_ITEM_FIELDS


def _media_report_item_text(item: dict) -> str:
    description = str(item.get("description") or "").strip()
    parts = [description] if description else []
    for field, label in MEDIA_REPORT_ITEM_DETAIL_FIELDS:
        value = str(item.get(field) or "").strip()
        if value:
            parts.append(f"{label}：{value}")
    return "\n".join(parts).strip()


def _task_results(task):
    return _prepare_task_results(
        _raw_task_results(task),
        task_type=task["task_type"] or DOCUMENT_TASK_TYPE,
        task_id=task["id"],
        record_suppression_hits=True,
    )


def _raw_task_results(task) -> list[dict]:
    return _parse_result_json(task["result_json"])


def _parse_result_json(result_json) -> list[dict]:
    if not result_json:
        return []
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _prepare_task_results(
    results: list[dict],
    *,
    task_type: str | None = None,
    task_id: int | None = None,
    record_suppression_hits: bool = False,
    suppression_rules: dict[str, list[dict]] | None = None,
) -> list[dict]:
    prepared = []
    if suppression_rules is None:
        suppression_rules = (
            _enabled_report_suppression_rules(task_type) if task_type else {}
        )
    for result in results:
        item = dict(result)
        result_code = str(item.get("code") or "")
        structured_report = _result_structured_report(item)
        report_items = _result_report_items(item, structured_report)
        filtered_visual_missing_count = 0
        if task_type in {
            DOCUMENT_TASK_TYPE,
            CONSISTENCY_TASK_TYPE,
            LANGUAGE_CONSISTENCY_TASK_TYPE,
        }:
            retained_report_items = []
            for report_item in report_items:
                if is_unsupported_visual_missing_item(report_item):
                    filtered_visual_missing_count += 1
                else:
                    retained_report_items.append(report_item)
            report_items = retained_report_items
        classifications = item.get("item_classifications")
        if not isinstance(classifications, dict):
            classifications = {}
        report_items = [
            report_item
            for report_item in report_items
            if report_item.get("type") != "non_issue"
            or _normalize_report_item_type(classifications.get(report_item.get("id")))
            is not None
        ]
        original_report_item_count = len(report_items)
        report_items = _deduplicate_report_items(report_items)
        report_items.sort(key=_report_item_priority_key)
        duplicate_count = original_report_item_count - len(report_items)
        report_items, suppressed_items = _apply_report_suppression(
            task_type=task_type,
            task_id=task_id,
            result_code=result_code,
            report_items=report_items,
            suppression_rules=suppression_rules,
            record_hits=record_suppression_hits,
        )
        report_items, report_limit = _limit_ranked_report_items(
            report_items,
            issue_output_limit=item.get("issue_output_limit"),
            original_count=original_report_item_count,
            duplicate_count=duplicate_count,
        )
        acceptances = item.get("item_acceptances")
        if not isinstance(acceptances, dict):
            acceptances = {}
        for report_item in report_items + suppressed_items:
            saved_type = classifications.get(report_item["id"])
            report_item["type"] = (
                _normalize_report_item_type(saved_type) or report_item["type"]
            )
            report_item["type_label"] = REPORT_ITEM_TYPES[report_item["type"]]
            acceptance = _normalize_report_acceptance(
                acceptances.get(report_item["id"])
            )
            report_item.update(acceptance)
            report_item["media_summary"] = _media_report_item_text(report_item)
        for display_index, report_item in enumerate(report_items, start=1):
            report_item["index"] = display_index
        item["result_summary"] = (
            guarded_report_summary(report_items)
            if filtered_visual_missing_count
            else _result_report_summary(item, structured_report)
        )
        item["report_items"] = report_items
        item["report_limit"] = report_limit
        item["suppressed_report_items"] = suppressed_items
        item["report_counts"] = _count_report_items(
            report_items, suppressed_count=len(suppressed_items)
        )
        prepared.append(item)
    return prepared


def _enabled_report_suppression_rules(task_type: str | None) -> dict[str, list[dict]]:
    if not task_type:
        return {}
    rows = (
        get_db()
        .execute(
            """
        SELECT id, check_code, reason, item_json
        FROM report_suppression_rules
        WHERE task_type = ? AND enabled = 1
        """,
            (task_type,),
        )
        .fetchall()
    )
    rules = {}
    for row in rows:
        snapshot = _parse_report_suppression_item_json(row["item_json"])
        description = _report_suppression_description(snapshot)
        if not description:
            continue
        rules.setdefault(row["check_code"], []).append(
            {
                "id": row["id"],
                "reason": row["reason"] or "",
                "description": description,
            }
        )
    return rules


def _apply_report_suppression(
    *,
    task_type: str | None,
    task_id: int | None,
    result_code: str,
    report_items: list[dict],
    suppression_rules: dict[str, list[dict]],
    record_hits: bool,
) -> tuple[list[dict], list[dict]]:
    if not task_type or not suppression_rules:
        return report_items, []

    visible_items = []
    suppressed_items = []
    db = get_db() if record_hits and task_id is not None else None
    for item in report_items:
        rule, similarity = _matching_report_suppression_rule(
            item,
            suppression_rules.get(result_code, []),
        )
        if rule is None:
            visible_items.append(item)
            continue
        suppressed = dict(item)
        suppressed["suppression_rule_id"] = rule["id"]
        suppressed["suppression_reason"] = rule["reason"]
        suppressed["suppression_similarity"] = similarity
        suppressed_items.append(suppressed)
        if db is not None:
            _record_report_suppression_hit(
                db,
                rule_id=int(rule["id"]),
                task_id=int(task_id),
                result_code=result_code,
                item=suppressed,
            )
    return visible_items, suppressed_items


def _matching_report_suppression_rule(
    item: dict, rules: list[dict]
) -> tuple[dict | None, float]:
    description = _report_suppression_description(item)
    if not description:
        return None, 0.0
    best_rule = None
    best_similarity = 0.0
    for rule in rules:
        similarity = _report_description_similarity(
            description, rule.get("description")
        )
        if similarity > best_similarity:
            best_rule = rule
            best_similarity = similarity
    if best_similarity < REPORT_SUPPRESSION_DESCRIPTION_SIMILARITY_THRESHOLD:
        return None, best_similarity
    return best_rule, best_similarity


def _report_description_similarity(left, right) -> float:
    left_text = _normalize_report_description(left)
    right_text = _normalize_report_description(right)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    shorter, longer = sorted((left_text, right_text), key=len)
    if len(shorter) >= 6 and shorter in longer:
        return 0.95
    if len(shorter) < 4:
        return 0.0
    sequence_score = SequenceMatcher(None, left_text, right_text).ratio()
    character_score = _report_description_dice(set(left_text), set(right_text)) * 0.9
    bigram_score = _report_description_dice(
        _report_description_ngrams(left_text, 2),
        _report_description_ngrams(right_text, 2),
    )
    return max(sequence_score, character_score, bigram_score)


def _normalize_report_description(value) -> str:
    text = str(value or "").strip().lower()
    for source, replacement in REPORT_SUPPRESSION_DESCRIPTION_REPLACEMENTS:
        text = text.replace(source, replacement)
    return re.sub(r"[\W_]+", "", text)


def _report_description_ngrams(text: str, size: int) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def _report_description_dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def _record_report_suppression_hit(
    db, *, rule_id: int, task_id: int, result_code: str, item: dict
):
    now = now_text()
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO report_suppression_hits(rule_id, task_id, result_code, item_id, item_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            rule_id,
            task_id,
            result_code,
            str(item.get("id") or ""),
            json.dumps(_report_suppression_item_snapshot(item), ensure_ascii=False),
            now,
        ),
    )
    if cursor.rowcount:
        db.execute(
            """
            UPDATE report_suppression_rules
            SET hit_count = hit_count + 1, last_hit_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, rule_id),
        )
        db.commit()


def _maybe_create_report_suppression_candidate(
    db,
    *,
    task,
    result_code: str,
    result: dict,
    item_id: str,
    item_type: str,
    acceptance_status: str | None,
    rejection_reason: str,
    rejection_note: str,
) -> bool:
    if (
        item_type != "non_issue"
        or acceptance_status != "rejected"
        or rejection_reason not in REPORT_SUPPRESSION_REJECTION_REASONS
    ):
        return False

    report_item = next(
        (item for item in _result_report_items(result) if item.get("id") == item_id),
        None,
    )
    if report_item is None:
        return False

    task_type = task["task_type"] or DOCUMENT_TASK_TYPE
    fingerprint = _report_item_suppression_fingerprint(
        task_type, result_code, report_item
    )
    now = now_text()
    reason = REPORT_REJECTION_REASONS.get(rejection_reason, "") or rejection_note
    snapshot = _report_suppression_item_snapshot(report_item)
    db.execute(
        """
        INSERT INTO report_suppression_rules(
            task_type, check_code, fingerprint, item_json, reason, enabled,
            source_task_id, source_result_code, source_item_id,
            hit_count, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(task_type, check_code, fingerprint) DO UPDATE SET
            item_json = excluded.item_json,
            reason = CASE
                WHEN report_suppression_rules.reason IS NULL OR report_suppression_rules.reason = ''
                THEN excluded.reason
                ELSE report_suppression_rules.reason
            END,
            updated_at = excluded.updated_at
        """,
        (
            task_type,
            result_code,
            fingerprint,
            json.dumps(snapshot, ensure_ascii=False),
            reason,
            task["id"],
            result_code,
            item_id,
            now,
            now,
        ),
    )
    return True


def _report_item_suppression_fingerprint(
    task_type: str, result_code: str, item: dict
) -> str:
    source = {
        "task_type": str(task_type or ""),
        "check_code": str(result_code or ""),
        "description": _report_suppression_description(item),
    }
    raw = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _report_suppression_description(item: dict) -> str:
    return _normalize_suppression_text(item.get("description") or item.get("text"))


def _report_suppression_item_snapshot(item: dict) -> dict:
    fields = _report_suppression_item_fields(item)
    return {
        **fields,
        "text": _normalize_suppression_text(item.get("text")),
        "type": _normalize_report_item_type(item.get("type")) or "issue",
    }


def _report_suppression_item_fields(item: dict) -> dict:
    return {
        field: _normalize_suppression_text(item.get(field))
        for field in REPORT_SUPPRESSION_FIELDS
    }


def _normalize_suppression_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _result_report_items(
    result: dict, structured_report: dict | None = None
) -> list[dict]:
    code = str(result.get("code") or "")
    if structured_report is None:
        structured_report = _result_structured_report(result)
    if structured_report is not None:
        return _structured_report_items(code, structured_report)

    text = str(result.get("result") or "").strip()
    if not text:
        return []
    chunks = _extract_report_item_chunks(text) or [text]
    items = []
    for index, chunk in enumerate(chunks, start=1):
        item_text = chunk.strip()
        if not item_text:
            continue
        fields = _legacy_report_item_fields(item_text)
        items.append(
            {
                "id": _report_item_id(code, index, item_text),
                "index": index,
                "text": item_text,
                **fields,
                "type": _infer_report_item_type(item_text),
            }
        )
    return items


def _result_structured_report(result: dict) -> dict | None:
    for key in ("structured_report", "report_json"):
        structured = _normalize_structured_report_payload(result.get(key))
        if structured is not None:
            return structured

    structured_items = result.get("structured_items")
    if isinstance(structured_items, list):
        summary = _first_report_field(result, REPORT_JSON_SUMMARY_KEYS)
        return {"summary": summary, "items": structured_items}

    return _normalize_structured_report_payload(result.get("result"))


def _result_report_summary(result: dict, structured_report: dict | None) -> str:
    if structured_report is not None:
        return str(structured_report.get("summary") or "").strip()
    return ""


def _normalize_structured_report_payload(value) -> dict | None:
    payload = value
    if isinstance(value, str):
        payload = _parse_structured_report_json(value)
    if isinstance(payload, list):
        return {"summary": "", "items": payload}
    if not isinstance(payload, dict):
        return None

    items = None
    for key in REPORT_JSON_ITEM_KEYS:
        candidate = payload.get(key)
        if isinstance(candidate, list):
            items = candidate
            break
        if isinstance(candidate, str):
            parsed_items = _parse_structured_report_json(candidate)
            if isinstance(parsed_items, list):
                items = parsed_items
                break
    summary = _first_report_field(payload, REPORT_JSON_SUMMARY_KEYS)
    if items is None:
        if any(
            _first_report_field(payload, aliases)
            for aliases in REPORT_FIELD_ALIASES.values()
        ):
            items = [payload]
        elif summary:
            items = []
        else:
            return None
    return {"summary": summary, "items": items}


def _parse_structured_report_json(text: str, depth: int = 0):
    text = str(text or "").strip()
    if not text:
        return None
    for candidate in _structured_json_candidates(text):
        parsed = _load_structured_json_candidate(candidate, depth)
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _load_structured_json_candidate(candidate: str, depth: int):
    if depth > 3:
        return None
    variants = _json_candidate_variants(candidate)
    for variant in variants:
        try:
            parsed = json.loads(variant)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, str):
            nested = _parse_structured_report_json(parsed, depth + 1)
            if nested is not None:
                return nested
            continue
        return parsed
    for variant in variants:
        parsed = _parse_truncated_structured_report_candidate(variant)
        if parsed is not None:
            return parsed
    return None


def _parse_truncated_structured_report_candidate(candidate: str) -> dict | None:
    text = str(candidate or "").strip()
    object_start = text.find("{")
    if object_start < 0:
        return None
    text = text[object_start:]

    item_array_matches = []
    for key in REPORT_JSON_ITEM_KEYS:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*\[', text)
        if match:
            item_array_matches.append((match.start(), match.end() - 1, key))
    if not item_array_matches:
        return None

    _, array_start, item_key = min(item_array_matches)
    try:
        payload = json.loads(f"{text[: array_start + 1]}]}}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    decoder = json.JSONDecoder()
    items = []
    position = array_start + 1
    while position < len(text):
        while position < len(text) and (
            text[position].isspace() or text[position] == ","
        ):
            position += 1
        if position >= len(text) or text[position] == "]":
            break
        try:
            item, end = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            break
        if isinstance(item, (dict, str)):
            items.append(item)
        position = end

    if not items:
        return None
    payload[item_key] = items
    return payload


def _json_candidate_variants(candidate: str) -> list[str]:
    raw = str(candidate or "").strip()
    if not raw:
        return []
    variants = [raw]
    repaired = _escape_json_string_control_chars(raw)
    if repaired != raw:
        variants.append(repaired)
    for variant in tuple(variants):
        repaired = _repair_common_json_model_errors(variant)
        if repaired != variant and repaired not in variants:
            variants.append(repaired)
    return variants


def _repair_common_json_model_errors(text: str) -> str:
    status_values = "|".join(re.escape(value) for value in REPORT_ITEM_TYPES)
    status_keys = r"status|classification|item_type|type"
    return re.sub(
        rf'("(?:(?:{status_keys}))"\s*:\s*")({status_values})"\s*:\s*"({status_values})(")',
        r"\1\3\4",
        str(text or ""),
    )


def _escape_json_string_control_chars(text: str) -> str:
    result = []
    in_string = False
    escaped = False
    for char in str(text or ""):
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            result.append(char)
            continue
        if in_string and char in {"\n", "\r", "\t"}:
            result.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
            continue
        result.append(char)
    return "".join(result)


def _structured_json_candidates(text: str) -> list[str]:
    candidates = [text]
    for match in re.finditer(
        r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL
    ):
        candidates.append(match.group(1).strip())
    object_start = text.find("{")
    object_end = text.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(text[object_start : object_end + 1])
    array_start = text.find("[")
    array_end = text.rfind("]")
    if array_start >= 0 and array_end > array_start:
        candidates.append(text[array_start : array_end + 1])

    seen = set()
    unique = []
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _structured_report_items(result_code: str, structured_report: dict) -> list[dict]:
    raw_items = structured_report.get("items")
    if not isinstance(raw_items, list):
        return []

    items = []
    for raw_item in raw_items:
        fields = _normalize_structured_report_item(raw_item)
        if not fields:
            continue
        index = len(items) + 1
        item_text = _structured_report_item_text(fields)
        item_type = fields.pop("type", "") or _infer_report_item_type(item_text)
        explicit_id = _structured_report_item_id(raw_item)
        items.append(
            {
                "id": explicit_id or _report_item_id(result_code, index, item_text),
                "index": index,
                "text": item_text,
                **fields,
                "type": item_type,
            }
        )
    return items


def _normalize_structured_report_item(raw_item) -> dict:
    if isinstance(raw_item, str):
        text = raw_item.strip()
        parsed = _parse_structured_report_json(text)
        if isinstance(parsed, dict):
            return _normalize_structured_report_item(parsed)
        return (
            {
                "severity": "",
                "severity_label": "",
                "confidence": "",
                "confidence_label": "",
                "category": "",
                "location": "",
                "excerpt": "",
                "description": text,
                "impact": "",
                "suggestion": "",
                "evidence_refs": [],
                "type": _infer_report_item_type(text),
            }
            if text
            else {}
        )
    if not isinstance(raw_item, dict):
        return {}

    status = _normalize_report_item_status(
        _first_report_field(raw_item, REPORT_STATUS_KEYS)
    )
    fields = {
        field: _first_report_field(raw_item, aliases)
        for field, aliases in REPORT_FIELD_ALIASES.items()
    }
    fields["severity"] = _normalize_report_severity(fields.get("severity"))
    fields["severity_label"] = REPORT_SEVERITY_LABELS.get(fields["severity"], "")
    fields["confidence"] = _normalize_report_confidence(fields.get("confidence"))
    fields["confidence_label"] = REPORT_CONFIDENCE_LABELS.get(fields["confidence"], "")
    fields["evidence_refs"] = _normalize_report_evidence_refs(
        raw_item.get("evidence_refs")
    )
    if _looks_like_status_only(fields["category"]):
        status = status or _normalize_report_item_status(fields["category"])
        fields["category"] = ""
    if not any(fields.get(field) for field in REPORT_SUPPRESSION_FIELDS):
        fields["description"] = _report_field_text(raw_item)
    item_text = _structured_report_item_text(fields)
    fields["type"] = (
        "non_issue"
        if _is_no_action_report_item(fields)
        else status or _infer_report_item_type(item_text)
    )
    return fields


def _structured_report_item_id(raw_item) -> str:
    if not isinstance(raw_item, dict):
        return ""
    value = str(
        raw_item.get("id")
        or raw_item.get("item_id")
        or raw_item.get("report_item_id")
        or ""
    ).strip()
    if not value or len(value) > 128:
        return ""
    return value if re.fullmatch(r"[A-Za-z0-9._:-]+", value) else ""


def _normalize_report_evidence_refs(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    refs = []
    seen = set()
    for raw_ref in value:
        if not isinstance(raw_ref, dict):
            continue
        media_id = str(
            raw_ref.get("id")
            or raw_ref.get("frame_id")
            or raw_ref.get("filename")
            or ""
        ).strip()
        filename = str(raw_ref.get("filename") or "").strip()
        position = str(
            raw_ref.get("position") or raw_ref.get("timestamp") or ""
        ).strip()
        if not media_id and not filename and not position:
            continue
        key = media_id or filename or position
        if key in seen:
            continue
        seen.add(key)
        timestamp_seconds = raw_ref.get("timestamp_seconds")
        try:
            timestamp_seconds = (
                None
                if timestamp_seconds is None
                else max(0.0, float(timestamp_seconds))
            )
        except (TypeError, ValueError):
            timestamp_seconds = None
        refs.append(
            {
                "id": media_id,
                "filename": filename,
                "position": position,
                "timestamp_seconds": timestamp_seconds,
                "relative_path": str(raw_ref.get("relative_path") or "").strip(),
                "mime_type": str(raw_ref.get("mime_type") or "").strip(),
                "kind": str(raw_ref.get("kind") or "").strip(),
            }
        )
    refs.sort(
        key=lambda ref: (
            ref.get("timestamp_seconds") is None,
            float(ref.get("timestamp_seconds") or 0),
            str(ref.get("filename") or ""),
        )
    )
    return refs


def _normalize_report_severity(value) -> str:
    compact = _compact_report_text(value)
    aliases = {
        "critical": "critical",
        "fatal": "critical",
        "blocker": "critical",
        "致命": "critical",
        "灾难性": "critical",
        "极高": "critical",
        "high": "high",
        "严重": "high",
        "重大": "high",
        "高": "high",
        "medium": "medium",
        "middle": "medium",
        "moderate": "medium",
        "一般": "medium",
        "中": "medium",
        "low": "low",
        "minor": "low",
        "轻微": "low",
        "低": "low",
    }
    return aliases.get(compact, "")


def _normalize_report_confidence(value) -> str:
    compact = _compact_report_text(value)
    aliases = {
        "high": "high",
        "certain": "high",
        "明确": "high",
        "高": "high",
        "medium": "medium",
        "middle": "medium",
        "moderate": "medium",
        "较高": "medium",
        "中": "medium",
        "low": "low",
        "uncertain": "low",
        "较低": "low",
        "低": "low",
    }
    return aliases.get(compact, "")


def _deduplicate_report_items(report_items: list[dict]) -> list[dict]:
    unique_items = []
    items_by_key = {}
    for report_item in report_items:
        key = (
            _normalize_suppression_text(report_item.get("type")),
            _normalize_suppression_text(report_item.get("category")),
            _normalize_suppression_text(report_item.get("excerpt")),
            _normalize_suppression_text(report_item.get("description")),
            _normalize_suppression_text(report_item.get("impact")),
            _normalize_suppression_text(report_item.get("suggestion")),
        )
        if not any(key[1:]):
            unique_items.append(report_item)
            continue
        existing = items_by_key.get(key)
        if existing is None:
            items_by_key[key] = report_item
            unique_items.append(report_item)
            continue
        existing["location"] = _merge_report_field_values(
            existing.get("location"), report_item.get("location")
        )
        existing["evidence_refs"] = _merge_report_evidence_refs(
            existing.get("evidence_refs"),
            report_item.get("evidence_refs"),
        )
        if _report_item_priority_key(report_item) < _report_item_priority_key(existing):
            existing["severity"] = report_item.get("severity", "")
            existing["severity_label"] = report_item.get("severity_label", "")
            existing["confidence"] = report_item.get("confidence", "")
            existing["confidence_label"] = report_item.get("confidence_label", "")
    return unique_items


def _merge_report_field_values(left, right) -> str:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text:
        return right_text
    if not right_text or right_text in left_text:
        return left_text
    return f"{left_text}；{right_text}"


def _merge_report_evidence_refs(left, right) -> list[dict]:
    return _normalize_report_evidence_refs(list(left or []) + list(right or []))


def _report_item_priority_key(report_item: dict) -> tuple[int, int, int, int]:
    item_type_order = {"issue": 0, "suggestion": 1, "non_issue": 2}
    item_type = str(report_item.get("type") or "")
    return (
        item_type_order.get(item_type, len(item_type_order)),
        REPORT_CONFIDENCE_ORDER.get(
            str(report_item.get("confidence") or ""), len(REPORT_CONFIDENCE_ORDER)
        ),
        REPORT_SEVERITY_ORDER.get(
            str(report_item.get("severity") or ""), len(REPORT_SEVERITY_ORDER)
        ),
        int(report_item.get("index") or 0),
    )


def _limit_ranked_report_items(
    report_items: list[dict],
    *,
    issue_output_limit,
    original_count: int,
    duplicate_count: int,
) -> tuple[list[dict], dict | None]:
    limit = normalize_issue_output_limit(issue_output_limit)

    before_limit_count = len(report_items)
    omitted_count = 0
    limit_applied = False
    if before_limit_count > limit:
        omitted_count = before_limit_count - limit
        report_items = report_items[:limit]
        limit_applied = True

    if not duplicate_count and not omitted_count:
        return report_items, None
    return report_items, {
        "limit": limit,
        "original_count": original_count,
        "duplicate_count": duplicate_count,
        "deduplicated_count": original_count - duplicate_count,
        "displayed_count": len(report_items),
        "omitted_count": omitted_count,
        "limit_applied": limit_applied,
        "missing_ranking": False,
    }


def _first_report_field(source: dict, aliases: tuple[str, ...]) -> str:
    if not isinstance(source, dict):
        return ""
    for key in aliases:
        if key in source:
            text = _report_field_text(source.get(key))
            if text:
                return text
    return ""


def _report_field_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _report_field_text(item)
            if text:
                parts.append(text)
        return "；".join(parts)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _structured_report_item_text(fields: dict) -> str:
    lines = []
    for key, label in REPORT_ITEM_FIELDS:
        value = str(fields.get(key) or "").strip()
        if value:
            lines.append(f"{label}：{value}")
    return "\n".join(lines).strip()


def _is_no_action_report_item(fields: dict) -> bool:
    impact = _compact_report_text(fields.get("impact"))
    suggestion = _compact_report_text(fields.get("suggestion"))
    return _has_report_marker(
        impact, REPORT_NO_ACTION_IMPACT_MARKERS
    ) and _has_report_marker(
        suggestion,
        REPORT_NO_ACTION_SUGGESTION_MARKERS,
    )


def _compact_report_text(value) -> str:
    return re.sub(
        r"[\s，。；、,.!！?？:：;；/\\|()（）【】\\[\\]\"'“”‘’_-]+",
        "",
        str(value or ""),
    ).lower()


def _has_report_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _legacy_report_item_fields(text: str) -> dict:
    fields = {key: "" for key, _ in REPORT_ITEM_FIELDS}
    body_lines = []
    for raw_line in str(text or "").splitlines():
        line = _clean_report_item_line(raw_line)
        if not line:
            continue
        match = re.match(r"^([^:：]{1,28})[:：]\s*(.*)$", line)
        if match:
            label = match.group(1).strip()
            value = match.group(2).strip()
            field = REPORT_LEGACY_LABEL_FIELDS.get(label)
            if field and value:
                fields[field] = _append_report_field(fields[field], value)
                continue
        body_lines.append(line)
    if not fields["description"]:
        fields["description"] = (
            "\n".join(body_lines).strip()
            or fields["suggestion"]
            or str(text or "").strip()
        )
    return fields


def _clean_report_item_line(line: str) -> str:
    stripped = REPORT_ITEM_PREFIX_RE.sub("", str(line or "").strip()).strip()
    stripped = re.sub(r"^(?:\d{1,3}[.、)]|\(\d{1,3}\)|（\d{1,3}）)\s*", "", stripped)
    return stripped.strip("*_`~ ")


def _append_report_field(current: str, value: str) -> str:
    current = str(current or "").strip()
    value = str(value or "").strip()
    if not current:
        return value
    if not value or value in current.split("；"):
        return current
    return f"{current}；{value}"


def _looks_like_status_only(value: str) -> bool:
    compact = re.sub(r"\s+", "", str(value or "")).strip()
    return compact.lower() in {
        "issue",
        "problem",
        "suggestion",
        "advice",
        "non_issue",
        "nonissue",
        "not_issue",
    } or compact in {
        "问题",
        "明确问题",
        "建议",
        "需人工确认",
        "非问题",
        "不是问题",
    }


def _extract_report_item_chunks(text: str) -> list[str]:
    chunks = []
    current = []
    for line in str(text or "").splitlines():
        if _is_report_item_start(line):
            if current:
                chunks.append("\n".join(current).strip())
            current = [line]
            continue
        if current and _is_report_auxiliary_section_start(line):
            chunks.append("\n".join(current).strip())
            current = []
            continue
        if current:
            current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def _is_report_item_start(line: str) -> bool:
    stripped = str(line or "").strip()
    if not stripped:
        return False
    if stripped.startswith(("|", "```", ">")):
        return False
    match_text = REPORT_ITEM_PREFIX_RE.sub("", stripped).strip()
    return bool(REPORT_ITEM_START_RE.match(match_text))


def _is_report_auxiliary_section_start(line: str) -> bool:
    stripped = REPORT_ITEM_PREFIX_RE.sub("", str(line or "").strip()).strip("*_`~ ")
    if not stripped:
        return False
    if stripped in {"总体判断", "明确问题", "需人工确认"}:
        return True
    return stripped.startswith(
        (
            "页面级检查结果",
            "图文联合检查结果",
            "图片检查结果",
            "视频帧检查结果",
            "检查汇总",
            "覆盖图片",
            "覆盖视频帧",
            "已跳过的图片",
            "系统需人工确认",
        )
    )


def _report_item_id(result_code: str, index: int, text: str) -> str:
    source = f"{result_code}\n{index}\n{text}"
    return hashlib.sha1(source.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _infer_report_item_type(text: str) -> str:
    compact = re.sub(r"\s+", "", str(text or ""))
    if any(
        marker in compact
        for marker in (
            "非问题",
            "未发现",
            "无明显",
            "未见明显",
            "无需修改",
            "未见异常",
            "无异常",
        )
    ):
        return "non_issue"
    if any(
        marker in compact
        for marker in (
            "需人工确认",
            "人工确认",
            "疑似",
            "不确定",
            "证据不足",
            "建议核实",
            "建议复核",
            "看不清",
            "无法确认",
        )
    ):
        return "suggestion"
    issue_markers = (
        "问题",
        "错误",
        "不一致",
        "矛盾",
        "冲突",
        "缺失",
        "风险",
        "不规范",
        "不匹配",
        "异常",
    )
    if "建议" in compact and not any(marker in compact for marker in issue_markers):
        return "suggestion"
    if any(marker in compact for marker in issue_markers):
        return "issue"
    return "suggestion"


def _normalize_report_item_status(value) -> str | None:
    exact = _normalize_report_item_type(value)
    if exact:
        return exact
    compact = re.sub(r"\s+", "", str(value or "")).strip().lower()
    if not compact:
        return None
    if any(
        marker in compact
        for marker in (
            "non_issue",
            "nonissue",
            "not_issue",
            "非问题",
            "不是问题",
            "无需修改",
            "无问题",
        )
    ):
        return "non_issue"
    if any(
        marker in compact
        for marker in (
            "suggestion",
            "advise",
            "manual",
            "uncertain",
            "建议",
            "需人工确认",
            "人工确认",
            "疑似",
            "不确定",
            "证据不足",
        )
    ):
        return "suggestion"
    if any(
        marker in compact
        for marker in (
            "issue",
            "problem",
            "明确问题",
            "问题",
            "错误",
            "不一致",
            "缺失",
            "冲突",
        )
    ):
        return "issue"
    return None


def _normalize_report_item_type(value) -> str | None:
    value = str(value or "").strip()
    return value if value in REPORT_ITEM_TYPES else None


def _normalize_report_acceptance_status(value) -> str | None:
    value = str(value or "").strip()
    return value if value in REPORT_ACCEPTANCE_STATUSES else None


def _normalize_report_rejection_reason(value) -> str:
    value = str(value or "").strip()
    return value if value in REPORT_REJECTION_REASONS else ""


def _normalize_report_acceptance(value) -> dict:
    if not isinstance(value, dict):
        status = _normalize_report_acceptance_status(value) or "pending"
        return {
            "acceptance_status": status,
            "acceptance_label": REPORT_ACCEPTANCE_STATUSES[status],
            "rejection_reason": "",
            "rejection_reason_label": "",
            "rejection_note": "",
        }

    status = _normalize_report_acceptance_status(value.get("status")) or "pending"
    reason = (
        _normalize_report_rejection_reason(value.get("rejection_reason"))
        if status == "rejected"
        else ""
    )
    note = (
        str(value.get("rejection_note") or "").strip() if status == "rejected" else ""
    )
    return {
        "acceptance_status": status,
        "acceptance_label": REPORT_ACCEPTANCE_STATUSES[status],
        "rejection_reason": reason,
        "rejection_reason_label": REPORT_REJECTION_REASONS.get(reason, ""),
        "rejection_note": note,
    }


def _report_rate_label(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "-"
    return f"{numerator / denominator * 100:.1f}%"


def _finalize_report_counts(counts: dict) -> dict:
    counts["total"] = sum(int(counts.get(key) or 0) for key in REPORT_ITEM_TYPE_ORDER)
    confirmed_issues = int(counts.get("accepted_issue") or 0) + int(
        counts.get("rejected_issue") or 0
    )
    counts["issue_detection_rate"] = _report_rate_label(
        int(counts.get("issue") or 0), counts["total"]
    )
    counts["issue_acceptance_rate"] = _report_rate_label(
        int(counts.get("accepted_issue") or 0), confirmed_issues
    )
    return counts


def _count_report_items(items: list[dict], *, suppressed_count: int = 0) -> dict:
    counts = {key: 0 for key in REPORT_COUNT_KEYS}
    counts["suppressed"] = max(0, int(suppressed_count or 0))
    for item in items:
        item_type = _normalize_report_item_type(item.get("type")) or "issue"
        counts[item_type] += 1
        acceptance_status = (
            _normalize_report_acceptance_status(item.get("acceptance_status"))
            or "pending"
        )
        if acceptance_status == "pending":
            counts["pending_review"] += 1
        else:
            counts["reviewed"] += 1
        if item_type == "issue":
            if acceptance_status == "accepted":
                counts["accepted_issue"] += 1
            elif acceptance_status == "rejected":
                counts["rejected_issue"] += 1
            else:
                counts["pending_issue_acceptance"] += 1
    return _finalize_report_counts(counts)


def _report_item_totals(results: list[dict]) -> dict:
    totals = {key: 0 for key in REPORT_COUNT_KEYS}
    for result in results:
        counts = result.get("report_counts") or {}
        for key in REPORT_COUNT_KEYS:
            totals[key] += int(counts.get(key) or 0)
    return _finalize_report_counts(totals)


def update_report_item_type(task, data):
    if task["status"] in {"queued", "running", "canceling"}:
        return {"ok": False, "error": "任务尚未完成，暂不能修改报告条目判定。"}, 409
    result_code = str(data.get("result_code") or "").strip()
    item_id = str(data.get("item_id") or "").strip()
    item_type = _normalize_report_item_type(data.get("item_type"))
    if not result_code or not item_id or not item_type:
        return {"ok": False, "error": "报告条目判定数据无效。"}, 400
    acceptance_supplied = "acceptance_status" in data
    acceptance_status = None
    rejection_reason = ""
    rejection_note = ""
    if acceptance_supplied:
        acceptance_status = _normalize_report_acceptance_status(
            data.get("acceptance_status")
        )
        rejection_reason = _normalize_report_rejection_reason(
            data.get("rejection_reason")
        )
        rejection_note = str(data.get("rejection_note") or "").strip()
        if acceptance_status is None:
            return {"ok": False, "error": "接纳状态数据无效。"}, 400
        if acceptance_status == "rejected":
            if not rejection_reason:
                return {"ok": False, "error": "选择不认可时必须选择原因。"}, 400
            if rejection_reason == "other" and not rejection_note:
                return {"ok": False, "error": "选择其他原因时必须填写具体原因。"}, 400

    results = _raw_task_results(task)
    target = None
    valid_item_ids = set()
    for result in results:
        if str(result.get("code") or "") != result_code:
            continue
        target = result
        valid_item_ids = {item["id"] for item in _result_report_items(result)}
        break
    if target is None or item_id not in valid_item_ids:
        return {"ok": False, "error": "报告条目不存在。"}, 404

    db = get_db()
    suppression_candidate_created = _apply_report_item_review(
        db,
        task=task,
        result_code=result_code,
        result=target,
        item_id=item_id,
        item_type=item_type,
        acceptance_supplied=acceptance_supplied,
        acceptance_status=acceptance_status,
        rejection_reason=rejection_reason,
        rejection_note=rejection_note,
    )
    task_updated_at = now_text()
    db.execute(
        "UPDATE tasks SET result_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(results, ensure_ascii=False), task_updated_at, task["id"]),
    )
    db.commit()

    prepared = _prepare_task_results(
        results,
        task_type=task["task_type"] or DOCUMENT_TASK_TYPE,
        task_id=task["id"],
    )
    _cache_prepared_task_report_stats(task, prepared, task_updated_at)
    updated_result = next(
        (item for item in prepared if str(item.get("code") or "") == result_code), None
    )
    saved_acceptances = target.get("item_acceptances")
    if not isinstance(saved_acceptances, dict):
        saved_acceptances = {}
    updated_acceptance = _normalize_report_acceptance(saved_acceptances.get(item_id))
    return {
        "ok": True,
        "item_id": item_id,
        "item_type": item_type,
        "item_type_label": REPORT_ITEM_TYPES[item_type],
        **updated_acceptance,
        "result_counts": (updated_result or {}).get("report_counts", {}),
        "totals": _report_item_totals(prepared),
        "suppression_candidate_created": suppression_candidate_created,
    }


def _apply_report_item_review(
    db,
    *,
    task,
    result_code: str,
    result: dict,
    item_id: str,
    item_type: str,
    acceptance_supplied: bool,
    acceptance_status: str | None,
    rejection_reason: str,
    rejection_note: str,
) -> bool:
    classifications = result.get("item_classifications")
    if not isinstance(classifications, dict):
        classifications = {}
    classifications[item_id] = item_type
    result["item_classifications"] = classifications

    if acceptance_supplied:
        acceptances = result.get("item_acceptances")
        if not isinstance(acceptances, dict):
            acceptances = {}
        if acceptance_status == "pending":
            acceptances.pop(item_id, None)
        else:
            record = {"status": acceptance_status}
            if acceptance_status == "rejected":
                record["rejection_reason"] = rejection_reason
                record["rejection_note"] = rejection_note
            acceptances[item_id] = record
        result["item_acceptances"] = acceptances

    return _maybe_create_report_suppression_candidate(
        db,
        task=task,
        result_code=result_code,
        result=result,
        item_id=item_id,
        item_type=item_type,
        acceptance_status=acceptance_status if acceptance_supplied else None,
        rejection_reason=rejection_reason,
        rejection_note=rejection_note,
    )
