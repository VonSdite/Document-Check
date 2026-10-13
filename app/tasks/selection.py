"""按任务快照和重试范围确定本次执行的检查项。"""

import json

from app.contracts.task_types import DOCUMENT_TASK_TYPE


def selected_check_items(db, task):
    return _check_items_for_retry(
        _task_check_items(db, task, dict(task).get("task_type") or DOCUMENT_TASK_TYPE),
        _stored_retry_check_codes(task),
    )


def _task_check_items(db, task, task_type: str) -> list[dict]:
    task = dict(task)
    snapshot_raw = task.get("checks_snapshot_json")
    snapshot = _check_items_from_snapshot(snapshot_raw)
    if task.get("retry_check_codes_json") is not None:
        try:
            snapshot_value = json.loads(snapshot_raw) if snapshot_raw else None
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("原任务缺少有效的检查项快照，无法重试") from exc
        if (
            not isinstance(snapshot_value, list)
            or not snapshot_value
            or len(snapshot) != len(snapshot_value)
        ):
            raise RuntimeError("原任务缺少有效的检查项快照，无法重试")
    if snapshot:
        return snapshot

    try:
        check_values = json.loads(task["checks_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("检查项数据无效") from exc
    if not isinstance(check_values, list) or not check_values:
        return []

    check_ids = [int(value) for value in check_values if isinstance(value, int)]
    check_codes = [
        str(value).strip()
        for value in check_values
        if isinstance(value, str) and str(value).strip()
    ]
    clauses = []
    params = []
    if check_ids:
        clauses.append(f"id IN ({','.join('?' for _ in check_ids)})")
        params.extend(check_ids)
    if check_codes:
        clauses.append(f"code IN ({','.join('?' for _ in check_codes)})")
        params.extend(check_codes)
    if not clauses:
        return []
    params.append(task_type)
    return [
        dict(row)
        for row in db.execute(
            f"""
            SELECT *
            FROM check_items
            WHERE ({" OR ".join(clauses)}) AND task_type = ? AND enabled = 1
            ORDER BY sort_order ASC, id ASC
            """,
            tuple(params),
        ).fetchall()
    ]


def _check_items_from_snapshot(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []

    items = []
    seen_codes = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not code or not name or not prompt or code in seen_codes:
            continue
        seen_codes.add(code)
        items.append({"code": code, "name": name, "prompt": prompt})
    return items


def _stored_retry_check_codes(task) -> list[str] | None:
    raw = dict(task).get("retry_check_codes_json")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("重试检查项范围无效") from exc
    if not isinstance(value, list) or not value:
        raise RuntimeError("重试检查项范围无效")

    codes = []
    seen_codes = set()
    for item in value:
        code = str(item or "").strip() if isinstance(item, str) else ""
        if not code or code in seen_codes:
            raise RuntimeError("重试检查项范围无效")
        seen_codes.add(code)
        codes.append(code)
    return codes


def _check_items_for_retry(
    check_items: list[dict],
    retry_check_codes: list[str] | None,
) -> list[dict]:
    if retry_check_codes is None:
        return check_items
    items_by_code = {str(item.get("code") or "").strip(): item for item in check_items}
    missing_codes = [code for code in retry_check_codes if code not in items_by_code]
    if missing_codes:
        raise RuntimeError(f"原任务检查项快照缺少重试项：{','.join(missing_codes)}")
    return [items_by_code[code] for code in retry_check_codes]
