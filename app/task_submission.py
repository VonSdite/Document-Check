import json
import re
import sqlite3
import uuid
from pathlib import Path

from flask import current_app, flash, redirect, request, url_for

from .auth import UserIdentity
from .db import get_db, now_text
from .documents import allowed_file, extension_of
from .model_service import _find_enabled_model
from .task_files import (
    _clean_upload_filename,
    _remove_uploaded_file,
    _remove_uploaded_files,
    _save_uploaded_file,
    _upload_destination,
)
from .task_types import (
    CONSISTENCY_MAX_DATA_FILES,
    CONSISTENCY_MAX_MATERIAL_FILES,
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
    document_groups_from_meta,
)
from .videos import allowed_video_file, video_extension_of

SUBMISSION_TOKEN_RE = re.compile(r"[0-9a-f]{32}")


def _row_value(row, key: str, default=None):
    if row is None:
        return default
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def _consistency_task_title(task, include_all: bool = False) -> str:
    groups = document_groups_from_meta(_row_value(task, "document_meta_json"))
    title = _consistency_title_from_groups(groups, include_all=include_all)
    if title:
        return title
    return str(
        _row_value(task, "original_filename", "多文档对照检查") or "多文档对照检查"
    )


def _consistency_title_from_groups(
    groups: list[dict], *, include_all: bool = False
) -> str:
    parts = []
    for group in groups:
        names = [
            Path(str(file_info.get("original_filename") or "")).name.strip()
            for file_info in group.get("files", [])
        ]
        names = [name for name in names if name]
        if not names:
            continue
        label = str(group.get("label") or "文档").strip() or "文档"
        if include_all or len(names) <= 2:
            summary = "、".join(names)
        else:
            summary = f"{'、'.join(names[:2])} 等{len(names)}个"
        parts.append(f"{label}：{summary}")
    return " / ".join(parts)


def _enabled_check_item_snapshots(
    db, check_ids: list[int], task_type: str
) -> list[dict]:
    unique_ids = []
    seen = set()
    for check_id in check_ids:
        if check_id not in seen:
            unique_ids.append(check_id)
            seen.add(check_id)
    if not unique_ids:
        return []

    placeholders = ",".join("?" for _ in unique_ids)
    rows = db.execute(
        f"""
        SELECT id, code, name, prompt
        FROM check_items
        WHERE id IN ({placeholders}) AND task_type = ? AND enabled = 1
        ORDER BY sort_order ASC, id ASC
        """,
        tuple(unique_ids + [task_type]),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "code": row["code"],
            "name": row["name"],
            "prompt": row["prompt"],
        }
        for row in rows
    ]


def create_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    db = get_db()
    uploads = _selected_uploads("document")
    if not uploads:
        flash("请选择要上传的文档。", "error")
        return _back_to_task_form(admin_created)
    for upload in uploads:
        if not allowed_file(upload.filename):
            flash(
                f"“{upload.filename}”不是支持的文件类型，仅支持 docx、pdf、txt、md、html、xlsx、xlsm、xls 文件。",
                "error",
            )
            return _back_to_task_form(admin_created)

    check_ids = [
        int(value) for value in request.form.getlist("checks") if value.isdigit()
    ]
    if not check_ids:
        flash("请至少选择一个检查项。", "error")
        return _back_to_task_form(admin_created)
    check_snapshots = _enabled_check_item_snapshots(db, check_ids, DOCUMENT_TASK_TYPE)
    if len(check_snapshots) != len(set(check_ids)):
        flash("请选择当前可用的检查项。", "error")
        return _back_to_task_form(admin_created)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id, identity.subject)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created)

    saved_paths: list[Path] = []
    try:
        rows = [
            _prepare_document_task_row(
                upload, identity, model, check_ids, check_snapshots, saved_paths
            )
            for upload in uploads
        ]
    except Exception as exc:
        _remove_uploaded_files(saved_paths)
        current_app.logger.exception("准备单文档检查任务失败")
        flash(_unexpected_upload_preparation_message(exc), "error")
        return _back_to_task_form(admin_created)

    try:
        db.executemany(
            """
            INSERT INTO tasks(
                task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source,
                original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json, provider_id, provider_name, model_name,
                api_base, api_key, request_timeout, max_input_chars, force_disable_thinking, reasoning_effort,
                status, progress, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
            """,
            rows,
        )
        db.commit()
    except Exception:
        db.rollback()
        _remove_uploaded_files(saved_paths)
        current_app.logger.exception("创建单文档检查任务失败")
        flash("创建任务失败，请稍后再试。", "error")
        return _back_to_task_form(admin_created)
    if admin_created:
        return redirect(url_for("admin_tasks"))
    return redirect(url_for("user_tasks"))


