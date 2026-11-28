"""同一检查项内的报告条目标识分配。"""


def ensure_unique_report_item_ids(items: list[dict], *, reserved_ids=()) -> None:
    """保留首个可用标识，为冲突条目分配稳定且长度受限的标识。"""
    used = set(reserved_ids)
    reserved = used | {item["id"] for item in items}
    next_numbers = {}
    for item in items:
        original_id = item["id"]
        if original_id in used:
            number = next_numbers.get(original_id, 2)
            while True:
                suffix = f".{number}"
                candidate = original_id[: 128 - len(suffix)] + suffix
                number += 1
                if candidate not in reserved:
                    item["id"] = candidate
                    reserved.add(candidate)
                    break
            next_numbers[original_id] = number
        used.add(item["id"])
