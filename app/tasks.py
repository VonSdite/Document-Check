import hashlib
import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from .common_terms import (
    COMMON_TERMS_CHECK_CODE,
    build_common_terms_invalid_report,
    build_common_terms_missing_report,
    build_common_terms_report,
    common_terms_file_candidates,
    find_common_terms_file,
    format_common_terms_report,
    load_common_terms,
)
from .db import get_bool_setting, get_db, get_setting, now_text
from .documents import DocumentReadError, extract_document, extract_text, format_document_text
from .file_cleanup import (
    describe_failures,
    remove_directory_tree,
    remove_empty_directory as cleanup_remove_empty_directory,
    remove_file,
)
from .images import (
    DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES,
    candidate_pdf_pages_for_image_check,
    default_image_folder,
    extract_images,
    format_image_document_text,
    image_items_from_meta,
    image_path_from_item,
    image_to_data_url,
    page_numbers_from_image_item,
    page_sections_from_document_text,
    render_pdf_page_images,
)
from .hyperlinks import (
    HYPERLINK_CHECK_CODE,
    build_hyperlink_report,
    format_hyperlink_report,
    hyperlinks_from_meta,
)
from .language_consistency import compose_language_consistency_text
from .limits import DEFAULT_ISSUE_OUTPUT_LIMIT, normalize_issue_output_limit
from .llm import (
    LLMError,
    MULTIMODAL_OUTPUT_CONTRACT_MULTI_CHECK,
    run_check,
    run_multimodal_document_check,
)
from .network import outbound_network_config
from .report_guardrails import build_pdf_table_evidence_index, sanitize_text_check_result
from .sensitive_terms import (
    SENSITIVE_TERMS_CHECK_CODE,
    build_sensitive_terms_invalid_report,
    build_sensitive_terms_missing_report,
    build_sensitive_terms_report,
    find_sensitive_terms_file,
    format_sensitive_terms_report,
    load_sensitive_terms,
    sensitive_terms_file_candidates,
)
from .task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
    document_groups_from_meta,
)
from .videos import extract_video_frames, format_video_document_text


class TaskCanceled(Exception):
    pass


class TaskArtifactCleanupError(RuntimeError):
    pass


DEFAULT_CHECK_ITEM_CONCURRENCY = 1
DEFAULT_IMAGE_CHECK_BATCH_SIZE = 4
MAX_IMAGE_CHECK_BATCH_SIZE = 4
MULTIMODAL_CHECK_GROUP_SIZE = 3
IMAGE_CONTEXT_NEIGHBOR_PAGES = 1
IMAGE_DOCUMENT_CONTEXT_MAX_CHARS = 20000
DEFAULT_TASK_FILE_RETENTION_DAYS = 0
TASK_FILE_CLEANUP_INTERVAL_SECONDS = 3600
TASK_FILE_CLEANUP_BATCH_SIZE = 100
TASK_LEASE_SECONDS = 90
TASK_LEASE_RENEW_INTERVAL_SECONDS = 10
TASK_CANCEL_POLL_INTERVAL_SECONDS = 1
REPORT_STATS_REFRESH_INTERVAL_SECONDS = 2
TASK_FILE_CACHE_SNAPSHOT_TTL_SECONDS = 15
STREAM_SNAPSHOT_INTERVAL_SECONDS = 5.0
STREAM_SNAPSHOT_MIN_CHAR_GROWTH = 256
IMAGE_PAGE_CHECK_CODES = {
    "image-text-correspondence",
    "image-ui-step-consistency",
    "image-figure-table-title-standard",
    "image-integrity-clarity",
}
IMAGE_RESOURCE_CHECK_CODES = {
    "image-small-language-text",
    "image-device-installation",
    "image-wiring",
    "image-drawing-standard",
}
IMAGE_CHECK_TARGET_LABELS = {
    "page": "页面级检查",
    "resource": "图片资源检查",
}


_TASK_FILE_CACHE_STATE_INIT_LOCK = threading.Lock()


