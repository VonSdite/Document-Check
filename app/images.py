import base64
import binascii
import mimetypes
import posixpath
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .documents import DocumentReadError


SUPPORTED_IMAGE_TYPES = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class ImageExtractionError(DocumentReadError):
    pass


def default_image_folder(upload_folder: str | Path) -> Path:
    return Path(upload_folder).parent / "extracted_images"


def extract_images(
    document_path: Path,
    file_type: str,
    output_dir: Path,
    *,
    source_filename: str = "",
) -> list[dict]:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_type = str(file_type or "").lower()
    source_name = Path(str(source_filename or document_path.name)).name
    try:
        if file_type == "docx":
            return _extract_docx_images(document_path, output_dir, source_name)
        if file_type == "pdf":
            return _extract_pdf_images(document_path, output_dir, source_name)
        if file_type in {"xlsx", "xlsm"}:
            return _extract_xlsx_images(document_path, output_dir, source_name)
        if file_type == "html":
            return _extract_html_images(document_path, output_dir, source_name)
        return []
    except ImageExtractionError:
        raise
    except Exception as exc:
        raise ImageExtractionError(str(exc)) from exc


def image_items_from_meta(raw: str | None) -> list[dict]:
    if not raw:
        return []
    import json

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, list):
        return []
    normalized = []
    for index, image in enumerate(images, start=1):
        if not isinstance(image, dict):
            continue
        filename = str(image.get("filename") or "").strip()
        stored_filename = str(image.get("stored_filename") or filename).strip()
        relative_path = str(image.get("relative_path") or stored_filename).strip()
        if not filename or not relative_path:
            continue
        normalized.append(
            {
                "id": str(image.get("id") or f"image-{index:04d}"),
                "filename": filename,
                "stored_filename": stored_filename,
                "relative_path": relative_path,
                "mime_type": str(image.get("mime_type") or "application/octet-stream"),
                "position": str(image.get("position") or ""),
                "source": str(image.get("source") or ""),
                "size_bytes": int(image.get("size_bytes") or 0),
            }
        )
    return normalized


def format_image_document_text(filename: str, images: list[dict], document_text: str = "", text_error: str = "") -> str:
    name = Path(str(filename or "")).name.strip() or "document"
    cleaned_text = str(document_text or "").strip()
    cleaned_error = str(text_error or "").strip()
    lines = [f"file: {name}"]
    if cleaned_text:
        lines.extend(["", "document_text:", cleaned_text])
    elif cleaned_error:
        lines.extend(["", f"document_text: 未提取到可检查文本（{cleaned_error}）"])
    else:
        lines.extend(["", "document_text: 未提取到可检查文本"])
    lines.extend(["", f"extracted_images: {len(images)}"])
    for image in images:
        size_kb = (int(image.get("size_bytes") or 0) / 1024)
        lines.append(
            f"- {image.get('filename')}: {image.get('position') or '-'} "
            f"({image.get('mime_type') or '-'}, {size_kb:.1f} KB)"
        )
    return "\n".join(lines).strip()


def image_path_from_item(image_folder: Path, item: dict) -> Path:
    relative_path = Path(str(item.get("relative_path") or item.get("stored_filename") or ""))
    safe_parts = [part for part in relative_path.parts if part not in {"", ".", ".."}]
    return image_folder.joinpath(*safe_parts)


