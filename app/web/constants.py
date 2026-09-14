from app.contracts.limits import DEFAULT_ISSUE_OUTPUT_LIMIT

STATUS_LABELS = {
    "queued": "排队",
    "running": "检查",
    "canceling": "取消中",
    "completed": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "canceled": "已取消",
}

DELETABLE_TASK_STATUSES = {"completed", "partial", "failed", "canceled"}
BULK_DELETABLE_TASK_STATUSES = DELETABLE_TASK_STATUSES | {"queued"}
DEFAULT_TASKS_PER_PAGE = 20
TASKS_PER_PAGE_OPTIONS = (DEFAULT_TASKS_PER_PAGE, 50, 100)
MAX_BULK_DELETE_TASKS = max(TASKS_PER_PAGE_OPTIONS)
CHECK_ITEM_CONCURRENCY_DEFAULT = 1
TASK_FILE_RETENTION_DAYS_DEFAULT = 0
ISSUE_OUTPUT_LIMIT_DEFAULT = DEFAULT_ISSUE_OUTPUT_LIMIT
MODEL_TEST_TIMEOUT_MAX = 60

CONSOLE_USER_ENDPOINTS = {
    "admin_tasks",
    "admin_new_task",
    "admin_consistency",
    "admin_language_consistency",
    "admin_images",
    "admin_videos",
    "admin_models",
}