def _prepare_document_task_row(
    upload,
    identity: UserIdentity,
    model: dict,
    check_ids: list[int],
    check_snapshots: list[dict],
    saved_paths: list[Path],
):
    file_type = extension_of(upload.filename)
    original_filename = _clean_upload_filename(upload.filename, file_type)
    created_at = now_text()
    stored_filename, destination = _upload_destination(
        original_filename,
        identity.subject,
        created_at,
        file_type,
    )
    file_size = _save_uploaded_file(upload, destination)
    saved_paths.append(destination)
    owner_name = identity.display_name or None
    return (
        DOCUMENT_TASK_TYPE,
        identity.ip,
        owner_name,
        identity.subject,
        owner_name,
        identity.source,
        original_filename,
        stored_filename,
        file_type,
        file_size,
        None,
        json.dumps({"preprocessing": {"status": "pending"}}, ensure_ascii=False),
        json.dumps(check_ids, ensure_ascii=False),
        json.dumps(check_snapshots, ensure_ascii=False),
        model["provider_id"],
        model["provider_name"],
        model["model_name"],
        model["api_base"],
        model["api_key"],
        model["request_timeout"],
        model["max_input_chars"],
        1 if model["force_disable_thinking"] else 0,
        model["reasoning_effort"] or None,
        created_at,
        created_at,
    )


def _unexpected_upload_preparation_message(error: Exception) -> str:
    detail = _compact_user_error(error)
    if detail:
        return f"文档上传或读取失败：{detail}。如持续出现请联系管理员查看日志。"
    return "文档上传或读取失败，请稍后重试；如持续出现请联系管理员查看日志。"