def image_to_data_url(path: Path, mime_type: str) -> str:
    data = path.read_bytes()
    mime = str(mime_type or "").strip() or _mime_type_for_path(path)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _extract_docx_images(path: Path, output_dir: Path, source_name: str) -> list[dict]:
    try:
        with ZipFile(path) as archive:
            records = []
            sequence = 0
            part_names = ["word/document.xml"]
            part_names.extend(
                sorted(
                    name
                    for name in archive.namelist()
                    if re.fullmatch(r"word/(header|footer)\d+\.xml", name)
                )
            )
            for part_name in part_names:
                if part_name not in archive.namelist():
                    continue
                rels = _zip_relationships(archive, part_name)
                part_label = _docx_part_label(part_name)
                root = ET.fromstring(archive.read(part_name))
                for rel_id, position in _docx_image_refs(root, rels, part_label):
                    media_path = rels.get(rel_id)
                    if not media_path:
                        continue
                    try:
                        data = archive.read(media_path)
                    except KeyError:
                        continue
                    sequence += 1
                    records.append(
                        _write_image_record(
                            output_dir,
                            sequence,
                            position,
                            media_path,
                            data,
                            source_name,
                        )
                    )
            return records
    except BadZipFile as exc:
        raise ImageExtractionError("无法读取 docx 图片：文件不是有效的 Office 文档。") from exc


def _docx_part_label(part_name: str) -> str:
    if part_name == "word/document.xml":
        return "document"
    return Path(part_name).stem


def _docx_image_refs(root: ET.Element, rels: dict[str, str], part_label: str) -> list[tuple[str, str]]:
    body = root.find(f".//{{{_WORD_NS}}}body")
    if body is None:
        return []
    refs = []
    paragraph_index = 0
    table_index = 0
    block_index = 0
    for child in list(body):
        if _local_name(child.tag) in {"sectPr"}:
            continue
        block_index += 1
        local = _local_name(child.tag)
        if local == "p":
            paragraph_index += 1
            position = f"{part_label}-block{block_index:03d}-p{paragraph_index:03d}"
            refs.extend((rel_id, position) for rel_id in _image_relation_ids(child) if rel_id in rels)
        elif local == "tbl":
            table_index += 1
            refs.extend(_docx_table_refs(child, rels, f"{part_label}-block{block_index:03d}-tbl{table_index:03d}"))
        else:
            position = f"{part_label}-block{block_index:03d}"
            refs.extend((rel_id, position) for rel_id in _image_relation_ids(child) if rel_id in rels)
    return refs


def _docx_table_refs(table: ET.Element, rels: dict[str, str], base_position: str) -> list[tuple[str, str]]:
    refs = []
    for row_index, row in enumerate(_direct_children(table, "tr"), start=1):
        for cell_index, cell in enumerate(_direct_children(row, "tc"), start=1):
            paragraph_index = 0
            nested_table_index = 0
            for child in list(cell):
                local = _local_name(child.tag)
                if local == "p":
                    paragraph_index += 1
                    position = f"{base_position}-r{row_index:03d}c{cell_index:03d}-p{paragraph_index:03d}"
                    refs.extend((rel_id, position) for rel_id in _image_relation_ids(child) if rel_id in rels)
                elif local == "tbl":
                    nested_table_index += 1
                    nested = f"{base_position}-r{row_index:03d}c{cell_index:03d}-tbl{nested_table_index:03d}"
                    refs.extend(_docx_table_refs(child, rels, nested))
    if not refs:
        refs.extend((rel_id, base_position) for rel_id in _image_relation_ids(table) if rel_id in rels)
    return refs


def _image_relation_ids(element: ET.Element) -> list[str]:
    ids = []
    seen = set()
    for node in element.iter():
        local = _local_name(node.tag)
        rel_id = ""
        if local == "blip":
            rel_id = node.attrib.get(f"{{{_OFFICE_REL_NS}}}embed") or node.attrib.get(f"{{{_OFFICE_REL_NS}}}link") or ""
        elif local == "imagedata":
            rel_id = node.attrib.get(f"{{{_OFFICE_REL_NS}}}id") or ""
        if rel_id and rel_id not in seen:
            seen.add(rel_id)
            ids.append(rel_id)
    return ids


