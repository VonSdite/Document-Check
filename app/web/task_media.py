import io
import mimetypes
import zipfile
from pathlib import Path

from flask import abort, current_app, flash, redirect, request, send_file, url_for

from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.documents.images import image_path_from_item
from app.tasks.files import (
    _image_folder,
    _task_document_groups,
    _task_image_items,
    _task_source_files_available,
    _task_upload_path,
)


def _download_task_document(task, fallback_endpoint: str):
    if task["task_type"] in {CONSISTENCY_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE}:
        return _download_task_documents_zip(task, fallback_endpoint)

    upload_path = _task_upload_path(task)
    if not upload_path.is_file():
        flash("原文件已清理或缺失，无法下载。", "error")
        return redirect(
            request.referrer or url_for(fallback_endpoint, task_id=task["id"])
        )
    return send_file(
        upload_path,
        as_attachment=True,
        download_name=task["original_filename"],
    )


def _task_video_stream_url(task, endpoint: str) -> str:
    if (task["task_type"] or DOCUMENT_TASK_TYPE) != VIDEO_TASK_TYPE:
        return ""
    if not _task_source_files_available(task):
        return ""
    return url_for(endpoint, task_id=task["id"])


def _stream_task_video(task):
    if (task["task_type"] or DOCUMENT_TASK_TYPE) != VIDEO_TASK_TYPE:
        abort(404)
    upload_path = _task_upload_path(task)
    if not upload_path.is_file():
        abort(404)
    mimetype = (
        mimetypes.guess_type(task["original_filename"])[0] or "application/octet-stream"
    )
    return send_file(
        upload_path,
        mimetype=mimetype,
        as_attachment=False,
        download_name=task["original_filename"],
        conditional=True,
    )


def _send_task_media(task, media_id: str):
    normalized_id = str(media_id or "").strip()
    if not normalized_id:
        abort(404)
    media_item = next(
        (
            item
            for item in _task_image_items(task)
            if normalized_id
            in {
                str(item.get("id") or ""),
                str(item.get("filename") or ""),
                str(item.get("stored_filename") or ""),
            }
        ),
        None,
    )
    if media_item is None:
        abort(404)
    media_path = image_path_from_item(_image_folder(), media_item)
    if media_path is None or not media_path.is_file():
        abort(404)
    return send_file(
        media_path,
        mimetype=str(media_item.get("mime_type") or "application/octet-stream"),
        as_attachment=False,
        download_name=str(media_item.get("filename") or media_path.name),
        conditional=True,
    )


def _attach_report_media_urls(results: list[dict], task, endpoint: str) -> None:
    available_items = [
        item
        for item in _task_image_items(task)
        if (media_path := image_path_from_item(_image_folder(), item)) is not None
        and media_path.is_file()
    ]
    available_ids = {
        value
        for item in available_items
        for value in (
            str(item.get("id") or "").strip(),
            str(item.get("filename") or "").strip(),
            str(item.get("stored_filename") or "").strip(),
        )
        if value
    }
    if not available_ids:
        return
    for result in results:
        for report_item in list(result.get("report_items") or []) + list(
            result.get("suppressed_report_items") or []
        ):
            for ref in report_item.get("evidence_refs") or []:
                media_id = str(ref.get("id") or ref.get("filename") or "").strip()
                if media_id in available_ids:
                    ref["url"] = url_for(
                        endpoint, task_id=task["id"], media_id=media_id
                    )


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
