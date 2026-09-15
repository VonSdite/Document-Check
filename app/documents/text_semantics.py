import re

SCRIPT_MARKER_PATTERN = re.compile(
    r"(?:\^\{([^{}\r\n]{1,64})\}|_\{([^{}\r\n]{1,64})\})"
)


def strip_script_markers(text: str) -> str:
    """将上下标语义标记还原为普通线性文本。"""
    return SCRIPT_MARKER_PATTERN.sub(
        lambda match: match.group(1) or match.group(2) or "",
        str(text or ""),
    )
