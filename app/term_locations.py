import re
from bisect import bisect_right


TERM_CONTEXT_CHARS = 45
TERM_LOCATIONS_PER_ISSUE = 5

_PAGE_MARKER_RE = re.compile(r"\[第(\d+)页\]")
_SHEET_MARKER_RE = re.compile(r"(?m)^#\s*工作表：(.+?)\s*$")
_FILE_MARKER_RE = re.compile(r"(?m)^file:\s*(.+?)\s*$")


def excerpt_for(text: str, position: int, length: int) -> str:
    start = max(0, position - TERM_CONTEXT_CHARS)
    end = min(len(text), position + length + TERM_CONTEXT_CHARS)
    excerpt = text[start:end].replace("\r", " ").replace("\n", " ")
    excerpt = re.sub(r"\s+", " ", excerpt).strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt += "..."
    return excerpt


class DocumentLocationIndex:
    def __init__(self, text: str):
        self.text = str(text or "")
        self.filename = self._filename()
        self.pages = self._markers(_PAGE_MARKER_RE)
        self.sheets = self._markers(_SHEET_MARKER_RE)
        self.line_starts = self._line_starts()

    def location_for(self, position: int, term: str) -> str:
        parts = []
        if self.filename:
            parts.append(f"文件：{self.filename}")
        page = self._marker_before(self.pages, position)
        if page:
            parts.append(f"页码：第{page}页")
        sheet = self._marker_before(self.sheets, position)
        if sheet:
            parts.append(f"工作表：{sheet}")
        parts.append(f"行：{self._line_number(position)}")
        line = self._line_text(position)
        if line:
            parts.append(f"附近线索：{_shorten(line, term)}")
        return "，".join(parts)

    def _filename(self) -> str:
        match = _FILE_MARKER_RE.search(self.text)
        return match.group(1).strip() if match else ""

    def _markers(self, pattern: re.Pattern) -> list[tuple[int, str]]:
        return [
            (match.start(), match.group(1).strip())
            for match in pattern.finditer(self.text)
            if match.group(1).strip()
        ]

    def _marker_before(self, markers: list[tuple[int, str]], position: int) -> str:
        index = bisect_right([marker[0] for marker in markers], position) - 1
        if index < 0:
            return ""
        return markers[index][1]

    def _line_starts(self) -> list[int]:
        return [0] + [match.end() for match in re.finditer(r"\n", self.text)]

    def _line_number(self, position: int) -> int:
        return bisect_right(self.line_starts, position)

    def _line_text(self, position: int) -> str:
        start_index = bisect_right(self.line_starts, position) - 1
        line_start = self.line_starts[max(0, start_index)]
        line_end = self.text.find("\n", position)
        if line_end < 0:
            line_end = len(self.text)
        return re.sub(r"\s+", " ", self.text[line_start:line_end]).strip()


def _shorten(text: str, term: str, limit: int = 80) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    position = text.find(term)
    if position < 0:
        return text[: limit - 3].rstrip() + "..."
    start = max(0, position - limit // 2)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    result = text[start:end].strip()
    if start > 0:
        result = "..." + result
    if end < len(text):
        result += "..."
    return result
