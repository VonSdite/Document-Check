"""按原文语义边界切分来源，保留可逐字核验的连续文本。"""

import re
from bisect import bisect_right


def split_source_text(text: str, kind: str, limit: int = 4000) -> list[str]:
    boundaries = [
        m.end() for m in re.finditer(r"\n+|[。！？；][”’」』]*|[.!?;](?=\s)", text)
    ]
    rows = (
        [m.end() for m in re.finditer(r"</tr>\s*", text, re.I)]
        if kind == "table"
        else []
    )
    cells = [m.end() for m in re.finditer(r"</t[dh]>\s*", text, re.I)] if rows else []
    parts, start = [], 0
    while len(text) - start > limit:
        end = start + limit
        for positions in (rows, cells, boundaries):
            position = bisect_right(positions, end) - 1
            if position >= 0 and positions[position] > start:
                end = positions[position]
                break
        else:
            space = text.rfind(" ", start + limit // 2, end)
            if space >= 0:
                end = space + 1
            if rows and text.rfind("<", start, end) > text.rfind(">", start, end):
                tag = text.rfind("<", start, end)
                end = tag if tag > start else text.find(">", end) + 1
                if end <= start:
                    end = start + limit
        parts.append(text[start:end])
        start = end
    if start < len(text):
        parts.append(text[start:])
    return parts
