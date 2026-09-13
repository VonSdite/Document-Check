import io
import os
import re
import uuid
import zipfile
from pathlib import Path

from flask import (
    current_app,
    flash,
    redirect,
    request,
    send_file,
    url_for,
)

from .db import get_setting, now_text
from .file_cleanup import remove_directory_tree, remove_file
from .file_cleanup import remove_empty_directory as cleanup_remove_empty_directory
from .images import default_image_folder, image_items_from_meta, image_path_from_item
from .task_types import (
    CONSISTENCY_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    document_groups_from_meta,
)

UPLOAD_PATH_SAFE_CHARS = 240
UPLOAD_FILENAME_SAFE_CHARS = 180
INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f/\\<>:"|?*]+')


def _remove_uploaded_file(path: Path):
    ok, error = remove_file(path)
    if not ok:
        current_app.logger.warning("删除文件失败 path=%s error=%s", path, error)
    return ok, error


def _save_uploaded_file(upload, destination: Path) -> int:
    try:
        upload.save(destination)
        return os.path.getsize(destination)
    except Exception:
        _remove_uploaded_file(destination)
        raise


def _remove_uploaded_files(paths: list[Path]):
    failures = []
    for path in paths:
        ok, error = _remove_uploaded_file(path)
        if not ok:
            failures.append((path, error))
    return failures


def _remove_directory(path: Path):
    ok, error = remove_directory_tree(path)
    if not ok:
        current_app.logger.warning("删除目录失败 path=%s error=%s", path, error)
    return ok


def _remove_empty_directory(path: Path):
    return cleanup_remove_empty_directory(path)


def _task_upload_path(task) -> Path:
    return (
        Path(current_app.config["UPLOAD_FOLDER"]) / Path(task["stored_filename"]).name
    )


def _task_upload_paths(task) -> list[Path]:
    paths = _task_source_file_paths(task)
    for image in _task_image_items(task):
        image_path = image_path_from_item(_image_folder(), image)
        if image_path is not None:
            paths.append(image_path)
    return paths


def _task_source_file_paths(task) -> list[Path]:
    groups = _task_document_groups(task)
    upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
    if not groups:
        return [_task_upload_path(task)]

    paths = []
    for group in groups:
        for file_info in group["files"]:
            stored_filename = Path(str(file_info.get("stored_filename") or "")).name
            if stored_filename:
                paths.append(upload_folder / stored_filename)
    return paths


def _task_source_files_available(task) -> bool:
    groups = _task_document_groups(task)
    if (
        task["task_type"] in {CONSISTENCY_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE}
        and not groups
    ):
        return False
    if groups and any(
        not Path(str(file_info.get("stored_filename") or "")).name
        for group in groups
        for file_info in group["files"]
    ):
        return False
    paths = _task_source_file_paths(task)
    return bool(paths) and all(path.is_file() for path in paths)


def _task_document_groups(task) -> list[dict]:
    return document_groups_from_meta(task["document_meta_json"])


def _task_image_items(task) -> list[dict]:
    raw = task["document_meta_json"]
    return (
        image_items_from_meta(raw)
        + image_items_from_meta(raw, "page_images")
        + image_items_from_meta(raw, "frames")
    )


def _image_folder() -> Path:
    configured = current_app.config.get("IMAGE_FOLDER")
    if configured:
        return Path(configured)
    return default_image_folder(current_app.config["UPLOAD_FOLDER"])


def _int_setting(key: str, default: int) -> int:
    value = get_setting(key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _download_task_documents_zip(task, fallback_endpoint: str):
    groups = _task_document_groups(task)
    if not groups:
        flash("文档信息缺失，无法下载。", "error")
        return redirect(
            request.referrer or url_for(fallback_endpoint, task_id=task["id"])
        )
    if not _task_source_files_available(task):
        flash("部分或全部原文件已清理或缺失，无法完整下载。", "error")
        return redirect(
            request.referrer or url_for(fallback_endpoint, task_id=task["id"])
        )

    upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        used_names = set()
        for group in groups:
            for file_info in group["files"]:
                stored_filename = Path(str(file_info.get("stored_filename") or "")).name
                if not stored_filename:
                    continue
                upload_path = upload_folder / stored_filename
                archive_name = _unique_archive_name(
                    used_names,
                    f"{group['label']}/{Path(str(file_info.get('original_filename') or stored_filename)).name}",
                )
                archive.write(upload_path, archive_name)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{task['task_type'] or 'document-check'}-{task['id']}-documents.zip",
    )


