"""可分配的管理能力与权限包含关系。"""

from app.persistence.permissions import subject_permissions

ASSIGNABLE_PERMISSIONS = {
    "tasks.view_all": {
        "label": "查看全部任务",
        "description": "查看全部用户的任务、报告、模型输出和原始文件。",
    },
    "tasks.manage_all": {
        "label": "管理全部任务",
        "description": "包含查看权限，可复核、回填、取消、重试和删除全部用户的任务。",
    },
    "stats.view_all": {
        "label": "查看全局统计",
        "description": "查看全局概览与各类任务的汇总指标。",
    },
    "rules.manage": {
        "label": "管理公共规则",
        "description": "维护公共检查项、提示词和全局误报忽略规则。",
    },
}


def effective_permissions(subject: str) -> frozenset[str]:
    permissions = set(subject_permissions(subject)) & ASSIGNABLE_PERMISSIONS.keys()
    if "tasks.manage_all" in permissions:
        permissions.add("tasks.view_all")
    return frozenset(permissions)


def normalize_permissions(permissions: set[str]) -> set[str]:
    if permissions - ASSIGNABLE_PERMISSIONS.keys():
        raise ValueError("权限选项无效。")
    permissions = set(permissions)
    if "tasks.manage_all" in permissions:
        permissions.add("tasks.view_all")
    return permissions
