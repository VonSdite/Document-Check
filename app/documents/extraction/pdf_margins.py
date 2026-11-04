import re
from collections import Counter, defaultdict
from dataclasses import dataclass

import fitz

from app.documents.extraction.common import (
    DocumentReadCanceled,
    check_extraction_canceled,
)

_PAGE_NUMBER_PATTERN = re.compile(r"(?:[0-9]+|[ivxlcdmIVXLCDM]+)")


@dataclass(frozen=True)
class _MarginLine:
    key: tuple
    bbox: tuple[float, ...]


@dataclass
class PdfMarginCleanup:
    raw_page: dict
    removed_lines: set[tuple[str, int]]
    unique_removed_text: set[str]
    rects: list[tuple[float, ...]]

    def clean_text(self, text: str, *, fallback: bool = False) -> str:
        if not self.removed_lines:
            return text
        lines = text.splitlines(keepends=True)
        totals = Counter(line.rstrip("\r\n") for line in lines) if fallback else {}
        occurrences = Counter()
        kept = []
        for line in lines:
            content = line.rstrip("\r\n")
            occurrences[content] += 1
            if fallback:
                remove = content in self.unique_removed_text and totals[content] == 1
            else:
                remove = (content, occurrences[content]) in self.removed_lines
            if not remove:
                kept.append(line)
        return "".join(kept)


def find_repeated_pdf_margins(document, *, cancel_event=None) -> dict[int, list]:
    """识别稳定边缘位置上跨页重复的文字，并保留每种文字的首次出现。"""
    page_count = document.page_count
    if page_count < 3:
        return {}
    groups = defaultdict(list)
    for index in range(page_count):
        check_extraction_canceled(cancel_event)
        try:
            page = document[index]
            layout = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
            width, height = layout["width"], layout["height"]
            candidates = []
            for block in layout.get("blocks", []):
                if block.get("type", 0) != 0:
                    continue
                for line in block.get("lines", []):
                    text = _line_text(line)
                    bbox = tuple(line.get("bbox", ()))
                    key = _margin_key(text, bbox, width, height)
                    if key is not None:
                        candidates.append(_MarginLine(key, bbox))
            if not candidates:
                continue
            drawings = page.get_drawings()
            candidates = [
                line for line in candidates if not _line_is_boxed(line.bbox, drawings)
            ]
        except DocumentReadCanceled:
            raise
        except Exception:
            continue
        for line in candidates:
            groups[line.key].append((index, line))

    excluded = defaultdict(list)
    for occurrences in groups.values():
        indexes = {index for index, _line in occurrences}
        if len(indexes) < 3:
            continue
        frequent = len(indexes) >= page_count * 0.5
        local_run = any(
            index + step in indexes and index + step * 2 in indexes
            for index in indexes
            for step in (1, 2)
        )
        if frequent or local_run:
            first = min(indexes)
            for index, line in occurrences:
                if index != first:
                    excluded[index].append(line)
    return dict(excluded)


def filter_pdf_margins(raw_page: dict, candidates: list) -> PdfMarginCleanup:
    cleanup = PdfMarginCleanup(raw_page, set(), set(), [])
    if not candidates:
        return cleanup
    width, height = raw_page.get("width", 0), raw_page.get("height", 0)
    excluded_keys = {line.key for line in candidates}
    occurrences = Counter()
    removed_text = set()
    blocks = []
    for block in raw_page.get("blocks", []):
        if block.get("type", 0) != 0:
            blocks.append(block)
            continue
        lines = []
        for line in block.get("lines", []):
            text = _line_text(line)
            occurrences[text] += 1
            bbox = tuple(line.get("bbox", ()))
            if _margin_key(text, bbox, width, height) in excluded_keys:
                cleanup.removed_lines.add((text, occurrences[text]))
                cleanup.rects.append(bbox)
                removed_text.add(text)
            else:
                lines.append(line)
        if lines:
            blocks.append({**block, "lines": lines})
    if cleanup.removed_lines:
        cleanup.raw_page = {**raw_page, "blocks": blocks}
        cleanup.unique_removed_text = {
            text for text in removed_text if occurrences[text] == 1
        }
    return cleanup


def _line_text(line: dict) -> str:
    return "".join(
        str(span.get("text", ""))
        if "text" in span
        else "".join(str(char.get("c", "")) for char in span.get("chars", []))
        for span in line.get("spans", [])
    )


def _margin_key(text: str, bbox: tuple, width: float, height: float) -> tuple | None:
    text = " ".join(text.split())
    if (
        not text
        or len(text) > 160
        or _PAGE_NUMBER_PATTERN.fullmatch(text)
        or len(bbox) != 4
        or width <= 0
        or height <= 0
    ):
        return None
    x0, y0, x1, y1 = bbox
    if y1 <= min(72, height * 0.085):
        edge = "top"
    elif y0 >= height - min(72, height * 0.09):
        edge = "bottom"
    else:
        return None
    center_x = (x0 + x1) / 2
    if center_x < width / 3:
        alignment, anchor_x = "left", x0
    elif center_x > width * 2 / 3:
        alignment, anchor_x = "right", x1
    else:
        alignment, anchor_x = "center", center_x
    return (
        edge,
        alignment,
        round(anchor_x / width * 100),
        round((y0 + y1) / 2 / height * 100),
        text,
    )


def _line_is_boxed(bbox: tuple, drawings: list[dict]) -> bool:
    center_x, center_y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    left_edge = right_edge = False
    for drawing in drawings:
        rect = drawing.get("rect")
        if rect is None:
            continue
        x0, y0, x1, y1 = rect
        if y1 - y0 <= 2 or not y0 <= center_y <= y1:
            continue
        if x1 - x0 > 2:
            if x0 <= center_x <= x1:
                return True
        else:
            left_edge = left_edge or x1 <= bbox[0]
            right_edge = right_edge or x0 >= bbox[2]
    return left_edge and right_edge