class TaskScheduler:
    def __init__(self, app):
        self.app = app
        self._stop_event = threading.Event()
        self._cancel_events: dict[int, threading.Event] = {}
        self._cancel_events_lock = threading.Lock()
        self._launcher = threading.Thread(target=self._loop, daemon=True, name="task-launcher")
        self._last_task_file_cleanup = 0.0
        self._last_report_stats_refresh = 0.0

    def start(self):
        self._launcher.start()

    def stop(self):
        self._stop_event.set()
        self._launcher.join(timeout=3)

    def is_alive(self) -> bool:
        return self._launcher.is_alive() and not self._stop_event.is_set()

    def request_cancel(self, task_id: int) -> bool:
        with self._cancel_events_lock:
            cancel_event = self._cancel_events.get(task_id)
        if cancel_event is None:
            return False
        cancel_event.set()
        return True

    def _register_cancel_event(self, task_id: int) -> threading.Event:
        cancel_event = threading.Event()
        with self._cancel_events_lock:
            self._cancel_events[task_id] = cancel_event
        return cancel_event

    def _unregister_cancel_event(self, task_id: int, cancel_event: threading.Event):
        with self._cancel_events_lock:
            if self._cancel_events.get(task_id) is cancel_event:
                self._cancel_events.pop(task_id, None)

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                with self.app.app_context():
                    self._cleanup_task_files_if_due()
                    self._refresh_report_stats_if_due()
                    self._launch_available_tasks()
            except Exception:
                self.app.logger.exception("任务调度循环异常")
            self._stop_event.wait(2)

    def _refresh_report_stats_if_due(self):
        now = time.monotonic()
        if now - self._last_report_stats_refresh < REPORT_STATS_REFRESH_INTERVAL_SECONDS:
            return
        self._last_report_stats_refresh = now
        try:
            # 延迟导入可避免 routes -> tasks 的模块循环依赖；调度器启动时
            # create_app 已完成路由注册，因此此处导入是安全的。
            from .routes import refresh_stale_report_stats_batch

            refreshed = refresh_stale_report_stats_batch()
            if refreshed:
                self.app.logger.info("后台刷新报告统计缓存 count=%s", refreshed)
        except Exception:
            # 统计缓存刷新失败不应阻塞任务领取，下一轮继续重试。
            self.app.logger.exception("后台刷新报告统计缓存失败")

    def _cleanup_task_files_if_due(self):
        now = time.monotonic()
        if now - self._last_task_file_cleanup < TASK_FILE_CLEANUP_INTERVAL_SECONDS:
            return
        self._last_task_file_cleanup = now
        cleanup_expired_task_files(self.app)

    def _launch_available_tasks(self):
        claimed_tasks = self._claim_available_tasks()
        for task_id, claim_token in claimed_tasks:
            worker = threading.Thread(
                target=self._run_task,
                args=(task_id, claim_token),
                daemon=True,
                name=f"task-worker-{task_id}",
            )
            worker.start()

    def _claim_available_tasks(self) -> list[tuple[int, str]]:
        db = get_db()
        claimed_tasks: list[tuple[int, str]] = []
        recovered_count = 0
        canceled_count = 0
        try:
            db.execute("BEGIN IMMEDIATE")
            now = now_text()
            canceled = db.execute(
                """
                UPDATE tasks
                SET status = 'canceled',
                    progress = 0,
                    api_key = NULL,
                    retry_check_codes_json = NULL,
                    claim_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?,
                    finished_at = ?
                WHERE (
                        status = 'canceling'
                        OR (status = 'running' AND cancel_requested = 1)
                      )
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (now, now, now),
            )
            canceled_count = max(0, canceled.rowcount)
            recovered = db.execute(
                """
                UPDATE tasks
                SET status = 'queued',
                    progress = 0,
                    cancel_requested = 0,
                    claim_token = NULL,
                    lease_expires_at = NULL,
                    result_json = CASE
                        WHEN retry_check_codes_json IS NULL THEN NULL
                        ELSE result_json
                    END,
                    summary = NULL,
                    error = NULL,
                    updated_at = ?,
                    started_at = NULL,
                    finished_at = NULL
                WHERE status = 'running'
                  AND cancel_requested = 0
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (now, now),
            )
            recovered_count = max(0, recovered.rowcount)
            db.execute(
                """
                DELETE FROM task_live_results
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM tasks
                    WHERE tasks.id = task_live_results.task_id
                      AND tasks.status IN ('running', 'canceling')
                )
                """
            )

            global_limit = max(1, _int_setting("global_concurrency", 3))
            user_limit = max(1, _int_setting("user_concurrency", 1))
            running_total = db.execute(
                "SELECT COUNT(*) AS total FROM tasks WHERE status IN ('running', 'canceling')"
            ).fetchone()["total"]
            slots = global_limit - running_total
            if slots > 0:
                queued = db.execute(
                    """
                    WITH running_by_owner AS (
                        SELECT owner_subject, COUNT(*) AS running_count
                        FROM tasks
                        WHERE status IN ('running', 'canceling')
                        GROUP BY owner_subject
                    ),
                    ranked_queued AS (
                        SELECT queued.id,
                               queued.owner_subject,
                               COALESCE(running_by_owner.running_count, 0) AS running_for_user,
                               ROW_NUMBER() OVER (
                                   PARTITION BY queued.owner_subject
                                   ORDER BY queued.created_at ASC, queued.id ASC
                               ) AS owner_queue_position,
                               queued.created_at
                        FROM tasks AS queued
                        LEFT JOIN running_by_owner
                          ON running_by_owner.owner_subject = queued.owner_subject
                        WHERE queued.status = 'queued'
                    )
                    SELECT id, owner_subject, running_for_user
                    FROM ranked_queued
                    WHERE owner_queue_position <= ? - running_for_user
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (user_limit, slots),
                ).fetchall()
                running_by_owner: dict[str, int] = {}
                for task in queued:
                    if len(claimed_tasks) >= slots:
                        break
                    owner_subject = str(task["owner_subject"])
                    running_for_user = running_by_owner.setdefault(
                        owner_subject,
                        int(task["running_for_user"] or 0),
                    )
                    if running_for_user >= user_limit:
                        continue

                    claim_token = uuid.uuid4().hex
                    claimed = db.execute(
                        """
                        UPDATE tasks
                        SET status = 'running',
                            progress = 1,
                            claim_token = ?,
                            lease_expires_at = ?,
                            started_at = ?,
                            updated_at = ?
                        WHERE id = ? AND status = 'queued'
                        """,
                        (claim_token, _task_lease_deadline_text(), now, now, task["id"]),
                    )
                    if claimed.rowcount == 1:
                        claimed_tasks.append((task["id"], claim_token))
                        running_by_owner[owner_subject] = running_for_user + 1
            db.commit()
        except Exception:
            db.rollback()
            raise

        if recovered_count:
            self.app.logger.warning("已回收租约过期的运行任务 count=%s", recovered_count)
        if canceled_count:
            self.app.logger.warning("已结束租约过期的取消中任务 count=%s", canceled_count)
        return claimed_tasks

    def _run_task(self, task_id: int, claim_token: str | None = None):
        with self.app.app_context():
            db = get_db()
            task = db.execute(
                """
                SELECT *
                FROM tasks
                WHERE id = ?
                  AND (? IS NULL OR (status IN ('running', 'canceling') AND claim_token = ?))
                """,
                (task_id, claim_token, claim_token),
            ).fetchone()
            if task is None:
                return

            cancel_event = self._register_cancel_event(task_id)
            if task["cancel_requested"] or task["status"] == "canceling":
                cancel_event.set()
            lease_stop, lease_thread = _start_task_lease_heartbeat(
                self.app,
                task_id,
                claim_token,
                cancel_event,
            )
            results = []
            try:
                self.app.logger.info(
                    "任务开始 task_id=%s owner=%s ip=%s file=%s model=%s/%s",
                    task_id,
                    task["owner_subject"] if "owner_subject" in task.keys() and task["owner_subject"] else f"ip:{task['ip']}",
                    task["ip"],
                    task["original_filename"],
                    task["provider_name"],
                    task["model_name"],
                )
                if cancel_event.is_set():
                    _mark_canceled(db, task_id, claim_token)
                    return
                task_type = task["task_type"] or DOCUMENT_TASK_TYPE
                retry_check_codes = _stored_retry_check_codes(task)
                original_results = (
                    _check_results_from_json(_task_value(task, "result_json"))
                    if retry_check_codes is not None
                    else []
                )
                retry_code_set = set(retry_check_codes or [])
                base_results = [
                    result
                    for result in original_results
                    if str(result.get("code") or "").strip() not in retry_code_set
                ]
                if retry_check_codes is not None:
                    self.app.logger.info(
                        "任务重试失败检查项 task_id=%s checks=%s retained=%s",
                        task_id,
                        ",".join(retry_check_codes),
                        len(base_results),
                    )
                max_workers = max(
                    1,
                    _int_setting("check_item_concurrency", DEFAULT_CHECK_ITEM_CONCURRENCY),
                )
                document_text, document_meta_raw = _prepare_task_inputs(
                    self.app,
                    db,
                    task,
                    task_type,
                    claim_token,
                )
                if task_type == IMAGE_TASK_TYPE:
                    image_items = image_items_from_meta(document_meta_raw)
                    page_image_items = image_items_from_meta(document_meta_raw, "page_images")
                    if not image_items and not page_image_items:
                        raise RuntimeError("未能从 PDF 中生成可检查页面截图或提取到可检查图片")
                    check_items = _task_check_items(db, task, IMAGE_TASK_TYPE)
                    check_items = _check_items_for_retry(check_items, retry_check_codes)
                    if not check_items:
                        raise RuntimeError("没有可执行的图片检查项")
                    retry_results = _run_image_check_items_concurrently(
                        self.app,
                        task,
                        check_items,
                        image_items,
                        page_image_items,
                        document_text,
                        document_meta=_document_meta(document_meta_raw),
                        max_workers=max_workers,
                        stream_trace_enabled=get_bool_setting("llm_stream_trace_enabled", False),
                        cancel_event=cancel_event,
                        base_results=base_results,
                    )
                elif task_type == VIDEO_TASK_TYPE:
                    frame_items = image_items_from_meta(document_meta_raw, "frames")
                    if not frame_items:
                        raise RuntimeError("未能从视频中抽取到可检查画面")
                    check_items = _task_check_items(db, task, VIDEO_TASK_TYPE)
                    check_items = _check_items_for_retry(check_items, retry_check_codes)
                    if not check_items:
                        raise RuntimeError("没有可执行的视频检查项")
                    retry_results = _run_video_check_items_concurrently(
                        self.app,
                        task,
                        check_items,
                        frame_items,
                        document_text,
                        document_meta=_document_meta(document_meta_raw),
                        max_workers=max_workers,
                        stream_trace_enabled=get_bool_setting("llm_stream_trace_enabled", False),
                        cancel_event=cancel_event,
                        base_results=base_results,
                    )
                else:
                    if task_type in {CONSISTENCY_TASK_TYPE, LANGUAGE_CONSISTENCY_TASK_TYPE}:
                        check_items = _task_check_items(db, task, task_type)
                    else:
                        check_items = _document_check_items(db, task)

                    check_items = _check_items_for_retry(check_items, retry_check_codes)
                    if not check_items:
                        raise RuntimeError("没有可执行的检查项")

                    retry_results = _run_check_items_concurrently(
                        self.app,
                        task,
                        check_items,
                        document_text,
                        document_meta=_document_meta(document_meta_raw),
                        max_workers=max_workers,
                        stream_trace_enabled=get_bool_setting("llm_stream_trace_enabled", False),
                        cancel_event=cancel_event,
                        base_results=base_results,
                    )
                results = (
                    _merge_check_results(original_results, retry_results)
                    if retry_check_codes is not None
                    else retry_results
                )
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled

                failed_results = _failed_check_results(results)
                successful_results = [result for result in results if not _check_result_failed(result)]
                if failed_results and not successful_results:
                    error = _failed_check_items_error(failed_results)
                    self.app.logger.warning("任务全部检查项失败 task_id=%s error=%s", task_id, error)
                    _mark_failed(db, task_id, error, results, claim_token)
                    return

                final_status = "partial" if failed_results else "completed"
                summary = _build_summary(results)
                error = _failed_check_items_error(failed_results) if failed_results else None
                completed = db.execute(
                    """
                    UPDATE tasks
                    SET status = ?,
                        progress = 100,
                        result_json = ?,
                        summary = ?,
                        error = ?,
                        api_key = NULL,
                        retry_check_codes_json = NULL,
                        claim_token = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?,
                        finished_at = ?
                    WHERE id = ? AND status = 'running' AND cancel_requested = 0
                      AND (? IS NULL OR claim_token = ?)
                    """,
                    (
                        final_status,
                        json.dumps(results, ensure_ascii=False),
                        summary,
                        error,
                        now_text(),
                        now_text(),
                        task_id,
                        claim_token,
                        claim_token,
                    ),
                )
                if completed.rowcount == 1:
                    db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task_id,))
                db.commit()
                if completed.rowcount == 1:
                    if failed_results:
                        self.app.logger.warning(
                            "任务部分完成 task_id=%s succeeded=%s failed=%s",
                            task_id,
                            len(successful_results),
                            len(failed_results),
                        )
                    else:
                        self.app.logger.info("任务完成 task_id=%s checks=%s", task_id, len(results))
                else:
                    if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                        _mark_canceled(db, task_id, claim_token)
                    else:
                        self.app.logger.warning("任务执行权已失效，忽略完成结果 task_id=%s", task_id)
            except TaskCanceled:
                self.app.logger.info("任务取消 task_id=%s", task_id)
                _mark_canceled(db, task_id, claim_token)
            except (DocumentReadError, LLMError, RuntimeError) as exc:
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    self.app.logger.info("任务取消 task_id=%s", task_id)
                    _mark_canceled(db, task_id, claim_token)
                else:
                    self.app.logger.warning("任务失败 task_id=%s error=%s", task_id, exc)
                    _mark_failed(db, task_id, str(exc), results, claim_token)
            except Exception as exc:
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    self.app.logger.info("任务取消 task_id=%s", task_id)
                    _mark_canceled(db, task_id, claim_token)
                else:
                    self.app.logger.exception("任务执行异常：%s", task_id)
                    _mark_failed(db, task_id, f"任务执行异常：{exc}", results, claim_token)
            finally:
                if lease_stop is not None:
                    lease_stop.set()
                if lease_thread is not None:
                    lease_thread.join(timeout=2)
                self._unregister_cancel_event(task_id, cancel_event)


def _prepare_task_inputs(app, db, task, task_type: str, claim_token: str | None) -> tuple[str, str | None]:
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
            {**item, "source": task["original_filename"]}
            for item in hyperlinks
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


def _prepare_image_task_inputs(app, db, task, claim_token: str | None) -> tuple[str, str]:
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
            extracted_text = extract_text(upload_path, task["file_type"]).strip()
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
                _int_setting("image_page_check_max_pages", DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES),
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
                "page_images": _generated_items_with_relative_path(page_images, output_dir),
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


def _prepare_video_task_inputs(app, db, task, claim_token: str | None) -> tuple[str, str]:
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
        if frame_selection.get("fallback_frame_count") or frame_selection.get("skipped_frame_count"):
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
        document_text = format_video_document_text(task["original_filename"], frames, frame_selection)
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
        document_text, document_meta = _build_consistency_task_inputs(app, task, task_type)
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
        document_text, static_summary = compose_language_consistency_text(file_a, file_b)
        document_meta["static_precheck"] = static_summary
        return document_text, document_meta
    return _format_consistency_document_text(groups), document_meta


def _extract_consistency_document_text(app, task) -> str:
    task_type = _task_value(task, "task_type") or CONSISTENCY_TASK_TYPE
    document_text, _document_meta_value = _build_consistency_task_inputs(app, task, task_type)
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
            original_filename = str(file_info.get("original_filename") or stored_filename or f"文档{index}")
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
            extracted_files.append({**file_info, "original_filename": original_filename, "text": text})
        extracted_groups.append({**group, "files": extracted_files})
    return extracted_groups


def _format_consistency_document_text(groups: list[dict]) -> str:
    sections = []
    for group in groups:
        label = group["label"]
        group_parts = [f"# {label}"]
        for index, file_info in enumerate(group["files"], start=1):
            group_parts.append(f"## {label}{index}：{file_info['original_filename']}\n{file_info['text']}")
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


def _generated_items_with_relative_path(items: list[dict], output_dir: Path) -> list[dict]:
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
    if output_dir.resolve() == image_root.resolve() or not _path_is_relative_to(output_dir, image_root):
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
    document_meta["preprocessing"] = {"status": "completed", "completed_at": completed_at}
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


def _validate_model_input(document_text: str, max_input_chars: int, label: str = "文档文本"):
    if not document_text:
        raise RuntimeError("未能从文档中提取到可检查文本")
    if len(document_text) > max_input_chars:
        raise RuntimeError(
            f"{label} {len(document_text)} 字，超过当前模型文本上限 {max_input_chars} 字"
        )


def _document_check_items(db, task) -> list[dict]:
    return _task_check_items(db, task, DOCUMENT_TASK_TYPE)


def _task_check_items(db, task, task_type: str) -> list[dict]:
    snapshot_raw = _task_value(task, "checks_snapshot_json")
    snapshot = _check_items_from_snapshot(snapshot_raw)
    if _task_value(task, "retry_check_codes_json") is not None:
        try:
            snapshot_value = json.loads(snapshot_raw) if snapshot_raw else None
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("原任务缺少有效的检查项快照，无法重试") from exc
        if (
            not isinstance(snapshot_value, list)
            or not snapshot_value
            or len(snapshot) != len(snapshot_value)
        ):
            raise RuntimeError("原任务缺少有效的检查项快照，无法重试")
    if snapshot:
        return snapshot

    try:
        check_values = json.loads(task["checks_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("检查项数据无效") from exc
    if not isinstance(check_values, list) or not check_values:
        return []

    check_ids = [int(value) for value in check_values if isinstance(value, int)]
    check_codes = [
        str(value).strip()
        for value in check_values
        if isinstance(value, str) and str(value).strip()
    ]
    clauses = []
    params = []
    if check_ids:
        clauses.append(f"id IN ({','.join('?' for _ in check_ids)})")
        params.extend(check_ids)
    if check_codes:
        clauses.append(f"code IN ({','.join('?' for _ in check_codes)})")
        params.extend(check_codes)
    if not clauses:
        return []
    params.append(task_type)
    return [
        dict(row)
        for row in db.execute(
            f"""
            SELECT *
            FROM check_items
            WHERE ({' OR '.join(clauses)}) AND task_type = ? AND enabled = 1
            ORDER BY sort_order ASC, id ASC
            """,
            tuple(params),
        ).fetchall()
    ]


def _check_items_from_snapshot(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []

    items = []
    seen_codes = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not code or not name or not prompt or code in seen_codes:
            continue
        seen_codes.add(code)
        items.append({"code": code, "name": name, "prompt": prompt})
    return items


def retry_check_codes_for_task(task) -> list[str]:
    status = str(_task_value(task, "status") or "").strip()
    if status not in {"failed", "partial"}:
        raise RuntimeError("仅失败或部分完成任务可重试。")

    snapshot_raw = _task_value(task, "checks_snapshot_json")
    try:
        snapshot_value = json.loads(snapshot_raw) if snapshot_raw else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("原任务缺少有效的检查项快照，无法重试。") from exc
    snapshot_items = _check_items_from_snapshot(snapshot_raw)
    if (
        not isinstance(snapshot_value, list)
        or not snapshot_value
        or len(snapshot_items) != len(snapshot_value)
    ):
        raise RuntimeError("原任务缺少有效的检查项快照，无法重试。")

    snapshot_codes = [item["code"] for item in snapshot_items]
    snapshot_code_set = set(snapshot_codes)
    results = _check_results_from_json(_task_value(task, "result_json"))
    results_by_code = {
        str(result.get("code") or "").strip(): result
        for result in results
        if str(result.get("code") or "").strip() in snapshot_code_set
    }
    failed_codes = {
        code
        for code, result in results_by_code.items()
        if _check_result_failed(result)
    }

    if status == "partial":
        retry_codes = [code for code in snapshot_codes if code in failed_codes]
    else:
        stored_codes = _stored_retry_check_codes(task)
        if stored_codes is not None:
            stored_code_set = set(stored_codes)
            retry_codes = [
                code
                for code in snapshot_codes
                if code in stored_code_set
                and (code not in results_by_code or code in failed_codes)
            ]
        elif results_by_code:
            retry_codes = [
                code
                for code in snapshot_codes
                if code not in results_by_code or code in failed_codes
            ]
        else:
            retry_codes = snapshot_codes

    if not retry_codes:
        raise RuntimeError("任务没有可重试的失败检查项。")
    return retry_codes


def _check_results_from_json(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _stored_retry_check_codes(task) -> list[str] | None:
    raw = _task_value(task, "retry_check_codes_json")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("重试检查项范围无效") from exc
    if not isinstance(value, list) or not value:
        raise RuntimeError("重试检查项范围无效")

    codes = []
    seen_codes = set()
    for item in value:
        code = str(item or "").strip() if isinstance(item, str) else ""
        if not code or code in seen_codes:
            raise RuntimeError("重试检查项范围无效")
        seen_codes.add(code)
        codes.append(code)
    return codes


def _check_items_for_retry(
    check_items: list[dict],
    retry_check_codes: list[str] | None,
) -> list[dict]:
    if retry_check_codes is None:
        return check_items
    items_by_code = {str(item.get("code") or "").strip(): item for item in check_items}
    missing_codes = [code for code in retry_check_codes if code not in items_by_code]
    if missing_codes:
        raise RuntimeError(f"原任务检查项快照缺少重试项：{','.join(missing_codes)}")
    return [items_by_code[code] for code in retry_check_codes]


def _merge_check_results(base_results: list[dict], updates: list[dict]) -> list[dict]:
    updates_by_code = {
        str(result.get("code") or "").strip(): result
        for result in updates
        if isinstance(result, dict) and str(result.get("code") or "").strip()
    }
    merged = []
    placed_codes = set()
    for result in base_results:
        if not isinstance(result, dict):
            continue
        code = str(result.get("code") or "").strip()
        if code and code in updates_by_code:
            if code not in placed_codes:
                merged.append(dict(updates_by_code[code]))
                placed_codes.add(code)
            continue
        merged.append(dict(result))

    for result in updates:
        if not isinstance(result, dict):
            continue
        code = str(result.get("code") or "").strip()
        if code:
            if code in placed_codes:
                continue
            merged.append(dict(result))
            placed_codes.add(code)
        else:
            merged.append(dict(result))
    return merged


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


def _run_check_items_concurrently(
    app,
    task,
    check_items: list[dict],
    document_text: str,
    *,
    document_meta: dict | None = None,
    max_workers: int,
    stream_trace_enabled: bool,
    cancel_event: threading.Event | None = None,
    base_results: list[dict] | None = None,
) -> list[dict]:
    task_id = task["id"]
    claim_token = _task_claim_token(task)
    total = len(check_items)
    total_units = max(1, total)
    completed_units = 0
    completed_by_code: dict[str, dict] = {}
    partial_by_code: dict[str, dict] = {}
    base_results = list(base_results or [])
    result_lock = threading.Lock()
    save_lock = threading.Lock()
    cancel_event = cancel_event or threading.Event()
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_progress_heartbeat,
        args=(
            app,
            task_id,
            heartbeat_stop,
            5,
            89,
            task["request_timeout"],
            claim_token,
        ),
        daemon=True,
        name=f"task-heartbeat-{task_id}",
    )
    with save_lock:
        db = get_db()
        if base_results:
            _save_intermediate_results(
                db,
                task_id,
                base_results,
                f"正在重试 {total} 个失败检查项。",
                5,
                claim_token,
            )
        else:
            _update_progress(db, task_id, 5, claim_token)
    heartbeat.start()
    task_type = _task_value(task, "task_type") or DOCUMENT_TASK_TYPE

    def save_snapshot(db, summary: str, progress: int):
        with result_lock:
            current_results = _ordered_results(check_items, completed_by_code, partial_by_code)
            snapshot = _merge_check_results(base_results, current_results)
        with save_lock:
            _save_intermediate_results(db, task_id, snapshot, summary, progress, claim_token)

    def current_progress() -> int:
        with result_lock:
            units = completed_units
        return 5 + int(units / total_units * 85)

    def mark_unit_completed() -> int:
        nonlocal completed_units
        with result_lock:
            completed_units += 1
            return 5 + int(completed_units / total_units * 85)

    issue_output_limit = _issue_output_limit()
    pdf_table_evidence = build_pdf_table_evidence_index(document_text)

    def run_item(index: int, item: dict) -> dict:
        with app.app_context():
            db = get_db()

            def ensure_active():
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled

            ensure_active()

            app.logger.info(
                "任务检查项开始 task_id=%s item=%s index=%s/%s",
                task_id,
                item["name"],
                index,
                total,
            )
            last_stream_write = 0.0
            last_stream_chars = 0
            def save_partial(content: str, summary: str, *, force: bool = False):
                nonlocal last_stream_write, last_stream_chars
                content = content.strip()
                now = time.monotonic()
                if not force and content and last_stream_write:
                    if now - last_stream_write < STREAM_SNAPSHOT_INTERVAL_SECONDS:
                        return
                    if abs(len(content) - last_stream_chars) < STREAM_SNAPSHOT_MIN_CHAR_GROWTH:
                        return
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled
                with result_lock:
                    had_partial = item["code"] in partial_by_code
                    if content:
                        partial_by_code[item["code"]] = {
                            "code": item["code"],
                            "name": item["name"],
                            "result": content,
                        }
                    else:
                        partial_by_code.pop(item["code"], None)

                last_stream_write = now
                last_stream_chars = len(content)
                if not content and not had_partial:
                    return
                save_snapshot(db, summary, current_progress())

            try:
                structured_report = None
                if item["code"] == SENSITIVE_TERMS_CHECK_CODE:
                    structured_report = _run_sensitive_terms_check(app, document_text, issue_output_limit)
                    content = format_sensitive_terms_report(structured_report)
                elif item["code"] == COMMON_TERMS_CHECK_CODE:
                    structured_report = _run_common_terms_check(app, document_text, issue_output_limit)
                    content = format_common_terms_report(structured_report)
                elif item["code"] == HYPERLINK_CHECK_CODE:
                    structured_report = build_hyperlink_report(
                        hyperlinks_from_meta(document_meta or {}),
                        issue_limit=issue_output_limit,
                        network=outbound_network_config(),
                    )
                    content = format_hyperlink_report(structured_report)
                else:
                    network = outbound_network_config()
                    run_check_kwargs = {
                        "api_base": task["api_base"],
                        "api_key": task["api_key"],
                        "proxy_mode": network["proxy_mode"],
                        "proxy": network["proxy"],
                        "ssl_verify": network["ssl_verify"],
                        "request_timeout": task["request_timeout"],
                        "model_name": task["model_name"],
                        "reasoning_effort": _task_value(task, "reasoning_effort"),
                        "force_disable_thinking": _task_flag(task, "force_disable_thinking"),
                        "check_name": item["name"],
                        "prompt": item["prompt"],
                        "document_text": document_text,
                        "issue_output_limit": issue_output_limit,
                        "on_content": lambda content: save_partial(content, f"正在并发检查：{item['name']}"),
                        "task_id": task_id,
                        "stream_trace_enabled": stream_trace_enabled,
                        "cancel_event": cancel_event,
                        "check_canceled": ensure_active,
                    }
                    if task_type == DOCUMENT_TASK_TYPE:
                        run_check_kwargs["max_completion_tokens"] = None
                    content = run_check(**run_check_kwargs)
                    content, filtered_unsupported_count = sanitize_text_check_result(
                        content,
                        pdf_table_evidence=pdf_table_evidence,
                    )
                    if filtered_unsupported_count:
                        app.logger.info(
                            "已过滤证据不足的视觉对象或表格数据缺失结论 task_id=%s item=%s count=%s",
                            task_id,
                            item["name"],
                            filtered_unsupported_count,
                        )
            except (LLMError, RuntimeError) as exc:
                progress = mark_unit_completed()
                error = str(exc).strip() or exc.__class__.__name__
                with result_lock:
                    partial_result = partial_by_code.pop(item["code"], None)
                    result = dict(partial_result or {})
                    result.update(
                        {
                            "code": item["code"],
                            "name": item["name"],
                            "error": error,
                            "issue_output_limit": issue_output_limit,
                        }
                    )
                    result.setdefault("result", "")
                    completed_by_code[item["code"]] = result
                    completed_count = len(completed_by_code)
                save_snapshot(
                    db,
                    f"{item['name']}检查失败，已完成 {completed_count}/{total} 个检查项，继续检查其他项目。",
                    progress,
                )
                app.logger.warning(
                    "任务检查项失败，继续其他检查 task_id=%s item=%s error=%s",
                    task_id,
                    item["name"],
                    error,
                )
                return result
            progress = mark_unit_completed()

            if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                raise TaskCanceled

            result = {
                "code": item["code"],
                "name": item["name"],
                "result": content,
                "issue_output_limit": issue_output_limit,
            }
            if structured_report is not None:
                result["structured_report"] = structured_report
            with result_lock:
                completed_by_code[item["code"]] = result
                partial_by_code.pop(item["code"], None)
                completed_count = len(completed_by_code)
            save_snapshot(
                db,
                f"已完成 {completed_count}/{total} 个检查项，继续检查中。",
                progress,
            )
            app.logger.info(
                "任务检查项完成 task_id=%s item=%s output_chars=%s",
                task_id,
                item["name"],
                len(content),
            )
            return result

    executor = ThreadPoolExecutor(max_workers=max(1, min(max_workers, total)), thread_name_prefix=f"task-check-{task_id}")
    futures = []
    try:
        futures = [executor.submit(run_item, index, item) for index, item in enumerate(check_items, start=1)]
        results = []
        for future in as_completed(futures):
            results.append(future.result())
        with result_lock:
            ordered = _ordered_results(check_items, completed_by_code, {})
        if len(ordered) != total:
            raise RuntimeError("部分检查项未完成")
        return ordered
    except Exception:
        cancel_event.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2)
        executor.shutdown(wait=True, cancel_futures=True)


def _run_sensitive_terms_check(app, document_text: str, issue_output_limit: int) -> dict:
    root_dir = Path(app.config.get("ROOT_DIR") or Path(app.instance_path).parent)
    instance_dir = Path(app.instance_path)
    configured_path = app.config.get("SENSITIVE_TERMS_PATH")
    candidates = sensitive_terms_file_candidates(
        root_dir=root_dir,
        instance_dir=instance_dir,
        configured_path=configured_path,
    )
    terms_path = find_sensitive_terms_file(
        root_dir=root_dir,
        instance_dir=instance_dir,
        configured_path=configured_path,
    )
    if terms_path is None:
        return build_sensitive_terms_missing_report(candidates=candidates)

    try:
        rules = load_sensitive_terms(terms_path)
    except Exception as exc:
        return build_sensitive_terms_invalid_report(source_path=terms_path, error=str(exc))
    if not rules:
        return build_sensitive_terms_invalid_report(
            source_path=terms_path,
            error="未读取到有效敏感词规则，请确认表头包含“不规范用语”和“规范用语”两列。",
        )
    return build_sensitive_terms_report(
        document_text,
        rules,
        source_path=terms_path,
        issue_limit=issue_output_limit,
    )


def _run_common_terms_check(app, document_text: str, issue_output_limit: int) -> dict:
    root_dir = Path(app.config.get("ROOT_DIR") or Path(app.instance_path).parent)
    instance_dir = Path(app.instance_path)
    configured_path = app.config.get("COMMON_TERMS_PATH")
    candidates = common_terms_file_candidates(
        root_dir=root_dir,
        instance_dir=instance_dir,
        configured_path=configured_path,
    )
    terms_path = find_common_terms_file(
        root_dir=root_dir,
        instance_dir=instance_dir,
        configured_path=configured_path,
    )
    if terms_path is None:
        return build_common_terms_missing_report(candidates=candidates)

    try:
        rules = load_common_terms(terms_path)
    except Exception as exc:
        return build_common_terms_invalid_report(source_path=terms_path, error=str(exc))
    if not rules:
        return build_common_terms_invalid_report(
            source_path=terms_path,
            error="未读取到有效常用词规则，请确认表头包含“常用词”和“常见错误/不推荐用法”两列。",
        )
    return build_common_terms_report(
        document_text,
        rules,
        source_path=terms_path,
        issue_limit=issue_output_limit,
    )


def _run_image_check_items_concurrently(
    app,
    task,
    check_items: list[dict],
    image_items: list[dict],
    page_image_items: list[dict],
    document_text: str,
    *,
    document_meta: dict | None,
    max_workers: int,
    stream_trace_enabled: bool,
    cancel_event: threading.Event | None = None,
    base_results: list[dict] | None = None,
) -> list[dict]:
    task_id = task["id"]
    claim_token = _task_claim_token(task)
    total = len(check_items)
    groups = _image_check_groups(check_items, image_items, page_image_items, document_meta or {})
    if not groups:
        raise RuntimeError("没有可检查的 PDF 页面截图或图片资源")
    total_units = max(1, sum(max(1, len(group["batches"])) for group in groups))
    completed_units = 0
    completed_by_code: dict[str, dict] = {}
    partial_by_code: dict[str, dict] = {}
    base_results = list(base_results or [])
    incomplete_codes: set[str] = set()
    result_lock = threading.Lock()
    save_lock = threading.Lock()
    cancel_event = cancel_event or threading.Event()
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_progress_heartbeat,
        args=(
            app,
            task_id,
            heartbeat_stop,
            5,
            89,
            task["request_timeout"],
            claim_token,
        ),
        daemon=True,
        name=f"task-image-heartbeat-{task_id}",
    )
    with save_lock:
        db = get_db()
        if base_results:
            _save_intermediate_results(
                db,
                task_id,
                base_results,
                f"正在重试 {total} 个失败检查项。",
                5,
                claim_token,
            )
        else:
            _update_progress(db, task_id, 5, claim_token)
    heartbeat.start()

    def save_snapshot(db, summary: str, progress: int):
        with result_lock:
            current_results = _ordered_results(check_items, completed_by_code, partial_by_code)
            snapshot = _merge_check_results(base_results, current_results)
        with save_lock:
            _save_intermediate_results(db, task_id, snapshot, summary, progress, claim_token)

    def current_progress() -> int:
        with result_lock:
            units = completed_units
        return 5 + int(units / total_units * 85)

    def mark_unit_completed() -> int:
        nonlocal completed_units
        with result_lock:
            completed_units += 1
            return 5 + int(completed_units / total_units * 85)

    issue_output_limit = _issue_output_limit()

    def run_group(group_index: int, group: dict):
        with app.app_context():
            db = get_db()

            def ensure_active():
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled

            ensure_active()

            items = group["items"]
            batches = group["batches"]
            batch_count = len(batches)
            skipped_images = group["skipped_images"]
            manual_notes = group["manual_notes"]
            target_label = group["label"]
            app.logger.info(
                "任务图文联合检查组开始 task_id=%s target=%s group=%s/%s checks=%s images=%s skipped_images=%s batches=%s",
                task_id,
                target_label,
                group_index,
                len(groups),
                len(items),
                len(group["checkable_images"]),
                len(skipped_images),
                batch_count,
            )
            batch_results_by_code = {item["code"]: [] for item in items}
            last_stream_write = 0.0
            last_stream_chars = 0

            def save_partial(current_batch: dict | None, content: str, summary: str, *, force: bool = False):
                nonlocal last_stream_write, last_stream_chars
                content = content.strip()
                now = time.monotonic()
                if not force and content and last_stream_write:
                    if now - last_stream_write < STREAM_SNAPSHOT_INTERVAL_SECONDS:
                        return
                    if abs(len(content) - last_stream_chars) < STREAM_SNAPSHOT_MIN_CHAR_GROWTH:
                        return
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled

                with result_lock:
                    sections = _split_combined_check_output(content, items, fill_missing=False) if content else {}
                    for item in items:
                        item_content = sections.get(item["code"], "")
                        result_text = _format_multimodal_image_check_result(
                            batch_results_by_code[item["code"]],
                            current_batch=current_batch if item_content else None,
                            current_content=item_content,
                            skipped_images=skipped_images,
                            manual_notes=manual_notes,
                        )
                        if result_text:
                            partial_by_code[item["code"]] = {
                                "code": item["code"],
                                "name": item["name"],
                                "result": result_text,
                            }
                        elif not batch_results_by_code[item["code"]]:
                            partial_by_code.pop(item["code"], None)

                last_stream_write = now
                last_stream_chars = len(content)
                save_snapshot(db, summary, current_progress())

            network = outbound_network_config()
            image_folder = _task_image_folder(app)
            if not batches:
                progress = mark_unit_completed()
                with result_lock:
                    for item in items:
                        completed_by_code[item["code"]] = {
                            "code": item["code"],
                            "name": item["name"],
                            "result": _format_multimodal_image_check_result(
                                [],
                                skipped_images=skipped_images,
                                manual_notes=manual_notes,
                            ),
                        }
                        partial_by_code.pop(item["code"], None)
                    completed_count = len(completed_by_code)
                save_snapshot(
                    db,
                    f"已完成 {completed_count}/{total} 个图片检查项，继续检查中。",
                    progress,
                )
                app.logger.info(
                    "任务图文联合检查组跳过 task_id=%s target=%s skipped_images=%s",
                    task_id,
                    target_label,
                    len(skipped_images),
                )
                return

            for batch_index, batch in enumerate(batches, start=1):
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled
                multimodal_images = _multimodal_image_inputs(image_folder, batch)
                batch_document_text = _document_text_for_image_batch(document_text, batch)
                current_batch = {
                    "batch_index": batch_index,
                    "batch_count": batch_count,
                    "images": batch,
                    "target_label": target_label,
                    "target_kind": group["target_kind"],
                }

                sections = _run_combined_multimodal_check_with_repair(
                    app,
                    check_items=items,
                    prompt_builder=lambda selected_items: _combined_image_check_prompt(
                        selected_items,
                        group["target_kind"],
                        target_label,
                    ),
                    check_name=f"{target_label}合并检查",
                    error_label=target_label,
                    run_kwargs={
                        "api_base": task["api_base"],
                        "api_key": task["api_key"],
                        "proxy_mode": network["proxy_mode"],
                        "proxy": network["proxy"],
                        "ssl_verify": network["ssl_verify"],
                        "request_timeout": task["request_timeout"],
                        "model_name": task["model_name"],
                        "reasoning_effort": _task_value(task, "reasoning_effort"),
                        "force_disable_thinking": _task_flag(task, "force_disable_thinking"),
                        "document_text": batch_document_text,
                        "image_items": multimodal_images,
                        "batch_index": batch_index,
                        "batch_count": batch_count,
                        "issue_output_limit": issue_output_limit,
                        "output_contract": MULTIMODAL_OUTPUT_CONTRACT_MULTI_CHECK,
                            "on_content": lambda content, current=current_batch: save_partial(
                                current,
                                content,
                                f"正在进行{target_label}：批次 {current['batch_index']}/{current['batch_count']}",
                            ),
                            "task_id": task_id,
                            "stream_trace_enabled": stream_trace_enabled,
                            "cancel_event": cancel_event,
                            "check_canceled": ensure_active,
                        },
                    )
                with result_lock:
                    incomplete_codes.update(_unresolved_check_codes(sections, items))
                for item in items:
                    batch_results_by_code[item["code"]].append(
                        {
                            "batch_index": batch_index,
                            "batch_count": batch_count,
                            "images": batch,
                            "target_label": target_label,
                            "target_kind": group["target_kind"],
                            "content": sections.get(item["code"], ""),
                        }
                    )
                progress = mark_unit_completed()
                save_partial(
                    None,
                    "",
                    f"已完成 {target_label}：{batch_index}/{batch_count} 个图文批次",
                    force=True,
                )
                save_snapshot(
                    db,
                    f"正在进行图片检查，已完成 {completed_units}/{total_units} 个图文批次。",
                    progress,
                )

            with result_lock:
                for item in items:
                    completed_by_code[item["code"]] = {
                        "code": item["code"],
                        "name": item["name"],
                        "result": _format_multimodal_image_check_result(
                            batch_results_by_code[item["code"]],
                            skipped_images=skipped_images,
                            manual_notes=manual_notes,
                        ),
                    }
                    partial_by_code.pop(item["code"], None)
                completed_count = len(completed_by_code)
            save_snapshot(
                db,
                f"已完成 {completed_count}/{total} 个图片检查项，继续检查中。",
                current_progress(),
            )
            app.logger.info(
                "任务图文联合检查组完成 task_id=%s target=%s checks=%s images=%s skipped_images=%s batches=%s",
                task_id,
                target_label,
                len(items),
                len(group["checkable_images"]),
                len(skipped_images),
                batch_count,
            )
            return

    executor = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(groups))), thread_name_prefix=f"task-image-check-{task_id}")
    futures = []
    try:
        futures = [executor.submit(run_group, index, group) for index, group in enumerate(groups, start=1)]
        for future in as_completed(futures):
            future.result()
        with result_lock:
            ordered = _ordered_results(check_items, completed_by_code, {})
        if len(ordered) != total:
            raise RuntimeError("部分图文检查项未完成")
        if incomplete_codes:
            raise RuntimeError(f"部分图片检查项补偿后仍未返回有效结果：{','.join(sorted(incomplete_codes))}")
        return ordered
    except Exception:
        cancel_event.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2)
        executor.shutdown(wait=True, cancel_futures=True)


def _run_video_check_items_concurrently(
    app,
    task,
    check_items: list[dict],
    frame_items: list[dict],
    document_text: str,
    *,
    document_meta: dict | None,
    max_workers: int,
    stream_trace_enabled: bool,
    cancel_event: threading.Event | None = None,
    base_results: list[dict] | None = None,
) -> list[dict]:
    task_id = task["id"]
    claim_token = _task_claim_token(task)
    total = len(check_items)
    checkable_frames, skipped_frames = _split_checkable_image_items(frame_items)
    if not checkable_frames and not skipped_frames:
        raise RuntimeError("没有可检查的视频抽帧画面")

    batches = _image_batches(checkable_frames, _image_check_batch_size())
    check_groups = _check_item_groups(check_items)
    total_units = max(1, len(batches) * max(1, len(check_groups)))
    completed_units = 0
    completed_by_code: dict[str, dict] = {}
    partial_by_code: dict[str, dict] = {}
    base_results = list(base_results or [])
    incomplete_codes: set[str] = set()
    result_lock = threading.Lock()
    save_lock = threading.Lock()
    cancel_event = cancel_event or threading.Event()
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_progress_heartbeat,
        args=(
            app,
            task_id,
            heartbeat_stop,
            5,
            89,
            task["request_timeout"],
            claim_token,
        ),
        daemon=True,
        name=f"task-video-heartbeat-{task_id}",
    )
    with save_lock:
        db = get_db()
        if base_results:
            _save_intermediate_results(
                db,
                task_id,
                base_results,
                f"正在重试 {total} 个失败检查项。",
                5,
                claim_token,
            )
        else:
            _update_progress(db, task_id, 5, claim_token)
    heartbeat.start()

    def save_snapshot(db, summary: str, progress: int):
        with result_lock:
            current_results = _ordered_results(check_items, completed_by_code, partial_by_code)
            snapshot = _merge_check_results(base_results, current_results)
        with save_lock:
            _save_intermediate_results(db, task_id, snapshot, summary, progress, claim_token)

    def current_progress() -> int:
        with result_lock:
            units = completed_units
        return 5 + int(units / total_units * 85)

    def mark_unit_completed() -> int:
        nonlocal completed_units
        with result_lock:
            completed_units += 1
            return 5 + int(completed_units / total_units * 85)

    issue_output_limit = _issue_output_limit()

    def run_checks():
        with app.app_context():
            db = get_db()

            def ensure_active():
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled

            ensure_active()

            app.logger.info(
                "任务视频多模态检查开始 task_id=%s checks=%s groups=%s frames=%s skipped_frames=%s batches=%s",
                task_id,
                len(check_items),
                len(check_groups),
                len(checkable_frames),
                len(skipped_frames),
                len(batches),
            )
            network = outbound_network_config()
            image_folder = _task_image_folder(app)
            if not batches:
                progress = mark_unit_completed()
                with result_lock:
                    for item in check_items:
                        structured_report = _merge_video_batch_reports(
                            item["code"],
                            [],
                            skipped_frames=skipped_frames,
                        )
                        completed_by_code[item["code"]] = {
                            "code": item["code"],
                            "name": item["name"],
                            "result": _format_multimodal_image_check_result(
                                [],
                                skipped_images=skipped_frames,
                            ),
                            "structured_report": structured_report,
                            "batch_reports": [],
                            "issue_output_limit": issue_output_limit,
                        }
                        partial_by_code.pop(item["code"], None)
                    completed_count = len(completed_by_code)
                save_snapshot(
                    db,
                    f"已完成 {completed_count}/{total} 个视频检查项。",
                    progress,
                )
                return

            for group_index, items in enumerate(check_groups, start=1):
                batch_results_by_code = {item["code"]: [] for item in items}
                last_stream_write = 0.0
                last_stream_chars = 0

                def save_partial(current_batch: dict | None, content: str, summary: str, *, force: bool = False):
                    nonlocal last_stream_write, last_stream_chars
                    content = content.strip()
                    now = time.monotonic()
                    if not force and content and last_stream_write:
                        if now - last_stream_write < STREAM_SNAPSHOT_INTERVAL_SECONDS:
                            return
                        if abs(len(content) - last_stream_chars) < STREAM_SNAPSHOT_MIN_CHAR_GROWTH:
                            return
                    if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                        raise TaskCanceled

                    with result_lock:
                        sections = _split_combined_check_output(content, items, fill_missing=False) if content else {}
                        for item in items:
                            item_content = sections.get(item["code"], "")
                            result_text = _format_multimodal_image_check_result(
                                batch_results_by_code[item["code"]],
                                current_batch=current_batch if item_content else None,
                                current_content=item_content,
                                skipped_images=skipped_frames,
                            )
                            if result_text:
                                partial_by_code[item["code"]] = {
                                    "code": item["code"],
                                    "name": item["name"],
                                    "result": result_text,
                                }
                            elif not batch_results_by_code[item["code"]]:
                                partial_by_code.pop(item["code"], None)

                    last_stream_write = now
                    last_stream_chars = len(content)
                    save_snapshot(db, summary, current_progress())

                app.logger.info(
                    "任务视频多模态检查组开始 task_id=%s group=%s/%s checks=%s",
                    task_id,
                    group_index,
                    len(check_groups),
                    len(items),
                )
                for batch_index, batch in enumerate(batches, start=1):
                    if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                        raise TaskCanceled
                    multimodal_images = _multimodal_image_inputs(image_folder, batch)
                    current_batch = {
                        "batch_index": batch_index,
                        "batch_count": len(batches),
                        "images": batch,
                        "target_label": "视频帧检查",
                        "target_kind": "video_frame",
                    }
                    sections = _run_combined_multimodal_check_with_repair(
                        app,
                        check_items=items,
                        prompt_builder=_combined_video_check_prompt,
                        check_name="视频帧检查合并检查",
                        error_label="视频",
                        preserve_structured=True,
                        run_kwargs={
                            "api_base": task["api_base"],
                            "api_key": task["api_key"],
                            "proxy_mode": network["proxy_mode"],
                            "proxy": network["proxy"],
                            "ssl_verify": network["ssl_verify"],
                            "request_timeout": task["request_timeout"],
                            "model_name": task["model_name"],
                            "reasoning_effort": _task_value(task, "reasoning_effort"),
                            "force_disable_thinking": _task_flag(task, "force_disable_thinking"),
                            "document_text": _document_text_for_video_batch(document_text, batch, document_meta or {}),
                            "image_items": multimodal_images,
                            "batch_index": batch_index,
                            "batch_count": len(batches),
                            "issue_output_limit": issue_output_limit,
                            "output_contract": MULTIMODAL_OUTPUT_CONTRACT_MULTI_CHECK,
                            "on_content": lambda content, current=current_batch: save_partial(
                                current,
                                content,
                                f"正在进行视频帧检查：批次 {current['batch_index']}/{current['batch_count']}",
                            ),
                            "task_id": task_id,
                            "stream_trace_enabled": stream_trace_enabled,
                            "cancel_event": cancel_event,
                            "check_canceled": ensure_active,
                        },
                    )
                    with result_lock:
                        incomplete_codes.update(_unresolved_check_codes(sections, items))
                    for item in items:
                        structured_report = sections.get(item["code"], {})
                        batch_results_by_code[item["code"]].append(
                            {
                                "batch_index": batch_index,
                                "batch_count": len(batches),
                                "images": batch,
                                "target_label": "视频帧检查",
                                "target_kind": "video_frame",
                                "content": _format_combined_json_result(item, structured_report),
                                "structured_report": structured_report,
                            }
                        )
                    progress = mark_unit_completed()
                    save_partial(
                        None,
                        "",
                        f"已完成视频帧检查：检查组 {group_index}/{len(check_groups)}，批次 {batch_index}/{len(batches)}",
                        force=True,
                    )
                    save_snapshot(
                        db,
                        f"正在进行视频检查，已完成 {completed_units}/{total_units} 个检查组批次。",
                        progress,
                    )

                with result_lock:
                    for item in items:
                        batch_results = batch_results_by_code[item["code"]]
                        structured_report = _merge_video_batch_reports(
                            item["code"],
                            batch_results,
                            skipped_frames=skipped_frames,
                        )
                        completed_by_code[item["code"]] = {
                            "code": item["code"],
                            "name": item["name"],
                            "result": _format_multimodal_image_check_result(
                                batch_results,
                                skipped_images=skipped_frames,
                            ),
                            "structured_report": structured_report,
                            "batch_reports": _video_batch_report_snapshots(batch_results),
                            "issue_output_limit": issue_output_limit,
                        }
                        partial_by_code.pop(item["code"], None)
                    completed_count = len(completed_by_code)
                save_snapshot(
                    db,
                    f"已完成 {completed_count}/{total} 个视频检查项。",
                    current_progress(),
                )
                app.logger.info(
                    "任务视频多模态检查组完成 task_id=%s group=%s/%s checks=%s",
                    task_id,
                    group_index,
                    len(check_groups),
                    len(items),
                )

            app.logger.info(
                "任务视频多模态检查完成 task_id=%s checks=%s groups=%s frames=%s skipped_frames=%s batches=%s",
                task_id,
                len(check_items),
                len(check_groups),
                len(checkable_frames),
                len(skipped_frames),
                len(batches),
            )

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"task-video-check-{task_id}")
    futures = []
    try:
        futures = [executor.submit(run_checks)]
        for future in as_completed(futures):
            future.result()
        with result_lock:
            ordered = _ordered_results(check_items, completed_by_code, {})
        if len(ordered) != total:
            raise RuntimeError("部分视频检查项未完成")
        if incomplete_codes:
            raise RuntimeError(f"部分视频检查项补偿后仍未返回有效结果：{','.join(sorted(incomplete_codes))}")
        return ordered
    except Exception:
        cancel_event.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=2)
        executor.shutdown(wait=True, cancel_futures=True)


def _combined_video_check_prompt(check_items: list[dict]) -> str:
    item_blocks = []
    for item in check_items:
        item_blocks.append(
            f"- code: {item['code']}\n"
            f"  name: {item['name']}\n"
            f"  专项要求: {str(item.get('prompt') or '').strip()}"
        )
    return (
        f"本次执行硬件产品安装调测视频质检，一次请求中合并 {len(check_items)} 个检查项。"
        "请对每个检查项独立判断，不要把其他检查项的结论混入当前检查项。\n\n"
        "检查对象说明：系统只提供从视频时间轴均匀抽取的采样帧，模型不能直接观看完整连续视频。"
        "请重点观察安装顺序、接线端子、安全防护、调测界面/参数、视频清晰度和关键步骤完整性。\n\n"
        "定位要求：引用画面时优先使用视频时间点（例如 00:12.300），必要时补充帧文件名；不要使用 PDF 页码定位。\n"
        "证据约束：只依据当前提供的采样帧和视频上下文判断；看不清、被遮挡、动作连续性证据不足或缺少产品图纸依据时放入“需人工确认”，不要编造不可见内容。\n"
        "汇总约束：status=issue 只放已确认异常；证据不足放 status=suggestion；正常、未发现、符合、清晰、完整、无需修改等结论不得标记为 issue。\n"
        "完整性要求：results 必须包含下列每个 code，且每个 code 只出现一次。\n\n"
        "检查项定义：\n"
        f"{chr(10).join(item_blocks)}"
    )


def _document_text_for_video_batch(document_text: str, frame_items: list[dict], document_meta: dict) -> str:
    text = _trim_document_context(str(document_text or "").strip(), IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)
    selection = document_meta.get("frame_selection") if isinstance(document_meta, dict) else {}
    frame_lines = []
    for frame in frame_items:
        filename = str(frame.get("filename") or frame.get("id") or "视频帧")
        position = str(frame.get("position") or "未标注")
        frame_lines.append(f"- {filename}：视频时间点 {position}")
    selection_lines = []
    if isinstance(selection, dict):
        duration = selection.get("duration_seconds")
        frame_count = selection.get("frame_count")
        if duration is not None:
            selection_lines.append(f"- 视频时长：{duration} 秒")
        if frame_count is not None:
            selection_lines.append(f"- 总抽帧数：{frame_count}")
        skipped_frame_count = int(selection.get("skipped_frame_count") or 0)
        if skipped_frame_count:
            selection_lines.append(f"- 已跳过无法解码的采样点：{skipped_frame_count}")
        if selection.get("strategy"):
            selection_lines.append(f"- 抽帧策略：{selection.get('strategy')}")

    parts = []
    if text:
        parts.append(text)
    if selection_lines:
        parts.append("video_sampling:\n" + "\n".join(selection_lines))
    parts.append("current_batch_video_frames:\n" + ("\n".join(frame_lines) if frame_lines else "- 未记录视频帧"))
    return "\n\n".join(parts).strip()


def _merge_video_batch_reports(
    result_code: str,
    batch_results: list[dict],
    *,
    skipped_frames: list[dict] | None = None,
) -> dict:
    merged_items: list[dict] = []
    items_by_key: dict[str, dict] = {}
    for batch in batch_results:
        report = batch.get("structured_report")
        if not isinstance(report, dict):
            continue
        raw_items = report.get("items")
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            item = _normalize_video_report_item(raw_item)
            if not item or _video_report_item_is_generic_non_issue(item):
                continue
            inferred_refs = _video_evidence_refs_for_item(item, batch)
            item["evidence_refs"] = _merge_video_evidence_refs(
                item.get("evidence_refs"),
                inferred_refs,
            )
            evidence_location = _video_evidence_location(item["evidence_refs"])
            if evidence_location:
                item["location"] = evidence_location
            key = _video_report_item_merge_key(item)
            existing = items_by_key.get(key)
            if existing is None:
                items_by_key[key] = item
                merged_items.append(item)
            else:
                _merge_video_report_item(existing, item)

    for frame in skipped_frames or []:
        item = _skipped_video_frame_report_item(frame)
        key = _video_report_item_merge_key(item)
        if key not in items_by_key:
            items_by_key[key] = item
            merged_items.append(item)

    for item in merged_items:
        evidence_location = _video_evidence_location(item.get("evidence_refs"))
        if evidence_location:
            item["location"] = evidence_location
        item["id"] = _video_report_item_id(result_code, item)
    merged_items.sort(key=_video_report_item_priority)

    issue_count = sum(1 for item in merged_items if item.get("status") == "issue")
    suggestion_count = sum(1 for item in merged_items if item.get("status") == "suggestion")
    if issue_count or suggestion_count:
        summary = f"视频检查形成 {issue_count} 个明确问题、{suggestion_count} 个需人工确认项。"
    else:
        summary = "未发现明确问题或需人工确认项。"
    return {"summary": summary, "items": merged_items}


def _normalize_video_report_item(raw_item) -> dict:
    if isinstance(raw_item, str):
        raw_item = {"status": "suggestion", "description": raw_item}
    if not isinstance(raw_item, dict):
        return {}
    status = str(raw_item.get("status") or "suggestion").strip().lower()
    if status not in {"issue", "suggestion", "non_issue"}:
        status = "suggestion"
    severity = str(raw_item.get("severity") or "").strip().lower()
    if severity not in {"critical", "high", "medium", "low"}:
        severity = "medium" if status == "issue" else "low"
    confidence = str(raw_item.get("confidence") or "").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium" if status == "issue" else "low"
    item = {
        "status": status,
        "severity": severity,
        "confidence": confidence,
        "category": str(raw_item.get("category") or "").strip(),
        "location": str(raw_item.get("location") or "").strip(),
        "excerpt": str(raw_item.get("excerpt") or "").strip(),
        "description": str(raw_item.get("description") or "").strip(),
        "impact": str(raw_item.get("impact") or "").strip(),
        "suggestion": str(raw_item.get("suggestion") or "").strip(),
        "evidence_refs": _normalize_video_evidence_refs(raw_item.get("evidence_refs")),
    }
    if not any(item.get(field) for field in ("category", "location", "excerpt", "description", "impact", "suggestion")):
        return {}
    return item


def _video_report_item_is_generic_non_issue(item: dict) -> bool:
    if item.get("status") != "non_issue":
        return False
    text = "".join(
        str(item.get(field) or "")
        for field in ("category", "excerpt", "description", "impact", "suggestion")
    )
    compact = re.sub(r"\s+", "", text)
    return not compact or any(
        marker in compact
        for marker in ("未发现", "无明显", "未见", "正常", "符合", "一致", "清晰", "完整", "无需修改", "无异常")
    )


def _video_evidence_refs_for_item(item: dict, batch: dict) -> list[dict]:
    frames = batch.get("images") or []
    searchable = "\n".join(
        str(item.get(field) or "")
        for field in ("location", "excerpt", "description")
    )
    refs = []
    for frame in frames:
        filename = str(frame.get("filename") or "").strip()
        frame_id = str(frame.get("id") or "").strip()
        position = str(frame.get("position") or "").strip()
        if not any(value and value in searchable for value in (filename, frame_id, position)):
            continue
        refs.append(_video_evidence_ref(frame))
    if not refs and len(frames) == 1:
        refs.append(_video_evidence_ref(frames[0]))
    return [ref for ref in refs if ref]


def _video_evidence_ref(frame: dict) -> dict:
    frame_id = str(frame.get("id") or "").strip()
    filename = str(frame.get("filename") or "").strip()
    if not frame_id and not filename:
        return {}
    position = str(frame.get("position") or "").strip()
    timestamp_seconds = _video_timestamp_seconds(frame.get("timestamp_seconds"), position)
    return {
        "id": frame_id or filename,
        "filename": filename,
        "position": position,
        "timestamp_seconds": timestamp_seconds,
        "relative_path": str(frame.get("relative_path") or "").strip(),
        "mime_type": str(frame.get("mime_type") or "image/jpeg").strip(),
        "kind": "video_frame",
    }


def _normalize_video_evidence_refs(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    refs = []
    for raw_ref in value:
        if not isinstance(raw_ref, dict):
            continue
        ref = _video_evidence_ref(raw_ref)
        if ref:
            refs.append(ref)
    return _merge_video_evidence_refs(refs, [])


def _merge_video_evidence_refs(left, right) -> list[dict]:
    merged = []
    seen = set()
    for raw_ref in list(left or []) + list(right or []):
        if not isinstance(raw_ref, dict):
            continue
        ref = _video_evidence_ref(raw_ref)
        if not ref:
            continue
        key = ref.get("id") or ref.get("filename") or ref.get("position")
        if key in seen:
            continue
        seen.add(key)
        merged.append(ref)
    merged.sort(
        key=lambda ref: (
            ref.get("timestamp_seconds") is None,
            float(ref.get("timestamp_seconds") or 0),
            str(ref.get("filename") or ""),
        )
    )
    return merged


def _video_timestamp_seconds(value, position: str = "") -> float | None:
    if value is not None and not isinstance(value, bool):
        try:
            return round(max(0.0, float(value)), 3)
        except (TypeError, ValueError):
            pass
    match = re.search(r"(?:(\d{1,2}):)?(\d{2}):(\d{2})(?:\.(\d{1,3}))?", str(position or ""))
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    millis = int((match.group(4) or "0").ljust(3, "0")[:3])
    return round(hours * 3600 + minutes * 60 + seconds + millis / 1000, 3)


def _video_evidence_location(refs) -> str:
    positions = []
    for ref in refs or []:
        position = str(ref.get("position") or "").strip()
        if position and position not in positions:
            positions.append(position)
    if not positions:
        return ""
    return "视频时间 " + "、".join(positions)


def _video_report_item_merge_key(item: dict) -> str:
    category = _normalize_video_issue_key_text(item.get("category"))
    description = _normalize_video_issue_key_text(
        item.get("description")
        or item.get("excerpt")
        or item.get("suggestion")
        or item.get("impact")
        or item.get("location")
    )
    return f"{category}\n{description}"


def _normalize_video_issue_key_text(value) -> str:
    text = str(value or "").lower()
    text = re.sub(r"(?:视频时间\s*)?(?:\d{1,2}:)?\d{2}:\d{2}(?:\.\d{1,3})?", "", text)
    text = re.sub(r"\b\d{4}_t\d+\.(?:jpg|jpeg|png)\b", "", text)
    return re.sub(r"[\s，。；、,.!！?？:：;；/\\|()（）【】\[\]\"'“”‘’_-]+", "", text)


def _merge_video_report_item(target: dict, source: dict) -> None:
    status_order = {"issue": 0, "suggestion": 1, "non_issue": 2}
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    if status_order.get(source.get("status"), 9) < status_order.get(target.get("status"), 9):
        target["status"] = source.get("status")
    if severity_order.get(source.get("severity"), 9) < severity_order.get(target.get("severity"), 9):
        target["severity"] = source.get("severity")
    if confidence_order.get(source.get("confidence"), 9) < confidence_order.get(target.get("confidence"), 9):
        target["confidence"] = source.get("confidence")
    for field in ("category", "excerpt", "description", "impact", "suggestion"):
        target[field] = _merge_video_report_field(target.get(field), source.get(field))
    target["evidence_refs"] = _merge_video_evidence_refs(
        target.get("evidence_refs"),
        source.get("evidence_refs"),
    )


def _merge_video_report_field(left, right) -> str:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text:
        return right_text
    if not right_text or right_text == left_text or right_text in left_text:
        return left_text
    if left_text in right_text:
        return right_text
    return f"{left_text}；{right_text}"


def _video_report_item_id(result_code: str, item: dict) -> str:
    source = "\n".join(
        (
            str(result_code or ""),
            _normalize_video_issue_key_text(item.get("category")),
            _normalize_video_issue_key_text(
                item.get("description")
                or item.get("excerpt")
                or item.get("suggestion")
                or item.get("impact")
                or item.get("location")
            ),
        )
    )
    return hashlib.sha1(source.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _video_report_item_priority(item: dict) -> tuple[int, int, int, str]:
    return (
        {"issue": 0, "suggestion": 1, "non_issue": 2}.get(str(item.get("status") or ""), 9),
        {"high": 0, "medium": 1, "low": 2}.get(str(item.get("confidence") or ""), 9),
        {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(str(item.get("severity") or ""), 9),
        str(item.get("id") or ""),
    )


def _skipped_video_frame_report_item(frame: dict) -> dict:
    ref = _video_evidence_ref(frame)
    filename = str(frame.get("filename") or frame.get("id") or "视频帧")
    position = str(frame.get("position") or "").strip()
    return {
        "status": "suggestion",
        "severity": "low",
        "confidence": "high",
        "category": "视频帧读取",
        "location": f"视频时间 {position}" if position else "",
        "excerpt": filename,
        "description": f"视频帧 {filename} 不是可识别的图片格式，系统已跳过。",
        "impact": "该时间点未参与模型检查，可能影响视频检查完整性。",
        "suggestion": "请人工回看对应时间点，或将视频转换为受支持格式后重新检查。",
        "evidence_refs": [ref] if ref else [],
    }


def _video_batch_report_snapshots(batch_results: list[dict]) -> list[dict]:
    snapshots = []
    for batch in batch_results:
        frames = []
        for frame in batch.get("images") or []:
            ref = _video_evidence_ref(frame)
            if ref:
                frames.append(ref)
        report = batch.get("structured_report")
        snapshots.append(
            {
                "batch_index": int(batch.get("batch_index") or 1),
                "batch_count": int(batch.get("batch_count") or 1),
                "frames": frames,
                "structured_report": report if isinstance(report, dict) else {"summary": "", "items": []},
            }
        )
    return snapshots


def _image_check_groups(
    check_items: list[dict],
    image_items: list[dict],
    page_image_items: list[dict],
    document_meta: dict,
) -> list[dict]:
    items_by_target = {"page": [], "resource": []}
    for item in check_items:
        items_by_target[_image_check_target(item)].append(item)

    groups = []
    for target_kind in ("page", "resource"):
        items = items_by_target[target_kind]
        if not items:
            continue
        source_images = page_image_items if target_kind == "page" else image_items
        fallback_used = False
        if not source_images and target_kind == "resource" and page_image_items:
            source_images = page_image_items
            fallback_used = True
        if not source_images and target_kind == "page" and image_items:
            source_images = image_items
            fallback_used = True
        checkable_images, skipped_images = _split_checkable_image_items(source_images)
        manual_notes = _image_check_manual_notes(target_kind, document_meta, fallback_used)
        for item_group in _check_item_groups(items):
            groups.append(
                {
                    "target_kind": target_kind,
                    "label": IMAGE_CHECK_TARGET_LABELS[target_kind],
                    "items": item_group,
                    "checkable_images": checkable_images,
                    "skipped_images": skipped_images,
                    "manual_notes": manual_notes,
                    "batches": _image_batches(checkable_images, _image_check_batch_size()),
                }
            )
    return groups


def _image_check_target(item: dict) -> str:
    code = str(item.get("code") or "")
    if code in IMAGE_RESOURCE_CHECK_CODES:
        return "resource"
    if code in IMAGE_PAGE_CHECK_CODES:
        return "page"
    return "page"


def _image_check_manual_notes(target_kind: str, document_meta: dict, fallback_used: bool) -> list[str]:
    notes = []
    if target_kind == "page":
        selection = document_meta.get("page_selection") if isinstance(document_meta, dict) else None
        if isinstance(selection, dict) and int(selection.get("omitted_pages") or 0) > 0:
            notes.append(
                "PDF 共 {total} 页，本次页面级检查按长文档策略选取 {selected} 页，未覆盖 {omitted} 页；"
                "未覆盖页需要人工抽查或调高 image_page_check_max_pages 后重跑。".format(
                    total=selection.get("total_pages", "-"),
                    selected=len(selection.get("selected_pages") or []),
                    omitted=selection.get("omitted_pages", "-"),
                )
            )
    if target_kind == "resource":
        image_error = str(document_meta.get("image_extraction_error") or "").strip() if isinstance(document_meta, dict) else ""
        if image_error:
            notes.append(f"PDF 内嵌图片提取异常，图片资源类检查可能未覆盖全部原始图片：{image_error}")
    if fallback_used:
        notes.append("当前检查对象缺少首选图片源，已回退使用另一类 PDF 图像；细节判断需要人工确认。")
    return notes


def _combined_image_check_prompt(check_items: list[dict], target_kind: str, target_label: str) -> str:
    target_instruction = (
        "本组检查对象是 PDF 整页截图。请重点观察页面中的正文、图、表、标题、页眉页脚、遮挡、裁切、版式和上下文对应关系。"
        if target_kind == "page"
        else "本组检查对象是从 PDF 中提取的内嵌图片资源。请重点观察图片自身的文字、接线、图形元素、标注和可读性。"
    )
    item_blocks = []
    for item in check_items:
        item_blocks.append(
            f"- code: {item['code']}\n"
            f"  name: {item['name']}\n"
            f"  专项要求: {str(item.get('prompt') or '').strip()}"
        )
    return (
        f"本次执行{target_label}，一次请求中合并 {len(check_items)} 个检查项。请对每个检查项独立判断，"
        "不要把其他检查项的结论混入当前检查项。\n\n"
        f"{target_instruction}\n\n"
        "定位要求：引用图片时优先使用 PDF 页码（例如“第12页”），同一页有多张图时再补充图片编号/位置；无法识别页码时使用图片编号或原始位置。\n"
        "汇总约束：status=issue 只放已确认异常；证据不足放 status=suggestion；正常、未发现、符合、一致、清晰、完整等结论不得标记为 issue。\n"
        "证据约束：只能依据本次提供的 PDF 页面/图片和文档上下文；看不清、证据不足、跨页缺上下文时放入“需人工确认”，不要编造不可见内容。\n\n"
        "完整性要求：results 必须包含下列每个 code，且每个 code 只出现一次。\n\n"
        "检查项定义：\n"
        f"{chr(10).join(item_blocks)}"
    )


def _run_combined_multimodal_check_with_repair(
    app,
    *,
    check_items: list[dict],
    prompt_builder,
    check_name: str,
    error_label: str,
    run_kwargs: dict,
    preserve_structured: bool = False,
) -> dict[str, str] | dict[str, dict]:
    initial_kwargs = dict(run_kwargs)
    initial_kwargs.update(
        check_name=f"{check_name}（{len(check_items)}项）",
        prompt=prompt_builder(check_items),
    )
    content = run_multimodal_document_check(**initial_kwargs)
    sections = (
        _split_combined_structured_output(content, check_items)
        if preserve_structured
        else _split_combined_check_output(content, check_items, fill_missing=False)
    )
    missing_items = _missing_check_items(check_items, sections)
    if missing_items:
        app.logger.warning(
            "模型多检查项结果缺失，准备补偿请求 task_id=%s target=%s returned=%s missing=%s output_chars=%s",
            run_kwargs.get("task_id") or "-",
            error_label,
            ",".join(sections) or "-",
            ",".join(str(item.get("code") or "") for item in missing_items),
            len(str(content or "")),
        )
        repair_kwargs = dict(run_kwargs)
        repair_kwargs.pop("on_content", None)
        repair_kwargs.update(
            check_name=f"{check_name}补偿检查（{len(missing_items)}项）",
            prompt=(
                "上一次响应缺少以下检查项。只返回本次列出的缺失 code，不要重复其他检查项。\n\n"
                + prompt_builder(missing_items)
            ),
        )
        repair_content = run_multimodal_document_check(**repair_kwargs)
        repair_sections = (
            _split_combined_structured_output(
                repair_content,
                missing_items,
                allow_single_plain_text=False,
            )
            if preserve_structured
            else _split_combined_check_output(
                repair_content,
                missing_items,
                fill_missing=False,
                allow_single_plain_text=False,
            )
        )
        sections.update(repair_sections)
        missing_items = _missing_check_items(check_items, sections)
        app.logger.info(
            "模型多检查项补偿请求完成 task_id=%s target=%s repaired=%s remaining=%s output_chars=%s",
            run_kwargs.get("task_id") or "-",
            error_label,
            ",".join(repair_sections) or "-",
            ",".join(str(item.get("code") or "") for item in missing_items) or "-",
            len(str(repair_content or "")),
        )

    if not sections:
        raise RuntimeError(f"模型连续两次未返回可识别的{error_label}多检查项结果")
    if preserve_structured:
        _fill_missing_structured_check_sections(sections, check_items)
    else:
        _fill_missing_check_sections(sections, check_items)
    return sections


def _missing_check_items(check_items: list[dict], sections: dict[str, str]) -> list[dict]:
    return [item for item in check_items if str(item.get("code") or "") not in sections]


def _unresolved_check_codes(sections: dict, check_items: list[dict]) -> set[str]:
    marker = "模型未按要求返回该检查项的独立结果。"
    unresolved = set()
    for item in check_items:
        code = str(item.get("code") or "")
        section = sections.get(code)
        if isinstance(section, dict):
            if section.get("incomplete"):
                unresolved.add(code)
            continue
        if marker in str(section or ""):
            unresolved.add(code)
    return unresolved


def _split_combined_check_output(
    content: str,
    check_items: list[dict],
    *,
    fill_missing: bool = True,
    allow_single_plain_text: bool = True,
) -> dict[str, str]:
    text = str(content or "").strip()
    if not text:
        if not fill_missing:
            return {}
        return {item["code"]: _missing_check_section(item, "模型未返回该检查项内容。") for item in check_items}

    sections = _split_combined_json_output(text, check_items)
    if sections:
        if fill_missing:
            _fill_missing_check_sections(sections, check_items)
        return sections

    if allow_single_plain_text and len(check_items) == 1 and "### 检查项：" not in text:
        return {check_items[0]["code"]: text}

    headers = list(re.finditer(r"(?m)^###\s*检查项[:：]\s*(.+?)\s*$", text))
    sections = {}
    for index, header in enumerate(headers):
        header_line = header.group(1).strip()
        start = header.start()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        matched_code = _match_check_code_from_header(header_line, check_items)
        if matched_code:
            sections[matched_code] = text[start:end].strip()

    if fill_missing:
        _fill_missing_check_sections(sections, check_items)
    return sections


def _split_combined_structured_output(
    content: str,
    check_items: list[dict],
    *,
    allow_single_plain_text: bool = True,
) -> dict[str, dict]:
    text = str(content or "").strip()
    if not text:
        return {}

    payload = _load_combined_json_output(text)
    if isinstance(payload, dict):
        results = payload.get("results")
        if results is None and len(check_items) == 1 and isinstance(payload.get("items"), list):
            results = [{"code": check_items[0]["code"], **payload}]
    elif isinstance(payload, list):
        results = payload
    else:
        results = None

    items_by_code = {str(item.get("code") or ""): item for item in check_items}
    sections: dict[str, dict] = {}
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict):
                continue
            code = str(result.get("code") or "").strip()
            if code not in items_by_code or code in sections:
                continue
            sections[code] = _normalize_combined_structured_result(result)
    if sections:
        return sections

    legacy_sections = _split_combined_check_output(
        text,
        check_items,
        fill_missing=False,
        allow_single_plain_text=allow_single_plain_text,
    )
    return {
        code: _legacy_combined_section_report(section)
        for code, section in legacy_sections.items()
        if str(section or "").strip()
    }


def _normalize_combined_structured_result(result: dict) -> dict:
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    items = []
    for raw_item in raw_items:
        if isinstance(raw_item, dict):
            items.append(dict(raw_item))
        elif isinstance(raw_item, str) and raw_item.strip():
            items.append(
                {
                    "status": "suggestion",
                    "severity": "low",
                    "confidence": "low",
                    "category": "视频检查结果",
                    "location": "",
                    "excerpt": "",
                    "description": raw_item.strip(),
                    "impact": "",
                    "suggestion": "请人工复核该结论。",
                }
            )
    return {
        "summary": str(result.get("summary") or "").strip(),
        "items": items,
    }


def _legacy_combined_section_report(section: str) -> dict:
    text = str(section or "").strip()
    items = []
    for line in _structured_section_items(text, "明确问题"):
        if _summary_line_is_negative(line) or _summary_line_is_normal(line):
            continue
        items.append(_legacy_video_report_item(line, "issue"))
    for line in _structured_section_items(text, "需人工确认"):
        if _summary_line_is_negative(line) or _summary_line_is_normal(line):
            continue
        items.append(_legacy_video_report_item(line, "suggestion"))

    summary_match = re.search(
        r"(?ms)^#{0,6}\s*总体判断\s*$\s*(.*?)(?=^#{1,6}\s|\Z)",
        text,
    )
    summary = summary_match.group(1).strip() if summary_match else ""
    if not items and text and not _summary_line_is_negative(text) and not _summary_line_is_normal(text):
        items.append(_legacy_video_report_item(text, "suggestion"))
    return {"summary": summary, "items": items}


def _legacy_video_report_item(line: str, status: str) -> dict:
    text = re.sub(r"^[-*]\s*", "", str(line or "").strip()).strip()
    location = ""
    description = text
    match = re.match(
        r"^((?:视频时间\s*)?(?:\d{1,2}:)?\d{2}:\d{2}(?:\.\d{1,3})?(?:\s*[-–—~至]\s*(?:\d{1,2}:)?\d{2}:\d{2}(?:\.\d{1,3})?)?|[^：:]{1,40}(?:帧|画面))[:：]\s*(.+)$",
        text,
    )
    if match:
        location = match.group(1).strip()
        description = match.group(2).strip()
    return {
        "status": status,
        "severity": "medium" if status == "issue" else "low",
        "confidence": "medium" if status == "issue" else "low",
        "category": "视频检查结果",
        "location": location,
        "excerpt": "",
        "description": description,
        "impact": "",
        "suggestion": "请人工复核并处理。" if status == "suggestion" else "",
    }


def _split_combined_json_output(content: str, check_items: list[dict]) -> dict[str, str]:
    payload = _load_combined_json_output(content)
    if isinstance(payload, dict):
        results = payload.get("results")
        if results is None and len(check_items) == 1 and isinstance(payload.get("items"), list):
            results = [{"code": check_items[0]["code"], **payload}]
    elif isinstance(payload, list):
        results = payload
    else:
        results = None
    if not isinstance(results, list):
        return {}

    items_by_code = {str(item.get("code") or ""): item for item in check_items}
    sections = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        code = str(result.get("code") or "").strip()
        item = items_by_code.get(code)
        if item is None or code in sections:
            continue
        sections[code] = _format_combined_json_result(item, result)
    return sections


def _load_combined_json_output(content: str, depth: int = 0):
    if depth > 2:
        return None
    text = str(content or "").strip()
    if not text:
        return None
    candidates = [text]
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    object_start = text.find("{")
    object_end = text.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(text[object_start : object_end + 1])
    array_start = text.find("[")
    array_end = text.rfind("]")
    if array_start >= 0 and array_end > array_start:
        candidates.append(text[array_start : array_end + 1])
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, str):
            nested = _load_combined_json_output(parsed, depth + 1)
            if nested is not None:
                return nested
            continue
        return parsed
    return None


def _format_combined_json_result(check_item: dict, result: dict) -> str:
    issue_lines = []
    manual_lines = []
    normal_lines = []
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        line = _format_combined_json_item(raw_item)
        if not line:
            continue
        status = str(raw_item.get("status") or "").strip().lower()
        if status == "issue":
            issue_lines.append(line)
        elif status == "non_issue":
            normal_lines.append(line)
        else:
            manual_lines.append(line)

    summary = str(result.get("summary") or "").strip()
    if not summary:
        summary = "发现明确问题。" if issue_lines else "未发现明确问题。"
    issue_text = "\n".join(f"- {line}" for line in issue_lines) if issue_lines else "- 未发现明确问题。"
    manual_text = "\n".join(f"- {line}" for line in manual_lines) if manual_lines else "- 未发现需人工确认项。"
    normal_text = "\n".join(f"- {line}" for line in normal_lines) if normal_lines else "- 未单独列出正常项。"
    return (
        f"### 检查项：{check_item.get('code')}｜{check_item.get('name')}\n\n"
        f"#### 总体判断\n{summary}\n\n"
        f"#### 明确问题\n{issue_text}\n\n"
        f"#### 需人工确认\n{manual_text}\n\n"
        f"#### 未发现问题\n{normal_text}"
    )


def _format_combined_json_item(item: dict) -> str:
    location = str(item.get("location") or "").strip()
    description = str(item.get("description") or item.get("suggestion") or item.get("excerpt") or "").strip()
    if not description:
        return ""
    line = f"{location}：{description}" if location else description
    details = []
    excerpt = str(item.get("excerpt") or "").strip()
    impact = str(item.get("impact") or "").strip()
    suggestion = str(item.get("suggestion") or "").strip()
    if excerpt and excerpt not in line:
        details.append(f"证据：{excerpt}")
    if impact and impact not in line:
        details.append(f"影响：{impact}")
    if suggestion and suggestion not in line:
        details.append(f"建议：{suggestion}")
    if details:
        line = line.rstrip("；。") + "；" + "；".join(details)
    return line


def _fill_missing_check_sections(sections: dict[str, str], check_items: list[dict]):
    for item in check_items:
        sections.setdefault(item["code"], _missing_check_section(item, "模型未按要求返回该检查项的独立结果。"))


def _fill_missing_structured_check_sections(sections: dict[str, dict], check_items: list[dict]):
    for item in check_items:
        sections.setdefault(item["code"], _missing_structured_check_section())


def _missing_structured_check_section() -> dict:
    return {
        "summary": "模型未按要求返回该检查项的独立结果。",
        "incomplete": True,
        "items": [
            {
                "status": "suggestion",
                "severity": "low",
                "confidence": "low",
                "category": "检查结果完整性",
                "location": "",
                "excerpt": "",
                "description": "模型未按要求返回该检查项的独立结果。",
                "impact": "该检查项可能未被完整执行。",
                "suggestion": "请重新执行任务或人工复核。",
            }
        ],
    }


def _match_check_code_from_header(header_line: str, check_items: list[dict]) -> str:
    normalized = str(header_line or "").strip()
    for item in check_items:
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        if code and code in normalized:
            return code
        if name and name in normalized:
            return code
    return ""


def _missing_check_section(item: dict, reason: str) -> str:
    return (
        f"### 检查项：{item.get('code')}｜{item.get('name')}\n\n"
        "#### 总体判断\n"
        "需人工确认。\n\n"
        "#### 明确问题\n"
        "- 未汇总到明确问题。\n\n"
        "#### 需人工确认\n"
        f"- {reason}\n\n"
        "#### 未发现问题\n"
        "- 未形成独立结论。"
    )


def _image_check_batch_size() -> int:
    return max(1, min(MAX_IMAGE_CHECK_BATCH_SIZE, _int_setting("image_check_batch_size", DEFAULT_IMAGE_CHECK_BATCH_SIZE)))


def _image_batches(image_items: list[dict], batch_size: int) -> list[list[dict]]:
    return [image_items[index : index + batch_size] for index in range(0, len(image_items), batch_size)]


def _check_item_groups(check_items: list[dict]) -> list[list[dict]]:
    return [
        check_items[index : index + MULTIMODAL_CHECK_GROUP_SIZE]
        for index in range(0, len(check_items), MULTIMODAL_CHECK_GROUP_SIZE)
    ]


def _document_text_for_image_batch(document_text: str, image_items: list[dict]) -> str:
    text = str(document_text or "").strip()
    if not text:
        return ""

    pages = sorted(
        {
            page
            for image in image_items
            for page in page_numbers_from_image_item(image)
        }
    )
    if not pages:
        return _trim_document_context(text, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)

    page_sections = page_sections_from_document_text(text)
    if not page_sections:
        return _trim_document_context(text, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)

    wanted_pages = set()
    for page in pages:
        for value in range(page - IMAGE_CONTEXT_NEIGHBOR_PAGES, page + IMAGE_CONTEXT_NEIGHBOR_PAGES + 1):
            if value > 0:
                wanted_pages.add(value)
    selected = [(page, section) for page, section in page_sections if page in wanted_pages]
    if not selected:
        return _trim_document_context(text, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)

    image_lines = []
    for image in image_items:
        filename = str(image.get("filename") or image.get("id") or "图片")
        position = str(image.get("position") or "未标注")
        image_lines.append(f"- {filename}（位置：{position}）")

    page_text = "\n\n".join(section.strip() for _, section in selected if section.strip())
    header = _document_header(text)
    scoped = (
        f"{header}\n\n"
        f"document_text_scope: 仅提供当前图片所在页及前后 {IMAGE_CONTEXT_NEIGHBOR_PAGES} 页的文本，避免跨页误配。\n"
        f"current_batch_images:\n{chr(10).join(image_lines)}\n\n"
        f"相关文档文本：\n{page_text}"
    ).strip()
    return _trim_document_context(scoped, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)


def _document_header(document_text: str) -> str:
    lines = []
    for line in str(document_text or "").splitlines():
        if line.startswith("file:"):
            lines.append(line.strip())
            continue
        if lines:
            break
    return "\n".join(lines) if lines else "file: document"


def _trim_document_context(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[文档上下文已按当前批次截断]"


def _split_checkable_image_items(image_items: list[dict]) -> tuple[list[dict], list[dict]]:
    checkable = []
    skipped = []
    for index, image in enumerate(image_items, start=1):
        item = dict(image)
        item["_image_index"] = index
        mime_type = str(item.get("mime_type") or "")
        if mime_type.startswith("image/"):
            checkable.append(item)
            continue
        item["skip_reason"] = "不是可识别的图片格式"
        skipped.append(item)
    return checkable, skipped


def _multimodal_image_inputs(image_folder: Path, image_items: list[dict]) -> list[dict]:
    inputs = []
    for fallback_index, image in enumerate(image_items, start=1):
        image_index = int(image.get("_image_index") or fallback_index)
        image_path = image_path_from_item(image_folder, image)
        if image_path is None or not image_path.is_file():
            raise RuntimeError(f"提取图片“{image.get('filename') or image_index}”已删除，无法检查")
        mime_type = str(image.get("mime_type") or "")
        if not mime_type.startswith("image/"):
            raise RuntimeError(f"提取图片“{image.get('filename') or image_index}”不是可识别的图片格式")
        inputs.append(
            {
                "index": image_index,
                "name": str(image.get("filename") or f"image-{image_index:04d}"),
                "position": _image_location_label(image),
                "mime_type": mime_type,
                "data_url": image_to_data_url(image_path, mime_type),
            }
        )
    return inputs


def _image_pdf_page_label(image: dict) -> str:
    try:
        page_number = int(image.get("page_number") or 0)
    except (TypeError, ValueError):
        page_number = 0
    if page_number <= 0:
        pages = page_numbers_from_image_item(image)
        page_number = pages[0] if pages else 0
    if page_number <= 0:
        return ""
    return f"PDF第{page_number}页"


def _image_location_label(image: dict) -> str:
    page_label = _image_pdf_page_label(image)
    position = str(image.get("position") or "").strip()
    if page_label and position:
        return f"{page_label}（{position}）"
    if page_label:
        return page_label
    return position or "未标注"


def _format_multimodal_image_check_result(
    batch_results: list[dict],
    *,
    current_batch: dict | None = None,
    current_content: str = "",
    skipped_images: list[dict] | None = None,
    manual_notes: list[str] | None = None,
) -> str:
    parts = []
    for item in batch_results:
        parts.append(_format_image_batch_result(item, item["content"]))
    if current_batch is not None and current_content:
        parts.append(_format_image_batch_result(current_batch, current_content))
    if skipped_images:
        parts.append(_format_skipped_image_result(skipped_images))
    if manual_notes:
        parts.append(_format_manual_notes(manual_notes))
    if current_batch is None:
        summary = _format_image_check_issue_summary(batch_results, skipped_images or [], manual_notes or [])
        if summary:
            parts.append(summary)
    return "\n\n".join(parts).strip()


def _format_image_batch_result(batch: dict, content: str) -> str:
    batch_index = int(batch.get("batch_index") or 1)
    batch_count = int(batch.get("batch_count") or 1)
    images = batch.get("images") or []
    target_label = str(batch.get("target_label") or "图文联合检查")
    media_label = "视频帧" if batch.get("target_kind") == "video_frame" else "图片"
    title = f"### {target_label}结果" if batch_count <= 1 else f"### {target_label}结果（批次 {batch_index}/{batch_count}）"
    image_lines = []
    for image in images:
        filename = str(image.get("filename") or image.get("id") or media_label)
        location = _image_location_label(image)
        image_lines.append(f"- {location}：{filename}")
    image_list = "\n".join(image_lines) if image_lines else f"- 未记录{media_label}"
    return f"{title}\n\n覆盖{media_label}：\n{image_list}\n\n{str(content or '').strip()}"


def _format_skipped_image_result(skipped_images: list[dict]) -> str:
    image_lines = []
    for image in skipped_images:
        filename = str(image.get("filename") or image.get("id") or "图片")
        position = str(image.get("position") or "未标注")
        mime_type = str(image.get("mime_type") or "未知格式")
        reason = str(image.get("skip_reason") or "已跳过")
        image_lines.append(f"- {filename}（位置：{position}，格式：{mime_type}，原因：{reason}）")
    return "### 已跳过的图片\n\n以下提取图片不是可识别图片格式，已跳过，不影响其他图片继续检查：\n" + "\n".join(image_lines)


def _format_manual_notes(manual_notes: list[str]) -> str:
    lines = [f"- {note}" for note in _dedupe_limited([str(note).strip() for note in manual_notes], 30) if note]
    if not lines:
        return ""
    return "### 系统需人工确认\n\n" + "\n".join(lines)


def _format_image_check_issue_summary(batch_results: list[dict], skipped_images: list[dict], manual_notes: list[str] | None = None) -> str:
    issues = []
    manual = []
    for batch in batch_results:
        batch_label = _image_batch_label(batch)
        structured_issues, structured_manual = _structured_summary_items(str(batch.get("content") or ""))
        for line in structured_issues:
            issues.append(f"{batch_label} {line}")
        for line in structured_manual:
            manual.append(f"{batch_label} {line}")
        if structured_issues or structured_manual:
            continue
        for line in _summary_candidate_lines(str(batch.get("content") or "")):
            normalized = _normalize_summary_line(line)
            if not normalized or _summary_line_is_negative(normalized) or _summary_line_is_normal(normalized):
                continue
            entry = f"{batch_label} {normalized}"
            if _summary_line_needs_manual(normalized):
                manual.append(entry)
            elif _summary_line_is_issue(normalized):
                issues.append(entry)
    for image in skipped_images:
        filename = str(image.get("filename") or image.get("id") or "图片")
        location = _image_location_label(image)
        manual.append(f"{location}：{filename} 提取后不是可识别图片格式，已跳过，需人工确认是否影响检查。")
    for note in manual_notes or []:
        manual.append(str(note).strip())

    issues = _dedupe_limited(issues, 30)
    manual = _dedupe_limited(manual, 30)
    if not issues and not manual:
        return "### 检查汇总\n\n#### 明确问题\n- 未发现明确问题。\n\n#### 需人工确认\n- 未发现需人工确认项。"

    issue_text = "\n".join(f"- {item}" for item in issues) if issues else "- 未发现明确问题。"
    manual_text = "\n".join(f"- {item}" for item in manual) if manual else "- 未发现需人工确认项。"
    return f"### 检查汇总\n\n#### 明确问题\n{issue_text}\n\n#### 需人工确认\n{manual_text}"


def _image_batch_label(batch: dict) -> str:
    batch_index = int(batch.get("batch_index") or 1)
    batch_count = int(batch.get("batch_count") or 1)
    page_label = _video_batch_time_label(batch) if batch.get("target_kind") == "video_frame" else _image_batch_pages_label(batch)
    if batch_count > 1:
        prefix = f"批次 {batch_index}/{batch_count}"
    else:
        prefix = "批次 1"
    if page_label:
        return f"{prefix}（{page_label}）："
    return f"{prefix}："


def _video_batch_time_label(batch: dict) -> str:
    positions = []
    seen = set()
    for image in batch.get("images") or []:
        position = str(image.get("position") or "").strip()
        if not position or position in seen:
            continue
        seen.add(position)
        positions.append(position)
    if not positions:
        return ""
    if len(positions) == 1:
        return f"视频时间 {positions[0]}"
    return f"视频时间 {positions[0]}-{positions[-1]}"


def _image_batch_pages_label(batch: dict) -> str:
    pages = []
    seen = set()
    for image in batch.get("images") or []:
        for page in page_numbers_from_image_item(image):
            if page in seen:
                continue
            seen.add(page)
            pages.append(page)
    if not pages:
        return ""
    pages = sorted(pages)
    if len(pages) == 1:
        return f"PDF第{pages[0]}页"
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f"PDF第{pages[0]}-{pages[-1]}页"
    return "PDF第" + "、".join(str(page) for page in pages) + "页"


def _summary_candidate_lines(content: str) -> list[str]:
    lines = []
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[-*]?\s*(覆盖图片|覆盖视频帧|图片名称|图片位置|输出要求|详细问题列表)[:：]?$", line):
            continue
        lines.append(line)
    return lines


def _structured_summary_items(content: str) -> tuple[list[str], list[str]]:
    issues = []
    manual = []
    for item in _structured_section_items(content, "明确问题"):
        if _summary_line_is_negative(item) or _summary_line_is_normal(item):
            continue
        if _summary_line_needs_manual(item):
            manual.append(item)
        elif _summary_line_is_issue(item):
            issues.append(item)
    for item in _structured_section_items(content, "需人工确认"):
        if _summary_line_is_negative(item) or _summary_line_is_normal(item):
            continue
        manual.append(item)
    return issues, manual


def _structured_section_items(content: str, section_title: str) -> list[str]:
    items = []
    current_section = ""
    for raw_line in str(content or "").splitlines():
        heading, inline_text = _summary_section_heading(raw_line)
        if heading:
            current_section = heading
            if heading == section_title and inline_text:
                line = _normalize_summary_line(inline_text)
                if line:
                    items.append(line)
            continue
        if current_section != section_title:
            continue
        line = _normalize_summary_line(raw_line)
        if line:
            items.append(line)
    return items


def _summary_section_heading(line: str) -> tuple[str, str]:
    value = str(line or "").strip()
    if not value:
        return "", ""
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value).strip()
    value = re.sub(r"^\s*[-*]\s*", "", value).strip()
    value = value.strip("*_` \t")
    value = re.sub(r"^\d+[.)、]\s*", "", value).strip()

    title_aliases = (
        ("总体判断", "总体判断"),
        ("明确问题", "明确问题"),
        ("发现问题", "明确问题"),
        ("发现明确问题", "明确问题"),
        ("明确冲突", "明确问题"),
        ("需人工确认", "需人工确认"),
        ("需要人工确认", "需人工确认"),
        ("未发现问题", "未发现问题"),
    )
    for title, normalized_title in title_aliases:
        if value == title:
            return normalized_title, ""
        match = re.match(rf"^{re.escape(title)}\s*[:：]\s*(.+)$", value)
        if match:
            return normalized_title, match.group(1).strip()
    return "", ""


def _normalize_summary_line(line: str) -> str:
    value = re.sub(r"^\s*[-*]\s*", "", str(line or "").strip())
    value = re.sub(r"^\s*\d+[.)、]\s*", "", value)
    value = re.sub(r"\s+", " ", value)
    return value[:240].strip()


def _summary_line_is_negative(line: str) -> bool:
    text = str(line or "")
    if any(marker in text for marker in ("未汇总到", "未发现明确问题", "没有明确问题", "无明确问题", "无问题", "没有问题", "未见问题", "无异常", "未见异常")):
        return True
    if any(marker in text for marker in ("未发现需人工确认", "没有需人工确认", "无需人工确认", "无须人工确认", "未见需人工确认")):
        return True
    return bool(
        re.search(
            r"(未发现|没有发现|未见|无明显|不存在明显).{0,12}(问题|异常|风险|冲突|错误|缺失|不一致|不匹配|不符合)",
            text,
        )
    )


def _summary_line_is_normal(line: str) -> bool:
    text = str(line or "")
    if _summary_line_needs_manual(text) or _summary_line_has_issue_marker(text):
        return False
    return any(marker in text for marker in ("正常", "符合要求", "符合规范", "一致", "匹配", "清晰", "完整", "可读", "无误"))


def _summary_line_needs_manual(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            "需人工确认",
            "需要人工确认",
            "建议人工",
            "建议复核",
            "无法确认",
            "无法判断",
            "无法辨认",
            "难以辨认",
            "不确定",
            "可能",
            "疑似",
            "证据不足",
            "上下文不足",
            "看不清",
            "文字过小",
            "较小",
            "分辨率不足",
        )
    )


def _summary_line_is_issue(line: str) -> bool:
    if _summary_line_is_negative(line) or _summary_line_is_normal(line) or _summary_line_needs_manual(line):
        return False
    return _summary_line_has_issue_marker(line)


def _summary_line_has_issue_marker(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            "问题",
            "风险",
            "不一致",
            "不匹配",
            "冲突",
            "错误",
            "异常",
            "不符合",
            "缺失",
            "缺少",
            "错位",
            "不对应",
            "不完整",
            "不清晰",
            "不规范",
            "模糊",
            "遮挡",
            "裁切",
            "乱码",
            "断裂",
            "变形",
            "过度拉伸",
            "漏标",
            "错标",
            "矛盾",
            "有误",
        )
    )


def _dedupe_limited(items: list[str], limit: int) -> list[str]:
    result = []
    seen = set()
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
        if len(result) >= limit:
            break
    return result


def _task_image_folder(app) -> Path:
    configured = app.config.get("IMAGE_FOLDER")
    if configured:
        return Path(configured)
    return default_image_folder(app.config["UPLOAD_FOLDER"])


def cleanup_expired_task_files(app) -> int:
    retention_days = _task_file_retention_days()
    if retention_days <= 0:
        return 0

    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    tasks = db.execute(
        """
        SELECT *
        FROM tasks
        WHERE status IN ('completed', 'partial', 'failed', 'canceled')
          AND source_files_cleaned_at IS NULL
          AND COALESCE(finished_at, updated_at, created_at) < ?
        ORDER BY COALESCE(finished_at, updated_at, created_at) ASC, id ASC
        LIMIT ?
        """,
        (cutoff, TASK_FILE_CLEANUP_BATCH_SIZE),
    ).fetchall()
    cleaned = 0
    for task in tasks:
        try:
            _remove_task_artifacts(app, task)
            db.execute(
                """
                UPDATE tasks
                SET document_text = NULL,
                    source_files_cleaned_at = ?
                WHERE id = ?
                """,
                (now_text(), task["id"]),
            )
            cleaned += 1
        except TaskArtifactCleanupError as exc:
            app.logger.warning("定期清理任务文件跳过 task_id=%s error=%s", task["id"], exc)
        except Exception:
            app.logger.exception("定期清理任务文件失败 task_id=%s", task["id"])
    if cleaned:
        db.commit()
        _invalidate_task_file_cache_snapshot(app)
        app.logger.info("定期清理任务文件完成 cleaned=%s cutoff=%s retention_days=%s", cleaned, cutoff, retention_days)
    return cleaned


def task_file_cache_snapshot(app, *, force: bool = False) -> dict:
    """返回任务文件缓存快照，并在短时间内复用已计算结果。

    该页面每分钟会刷新一次。快照计算涉及大量文件 stat 和元数据解析，
    因此使用进程内短缓存，避免同一进程内的重复扫描。
    """

    state = _task_file_cache_state(app)
    now = time.monotonic()
    with state["lock"]:
        if (
            not force
            and state["snapshot"] is not None
            and now - state["cached_at"] < TASK_FILE_CACHE_SNAPSHOT_TTL_SECONDS
        ):
            return deepcopy(state["snapshot"])
        snapshot = _build_task_file_cache_snapshot(app)
        state["snapshot"] = snapshot
        state["cached_at"] = time.monotonic()
        return deepcopy(snapshot)


def task_file_cache_snapshot_async(app) -> tuple[dict, bool]:
    """非阻塞地获取快照；首次计算由后台线程完成。"""

    state = _task_file_cache_state(app)
    now = time.monotonic()
    with state["lock"]:
        if state["snapshot"] is not None and now - state["cached_at"] < TASK_FILE_CACHE_SNAPSHOT_TTL_SECONDS:
            return deepcopy(state["snapshot"]), True
        if not state["building"]:
            state["building"] = True
            generation = state["generation"]
            threading.Thread(
                target=_build_task_file_cache_snapshot_background,
                args=(app, generation),
                daemon=True,
                name="task-file-cache-snapshot",
            ).start()
        return _empty_task_file_cache_snapshot(), False


def _task_file_cache_state(app) -> dict:
    state = app.extensions.get("task_file_cache_snapshot")
    if state is not None:
        return state
    with _TASK_FILE_CACHE_STATE_INIT_LOCK:
        state = app.extensions.get("task_file_cache_snapshot")
        if state is None:
            state = {
                "lock": threading.RLock(),
                "snapshot": None,
                "cached_at": 0.0,
                "building": False,
                "generation": 0,
            }
            app.extensions["task_file_cache_snapshot"] = state
    return state


def _invalidate_task_file_cache_snapshot(app) -> None:
    state = _task_file_cache_state(app)
    with state["lock"]:
        state["snapshot"] = None
        state["cached_at"] = 0.0
        state["generation"] += 1


def _build_task_file_cache_snapshot_background(app, generation: int) -> None:
    state = _task_file_cache_state(app)
    try:
        with app.app_context():
            snapshot = _build_task_file_cache_snapshot(app)
        with state["lock"]:
            if state["generation"] == generation:
                state["snapshot"] = snapshot
                state["cached_at"] = time.monotonic()
    except Exception:
        app.logger.exception("后台生成任务文件缓存快照失败")
    finally:
        with state["lock"]:
            state["building"] = False


def _empty_task_file_cache_snapshot() -> dict:
    return {
        "generated_at": now_text(),
        "total_size_bytes": 0,
        "upload_size_bytes": 0,
        "generated_size_bytes": 0,
        "cleanable_size_bytes": 0,
        "cleanable_count": 0,
        "items": [],
    }


def _build_task_file_cache_snapshot(app) -> dict:
    db = get_db()
    file_index, upload_size_bytes, generated_size_bytes = _task_file_cache_file_index(app)
    tasks = db.execute(
        """
        SELECT id, task_type, original_filename, stored_filename, document_meta_json,
               created_at, updated_at, finished_at
        FROM tasks
        WHERE status IN ('completed', 'partial', 'failed', 'canceled')
          AND source_files_cleaned_at IS NULL
        """
    ).fetchall()
    items = []
    for task in tasks:
        size_bytes, file_count = _task_artifact_usage(app, task, file_index=file_index)
        if file_count <= 0:
            continue
        finished_at = task["finished_at"] or task["updated_at"] or task["created_at"]
        items.append(
            {
                "id": int(task["id"]),
                "task_type": task["task_type"],
                "original_filename": task["original_filename"],
                "document_meta_json": task["document_meta_json"],
                "finished_at": finished_at,
                "size_bytes": size_bytes,
                "file_count": file_count,
            }
        )
    items.sort(key=lambda item: (item["finished_at"], item["size_bytes"], item["id"]))

    return {
        "generated_at": now_text(),
        "total_size_bytes": upload_size_bytes + generated_size_bytes,
        "upload_size_bytes": upload_size_bytes,
        "generated_size_bytes": generated_size_bytes,
        "cleanable_size_bytes": sum(item["size_bytes"] for item in items),
        "cleanable_count": len(items),
        "items": items,
    }


def _task_file_cache_file_index(app) -> tuple[dict[str, int], int, int]:
    index: dict[str, int] = {}
    totals = []
    for root in (Path(app.config["UPLOAD_FOLDER"]), _task_image_folder(app)):
        total = 0
        for path, size_bytes in _iter_regular_files(root):
            index[_artifact_path_key(path)] = size_bytes
            total += size_bytes
        totals.append(total)
    return index, totals[0], totals[1]


def _iter_regular_files(root: Path):
    """递归遍历常规文件，避免 Path.rglob 对大量文件反复创建对象。"""

    if not root.is_dir():
        return
    pending = [os.fspath(root)]
    while pending:
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        yield Path(entry.path), int(entry.stat(follow_symlinks=False).st_size)
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                except OSError:
                    continue


def _artifact_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def cleanup_task_file_cache(app, task_ids: list[int]) -> dict:
    normalized_ids = []
    for value in task_ids:
        try:
            task_id = int(value)
        except (TypeError, ValueError):
            continue
        if task_id > 0 and task_id not in normalized_ids:
            normalized_ids.append(task_id)

    result = {"cleaned_ids": [], "freed_size_bytes": 0, "failed": [], "skipped_ids": []}
    if not normalized_ids:
        return result

    db = get_db()
    placeholders = ",".join("?" for _ in normalized_ids)
    tasks = {
        int(task["id"]): task
        for task in db.execute(
            f"""
            SELECT *
            FROM tasks
            WHERE id IN ({placeholders})
              AND status IN ('completed', 'partial', 'failed', 'canceled')
              AND source_files_cleaned_at IS NULL
            """,
            tuple(normalized_ids),
        ).fetchall()
    }
    for task_id in normalized_ids:
        task = tasks.get(task_id)
        if task is None:
            result["skipped_ids"].append(task_id)
            continue
        size_bytes, _ = _task_artifact_usage(app, task)
        try:
            _remove_task_artifacts(app, task)
        except TaskArtifactCleanupError as exc:
            result["failed"].append({"id": task_id, "error": str(exc)})
            continue
        except Exception:
            app.logger.exception("手动清理任务文件失败 task_id=%s", task_id)
            result["failed"].append({"id": task_id, "error": "清理失败，请稍后重试。"})
            continue
        db.execute(
            """
            UPDATE tasks
            SET document_text = NULL,
                source_files_cleaned_at = ?
            WHERE id = ?
            """,
            (now_text(), task_id),
        )
        result["cleaned_ids"].append(task_id)
        result["freed_size_bytes"] += size_bytes

    if result["cleaned_ids"]:
        db.commit()
        _invalidate_task_file_cache_snapshot(app)
        app.logger.info(
            "手动清理任务文件完成 cleaned=%s freed_size_bytes=%s",
            len(result["cleaned_ids"]),
            result["freed_size_bytes"],
        )
    return result


def _task_file_retention_days() -> int:
    return max(0, _int_setting("task_file_retention_days", DEFAULT_TASK_FILE_RETENTION_DAYS))


def _issue_output_limit() -> int:
    return normalize_issue_output_limit(_int_setting("issue_output_limit", DEFAULT_ISSUE_OUTPUT_LIMIT))


def _int_setting(key: str, default: int) -> int:
    value = get_setting(key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _remove_task_artifacts(app, task):
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    image_root = _task_image_folder(app)
    paths = _task_artifact_paths(app, task)
    image_dirs = {
        path.parent
        for path in paths
        if path.parent.resolve() != image_root.resolve() and _path_is_relative_to(path.parent, image_root)
    }
    failures = []
    for path in paths:
        if not (_path_is_relative_to(path, upload_root) or _path_is_relative_to(path, image_root)):
            app.logger.warning("跳过不在运行目录内的任务文件 task_id=%s path=%s", task["id"], path)
            continue
        if path.exists() and path.is_file():
            ok, error = remove_file(path)
            if not ok:
                failures.append((path, error))
    for image_dir in sorted(image_dirs, key=lambda value: len(value.parts), reverse=True):
        _remove_empty_directory(image_dir)
    if failures:
        raise TaskArtifactCleanupError(
            f"任务文件正被其他程序使用，暂时无法清理：{describe_failures(failures)}。"
            "请关闭正在下载、预览或扫描该文件的程序后稍后重试。"
        )


def _task_artifact_paths(app, task) -> list[Path]:
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    image_root = _task_image_folder(app)
    raw_meta = _task_value(task, "document_meta_json")
    paths = []
    groups = document_groups_from_meta(raw_meta)
    if groups:
        for group in groups:
            for file_info in group["files"]:
                stored_filename = Path(str(file_info.get("stored_filename") or "")).name
                if stored_filename:
                    paths.append(upload_root / stored_filename)
    else:
        stored_filename = Path(str(_task_value(task, "stored_filename") or "")).name
        if stored_filename:
            paths.append(upload_root / stored_filename)

    for image in image_items_from_meta(raw_meta):
        image_path = image_path_from_item(image_root, image)
        if image_path is not None:
            paths.append(image_path)
    for image in image_items_from_meta(raw_meta, "page_images"):
        image_path = image_path_from_item(image_root, image)
        if image_path is not None:
            paths.append(image_path)
    for image in image_items_from_meta(raw_meta, "frames"):
        image_path = image_path_from_item(image_root, image)
        if image_path is not None:
            paths.append(image_path)
    return _dedupe_paths(paths)


def _task_artifact_usage(app, task, *, file_index: dict[str, int] | None = None) -> tuple[int, int]:
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    image_root = _task_image_folder(app)
    size_bytes = 0
    file_count = 0
    for path in _task_artifact_paths(app, task):
        if not (_path_is_relative_to(path, upload_root) or _path_is_relative_to(path, image_root)):
            continue
        if file_index is not None:
            artifact_size = file_index.get(_artifact_path_key(path))
            if artifact_size is None:
                continue
            size_bytes += artifact_size
            file_count += 1
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size_bytes += path.stat().st_size
            file_count += 1
        except OSError:
            continue
    return size_bytes, file_count


def _directory_file_size(root: Path) -> int:
    return sum(size_bytes for _, size_bytes in _iter_regular_files(Path(root)))


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _remove_empty_directory(path: Path):
    return cleanup_remove_empty_directory(path)


def _ordered_results(check_items: list[dict], completed: dict[str, dict], partial: dict[str, dict]) -> list[dict]:
    results = []
    for item in check_items:
        result = completed.get(item["code"]) or partial.get(item["code"])
        if result:
            results.append(result)
    return results


def _task_flag(task, key: str) -> bool:
    if hasattr(task, "keys") and key in task.keys():
        return bool(task[key])
    if isinstance(task, dict):
        return bool(task.get(key))
    return False


def _task_claim_token(task) -> str | None:
    value = _task_value(task, "claim_token")
    return str(value).strip() if value else None


def _task_lease_deadline_text() -> str:
    deadline = datetime.now() + timedelta(seconds=TASK_LEASE_SECONDS)
    return deadline.strftime("%Y-%m-%d %H:%M:%S")


def _start_task_lease_heartbeat(
    app,
    task_id: int,
    claim_token: str | None,
    cancel_event: threading.Event,
):
    if not claim_token:
        return None, None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_task_lease_heartbeat,
        args=(app, task_id, claim_token, stop_event, cancel_event),
        daemon=True,
        name=f"task-lease-{task_id}",
    )
    thread.start()
    return stop_event, thread


def _task_lease_heartbeat(
    app,
    task_id: int,
    claim_token: str,
    stop_event: threading.Event,
    cancel_event: threading.Event,
):
    next_renew_at = time.monotonic() + TASK_LEASE_RENEW_INTERVAL_SECONDS
    while not stop_event.wait(TASK_CANCEL_POLL_INTERVAL_SECONDS):
        try:
            with app.app_context():
                db = get_db()
                task = db.execute(
                    "SELECT status, cancel_requested, claim_token FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if (
                    task is None
                    or task["claim_token"] != claim_token
                    or task["status"] not in {"running", "canceling"}
                ):
                    cancel_event.set()
                    return
                if task["cancel_requested"] or task["status"] == "canceling":
                    cancel_event.set()
                if time.monotonic() < next_renew_at:
                    continue
                renewed = db.execute(
                    """
                    UPDATE tasks
                    SET lease_expires_at = ?
                    WHERE id = ? AND status IN ('running', 'canceling') AND claim_token = ?
                    """,
                    (_task_lease_deadline_text(), task_id, claim_token),
                )
                db.commit()
                if renewed.rowcount != 1:
                    cancel_event.set()
                    return
                next_renew_at = time.monotonic() + TASK_LEASE_RENEW_INTERVAL_SECONDS
        except Exception:
            app.logger.exception("任务租约续期失败 task_id=%s", task_id)


def _cancel_requested(db, task_id: int, claim_token: str | None = None) -> bool:
    row = db.execute(
        "SELECT status, cancel_requested, claim_token FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return True
    if claim_token is not None and (
        row["status"] not in {"running", "canceling"} or row["claim_token"] != claim_token
    ):
        return True
    return bool(row["cancel_requested"])


def _update_progress(db, task_id: int, progress: int, claim_token: str | None = None):
    db.execute(
        """
        UPDATE tasks
        SET progress = ?, updated_at = ?
        WHERE id = ? AND status = 'running'
          AND (? IS NULL OR claim_token = ?)
        """,
        (progress, now_text(), task_id, claim_token, claim_token),
    )
    db.commit()


def _save_intermediate_results(
    db,
    task_id: int,
    results: list[dict],
    summary: str,
    progress: int,
    claim_token: str | None = None,
):
    updated_at = now_text()
    db.execute(
        """
        UPDATE tasks
        SET summary = ?,
            progress = MAX(progress, ?),
            updated_at = ?
        WHERE id = ? AND status = 'running'
          AND (? IS NULL OR claim_token = ?)
        """,
        (
            summary,
            progress,
            updated_at,
            task_id,
            claim_token,
            claim_token,
        ),
    )
    db.execute(
        """
        INSERT INTO task_live_results(task_id, result_json, summary, progress, updated_at)
        SELECT ?, ?, ?, ?, ?
        WHERE EXISTS (
            SELECT 1
            FROM tasks
            WHERE id = ? AND status = 'running'
              AND (? IS NULL OR claim_token = ?)
        )
        ON CONFLICT(task_id) DO UPDATE SET
            result_json = excluded.result_json,
            summary = excluded.summary,
            progress = MAX(task_live_results.progress, excluded.progress),
            updated_at = excluded.updated_at
        """,
        (
            task_id,
            json.dumps(results, ensure_ascii=False),
            summary,
            progress,
            updated_at,
            task_id,
            claim_token,
            claim_token,
        ),
    )
    db.commit()


def _progress_heartbeat(
    app,
    task_id: int,
    stop_event: threading.Event,
    start: int,
    end: int,
    timeout_seconds: int,
    claim_token: str | None = None,
):
    if end <= start:
        return
    started_at = time.monotonic()
    climb_seconds = max(60, min(int(timeout_seconds or 300), 300))
    while not stop_event.wait(8):
        elapsed = time.monotonic() - started_at
        ratio = min(1, elapsed / climb_seconds)
        progress = start + int((end - start) * ratio)
        if progress <= start:
            continue
        with app.app_context():
            db = get_db()
            row = db.execute(
                "SELECT status, progress, claim_token FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] != "running":
                return
            if claim_token is not None and row["claim_token"] != claim_token:
                return
            if row["progress"] >= progress:
                continue
            _update_progress(db, task_id, progress, claim_token)


def _mark_canceled(db, task_id: int, claim_token: str | None = None):
    canceled = db.execute(
        """
        UPDATE tasks
        SET status = 'canceled',
            progress = 0,
            api_key = NULL,
            retry_check_codes_json = NULL,
            claim_token = NULL,
            lease_expires_at = NULL,
            updated_at = ?,
            finished_at = ?
        WHERE id = ? AND status IN ('running', 'canceling')
          AND (? IS NULL OR claim_token = ?)
        """,
        (now_text(), now_text(), task_id, claim_token, claim_token),
    )
    if canceled.rowcount == 1:
        db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task_id,))
    db.commit()


def _mark_failed(
    db,
    task_id: int,
    error: str,
    results: list[dict] | None = None,
    claim_token: str | None = None,
):
    existing = db.execute(
        """
        SELECT COALESCE(live.result_json, tasks.result_json) AS result_json,
               COALESCE(live.summary, tasks.summary) AS summary
        FROM tasks
        LEFT JOIN task_live_results live ON live.task_id = tasks.id
        WHERE tasks.id = ? AND (? IS NULL OR tasks.claim_token = ?)
        """,
        (task_id, claim_token, claim_token),
    ).fetchone()
    result_json = existing["result_json"] if existing else None
    summary = existing["summary"] if existing else None
    if results:
        result_json = json.dumps(results, ensure_ascii=False)
        summary = _build_summary(results)
    failed = db.execute(
        """
        UPDATE tasks
        SET status = 'failed',
            error = ?,
            result_json = ?,
            summary = ?,
            api_key = NULL,
            claim_token = NULL,
            lease_expires_at = NULL,
            updated_at = ?,
            finished_at = ?
        WHERE id = ? AND status = 'running'
          AND (? IS NULL OR claim_token = ?)
        """,
        (
            error,
            result_json,
            summary,
            now_text(),
            now_text(),
            task_id,
            claim_token,
            claim_token,
        ),
    )
    if failed.rowcount == 1:
        db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task_id,))
    db.commit()


def _build_summary(results: list[dict]) -> str:
    failed = _failed_check_results(results)
    succeeded = [result for result in results if not _check_result_failed(result)]
    if failed and succeeded:
        failed_names = "、".join(str(item.get("name") or item.get("code") or "未命名检查项") for item in failed)
        return f"已完成 {len(succeeded)}/{len(results)} 个检查项，{len(failed)} 个检查项失败：{failed_names}"
    if failed:
        failed_names = "、".join(str(item.get("name") or item.get("code") or "未命名检查项") for item in failed)
        return f"{len(failed)} 个检查项全部失败：{failed_names}"
    names = "、".join(item["name"] for item in results)
    return f"已完成 {len(results)} 个检查项：{names}"


def _check_result_failed(result: dict) -> bool:
    return bool(str(result.get("error") or "").strip())


def _failed_check_results(results: list[dict]) -> list[dict]:
    return [result for result in results if _check_result_failed(result)]


def _failed_check_items_error(results: list[dict]) -> str:
    parts = []
    for result in results:
        name = str(result.get("name") or result.get("code") or "未命名检查项")
        error = str(result.get("error") or "检查失败").strip()
        if len(error) > 300:
            error = f"{error[:297]}..."
        parts.append(f"{name}：{error}")
    return f"{len(parts)} 个检查项失败：" + "；".join(parts)