def _unique_archive_name(used_names: set[str], archive_name: str) -> str:
    archive_name = archive_name.strip("/\\") or "document"
    if archive_name not in used_names:
        used_names.add(archive_name)
        return archive_name
    path = Path(archive_name)
    parent = str(path.parent)
    stem = path.stem or "document"
    suffix = path.suffix
    for index in range(2, 1000):
        candidate_name = f"{stem}-{index}{suffix}"
        candidate = (
            f"{parent}/{candidate_name}" if parent and parent != "." else candidate_name
        )
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
    raise RuntimeError("压缩包内文件名过多，无法生成唯一名称")


def _clean_upload_filename(filename: str, file_type: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    name = _safe_filename_part(name, f"document.{file_type}")
    if "." not in name:
        name = f"{name}.{file_type}"
    return name


def _upload_destination(
    original_filename: str, ip: str, created_at: str, file_type: str
) -> tuple[str, Path]:
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    timestamp = re.sub(r"\D", "", created_at) or now_text().replace("-", "").replace(
        ":", ""
    ).replace(" ", "")
    raw_stem = _limit_utf8_bytes(
        _safe_filename_part(Path(original_filename).stem, "document"), 140
    )
    raw_ip_part = _limit_utf8_bytes(_safe_filename_part(ip, "0.0.0.0"), 80)
    token = uuid.uuid4().hex[:12]
    stored_filename = _stored_upload_filename(
        raw_stem, raw_ip_part, timestamp, token, file_type, upload_dir
    )
    destination = upload_dir / stored_filename
    if not destination.exists():
        return stored_filename, destination

    for index in range(2, 1000):
        candidate = _stored_upload_filename(
            raw_stem, raw_ip_part, timestamp, token, file_type, upload_dir, index=index
        )
        destination = upload_dir / candidate
        if not destination.exists():
            return candidate, destination
    raise RuntimeError("无法保存上传文档，请稍后再试")


def _stored_upload_filename(
    stem: str,
    ip_part: str,
    timestamp: str,
    token: str,
    file_type: str,
    upload_dir: Path,
    *,
    index: int | None = None,
) -> str:
    budget = _upload_filename_char_budget(upload_dir)
    index_suffix = f"-{index}" if index else ""
    fixed_suffix = f"__{timestamp}_{token}{index_suffix}.{file_type}"
    if budget < len(fixed_suffix) + 2:
        raise RuntimeError(
            "上传目录路径过长，无法生成可保存文件名，请将项目目录移动到更短路径后重试。"
        )
    max_ip_chars = max(1, budget - len(fixed_suffix) - 1)
    ip_part = _limit_chars(ip_part, max_ip_chars)
    suffix = f"_{ip_part}_{timestamp}_{token}{index_suffix}.{file_type}"
    max_stem_chars = max(1, budget - len(suffix))
    return f"{_limit_chars(stem, max_stem_chars)}{suffix}"


def _upload_filename_char_budget(upload_dir: Path) -> int:
    try:
        upload_dir_text = str(upload_dir.resolve())
    except OSError:
        upload_dir_text = str(upload_dir.absolute())
    path_budget = UPLOAD_PATH_SAFE_CHARS - len(upload_dir_text) - 1
    return min(UPLOAD_FILENAME_SAFE_CHARS, path_budget)


def _safe_filename_part(value: str, fallback: str) -> str:
    value = INVALID_FILENAME_CHARS.sub("_", value).strip(" ._")
    value = re.sub(r"_+", "_", value)
    return value or fallback


def _limit_chars(value: str, max_chars: int) -> str:
    value = str(value or "").strip(" ._")
    if max_chars <= 0:
        return "d"
    value = value[:max_chars].strip(" ._")
    if value:
        return value
    return "document"[:max_chars] or "d"


def _limit_utf8_bytes(value: str, max_bytes: int) -> str:
    while len(value.encode("utf-8")) > max_bytes:
        value = value[:-1]
    return value or "document"
