DEFAULT_ISSUE_OUTPUT_LIMIT = 30
MAX_ISSUE_OUTPUT_LIMIT = 30
MAX_TASK_REPORT_LIMIT = 300


def normalize_task_report_limit(value) -> int:
    try:
        return min(MAX_TASK_REPORT_LIMIT, max(0, int(value)))
    except (TypeError, ValueError):
        return 0


def normalize_issue_output_limit(value) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ISSUE_OUTPUT_LIMIT
    if limit < 1:
        return DEFAULT_ISSUE_OUTPUT_LIMIT
    return min(limit, MAX_ISSUE_OUTPUT_LIMIT)
