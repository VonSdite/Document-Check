from pathlib import Path

import mistune
from bs4 import BeautifulSoup

from app.documents.extraction.common import (
    DocumentReadError,
    _clean_hyperlink_target,
    _format_hyperlink_text,
    _record_hyperlink,
)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentReadError("无法识别文本编码，请使用 UTF-8 文档")


def _extract_markdown_hyperlinks(text: str, hyperlinks: list[dict]) -> None:
    rendered = mistune.html(text)
    soup = BeautifulSoup(rendered, "html.parser")
    for index, anchor in enumerate(soup.find_all("a"), start=1):
        if not anchor.has_attr("href"):
            continue
        target = _clean_hyperlink_target(anchor.get("href"))
        _record_hyperlink(
            hyperlinks,
            anchor.get_text(" ", strip=True),
            target,
            f"Markdown 链接{index}",
        )


def _extract_html(path: Path, hyperlinks: list[dict] | None = None) -> str:
    html = _read_text(path)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    internal_targets = {
        str(tag.get("id") or tag.get("name") or "").strip()
        for tag in soup.find_all(attrs={"id": True})
        + soup.find_all(attrs={"name": True})
        if str(tag.get("id") or tag.get("name") or "").strip()
    }
    for index, anchor in enumerate(soup.find_all("a"), start=1):
        if not anchor.has_attr("href"):
            continue
        label = anchor.get_text(" ", strip=True)
        target = _clean_hyperlink_target(anchor.get("href"))
        _record_hyperlink(
            hyperlinks,
            label,
            target,
            f"HTML 链接{index}",
            internal_target_exists=(
                target == "#" or target.lstrip("#") in internal_targets
                if target.startswith("#")
                else None
            ),
        )
        anchor.replace_with(_format_hyperlink_text(label, target))
    return soup.get_text("\n", strip=True)
