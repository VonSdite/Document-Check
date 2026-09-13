"""文档文本提取的公共入口。"""

from app.documents.extraction import ALLOWED_EXTENSIONS as ALLOWED_EXTENSIONS
from app.documents.extraction import DocumentReadError as DocumentReadError
from app.documents.extraction import allowed_file as allowed_file
from app.documents.extraction import extension_of as extension_of
from app.documents.extraction import extract_document as extract_document
from app.documents.extraction import extract_text as extract_text
from app.documents.extraction import format_document_text as format_document_text
