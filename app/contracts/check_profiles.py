"""检查方案标识与任务快照的展示约定。"""

import json

DEFAULT_CHECK_PROFILE = "default"
SOFTWARE_CHECK_PROFILE = "software"


def task_check_profile(task) -> dict:
    values = dict(task)
    profile_id = values.get("check_profile_id") or DEFAULT_CHECK_PROFILE
    try:
        snapshot = json.loads(values.get("check_profile_snapshot_json") or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    return {
        "id": profile_id,
        "name": snapshot.get("name")
        or (
            "现有文档方案"
            if profile_id == DEFAULT_CHECK_PROFILE
            else "软件文档方案"
            if profile_id == SOFTWARE_CHECK_PROFILE
            else profile_id
        ),
        "revision": snapshot.get("revision"),
    }
