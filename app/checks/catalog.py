from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.persistence.connection import get_db, now_text


def _check_item_task_type(value: str | None) -> str:
    if value == CONSISTENCY_TASK_TYPE:
        return CONSISTENCY_TASK_TYPE
    if value == LANGUAGE_CONSISTENCY_TASK_TYPE:
        return LANGUAGE_CONSISTENCY_TASK_TYPE
    if value == IMAGE_TASK_TYPE:
        return IMAGE_TASK_TYPE
    if value == VIDEO_TASK_TYPE:
        return VIDEO_TASK_TYPE
    return DOCUMENT_TASK_TYPE


def _check_item_code_prefix(task_type: str) -> str:
    if task_type == CONSISTENCY_TASK_TYPE:
        return "custom-consistency"
    if task_type == LANGUAGE_CONSISTENCY_TASK_TYPE:
        return "custom-language-consistency"
    if task_type == IMAGE_TASK_TYPE:
        return "custom-image"
    if task_type == VIDEO_TASK_TYPE:
        return "custom-video"
    return "custom"


def _check_items_for_task_type(db, task_type: str):
    return db.execute(
        """
        SELECT *
        FROM check_items
        WHERE task_type = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (task_type,),
    ).fetchall()


def get_enabled_check_items(task_type: str = DOCUMENT_TASK_TYPE):
    return (
        get_db()
        .execute(
            """
        SELECT *
        FROM check_items
        WHERE task_type = ? AND enabled = 1
        ORDER BY sort_order ASC, id ASC
        """,
            (task_type,),
        )
        .fetchall()
    )


def _next_check_item_sort_order(db, task_type: str = DOCUMENT_TASK_TYPE) -> int:
    row = db.execute(
        "SELECT MIN(sort_order) AS value FROM check_items WHERE task_type = ?",
        (task_type,),
    ).fetchone()
    if row is None or row["value"] is None:
        return 10
    return int(row["value"]) - 10


def _reorder_check_items(
    db, item_ids: list[int], task_type: str = DOCUMENT_TASK_TYPE
) -> list[int]:
    rows = db.execute(
        """
        SELECT id
        FROM check_items
        WHERE task_type = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (task_type,),
    ).fetchall()
    existing_ids = [int(row["id"]) for row in rows]
    existing_set = set(existing_ids)
    ordered_ids = []
    seen_ids = set()
    for item_id in item_ids:
        if item_id in existing_set and item_id not in seen_ids:
            ordered_ids.append(item_id)
            seen_ids.add(item_id)
    ordered_ids.extend(item_id for item_id in existing_ids if item_id not in seen_ids)

    updated_at = now_text()
    for index, item_id in enumerate(ordered_ids, start=1):
        db.execute(
            "UPDATE check_items SET sort_order = ?, updated_at = ? WHERE id = ?",
            (index * 10, updated_at, item_id),
        )
    return ordered_ids
