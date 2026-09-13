import json
from pathlib import Path

from ..db import get_setting, now_text
from ..documents import (
    DocumentReadError,
    extract_document,
    extract_text,
    format_document_text,
)
from ..file_cleanup import remove_directory_tree
from ..images import (
    DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES,
    candidate_pdf_pages_for_image_check,
    extract_images,
    format_image_document_text,
    image_items_from_meta,
    render_pdf_page_images,
)
from ..language_consistency import compose_language_consistency_text
from ..task_types import (
    CONSISTENCY_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
    document_groups_from_meta,
)
from ..videos import extract_video_frames, format_video_document_text
from .artifacts import _path_is_relative_to, _task_image_folder
from .state import TaskCanceled


def _task_value(task, key: str):
    if hasattr(task, "keys") and key in task.keys():
        return task[key]
    if isinstance(task, dict):
        return task.get(key)
    return None


def _document_meta(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _int_setting(key: str, default: int) -> int:
    value = get_setting(key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _prepare_task_inputs(
    app, db, task, task_type: str, claim_token: str | None
) -> tuple[str, str | None]:
    if task_type == IMAGE_TASK_TYPE:
        return _prepare_image_task_inputs(app, db, task, claim_token)
    if task_type == VIDEO_TASK_TYPE:
        return _prepare_video_task_inputs(app, db, task, claim_token)
    if task_type in {CONSISTENCY_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE}:
        return _prepare_consistency_task_inputs(app, db, task, task_type, claim_token)

    document_text = str(_task_value(task, "document_text") or "")
    document_meta_raw = _task_value(task, "document_meta_json")
    if not document_text:
        upload_path = _task_upload_path(app, task)
        extracted_text, hyperlinks = extract_document(
            upload_path,
            task["file_type"],
        )
        extracted_text = extracted_text.strip()
        if not extracted_text:
            raise RuntimeError("未能从文档中提取到可检查文本")
        document_text = format_document_text(task["original_filename"], extracted_text)
        document_meta = _document_meta(document_meta_raw)
        document_meta["hyperlinks"] = [
            {**item, "source": task["original_filename"]} for item in hyperlinks
        ]
        document_text, document_meta_raw = _persist_preprocessed_inputs(
            db,
            task,
            document_text,
            document_meta,
            claim_token,
        )
    _validate_model_input(document_text, task["max_input_chars"])
    return document_text, document_meta_raw


def _prepare_image_task_inputs(
    app, db, task, claim_token: str | None
) -> tuple[str, str]:
    document_meta_raw = _task_value(task, "document_meta_json")
    document_meta = _document_meta(document_meta_raw)
    images = image_items_from_meta(document_meta_raw)
    page_images = image_items_from_meta(document_meta_raw, "page_images")
    document_text = str(_task_value(task, "document_text") or "")
    if images or page_images:
        if not document_text:
            document_text = format_image_document_text(
                task["original_filename"],
                images,
                page_images=page_images,
                page_selection=document_meta.get("page_selection"),
            )
            document_text, document_meta_raw = _persist_preprocessed_inputs(
                db,
                task,
                document_text,
                document_meta,
                claim_token,
            )
        _validate_model_input(document_text, task["max_input_chars"], "图片检查上下文")
        return document_text, document_meta_raw or "{}"

    upload_path = _task_upload_path(app, task)
    output_dir = _task_generated_output_dir(app, task)
    _reset_generated_output_dir(app, output_dir)
    extracted_text = ""
    text_error = ""
    try:
        try:
            extracted_text = extract_text(
                upload_path,
                task["file_type"],
                include_tables=False,
            ).strip()
        except DocumentReadError as exc:
            text_error = str(exc)
            app.logger.warning(
                "图片检查任务未能提取文档文本 task_id=%s file=%s error=%s",
                task["id"],
                task["original_filename"],
                exc,
            )

        image_error = ""
        try:
            images = extract_images(
                upload_path,
                task["file_type"],
                output_dir,
                source_filename=task["original_filename"],
            )
        except DocumentReadError as exc:
            images = []
            image_error = str(exc)
            _reset_generated_output_dir(app, output_dir)
            app.logger.warning(
                "图片检查任务未能提取 PDF 内嵌图片 task_id=%s file=%s error=%s",
                task["id"],
                task["original_filename"],
                exc,
            )

        candidate_pages = candidate_pdf_pages_for_image_check(extracted_text, images)
        page_images, page_selection = render_pdf_page_images(
            upload_path,
            output_dir,
            source_filename=task["original_filename"],
            max_pages=max(
                1,
                _int_setting(
                    "image_page_check_max_pages", DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES
                ),
            ),
            candidate_pages=candidate_pages,
        )
        if not images and not page_images:
            raise RuntimeError("未能从 PDF 中生成可检查页面截图或提取到可检查图片")

        document_text = format_image_document_text(
            task["original_filename"],
            images,
            document_text=extracted_text,
            text_error=text_error,
            page_images=page_images,
            page_selection=page_selection,
        )
        _validate_model_input(document_text, task["max_input_chars"], "图片检查上下文")
        document_meta.update(
            {
                "image_extraction_error": image_error,
                "page_selection": page_selection,
                "images": _generated_items_with_relative_path(images, output_dir),
                "page_images": _generated_items_with_relative_path(
                    page_images, output_dir
                ),
            }
        )
        return _persist_preprocessed_inputs(
            db,
            task,
            document_text,
            document_meta,
            claim_token,
        )
    except TaskCanceled:
        raise
    except Exception:
        _remove_generated_output_dir(app, output_dir)
        raise


def _prepare_video_task_inputs(
    app, db, task, claim_token: str | None
) -> tuple[str, str]:
    document_meta_raw = _task_value(task, "document_meta_json")
    document_meta = _document_meta(document_meta_raw)
    frames = image_items_from_meta(document_meta_raw, "frames")
    document_text = str(_task_value(task, "document_text") or "")
    if frames:
        if not document_text:
            document_text = format_video_document_text(
                task["original_filename"],
                frames,
                document_meta.get("frame_selection"),
            )
            document_text, document_meta_raw = _persist_preprocessed_inputs(
                db,
                task,
                document_text,
                document_meta,
                claim_token,
            )
        _validate_model_input(document_text, task["max_input_chars"], "视频帧上下文")
        return document_text, document_meta_raw or "{}"

    upload_path = _task_upload_path(app, task)
    output_dir = _task_generated_output_dir(app, task)
    _reset_generated_output_dir(app, output_dir)
    try:
        frames, frame_selection = extract_video_frames(
            upload_path,
            output_dir,
            source_filename=task["original_filename"],
        )
        if frame_selection.get("fallback_frame_count") or frame_selection.get(
            "skipped_frame_count"
        ):
            app.logger.warning(
                "视频抽帧启用容错 task_id=%s file=%s fallback=%s skipped=%s skipped_timestamps=%s",
                task["id"],
                task["original_filename"],
                frame_selection.get("fallback_frame_count", 0),
                frame_selection.get("skipped_frame_count", 0),
                frame_selection.get("skipped_timestamps", []),
            )
        if not frames:
            raise RuntimeError("未能从视频中抽取可检查画面")
        document_text = format_video_document_text(
            task["original_filename"], frames, frame_selection
        )
        _validate_model_input(document_text, task["max_input_chars"], "视频帧上下文")
        document_meta.update(
            {
                "frame_selection": frame_selection,
                "frames": _generated_items_with_relative_path(frames, output_dir),
            }
        )
        return _persist_preprocessed_inputs(
            db,
            task,
            document_text,
            document_meta,
            claim_token,
        )
    except TaskCanceled:
        raise
    except Exception:
        _remove_generated_output_dir(app, output_dir)
        raise


def _prepare_consistency_task_inputs(
    app,
    db,
    task,
    task_type: str,
    claim_token: str | None,
) -> tuple[str, str | None]:
    document_text = str(_task_value(task, "document_text") or "")
    document_meta_raw = _task_value(task, "document_meta_json")
    if not document_text:
        document_text, document_meta = _build_consistency_task_inputs(
            app, task, task_type
        )
        document_text, document_meta_raw = _persist_preprocessed_inputs(
            db,
            task,
            document_text,
            document_meta,
            claim_token,
        )
    _validate_model_input(document_text, task["max_input_chars"])
    return document_text, document_meta_raw


def _build_consistency_task_inputs(app, task, task_type: str) -> tuple[str, dict]:
    groups = _extract_consistency_groups(app, task)
    document_meta = _document_meta(task["document_meta_json"])
    if task_type == LANGUAGE_CONSISTENCY_TASK_TYPE:
        groups_by_role = {group["role"]: group for group in groups}
        group_a = groups_by_role.get("document_a")
        group_b = groups_by_role.get("document_b")
        if not group_a or not group_b:
            raise RuntimeError("跨语种检查缺少文档A或文档B信息")
        file_a = group_a["files"][0]
        file_b = group_b["files"][0]
        document_text, static_summary = compose_language_consistency_text(
            file_a, file_b
        )
        document_meta["static_precheck"] = static_summary
        return document_text, document_meta
    return _format_consistency_document_text(groups), document_meta


def _extract_consistency_document_text(app, task) -> str:
    task_type = _task_value(task, "task_type") or CONSISTENCY_TASK_TYPE
    document_text, _document_meta_value = _build_consistency_task_inputs(
        app, task, task_type
    )
    return document_text


def _extract_consistency_groups(app, task) -> list[dict]:
    groups = document_groups_from_meta(task["document_meta_json"])
    if not groups:
        raise RuntimeError("多文档对照检查缺少文档组信息")

    upload_folder = Path(app.config["UPLOAD_FOLDER"])
    extracted_groups = []
    for group in groups:
        label = group["label"]
        extracted_files = []
        for index, file_info in enumerate(group["files"], start=1):
            stored_filename = Path(str(file_info.get("stored_filename") or "")).name
            file_type = str(file_info.get("file_type") or "").lower()
            original_filename = str(
                file_info.get("original_filename") or stored_filename or f"文档{index}"
            )
            if not stored_filename or not file_type:
                raise RuntimeError(f"{label}第 {index} 个文档信息不完整")
            upload_path = upload_folder / stored_filename
            if not upload_path.is_file():
                raise RuntimeError(f"{label}“{original_filename}”已删除，无法检查")
            try:
                text = extract_text(upload_path, file_type).strip()
            except DocumentReadError as exc:
                raise DocumentReadError(f"{label}“{original_filename}”：{exc}") from exc
            if not text:
                raise RuntimeError(f"{label}“{original_filename}”未能提取到可检查文本")
            extracted_files.append(
                {**file_info, "original_filename": original_filename, "text": text}
            )
        extracted_groups.append({**group, "files": extracted_files})
    return extracted_groups


def _format_consistency_document_text(groups: list[dict]) -> str:
    sections = []
    for group in groups:
        label = group["label"]
        group_parts = [f"# {label}"]
        for index, file_info in enumerate(group["files"], start=1):
            group_parts.append(
                f"## {label}{index}：{file_info['original_filename']}\n{file_info['text']}"
            )
        sections.append("\n\n".join(group_parts))
    return "\n\n".join(sections).strip()


def _task_upload_path(app, task) -> Path:
    stored_filename = Path(str(_task_value(task, "stored_filename") or "")).name
    if not stored_filename:
        raise RuntimeError("任务缺少源文件信息")
    upload_path = Path(app.config["UPLOAD_FOLDER"]) / stored_filename
    if not upload_path.is_file():
        raise RuntimeError(f"源文件“{task['original_filename']}”已删除，无法检查")
    return upload_path


def _task_generated_output_dir(app, task) -> Path:
    stored_filename = Path(str(_task_value(task, "stored_filename") or "")).name
    folder_name = Path(stored_filename).stem or f"task-{task['id']}"
    return _task_image_folder(app) / folder_name


def _generated_items_with_relative_path(
    items: list[dict], output_dir: Path
) -> list[dict]:
    return [
        {
            **item,
            "relative_path": f"{output_dir.name}/{item['filename']}",
        }
        for item in items
    ]


def _reset_generated_output_dir(app, output_dir: Path):
    _remove_generated_output_dir(app, output_dir, required=True)


def _remove_generated_output_dir(app, output_dir: Path, *, required: bool = False):
    image_root = _task_image_folder(app)
    if output_dir.resolve() == image_root.resolve() or not _path_is_relative_to(
        output_dir, image_root
    ):
        raise RuntimeError("任务生成物目录不安全，已停止清理")
    ok, error = remove_directory_tree(output_dir)
    if not ok:
        message = f"清理任务生成物失败：{error or output_dir.name}"
        if required:
            raise RuntimeError(message)
        app.logger.warning("%s", message)


def _persist_preprocessed_inputs(
    db,
    task,
    document_text: str,
    document_meta: dict,
    claim_token: str | None,
) -> tuple[str, str]:
    _validate_model_input(document_text, task["max_input_chars"])
    completed_at = now_text()
    document_meta["preprocessing"] = {
        "status": "completed",
        "completed_at": completed_at,
    }
    document_meta_raw = json.dumps(document_meta, ensure_ascii=False)
    updated = db.execute(
        """
        UPDATE tasks
        SET document_text = ?,
            document_meta_json = ?,
            progress = MAX(progress, 4),
            summary = ?,
            updated_at = ?
        WHERE id = ? AND status = 'running'
          AND (? IS NULL OR claim_token = ?)
        """,
        (
            document_text,
            document_meta_raw,
            "预处理完成，正在执行检查",
            completed_at,
            task["id"],
            claim_token,
            claim_token,
        ),
    )
    db.commit()
    if updated.rowcount != 1:
        raise TaskCanceled
    return document_text, document_meta_raw


def _validate_model_input(
    document_text: str, max_input_chars: int, label: str = "文档文本"
):
    if not document_text:
        raise RuntimeError("未能从文档中提取到可检查文本")
    if len(document_text) > max_input_chars:
        raise RuntimeError(
            f"{label} {len(document_text)} 字，超过当前模型文本上限 {max_input_chars} 字"
        )
