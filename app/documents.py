import re
from pathlib import Path

from bs4 import BeautifulSoup
from docx import Document
from pypdf import PdfReader


ALLOWED_EXTENSIONS = {"docx", "pdf", "txt", "md", "html"}


class DocumentReadError(Exception):
    pass


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[1].lower()


def extract_text(path: Path, file_type: str) -> str:
    try:
        if file_type == "docx":
            return _extract_docx(path)
        if file_type == "pdf":
            return _extract_pdf(path)
        if file_type in {"txt", "md"}:
            return _read_text(path)
        if file_type == "html":
            return _extract_html(path)
    except Exception as exc:
        raise DocumentReadError(str(exc)) from exc
    raise DocumentReadError(f"不支持的文件类型：{file_type}")


def split_text_chunks(text: str, max_chars: int) -> list[dict]:
    text = str(text or "").strip()
    if not text:
        return []

    max_chars = max(1, int(max_chars or 1))
    if len(text) <= max_chars:
        return [{"index": 1, "total": 1, "label": _chunk_label(text), "text": text}]

    chunks = []
    current_parts = []
    current_len = 0
    for unit in _text_units(text):
        for piece in _split_oversized_unit(unit, max_chars):
            sep_len = 2 if current_parts else 0
            if current_parts and current_len + sep_len + len(piece) > max_chars:
                _append_text_chunk(chunks, current_parts)
                current_parts = []
                current_len = 0
                sep_len = 0
            current_parts.append(piece)
            current_len += sep_len + len(piece)

    if current_parts:
        _append_text_chunk(chunks, current_parts)

    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        chunk["index"] = index
        chunk["total"] = total
    return chunks


def _extract_docx(path: Path) -> str:
    document = Document(path)
    parts = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            parts.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _text_units(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
    if len(parts) > 1:
        return parts
    return [line.strip() for line in text.splitlines() if line.strip()] or [text]


def _split_oversized_unit(unit: str, max_chars: int) -> list[str]:
    if len(unit) <= max_chars:
        return [unit]

    pieces = []
    current = ""
    for sentence in _sentence_units(unit):
        if len(sentence) > max_chars:
            if current:
                pieces.append(current.strip())
                current = ""
            pieces.extend(sentence[start : start + max_chars].strip() for start in range(0, len(sentence), max_chars))
            continue

        if current and len(current) + len(sentence) > max_chars:
            pieces.append(current.strip())
            current = sentence
        else:
            current = f"{current}{sentence}"

    if current.strip():
        pieces.append(current.strip())
    return [piece for piece in pieces if piece]


def _sentence_units(text: str) -> list[str]:
    parts = re.findall(r".+?(?:[。！？!?；;]|$)", text, flags=re.S)
    return [part.strip() for part in parts if part.strip()]


def _append_text_chunk(chunks: list[dict], parts: list[str]):
    chunk_text = "\n\n".join(parts).strip()
    if chunk_text:
        chunks.append({"label": _chunk_label(chunk_text), "text": chunk_text})


def _chunk_label(text: str) -> str:
    for line in text.splitlines():
        label = re.sub(r"\s+", " ", line).strip()
        if label:
            return label[:80]
    return "片段"


def _extract_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"[第{index}页]\n{text.strip()}")
    return "\n\n".join(pages)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentReadError("无法识别文本编码，请使用 UTF-8 文档")


def _extract_html(path: Path) -> str:
    html = _read_text(path)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)
