from pathlib import Path

from app.documents.extraction.common import DocumentReadError, check_extraction_canceled
from app.documents.extraction.docx import _extract_docx
from app.documents.extraction.markup import (
    _extract_html,
    _extract_markdown_hyperlinks,
    _read_text,
)
from app.documents.extraction.pdf import _extract_pdf
from app.documents.extraction.spreadsheets import (
    _extract_openpyxl_workbook,
    _extract_xls,
)

ALLOWED_EXTENSIONS = {"docx", "pdf", "txt", "md", "html", "xlsx", "xlsm", "xls"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[1].lower()


def extract_text(
    path: Path, file_type: str, *, include_tables: bool = True, cancel_event=None
) -> str:
    text, _hyperlinks = extract_document(
        path, file_type, include_tables=include_tables, cancel_event=cancel_event
    )
    return text


def extract_document(
    path: Path,
    file_type: str,
    *,
    include_tables: bool = True,
    cancel_event=None,
) -> tuple[str, list[dict]]:
    check_extraction_canceled(cancel_event)
    hyperlinks = []
    try:
        if file_type == "docx":
            return _extract_docx(path, hyperlinks), hyperlinks
        if file_type == "pdf":
            return (
                _extract_pdf(
                    path,
                    hyperlinks,
                    include_tables=include_tables,
                    cancel_event=cancel_event,
                ),
                hyperlinks,
            )
        if file_type == "txt":
            return _read_text(path), hyperlinks
        if file_type == "md":
            text = _read_text(path)
            _extract_markdown_hyperlinks(text, hyperlinks)
            return text, hyperlinks
        if file_type == "html":
            return _extract_html(path, hyperlinks), hyperlinks
        if file_type in {"xlsx", "xlsm"}:
            return _extract_openpyxl_workbook(
                path, hyperlinks, cancel_event=cancel_event
            ), hyperlinks
        if file_type == "xls":
            return _extract_xls(path, hyperlinks, cancel_event=cancel_event), hyperlinks
    except DocumentReadError:
        raise
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