def _compact_user_error(error: Exception, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(error or "").strip())
    if not text:
        return ""
    text = re.sub(r"[A-Za-z]:[\\/][^，。；;]*", "[本地路径]", text)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def create_image_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    db = get_db()
    upload = request.files.get("document")
    if upload is None or not upload.filename:
        flash("请选择要提取图片的文档。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)
    file_type = extension_of(upload.filename)
    if file_type != "pdf":
        flash("图片检查仅支持 PDF 文件。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)

    check_ids = [
        int(value) for value in request.form.getlist("checks") if value.isdigit()
    ]
    if not check_ids:
        flash("请至少选择一个图片检查项。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)
    check_snapshots = _enabled_check_item_snapshots(db, check_ids, IMAGE_TASK_TYPE)
    if len(check_snapshots) != len(set(check_ids)):
        flash("请选择当前可用的图片检查项。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id, identity.subject)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)

    original_filename = _clean_upload_filename(upload.filename, file_type)
    created_at = now_text()
    stored_filename, destination = _upload_destination(
        original_filename, identity.subject, created_at, file_type
    )
    try:
        file_size = _save_uploaded_file(upload, destination)
    except Exception:
        current_app.logger.exception("保存图片检查文档失败")
        flash("PDF 上传失败，请稍后再试。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)

    document_meta = {
        "source_document": {
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "file_type": file_type,
            "file_size": file_size,
        },
        "preprocessing": {"status": "pending"},
    }
    owner_name = identity.display_name or None
    try:
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source,
                original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json,
                provider_id, provider_name, model_name, api_base, api_key, request_timeout,
                max_input_chars, force_disable_thinking, reasoning_effort,
                status, progress, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
            """,
            (
                IMAGE_TASK_TYPE,
                identity.ip,
                owner_name,
                identity.subject,
                owner_name,
                identity.source,
                original_filename,
                stored_filename,
                file_type,
                file_size,
                None,
                json.dumps(document_meta, ensure_ascii=False),
                json.dumps(check_ids, ensure_ascii=False),
                json.dumps(check_snapshots, ensure_ascii=False),
                model["provider_id"],
                model["provider_name"],
                model["model_name"],
                model["api_base"],
                model["api_key"],
                model["request_timeout"],
                model["max_input_chars"],
                1 if model["force_disable_thinking"] else 0,
                model["reasoning_effort"] or None,
                created_at,
                created_at,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        _remove_uploaded_file(destination)
        current_app.logger.exception("创建图片检查任务失败")
        flash("创建图片检查任务失败，请稍后再试。", "error")
        return _back_to_task_form(admin_created, IMAGE_TASK_TYPE)
    return redirect(url_for(_task_list_endpoint(admin_created, IMAGE_TASK_TYPE)))


def create_video_task_for_identity(identity: UserIdentity, *, admin_created: bool):
    db = get_db()
    uploads = _selected_uploads("video")
    if not uploads:
        flash("请至少选择一个要质检的视频。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)

    check_ids = [
        int(value) for value in request.form.getlist("checks") if value.isdigit()
    ]
    if not check_ids:
        flash("请至少选择一个视频检查项。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)
    check_snapshots = _enabled_check_item_snapshots(db, check_ids, VIDEO_TASK_TYPE)
    if len(check_snapshots) != len(set(check_ids)):
        flash("请选择当前可用的视频检查项。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id, identity.subject)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created, VIDEO_TASK_TYPE)

    created_count = 0
    failures = []
    for upload in uploads:
        filename = (
            Path(str(upload.filename or "").replace("\\", "/")).name or "未命名视频"
        )
        error = _create_video_task_from_upload(
            db,
            upload,
            identity,
            model,
            check_ids,
            check_snapshots,
        )
        if error:
            failures.append((filename, error))
        else:
            created_count += 1

    if created_count:
        flash(f"已创建 {created_count} 个视频检查任务。", "success")
    if failures:
        flash(_video_task_failure_summary(failures), "error")
    return redirect(url_for(_task_list_endpoint(admin_created, VIDEO_TASK_TYPE)))


def _create_video_task_from_upload(
    db,
    upload,
    identity: UserIdentity,
    model: dict,
    check_ids: list[int],
    check_snapshots: list[dict],
) -> str | None:
    upload_filename = (
        Path(str(upload.filename or "").replace("\\", "/")).name or "未命名视频"
    )
    if not allowed_video_file(upload_filename):
        return "不是支持的视频类型，仅支持 mp4、mov、mkv、webm、avi、m4v 文件。"

    file_type = video_extension_of(upload_filename)
    original_filename = _clean_upload_filename(upload_filename, file_type)
    try:
        created_at = now_text()
        stored_filename, destination = _upload_destination(
            original_filename, identity.subject, created_at, file_type
        )
    except Exception:
        current_app.logger.exception(
            "准备视频检查上传路径失败 file=%s", original_filename
        )
        return "视频上传准备失败，请稍后再试。"
    try:
        file_size = _save_uploaded_file(upload, destination)
    except Exception:
        current_app.logger.exception("保存视频检查文件失败 file=%s", original_filename)
        return "视频上传失败，请稍后再试。"

    try:
        document_meta = {
            "source_video": {
                "original_filename": original_filename,
                "stored_filename": stored_filename,
                "file_type": file_type,
                "file_size": file_size,
            },
            "preprocessing": {"status": "pending"},
        }
        owner_name = identity.display_name or None
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source,
                original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json,
                provider_id, provider_name, model_name, api_base, api_key, request_timeout,
                max_input_chars, force_disable_thinking, reasoning_effort,
                status, progress, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
            """,
            (
                VIDEO_TASK_TYPE,
                identity.ip,
                owner_name,
                identity.subject,
                owner_name,
                identity.source,
                original_filename,
                stored_filename,
                file_type,
                file_size,
                None,
                json.dumps(document_meta, ensure_ascii=False),
                json.dumps(check_ids, ensure_ascii=False),
                json.dumps(check_snapshots, ensure_ascii=False),
                model["provider_id"],
                model["provider_name"],
                model["model_name"],
                model["api_base"],
                model["api_key"],
                model["request_timeout"],
                model["max_input_chars"],
                1 if model["force_disable_thinking"] else 0,
                model["reasoning_effort"] or None,
                created_at,
                created_at,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        _remove_uploaded_file(destination)
        current_app.logger.exception("创建视频检查任务失败 file=%s", original_filename)
        return "创建视频检查任务失败，请稍后再试。"
    return None


def _video_task_failure_summary(
    failures: list[tuple[str, str]], max_items: int = 5
) -> str:
    details = []
    for filename, error in failures[:max_items]:
        compact_error = _compact_user_error(error, limit=240) or "处理失败"
        details.append(f"“{filename}”：{compact_error}")
    omitted_count = len(failures) - len(details)
    suffix = f"；另有 {omitted_count} 个失败视频未展开" if omitted_count > 0 else ""
    return f"{len(failures)} 个视频未创建：" + "；".join(details) + suffix


def create_consistency_task_for_identity(
    identity: UserIdentity, *, admin_created: bool
):
    db = get_db()
    master_uploads = _selected_uploads("master_documents")
    related_uploads = _selected_uploads("related_documents")
    if not _validate_consistency_uploads(
        master_uploads, "素材文档", CONSISTENCY_MAX_MATERIAL_FILES
    ):
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)
    if not _validate_consistency_uploads(
        related_uploads, "资料", CONSISTENCY_MAX_DATA_FILES
    ):
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)

    check_ids = [
        int(value) for value in request.form.getlist("checks") if value.isdigit()
    ]
    if not check_ids:
        flash("请至少选择一个多文档对照项。", "error")
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)
    check_snapshots = _enabled_check_item_snapshots(
        db, check_ids, CONSISTENCY_TASK_TYPE
    )
    if len(check_snapshots) != len(set(check_ids)):
        flash("请选择当前可用的多文档对照项。", "error")
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id, identity.subject)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)

    created_at = now_text()
    saved_paths = []
    try:
        master_files = _save_consistency_upload_group(
            master_uploads, identity.subject, created_at, saved_paths
        )
        related_files = _save_consistency_upload_group(
            related_uploads, identity.subject, created_at, saved_paths
        )
    except Exception:
        _remove_uploaded_files(saved_paths)
        current_app.logger.exception("准备多文档对照任务失败")
        flash("文档上传失败，请稍后再试。", "error")
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)

    document_meta = {
        "groups": [
            {
                "role": "master",
                "label": "素材文档",
                "files": [
                    _persisted_file_info(file_info) for file_info in master_files
                ],
            },
            {
                "role": "related",
                "label": "资料",
                "files": [
                    _persisted_file_info(file_info) for file_info in related_files
                ],
            },
        ],
        "preprocessing": {"status": "pending"},
    }
    all_files = master_files + related_files
    first_file = all_files[0]
    file_size = sum(file_info["file_size"] for file_info in all_files)
    original_filename = _consistency_title_from_groups(document_meta["groups"])
    owner_name = identity.display_name or None

    try:
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source,
                original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json,
                provider_id, provider_name, model_name, api_base, api_key, request_timeout,
                max_input_chars, force_disable_thinking, reasoning_effort,
                status, progress, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
            """,
            (
                CONSISTENCY_TASK_TYPE,
                identity.ip,
                owner_name,
                identity.subject,
                owner_name,
                identity.source,
                original_filename,
                first_file["stored_filename"],
                "多文档",
                file_size,
                None,
                json.dumps(document_meta, ensure_ascii=False),
                json.dumps(check_ids, ensure_ascii=False),
                json.dumps(check_snapshots, ensure_ascii=False),
                model["provider_id"],
                model["provider_name"],
                model["model_name"],
                model["api_base"],
                model["api_key"],
                model["request_timeout"],
                model["max_input_chars"],
                1 if model["force_disable_thinking"] else 0,
                model["reasoning_effort"] or None,
                created_at,
                created_at,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        _remove_uploaded_files(saved_paths)
        current_app.logger.exception("创建多文档对照任务失败")
        flash("创建多文档对照任务失败，请稍后再试。", "error")
        return _back_to_task_form(admin_created, CONSISTENCY_TASK_TYPE)
    return redirect(url_for(_task_list_endpoint(admin_created, CONSISTENCY_TASK_TYPE)))


def create_language_consistency_task_for_identity(
    identity: UserIdentity, *, admin_created: bool
):
    db = get_db()
    submission_token = _request_submission_token()
    if _language_consistency_submission_exists(db, identity.subject, submission_token):
        flash("该跨语种检查任务已提交，无需重复提交。", "success")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)

    document_a = request.files.get("document_a")
    document_b = request.files.get("document_b")
    if not _validate_language_consistency_upload(document_a, "文档A"):
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)
    if not _validate_language_consistency_upload(document_b, "文档B"):
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)

    check_ids = [
        int(value) for value in request.form.getlist("checks") if value.isdigit()
    ]
    if not check_ids:
        flash("请至少选择一个跨语种检查项。", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)
    check_snapshots = _enabled_check_item_snapshots(
        db, check_ids, LANGUAGE_CONSISTENCY_TASK_TYPE
    )
    if len(check_snapshots) != len(set(check_ids)):
        flash("请选择当前可用的跨语种检查项。", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id, identity.subject)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)

    created_at = now_text()
    saved_paths = []
    try:
        file_a = _save_consistency_upload_group(
            [document_a], identity.subject, created_at, saved_paths
        )[0]
        file_b = _save_consistency_upload_group(
            [document_b], identity.subject, created_at, saved_paths
        )[0]
    except Exception:
        _remove_uploaded_files(saved_paths)
        current_app.logger.exception("准备跨语种检查任务失败")
        flash("文档上传失败，请稍后再试。", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)

    document_meta = {
        "groups": [
            {
                "role": "document_a",
                "label": "文档A",
                "files": [_persisted_file_info(file_a)],
            },
            {
                "role": "document_b",
                "label": "文档B",
                "files": [_persisted_file_info(file_b)],
            },
        ],
        "preprocessing": {"status": "pending"},
    }
    file_size = file_a["file_size"] + file_b["file_size"]
    original_filename = (
        f"跨语种检查：{file_a['original_filename']} / {file_b['original_filename']}"
    )
    owner_name = identity.display_name or None

    try:
        db.execute(
            """
            INSERT INTO tasks(
                task_type, ip, username_snapshot, owner_subject, owner_name_snapshot, owner_source, submission_token,
                original_filename, stored_filename, file_type, file_size,
                document_text, document_meta_json, checks_json, checks_snapshot_json,
                provider_id, provider_name, model_name, api_base, api_key, request_timeout,
                max_input_chars, force_disable_thinking, reasoning_effort,
                status, progress, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
            """,
            (
                LANGUAGE_CONSISTENCY_TASK_TYPE,
                identity.ip,
                owner_name,
                identity.subject,
                owner_name,
                identity.source,
                submission_token,
                original_filename,
                file_a["stored_filename"],
                "双文档",
                file_size,
                None,
                json.dumps(document_meta, ensure_ascii=False),
                json.dumps(check_ids, ensure_ascii=False),
                json.dumps(check_snapshots, ensure_ascii=False),
                model["provider_id"],
                model["provider_name"],
                model["model_name"],
                model["api_base"],
                model["api_key"],
                model["request_timeout"],
                model["max_input_chars"],
                1 if model["force_disable_thinking"] else 0,
                model["reasoning_effort"] or None,
                created_at,
                created_at,
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        duplicate = _language_consistency_submission_exists(
            db, identity.subject, submission_token
        )
        _remove_uploaded_files(saved_paths)
        if duplicate:
            flash("该跨语种检查任务已提交，无需重复提交。", "success")
            return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)
        current_app.logger.exception("创建跨语种检查任务失败")
        flash("创建跨语种检查任务失败，请稍后再试。", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)
    except Exception:
        db.rollback()
        _remove_uploaded_files(saved_paths)
        current_app.logger.exception("创建跨语种检查任务失败")
        flash("创建跨语种检查任务失败，请稍后再试。", "error")
        return _back_to_task_form(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE)
    return redirect(
        url_for(_task_list_endpoint(admin_created, LANGUAGE_CONSISTENCY_TASK_TYPE))
    )


def _request_submission_token() -> str:
    value = str(request.form.get("submission_token") or "").strip().lower()
    return value if SUBMISSION_TOKEN_RE.fullmatch(value) else uuid.uuid4().hex


def _language_consistency_submission_exists(
    db, owner_subject: str, submission_token: str
) -> bool:
    return (
        db.execute(
            """
            SELECT 1
            FROM tasks
            WHERE task_type = ? AND owner_subject = ? AND submission_token = ?
            LIMIT 1
            """,
            (LANGUAGE_CONSISTENCY_TASK_TYPE, owner_subject, submission_token),
        ).fetchone()
        is not None
    )


def _back_to_task_form(admin_created: bool, task_type: str = DOCUMENT_TASK_TYPE):
    return redirect(url_for(_task_list_endpoint(admin_created, task_type)))


def _task_list_endpoint(
    admin_created: bool, task_type: str | None = DOCUMENT_TASK_TYPE
) -> str:
    if task_type == CONSISTENCY_TASK_TYPE:
        return "admin_consistency" if admin_created else "user_consistency"
    if task_type == LANGUAGE_CONSISTENCY_TASK_TYPE:
        return (
            "admin_language_consistency"
            if admin_created
            else "user_language_consistency"
        )
    if task_type == IMAGE_TASK_TYPE:
        return "admin_images" if admin_created else "user_images"
    if task_type == VIDEO_TASK_TYPE:
        return "admin_videos" if admin_created else "user_videos"
    return "admin_tasks" if admin_created else "user_tasks"


def _selected_uploads(field_name: str):
    return [
        upload
        for upload in request.files.getlist(field_name)
        if upload and upload.filename
    ]


def _validate_consistency_uploads(uploads: list, label: str, max_files: int) -> bool:
    if not uploads:
        flash(f"请至少选择 1 个{label}。", "error")
        return False
    if len(uploads) > max_files:
        flash(f"{label}最多上传 {max_files} 个。", "error")
        return False
    for upload in uploads:
        if not allowed_file(upload.filename):
            flash(
                f"{label}仅支持 docx、pdf、txt、md、html、xlsx、xlsm、xls 文件。",
                "error",
            )
            return False
    return True


def _validate_language_consistency_upload(upload, label: str) -> bool:
    if upload is None or not upload.filename:
        flash(f"请选择{label}。", "error")
        return False
    if not allowed_file(upload.filename):
        flash(
            f"{label}仅支持 docx、pdf、txt、md、html、xlsx、xlsm、xls 文件。", "error"
        )
        return False
    return True


def _save_consistency_upload_group(
    uploads: list, ip: str, created_at: str, saved_paths: list[Path]
) -> list[dict]:
    files = []
    for upload in uploads:
        file_type = extension_of(upload.filename)
        original_filename = _clean_upload_filename(upload.filename, file_type)
        stored_filename, destination = _upload_destination(
            original_filename, ip, created_at, file_type
        )
        file_size = _save_uploaded_file(upload, destination)
        saved_paths.append(destination)
        files.append(
            {
                "original_filename": original_filename,
                "stored_filename": stored_filename,
                "file_type": file_type,
                "file_size": file_size,
            }
        )
    return files


def _persisted_file_info(file_info: dict) -> dict:
    return {
        "original_filename": file_info["original_filename"],
        "stored_filename": file_info["stored_filename"],
        "file_type": file_info["file_type"],
        "file_size": file_info["file_size"],
    }
