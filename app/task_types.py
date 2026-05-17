import json


DOCUMENT_TASK_TYPE = "document_check"
CONSISTENCY_TASK_TYPE = "consistency_check"
CONSISTENCY_MAX_FILES_PER_GROUP = 3

TASK_TYPE_LABELS = {
    DOCUMENT_TASK_TYPE: "文档检查",
    CONSISTENCY_TASK_TYPE: "一致性检查",
}

CONSISTENCY_CHECK_ITEM = {
    "code": "consistency-cross-document",
    "name": "一致性检查",
    "prompt": """你是一名跨文档一致性审查专家。用户会提供两组内容：母本文档和相关文档。请以母本文档作为基准，检查相关文档是否存在与母本口径不一致、遗漏、偏差或需要人工确认的地方。
重点关注：
1. 产品/项目/组织/人名/地点/日期/版本/编号/术语是否一致。
2. 指标、参数、规格、数量、单位、阈值、流程步骤和限制条件是否一致。
3. 相关文档是否遗漏母本文档中的关键约束，或新增了母本没有支撑的说法。
4. 多份相关文档之间如存在互相冲突，也请标注，但优先说明它们与母本文档的关系。

输出要求：
1. 先给出总体结论，说明一致性风险等级。
2. 按条列出偏差：相关文档名称、位置线索、相关文档表述、母本文档依据、偏差说明、修改建议。
3. 对证据不足或需要业务判断的问题标注“需人工确认”。
4. 如果未发现明显偏差，明确说明“未发现相关文档与母本文档存在明显不一致”。不要编造文档中不存在的内容。""",
}


def task_type_label(task_type: str | None) -> str:
    return TASK_TYPE_LABELS.get(task_type or DOCUMENT_TASK_TYPE, "文档检查")


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
