import html
import re
from bisect import bisect_right
from dataclasses import dataclass, replace
from html.parser import HTMLParser

_METADATA_RE = re.compile(
    r"(?m)^file:[^\r\n]*|^#\s*工作表：[^\r\n]*"
    r"|\[第\d+页\]"
    r"|\[PDF结构化表格\s+[^\]\r\n]+\]"
    r"|\[(?:空单元格|非文本图形或图标|未能可靠还原的单元格)\]"
    r"|\[合并覆盖[^\]\r\n]*\]"
)
_LINK_ANNOTATIONS_RE = re.compile(r"(?ms)^\[超链接\].*?(?=^\[第\d+页\]|\Z)")
_LINK_TARGET_RE = re.compile(r"（超链接：[^）\r\n]*）")
_URL_RE = re.compile(
    r"\b(?:https?://|ftp://|www\.)[^\s<>\"'\[\]（），。；！？、]+",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PATH_RE = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|~/|/)"
    r"[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)*"
    r"|\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){2,}\b"
    r"|\b[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+\b"
)
_MARKDOWN_TARGET_RE = re.compile(r"(?<=\])\([^\r\n()]*\)")
_MARKDOWN_REFERENCE_RE = re.compile(r"(?m)^ {0,3}\[[^\]\r\n]+\]:[^\r\n]*")
_FENCED_CODE_RE = re.compile(
    r"(?ms)^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n"
    r".*?(?:^ {0,3}(?P=fence)[ \t]*(?=\r?$)|\Z)"
)
_INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)(?!`)[^\r\n]*?(?P=ticks)(?!`)")
_BLOCK_TAGS = {
    "br",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
_IGNORED_TAGS = {"script", "style", "noscript", "pre", "code"}


@dataclass(frozen=True)
class _ContentSegment:
    start: int
    end: int
    source_start: int
    source_end: int


@dataclass(frozen=True)
class DocumentBodyView:
    """正文及其到原始提取文本的区间映射。"""

    source: str
    text: str
    _segments: tuple[_ContentSegment, ...]
    _starts: tuple[int, ...]

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        return self._source_boundary(start), self._source_boundary(end, ending=True)

    def _source_boundary(self, position: int, *, ending: bool = False) -> int:
        index = bisect_right(self._starts, position - int(ending)) - 1
        segment = self._segments[index]
        if segment.end - segment.start == segment.source_end - segment.source_start:
            return segment.source_start + position - segment.start
        return segment.source_end if ending else segment.source_start


def document_body_view(text: str) -> DocumentBodyView:
    source = str(text or "")
    edits = []
    for pattern in (
        _METADATA_RE,
        _LINK_ANNOTATIONS_RE,
        _LINK_TARGET_RE,
        _URL_RE,
        _EMAIL_RE,
        _PATH_RE,
        _MARKDOWN_TARGET_RE,
        _MARKDOWN_REFERENCE_RE,
        _FENCED_CODE_RE,
        _INLINE_CODE_RE,
    ):
        edits.extend(
            (match.start(), match.end(), _blank_text(match.group(0)))
            for match in pattern.finditer(source)
        )
    if "<" in source or "&" in source:
        parser = _BodyMarkupParser(source)
        parser.feed(source)
        parser.close()
        edits.extend(parser.edits)
    body = _project_body(source, edits)
    value = body.text
    for pattern in (_URL_RE, _EMAIL_RE, _PATH_RE):
        value = pattern.sub(lambda match: _blank_text(match.group(0)), value)
    return replace(body, text=value)


def _blank_text(value: str) -> str:
    return re.sub(r"[^\r\n]", " ", value)


def _project_body(source: str, edits: list[tuple[int, int, str]]) -> DocumentBodyView:
    parts = []
    segments = []
    output_position = 0
    source_position = 0

    def append(value: str, start: int, end: int) -> None:
        nonlocal output_position
        if value:
            parts.append(value)
            segments.append(
                _ContentSegment(
                    output_position, output_position + len(value), start, end
                )
            )
            output_position += len(value)

    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], -edit[1])):
        if start < source_position:
            continue
        append(source[source_position:start], source_position, start)
        append(replacement, start, end)
        source_position = end
    append(source[source_position:], source_position, len(source))
    return DocumentBodyView(
        source,
        "".join(parts),
        tuple(segments),
        tuple(segment.start for segment in segments),
    )


class _BodyMarkupParser(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_starts = [0] + [match.end() for match in re.finditer(r"\n", source)]
        self.edits = []
        self.ignored_tag = ""
        self.ignored_start = 0
        self.ignored_depth = 0

    def _position(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list) -> None:
        start = self._position()
        end = start + len(self.get_starttag_text())
        self.edits.append((start, end, "\n" if tag in _BLOCK_TAGS else ""))
        if self.ignored_tag:
            if tag == self.ignored_tag:
                self.ignored_depth += 1
            return
        attributes = dict(attrs)
        if tag in _IGNORED_TAGS or (
            tag in {"td", "th"}
            and any(
                attribute in attributes
                for attribute in (
                    "data-inherited-from",
                    "data-empty",
                    "data-non-text",
                    "data-unresolved",
                )
            )
        ):
            self.ignored_tag = tag
            self.ignored_start = start
            self.ignored_depth = 1

    def handle_endtag(self, tag: str) -> None:
        start = self._position()
        end = self.source.find(">", start) + 1
        self.edits.append((start, end, "\n" if tag in _BLOCK_TAGS else ""))
        if tag == self.ignored_tag:
            self.ignored_depth -= 1
            if not self.ignored_depth:
                self.edits.append(
                    (
                        self.ignored_start,
                        end,
                        _blank_text(self.source[self.ignored_start : end]),
                    )
                )
                self.ignored_tag = ""

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        start = self._position()
        self.edits.append(
            (
                start,
                start + len(self.get_starttag_text()),
                "\n" if tag in _BLOCK_TAGS else "",
            )
        )

    def handle_comment(self, data: str) -> None:
        start = self._position()
        delimiter = "-->" if self.source.startswith("<!--", start) else ">"
        closing = self.source.find(delimiter, start)
        end = len(self.source) if closing < 0 else closing + len(delimiter)
        self.edits.append((start, end, ""))

    def handle_decl(self, decl: str) -> None:
        start = self._position()
        self.edits.append((start, self.source.find(">", start) + 1, ""))

    def handle_pi(self, data: str) -> None:
        start = self._position()
        self.edits.append((start, self.source.find(">", start) + 1, ""))

    def handle_entityref(self, name: str) -> None:
        self._decode_reference("&" + name)

    def handle_charref(self, name: str) -> None:
        self._decode_reference("&#" + name)

    def _decode_reference(self, value: str) -> None:
        start = self._position()
        if self.source.startswith(value + ";", start):
            value += ";"
        self.edits.append((start, start + len(value), html.unescape(value)))

    def close(self) -> None:
        super().close()
        if self.ignored_tag:
            self.edits.append(
                (
                    self.ignored_start,
                    len(self.source),
                    _blank_text(self.source[self.ignored_start :]),
                )
            )