def _extract_xlsx_images(path: Path, output_dir: Path, source_name: str) -> list[dict]:
    try:
        with ZipFile(path) as archive:
            if "xl/workbook.xml" not in archive.namelist():
                return []
            workbook_rels = _zip_relationships(archive, "xl/workbook.xml")
            workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
            records = []
            sequence = 0
            for sheet_index, sheet in enumerate(_workbook_sheets(workbook_root), start=1):
                sheet_part = workbook_rels.get(sheet["rel_id"])
                if not sheet_part or sheet_part not in archive.namelist():
                    continue
                sheet_rels = _zip_relationships(archive, sheet_part)
                drawing_parts = [
                    target
                    for target in sheet_rels.values()
                    if target.startswith("xl/drawings/") and target in archive.namelist()
                ]
                for drawing_part in drawing_parts:
                    drawing_rels = _zip_relationships(archive, drawing_part)
                    drawing_root = ET.fromstring(archive.read(drawing_part))
                    image_index = 0
                    for anchor in _drawing_anchors(drawing_root):
                        rel_id = _first_image_rel_id(anchor)
                        media_path = drawing_rels.get(rel_id or "")
                        if not media_path:
                            continue
                        try:
                            data = archive.read(media_path)
                        except KeyError:
                            continue
                        image_index += 1
                        sequence += 1
                        position = _xlsx_anchor_position(sheet_index, sheet["name"], anchor, image_index)
                        records.append(
                            _write_image_record(
                                output_dir,
                                sequence,
                                position,
                                media_path,
                                data,
                                source_name,
                            )
                        )
            return records
    except BadZipFile as exc:
        raise ImageExtractionError("无法读取 Excel 图片：文件不是有效的 Office 工作簿。") from exc


def _workbook_sheets(workbook_root: ET.Element) -> list[dict]:
    sheets = []
    for sheet in workbook_root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"):
        rel_id = sheet.attrib.get(f"{{{_OFFICE_REL_NS}}}id") or ""
        name = str(sheet.attrib.get("name") or "Sheet")
        if rel_id:
            sheets.append({"rel_id": rel_id, "name": name})
    return sheets


def _drawing_anchors(root: ET.Element) -> list[ET.Element]:
    return [node for node in list(root) if _local_name(node.tag) in {"oneCellAnchor", "twoCellAnchor", "absoluteAnchor"}]


def _first_image_rel_id(anchor: ET.Element) -> str:
    for node in anchor.iter():
        if _local_name(node.tag) != "blip":
            continue
        rel_id = node.attrib.get(f"{{{_OFFICE_REL_NS}}}embed") or node.attrib.get(f"{{{_OFFICE_REL_NS}}}link") or ""
        if rel_id:
            return rel_id
    return ""


def _xlsx_anchor_position(sheet_index: int, sheet_name: str, anchor: ET.Element, image_index: int) -> str:
    row = None
    col = None
    for child in list(anchor):
        if _local_name(child.tag) != "from":
            continue
        for item in list(child):
            if _local_name(item.tag) == "row" and item.text is not None:
                row = int(item.text) + 1
            if _local_name(item.tag) == "col" and item.text is not None:
                col = int(item.text) + 1
        break
    sheet_part = f"sheet{sheet_index:03d}-{_safe_name(sheet_name, 'sheet')}"
    if row is not None and col is not None:
        return f"{sheet_part}-r{row:04d}c{col:04d}-image{image_index:03d}"
    return f"{sheet_part}-image{image_index:03d}"


def _extract_pdf_images(path: Path, output_dir: Path, source_name: str) -> list[dict]:
    reader = PdfReader(str(path))
    records = []
    sequence = 0
    for page_index, page in enumerate(reader.pages, start=1):
        images = getattr(page, "images", []) or []
        for image_index, image_file in enumerate(images, start=1):
            data = getattr(image_file, "data", b"") or b""
            if not data:
                continue
            sequence += 1
            image_name = str(getattr(image_file, "name", "") or f"page{page_index:03d}-image{image_index:03d}")
            position = f"page{page_index:03d}-image{image_index:03d}"
            records.append(_write_image_record(output_dir, sequence, position, image_name, data, source_name))
    return records


