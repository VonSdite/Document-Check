"""报告逐条初审、终审、版本校验与操作记录。"""

import hashlib
import json
from contextlib import contextmanager

from app.contracts.task_types import DOCUMENT_TASK_TYPE
from app.persistence.connection import get_db, now_text
from app.reporting.constants import REPORT_ITEM_FIELDS, REPORT_ITEM_TYPES
from app.reporting.service import (
    _count_report_items,
    _media_report_item_text,
    _normalize_report_acceptance,
    _prepare_task_results,
    _raw_task_results,
    _result_report_items,
)

FINAL_DECISIONS = {
    "confirmed": "确认初审",
    "returned": "提单修改",
    "deferred": "暂无法判断",
}
FINAL_STATUSES = {
    "unavailable": "检查中",
    "empty": "无审核条目",
    "not_required": "无需终审",
    "awaiting_initial": "待初审",
    "pending": "待终审",
    "in_progress": "终审中",
    "returned": "待提单修改",
    "deferred": "待补充依据",
    "completed": "终审完成",
}
ACTIVE_STATUSES = {"queued", "running", "canceling"}


class ReviewConflict(ValueError):
    """审核请求所依据的状态需要更新。"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def item_fingerprint(item):
    fields = {
        key: item.get(key)
        for key in (
            "type",
            "text",
            "category",
            "location",
            "excerpt",
            "description",
            "impact",
            "suggestion",
            "severity",
            "confidence",
            "evidence_refs",
            "source_items",
            "source_rounds",
        )
    }
    return hashlib.sha256(_json(fields).encode("utf-8")).hexdigest()


def review_events(task_id):
    rows = get_db().execute(
        "SELECT * FROM report_review_events WHERE task_id = ? ORDER BY id", (task_id,)
    )
    grouped = {}
    for row in rows:
        event = dict(row)
        event["acceptance"] = json.loads(event["acceptance_json"])
        event["item"] = json.loads(event["item_json"])
        event["decision_label"] = (
            FINAL_DECISIONS.get(event["decision"], event["decision"])
            if event["stage"] == "final"
            else event["acceptance"]["acceptance_label"]
        )
        grouped.setdefault((event["result_code"], event["item_id"]), []).append(event)
    return grouped


def item_review_state(item, acceptance, history):
    fingerprint = item_fingerprint(item)
    requires_final = acceptance["acceptance_status"] == "rejected"
    initial = next((e for e in reversed(history) if e["stage"] == "initial"), None)
    final = next((e for e in reversed(history) if e["stage"] == "final"), None)
    initial_current = bool(
        initial
        and initial["item_hash"] == fingerprint
        and initial["acceptance"] == acceptance
        and acceptance["acceptance_status"] != "pending"
    )
    final_current = bool(
        requires_final
        and initial_current
        and final
        and final["initial_review_id"] == initial["id"]
        and final["item_hash"] == fingerprint
    )
    status = final["decision"] if final_current else "pending"
    if not requires_final:
        status = "not_required"
    elif not initial_current:
        status = "awaiting_initial"
    token = hashlib.sha256(
        _json(
            [
                fingerprint,
                acceptance,
                history[-1]["id"] if history else 0,
            ]
        ).encode("utf-8")
    ).hexdigest()
    return {
        "token": token,
        "requires_final": requires_final,
        "status": status,
        "status_label": FINAL_DECISIONS.get(status, FINAL_STATUSES.get(status)),
        "initial": initial if initial_current else None,
        "final": final if final_current else None,
        "history": list(reversed(history)),
    }


def review_context(task):
    history = review_events(task["id"])
    current_keys = set()
    results = []
    counts = dict.fromkeys(
        (
            "total",
            "initial_count",
            "required_count",
            "confirmed",
            "returned",
            "deferred",
            "pending",
        ),
        0,
    )
    required_initial_count = 0
    for result in _raw_task_results(task):
        code = str(result.get("code") or "")
        acceptances = result.get("item_acceptances") or {}
        if not isinstance(acceptances, dict):
            acceptances = {}
        items = []
        for item in _result_report_items(result):
            if item.get("type") not in {"issue", "suggestion"}:
                continue
            current_keys.add((code, item["id"]))
            item.update(_normalize_report_acceptance(acceptances.get(item["id"])))
            item["type_label"] = REPORT_ITEM_TYPES[item["type"]]
            item["review"] = item_review_state(
                item,
                _normalize_report_acceptance(acceptances.get(item["id"])),
                history.get((code, item["id"]), []),
            )
            counts["total"] += 1
            counts["initial_count"] += int(item["review"]["initial"] is not None)
            if item["review"]["requires_final"]:
                counts["required_count"] += 1
                required_initial_count += int(item["review"]["initial"] is not None)
                state = item["review"]["status"]
                counts[state if state in FINAL_DECISIONS else "pending"] += 1
            items.append(item)
        results.append({**result, "report_items": items})
    counts["status"] = _progress_status(task["status"], counts, required_initial_count)
    archived = [
        event
        for key, entries in history.items()
        if key not in current_keys
        for event in entries
    ]
    return {
        "results": results,
        "progress": present_progress(counts),
        "archived_history": sorted(
            archived, key=lambda event: event["id"], reverse=True
        ),
    }


def _progress_status(task_status, counts, required_initial_count):
    if task_status in ACTIVE_STATUSES:
        return "unavailable"
    if not counts["total"]:
        return "empty"
    if not counts["required_count"]:
        return "not_required"
    if counts["returned"]:
        return "returned"
    if counts["deferred"]:
        return "deferred"
    if counts["confirmed"] == counts["required_count"]:
        return "completed"
    if counts["confirmed"]:
        return "in_progress"
    return "pending" if required_initial_count else "awaiting_initial"


def review_report(task):
    """将完整审核条目合入报告，保留展示标记和历史。"""
    context = review_context(task)
    prepared = _prepare_task_results(
        _raw_task_results(task),
        task_type=task["task_type"] or DOCUMENT_TASK_TYPE,
        task_id=task["id"],
        record_suppression_hits=True,
    )
    originals = {str(result.get("code") or ""): result for result in context["results"]}
    for result in prepared:
        original = originals[str(result.get("code") or "")]
        review_items = {item["id"]: item for item in original["report_items"]}
        visible = result["report_items"]
        suppressed = result["suppressed_report_items"]
        present_ids = {item["id"] for item in visible + suppressed}
        additional = [
            item for item in original["report_items"] if item["id"] not in present_ids
        ]
        for item in suppressed:
            item["review_display_label"] = "已忽略误报"
        for item in additional:
            item["review_display_label"] = "补充审核条目"
            item["media_summary"] = _media_report_item_text(item)
        items = visible + suppressed + additional
        for index, item in enumerate(items, start=1):
            item["index"] = index
            item["review"] = review_items[item["id"]]["review"]
        result["report_items"] = items
        result["suppressed_report_items"] = []
        result["report_counts"] = _count_report_items(
            items, suppressed_count=len(suppressed)
        )
    context["results"] = prepared
    return context


def present_progress(counts):
    return {
        **counts,
        "status_label": FINAL_STATUSES[counts["status"]],
        "key": ":".join(
            str(counts[k])
            for k in (
                "status",
                "total",
                "initial_count",
                "required_count",
                "confirmed",
                "returned",
                "deferred",
                "pending",
            )
        ),
    }


def final_review_progress(task_ids, *, refresh=True):
    """读取当前任务的缓存，仅重建已失效的记录。"""
    if not task_ids:
        return {}
    db = get_db()
    placeholders = ",".join("?" for _ in task_ids)
    cached = {
        row["task_id"]: dict(row)
        for row in db.execute(
            f"SELECT * FROM task_final_review_stats WHERE task_id IN ({placeholders})",
            tuple(task_ids),
        )
    }
    missing = (
        [task_id for task_id in task_ids if task_id not in cached] if refresh else []
    )
    for task_id in missing:
        # 短事务保证报告版本、审核记录与缓存属于同一个数据库快照。
        with db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT id, status, result_json FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                continue
            cached[task_id] = _write_final_review_progress(task)
    return {task_id: present_progress(counts) for task_id, counts in cached.items()}


def _write_final_review_progress(task):
    counts = review_context(task)["progress"]
    get_db().execute(
        "INSERT OR REPLACE INTO task_final_review_stats "
        "(task_id, total, initial_count, required_count, confirmed, returned, deferred, pending, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task["id"],
            *(
                counts[k]
                for k in (
                    "total",
                    "initial_count",
                    "required_count",
                    "confirmed",
                    "returned",
                    "deferred",
                    "pending",
                    "status",
                )
            ),
        ),
    )
    return counts


@contextmanager
def review_transaction(task_id):
    db = get_db()
    with db:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise ReviewConflict("任务已删除，请返回任务列表。")
        if task["status"] in ACTIVE_STATUSES:
            raise ReviewConflict("任务尚未完成，暂不能审核报告条目。")
        yield task
        updated_task = db.execute(
            "SELECT id, status, result_json FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        _write_final_review_progress(updated_task)


def find_review_item(task, result_code, item_id):
    for result in _raw_task_results(task):
        if str(result.get("code") or "") == result_code:
            for item in _result_report_items(result):
                if item["id"] == item_id:
                    acceptances = result.get("item_acceptances")
                    acceptance = _normalize_report_acceptance(
                        acceptances.get(item_id)
                        if isinstance(acceptances, dict)
                        else None
                    )
                    history = review_events(task["id"]).get((result_code, item_id), [])
                    return result, item, item_review_state(item, acceptance, history)
    raise ReviewConflict("报告条目已变化，请刷新后审核。")


def _insert_event(
    task_id,
    result_code,
    item,
    acceptance,
    actor,
    *,
    stage,
    decision,
    note="",
    channel="web",
    initial_review_id=None,
):
    get_db().execute(
        "INSERT INTO report_review_events (task_id, result_code, item_id, stage, decision, "
        "note, actor_subject, actor_name, actor_source, channel, item_hash, item_json, "
        "acceptance_json, initial_review_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            result_code,
            item["id"],
            stage,
            decision,
            note,
            actor["subject"],
            actor["name"],
            actor["source"],
            channel,
            item_fingerprint(item),
            _json(item),
            _json(acceptance),
            initial_review_id,
            now_text(),
        ),
    )


def record_initial_review(task, result, item_id, actor, *, channel="web"):
    item = next(item for item in _result_report_items(result) if item["id"] == item_id)
    acceptance = _normalize_report_acceptance(result["item_acceptances"].get(item_id))
    _insert_event(
        task["id"],
        str(result.get("code") or ""),
        item,
        acceptance,
        actor,
        stage="initial",
        decision=acceptance["acceptance_status"],
        channel=channel,
    )


def save_final_review(task_id, data, actor):
    from app.reporting.suppression import (
        record_confirmed_suppression,
        refresh_suppression_reviews,
    )

    decision = str(data.get("decision") or "")
    note = str(data.get("note") or "").strip()
    if decision not in FINAL_DECISIONS:
        raise ReviewConflict("请选择终审意见。")
    if decision in {"returned", "deferred"} and not note:
        raise ReviewConflict("提单修改或暂无法判断时，请填写具体说明。")
    if len(note) > 4000:
        raise ReviewConflict("终审说明最多填写 4000 字。")
    with review_transaction(task_id) as task:
        code, item_id = (
            str(data.get("result_code") or ""),
            str(data.get("item_id") or ""),
        )
        result, item, state = find_review_item(task, code, item_id)
        if item.get("type") not in {"issue", "suggestion"}:
            raise ReviewConflict("该条目无需终审。")
        if data.get("review_token") != state["token"]:
            raise ReviewConflict("报告或审核记录已更新，请刷新后重新确认。")
        if not state["requires_final"]:
            raise ReviewConflict("仅初审结果为“不认可”的条目需要终审。")
        initial = state["initial"]
        if initial is None:
            raise ReviewConflict("请先完成初审并记录审核人，再进行终审。")
        acceptance = _normalize_report_acceptance(
            (result.get("item_acceptances") or {}).get(item_id)
        )
        _insert_event(
            task_id,
            code,
            item,
            acceptance,
            actor,
            stage="final",
            decision=decision,
            note=note,
            initial_review_id=initial["id"],
        )
        _, _, saved_state = find_review_item(task, code, item_id)
        record_confirmed_suppression(task, result, item, saved_state)
        refresh_suppression_reviews()


def attach_review_metadata(task, results):
    context = review_context(task)
    states = {
        (str(r.get("code") or ""), i["id"]): i["review"]
        for r in context["results"]
        for i in r["report_items"]
    }
    for result in results:
        for item in result["report_items"] + result["suppressed_report_items"]:
            item["review"] = states.get((str(result.get("code") or ""), item["id"]), {})
    return context["progress"]


def audit_export_rows(task):
    for history in review_events(task["id"]).values():
        for event in history:
            yield [
                event["id"],
                event["result_code"],
                event["item_id"],
                "初审" if event["stage"] == "initial" else "终审",
                event["decision_label"],
                event["actor_name"],
                event["actor_subject"],
                event["actor_source"],
                event["created_at"],
                event["channel"],
                event["initial_review_id"],
                event["acceptance"]["rejection_reason_label"],
                event["acceptance"]["rejection_note"],
                event["note"],
                event["item_hash"],
                *(event["item"].get(key, "") for key, _ in REPORT_ITEM_FIELDS),
            ]
