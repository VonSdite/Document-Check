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


def format_document_text(filename: str, text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    name = Path(str(filename or "")).name.strip()
    if not name:
        return text
    return f"file: {name}\n\n{text}"


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