def _extract_html_images(path: Path, output_dir: Path, source_name: str) -> list[dict]:
    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    records = []
    sequence = 0
    for image_index, tag in enumerate(soup.find_all("img"), start=1):
        src = str(tag.get("src") or "").strip()
        if not src.startswith("data:image/"):
            continue
        mime_type, data = _decode_data_uri(src)
        if not data:
            continue
        sequence += 1
        ext = mimetypes.guess_extension(mime_type) or ".png"
        alt = _safe_name(str(tag.get("alt") or ""), "image")
        position = f"html-img{image_index:03d}-{alt}"
        records.append(_write_image_record(output_dir, sequence, position, f"inline{ext}", data, source_name))
    return records


def _decode_data_uri(src: str) -> tuple[str, bytes]:
    header, _, payload = src.partition(",")
    if not payload or ";base64" not in header:
        return "", b""
    mime_type = header[5:].split(";", 1)[0]
    try:
        return mime_type, base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        return "", b""


def _zip_relationships(archive: ZipFile, part_name: str) -> dict[str, str]:
    rels_name = _rels_part_name(part_name)
    if rels_name not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(rels_name))
    relationships = {}
    for rel in root.findall(f"{{{_REL_NS}}}Relationship"):
        rel_id = str(rel.attrib.get("Id") or "")
        target = str(rel.attrib.get("Target") or "")
        if not rel_id or not target or target.startswith(("http://", "https://")):
            continue
        relationships[rel_id] = _resolve_zip_target(part_name, target)
    return relationships


def _rels_part_name(part_name: str) -> str:
    dirname = posixpath.dirname(part_name)
    basename = posixpath.basename(part_name)
    return posixpath.join(dirname, "_rels", f"{basename}.rels")


def _resolve_zip_target(part_name: str, target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(part_name), target))


def _write_image_record(
    output_dir: Path,
    sequence: int,
    position: str,
    source_image_name: str,
    data: bytes,
    source_document_name: str,
) -> dict:
    ext = _image_extension(source_image_name, data)
    position_part = _safe_name(position, f"image{sequence:04d}")
    filename = _unique_filename(output_dir, f"{sequence:04d}_{position_part}", ext)
    destination = output_dir / filename
    destination.write_bytes(data)
    mime_type = _mime_type_for_path(destination)
    return {
        "id": f"image-{sequence:04d}",
        "filename": filename,
        "stored_filename": filename,
        "relative_path": filename,
        "mime_type": mime_type,
        "position": position,
        "source": source_document_name,
        "size_bytes": destination.stat().st_size,
    }


def _image_extension(source_name: str, data: bytes) -> str:
    suffix = Path(str(source_name or "")).suffix.lower().lstrip(".")
    if suffix == "jpe":
        suffix = "jpg"
    if suffix in SUPPORTED_IMAGE_TYPES:
        return "jpg" if suffix == "jpeg" else suffix
    if suffix and (mimetypes.guess_type(f"file.{suffix}")[0] or "").startswith("image/"):
        return "jpg" if suffix == "jpeg" else suffix
    guessed = mimetypes.guess_extension(_mime_type_from_magic(data) or "")
    if guessed:
        suffix = guessed.lower().lstrip(".")
        return "jpg" if suffix == "jpeg" else suffix
    return "bin"


def _mime_type_for_path(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(str(path))
    return mime_type or "application/octet-stream"


def _mime_type_from_magic(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return ""


def _unique_filename(output_dir: Path, stem: str, extension: str) -> str:
    candidate = f"{stem}.{extension}"
    if not (output_dir / candidate).exists():
        return candidate
    for index in range(2, 1000):
        candidate = f"{stem}-{index}.{extension}"
        if not (output_dir / candidate).exists():
            return candidate
    raise ImageExtractionError("提取图片数量过多，无法生成唯一文件名。")


def _safe_name(value: str, fallback: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f/\\<>:\"|?*\s]+", "-", str(value or "")).strip(" .-_")
    value = re.sub(r"-+", "-", value)
    return value[:120] or fallback


def _direct_children(element: ET.Element, local_name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == local_name]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
