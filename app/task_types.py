import json


DOCUMENT_TASK_TYPE = "document_check"
CONSISTENCY_TASK_TYPE = "consistency_check"
LANGUAGE_CONSISTENCY_TASK_TYPE = "language_consistency_check"
IMAGE_TASK_TYPE = "image_check"
VIDEO_TASK_TYPE = "video_check"
CONSISTENCY_MAX_MATERIAL_FILES = 5
CONSISTENCY_MAX_DATA_FILES = 3

TASK_TYPE_LABELS = {
    DOCUMENT_TASK_TYPE: "单文档检查",
    CONSISTENCY_TASK_TYPE: "多文档对照检查",
    LANGUAGE_CONSISTENCY_TASK_TYPE: "跨语种文档一致性对比",
    IMAGE_TASK_TYPE: "图片检查",
    VIDEO_TASK_TYPE: "视频检查",
}

def task_type_label(task_type: str | None) -> str:
    return TASK_TYPE_LABELS.get(task_type or DOCUMENT_TASK_TYPE, "单文档检查")


def document_groups_from_meta(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return []
    normalized = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        files = group.get("files")
        if not isinstance(files, list):
            continue
        normalized_files = [file for file in files if isinstance(file, dict)]
        if not normalized_files:
            continue
        normalized.append(
            {
                "role": str(group.get("role") or ""),
                "label": str(group.get("label") or "文档"),
                "files": normalized_files,
            }
        )
    return normalized
