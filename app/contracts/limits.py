DEFAULT_ISSUE_OUTPUT_LIMIT = 30
MAX_ISSUE_OUTPUT_LIMIT = 30
DEFAULT_DOCUMENT_CHECK_ROUNDS = 5
MAX_DOCUMENT_CHECK_ROUNDS = 5


def normalize_check_rounds(value, *, default: int = 1, maximum: int = 5) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return default
    try:
        rounds = int(value)
    except ValueError:
        return default
    return min(maximum, max(1, rounds))


def normalize_issue_output_limit(value) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ISSUE_OUTPUT_LIMIT
    if limit < 1:
        return DEFAULT_ISSUE_OUTPUT_LIMIT
    return min(limit, MAX_ISSUE_OUTPUT_LIMIT)
