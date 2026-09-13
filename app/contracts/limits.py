DEFAULT_ISSUE_OUTPUT_LIMIT = 30
MAX_ISSUE_OUTPUT_LIMIT = 30


def normalize_issue_output_limit(value) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ISSUE_OUTPUT_LIMIT
    if limit < 1:
        return DEFAULT_ISSUE_OUTPUT_LIMIT
    return min(limit, MAX_ISSUE_OUTPUT_LIMIT)
