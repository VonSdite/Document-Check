import json
import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from app.checks.common_terms import (
    COMMON_TERMS_CHECK_CODE,
    build_common_terms_invalid_report,
    build_common_terms_missing_report,
    build_common_terms_report,
    common_terms_file_candidates,
    find_common_terms_file,
    format_common_terms_report,
    load_common_terms,
)
from app.checks.guardrails import (
    build_pdf_table_evidence_index,
    sanitize_text_check_result,
)
from app.checks.hyperlinks import (
    HYPERLINK_CHECK_CODE,
    build_hyperlink_report,
    format_hyperlink_report,
    hyperlinks_from_meta,
)
from app.checks.sensitive_terms import (
    SENSITIVE_TERMS_CHECK_CODE,
    build_sensitive_terms_invalid_report,
    build_sensitive_terms_missing_report,
    build_sensitive_terms_report,
    find_sensitive_terms_file,
    format_sensitive_terms_report,
    load_sensitive_terms,
    sensitive_terms_file_candidates,
)
from app.contracts.task_types import (
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    VIDEO_TASK_TYPE,
)
from app.documents.extraction.common import DocumentReadError
from app.documents.images import image_items_from_meta
from app.infrastructure.network import outbound_network_config
from app.models.client import LLMError, run_check
from app.persistence.connection import get_db, now_text
from app.persistence.settings import get_bool_setting
from app.tasks.activity import (
    CHECK_CANCELED_MESSAGE,
    CheckCancelEvent,
    clear_activity,
    finish_check_activity,
    initialize_activity,
    start_check_activity,
    take_check_retries,
    task_activities,
    update_check_activity,
)
from app.tasks.model_output import ModelOutputRecorder
from app.tasks.runtime.common import (
    STREAM_SNAPSHOT_INTERVAL_SECONDS,
    STREAM_SNAPSHOT_MIN_CHAR_GROWTH,
    _check_results_from_json,
    _document_meta,
    _int_setting,
    _issue_output_limit,
    _merge_check_results,
    _ordered_results,
    _task_value,
)
from app.tasks.runtime.image_checks import _run_image_check_items_concurrently
from app.tasks.runtime.preprocessing import _prepare_task_inputs
from app.tasks.runtime.state import (
    TaskCanceled,
    _build_summary,
    _cancel_requested,
    _check_result_failed,
    _failed_check_items_error,
    _failed_check_results,
    _mark_canceled,
    _mark_failed,
    _progress_heartbeat,
    _save_intermediate_results,
    _start_task_cancel_watcher,
    _task_claim_token,
    _task_flag,
    _update_progress,
)
from app.tasks.runtime.video_checks import _run_video_check_items_concurrently
from app.tasks.selection import (
    _stored_retry_check_codes,
    _task_check_items,
    selected_check_items,
)

logger = logging.getLogger(__name__)


DEFAULT_CHECK_ITEM_CONCURRENCY = 1


class TaskRunner:
    def __init__(self, app):
        self.app = app

    def run(self, task_id: int, claim_token: str | None = None):
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

            claim_token = claim_token or _task_claim_token(task)
            cancel_event = threading.Event()
            check_events = {}
            if task["cancel_requested"] or task["status"] == "canceling":
                cancel_event.set()
            cancel_stop, cancel_thread = _start_task_cancel_watcher(
                self.app,
                task_id,
                claim_token,
                cancel_event,
                check_events,
            )
            results = []
            try:
                initialize_activity(task_id, claim_token)
                logger.info(
                    "任务开始 task_id=%s owner=%s ip=%s file=%s model=%s/%s",
                    task_id,
                    task["owner_subject"]
                    if "owner_subject" in task.keys() and task["owner_subject"]
                    else f"ip:{task['ip']}",
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
                previous_by_code = {item.get("code"): item for item in original_results}
                retry_code_set = set(retry_check_codes or [])
                base_results = [
                    result
                    for result in original_results
                    if str(result.get("code") or "").strip() not in retry_code_set
                ]
                if retry_check_codes is not None:
                    logger.info(
                        "任务重试未完成检查项 task_id=%s checks=%s retained=%s",
                        task_id,
                        ",".join(retry_check_codes),
                        len(base_results),
                    )
                max_workers = max(
                    1,
                    _int_setting(
                        "check_item_concurrency", DEFAULT_CHECK_ITEM_CONCURRENCY
                    ),
                )
                check_items = selected_check_items(db, task)
                if retry_check_codes is not None:
                    for item in check_items:
                        item["execution"] = (
                            previous_by_code.get(item["code"], {}).get("execution", 0)
                            + 1
                        )
                initialize_activity(task_id, claim_token, checks=check_items)
                preprocessing_started = time.monotonic()
                logger.info(
                    "任务预处理开始 task_id=%s file_type=%s", task_id, task["file_type"]
                )
                document_text, document_meta_raw = _prepare_task_inputs(
                    self.app,
                    db,
                    task,
                    task_type,
                    claim_token,
                    cancel_event=cancel_event,
                )
                logger.info(
                    "任务预处理完成 task_id=%s duration_seconds=%.3f text_chars=%s",
                    task_id,
                    time.monotonic() - preprocessing_started,
                    len(document_text),
                )
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled
                if task_type == IMAGE_TASK_TYPE:
                    image_items = image_items_from_meta(document_meta_raw)
                    page_image_items = image_items_from_meta(
                        document_meta_raw, "page_images"
                    )
                    if not image_items and not page_image_items:
                        raise RuntimeError(
                            "未能从 PDF 中生成可检查页面截图或提取到可检查图片"
                        )
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
                        stream_trace_enabled=get_bool_setting(
                            "llm_stream_trace_enabled", False
                        ),
                        cancel_event=cancel_event,
                        base_results=base_results,
                    )
                elif task_type == VIDEO_TASK_TYPE:
                    frame_items = image_items_from_meta(document_meta_raw, "frames")
                    if not frame_items:
                        raise RuntimeError("未能从视频中抽取到可检查画面")
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
                        stream_trace_enabled=get_bool_setting(
                            "llm_stream_trace_enabled", False
                        ),
                        cancel_event=cancel_event,
                        base_results=base_results,
                    )
                else:
                    if not check_items:
                        raise RuntimeError("没有可执行的检查项")
                    retry_results = _run_check_items_concurrently(
                        self.app,
                        task,
                        check_items,
                        document_text,
                        document_meta=_document_meta(document_meta_raw),
                        max_workers=max_workers,
                        stream_trace_enabled=get_bool_setting(
                            "llm_stream_trace_enabled", False
                        ),
                        cancel_event=cancel_event,
                        check_events=check_events,
                        base_results=base_results,
                    )
                execution_states = (
                    task_activities([task_id]).get(task_id, {}).get("checks", {})
                )
                for result in retry_results:
                    execution = execution_states.get(result.get("code"), {}).get(
                        "execution", 0
                    )
                    if retry_check_codes is not None:
                        previous = previous_by_code.get(result.get("code"), {})
                        execution = max(execution, previous.get("execution", 0) + 1)
                    if execution:
                        result["execution"] = execution
                results = (
                    _merge_check_results(original_results, retry_results)
                    if retry_check_codes is not None
                    else retry_results
                )
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled

                initialize_activity(task_id, claim_token, phase="finalizing")
                canceled_results = [
                    result for result in results if result.get("canceled")
                ]
                failed_results = _failed_check_results(results)
                successful_results = [
                    result for result in results if not _check_result_failed(result)
                ]
                if failed_results and not successful_results:
                    error = _failed_check_items_error(failed_results)
                    logger.warning(
                        "任务全部检查项失败 task_id=%s error=%s", task_id, error
                    )
                    _mark_failed(db, task_id, error, results, claim_token)
                    return

                final_status = (
                    "canceled"
                    if canceled_results and not successful_results
                    else "partial"
                    if failed_results or canceled_results
                    else "completed"
                )
                summary = _build_summary(results)
                error = (
                    _failed_check_items_error(failed_results)
                    if failed_results
                    else None
                )
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
                    db.execute(
                        "DELETE FROM task_live_results WHERE task_id = ?", (task_id,)
                    )
                db.commit()
                if completed.rowcount == 1:
                    if failed_results:
                        logger.warning(
                            "任务部分完成 task_id=%s succeeded=%s failed=%s",
                            task_id,
                            len(successful_results),
                            len(failed_results),
                        )
                    else:
                        logger.info(
                            "任务检查结束 task_id=%s status=%s checks=%s canceled=%s",
                            task_id,
                            final_status,
                            len(results),
                            len(canceled_results),
                        )
                else:
                    if cancel_event.is_set() or _cancel_requested(
                        db, task_id, claim_token
                    ):
                        _mark_canceled(db, task_id, claim_token)
                    else:
                        logger.warning(
                            "任务执行权已失效，忽略完成结果 task_id=%s", task_id
                        )
            except TaskCanceled:
                logger.info("任务取消 task_id=%s", task_id)
                _mark_canceled(db, task_id, claim_token)
            except (DocumentReadError, LLMError, RuntimeError) as exc:
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    logger.info("任务取消 task_id=%s", task_id)
                    _mark_canceled(db, task_id, claim_token)
                else:
                    logger.warning("任务失败 task_id=%s error=%s", task_id, exc)
                    _mark_failed(db, task_id, str(exc), results, claim_token)
            except Exception as exc:
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    logger.info("任务取消 task_id=%s", task_id)
                    _mark_canceled(db, task_id, claim_token)
                else:
                    logger.exception("任务执行异常 task_id=%s", task_id)
                    _mark_failed(
                        db, task_id, f"任务执行异常：{exc}", results, claim_token
                    )
            finally:
                if cancel_stop is not None:
                    cancel_stop.set()
                if cancel_thread is not None:
                    cancel_thread.join(timeout=2)
                clear_activity(task_id, claim_token)


def _document_check_items(db, task) -> list[dict]:
    return _task_check_items(db, task, DOCUMENT_TASK_TYPE)


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
    check_events: dict[str, threading.Event] | None = None,
) -> list[dict]:
    task_id = task["id"]
    output_recorder = ModelOutputRecorder(app, task_id)
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
    initialize_activity(task_id, claim_token, phase="checking", checks=check_items)
    own_cancel_watcher = check_events is None
    if check_events is None:
        check_events = {}
    initial_states = task_activities([task_id]).get(task_id, {}).get("checks", {})
    check_events.update(
        {
            item["code"]: CheckCancelEvent(
                initial_states.get(item["code"], {}).get("execution", 0)
            )
            for item in check_items
        }
    )
    cancel_stop, cancel_thread = (
        _start_task_cancel_watcher(
            app, task_id, claim_token, cancel_event, check_events
        )
        if own_cancel_watcher
        else (None, None)
    )
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
                f"正在重试 {total} 个未完成检查项。",
                5,
                claim_token,
            )
        else:
            _update_progress(db, task_id, 5, claim_token)
    heartbeat.start()
    task_type = _task_value(task, "task_type") or DOCUMENT_TASK_TYPE
    catalog = {
        item["code"]: item for item in _task_check_items(get_db(), task, task_type)
    }
    catalog.update({item["code"]: item for item in check_items})

    def save_snapshot(db, summary: str, progress: int):
        with result_lock:
            current_results = _ordered_results(
                check_items, completed_by_code, partial_by_code
            )
            snapshot = _merge_check_results(base_results, current_results)
        with save_lock:
            _save_intermediate_results(
                db, task_id, snapshot, summary, progress, claim_token
            )

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

    def save_failed_result(item, execution, error, *, canceled):
        progress = mark_unit_completed()
        with result_lock:
            result = dict(partial_by_code.pop(item["code"], None) or {})
            result.update(
                code=item["code"],
                name=item["name"],
                error=CHECK_CANCELED_MESSAGE if canceled else error,
                issue_output_limit=issue_output_limit,
            )
            if execution:
                result["execution"] = execution
            if canceled:
                result["canceled"] = True
            result.setdefault("result", "")
            completed_by_code[item["code"]] = result
            completed_count = len(completed_by_code)
        save_snapshot(
            get_db(),
            f"{item['name']}{'已取消' if canceled else '检查失败'}，已结束 {completed_count}/{total} 个检查项，继续检查其他项目。",
            progress,
        )
        if canceled:
            logger.info(
                "任务检查项已取消，继续其他检查 task_id=%s item=%s",
                task_id,
                item["name"],
            )
        else:
            logger.warning(
                "任务检查项失败，继续其他检查 task_id=%s item=%s error=%s",
                task_id,
                item["name"],
                error,
            )
        return result

    def run_item(index: int, item: dict) -> dict:
        with app.app_context():
            db = get_db()

            item_cancel_event = check_events[item["code"]]
            execution = item_cancel_event.execution
            execution_meta = {"execution": execution} if execution else {}

            def ensure_active():
                if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                    raise TaskCanceled
                if item_cancel_event.is_set():
                    raise RuntimeError("本检查项已由用户取消。")

            logger.info(
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
                    if (
                        abs(len(content) - last_stream_chars)
                        < STREAM_SNAPSHOT_MIN_CHAR_GROWTH
                    ):
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
                            **execution_meta,
                        }
                    else:
                        partial_by_code.pop(item["code"], None)

                last_stream_write = now
                last_stream_chars = len(content)
                if not content and not had_partial:
                    return
                save_snapshot(db, summary, current_progress())

            try:
                if not start_check_activity(task_id, claim_token, item["code"]):
                    item_cancel_event.set()
                ensure_active()
                structured_report = None
                if item["code"] == SENSITIVE_TERMS_CHECK_CODE:
                    structured_report = _run_sensitive_terms_check(
                        app, document_text, issue_output_limit
                    )
                    content = format_sensitive_terms_report(structured_report)
                elif item["code"] == COMMON_TERMS_CHECK_CODE:
                    structured_report = _run_common_terms_check(
                        app, document_text, issue_output_limit
                    )
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
                        "force_disable_thinking": _task_flag(
                            task, "force_disable_thinking"
                        ),
                        "check_name": item["name"],
                        "prompt": item["prompt"],
                        "document_text": document_text,
                        "issue_output_limit": issue_output_limit,
                        "on_content": lambda content: save_partial(
                            content, f"正在并发检查：{item['name']}"
                        ),
                        "task_id": task_id,
                        "stream_trace_enabled": stream_trace_enabled,
                        "on_output": output_recorder.for_checks(
                            [item["code"]],
                            label=f"第 {execution + 1} 次执行" if execution else "",
                            executions={item["code"]: execution},
                        ),
                        "on_activity": lambda phase, attempt: update_check_activity(
                            task_id, claim_token, [item["code"]], phase, attempt
                        ),
                        "cancel_event": item_cancel_event,
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
                        logger.info(
                            "已过滤证据不足的视觉对象或表格数据缺失结论 task_id=%s item=%s count=%s",
                            task_id,
                            item["name"],
                            filtered_unsupported_count,
                        )
                ensure_active()
                if finish_check_activity(task_id, claim_token, item["code"]):
                    raise RuntimeError("本检查项已由用户取消。")
            except (LLMError, RuntimeError) as exc:
                if cancel_event.is_set():
                    raise TaskCanceled
                canceled = finish_check_activity(
                    task_id, claim_token, item["code"], failed=True
                )
                return save_failed_result(
                    item,
                    execution,
                    str(exc).strip() or exc.__class__.__name__,
                    canceled=canceled,
                )
            progress = mark_unit_completed()

            if cancel_event.is_set() or _cancel_requested(db, task_id, claim_token):
                raise TaskCanceled

            result = {
                "code": item["code"],
                "name": item["name"],
                "result": content,
                "issue_output_limit": issue_output_limit,
                **execution_meta,
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
            logger.info(
                "任务检查项完成 task_id=%s item=%s output_chars=%s",
                task_id,
                item["name"],
                len(content),
            )
            return result

    executor = ThreadPoolExecutor(
        max_workers=max(1, max_workers),
        thread_name_prefix=f"task-check-{task_id}",
    )
    futures = {}
    try:
        futures = {
            executor.submit(run_item, index, item): item["code"]
            for index, item in enumerate(check_items, start=1)
        }
        while True:
            checks = task_activities([task_id]).get(task_id, {}).get("checks", {})
            for future, code in list(futures.items()):
                if checks.get(code, {}).get("phase") == "canceled" and future.cancel():
                    # 队列中的已取消项由调度线程收尾，释放后即可接收该项重试。
                    save_failed_result(
                        catalog[code], check_events[code].execution, "", canceled=True
                    )
                    del futures[future]
            if futures:
                done, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                    del futures[future]
            if cancel_event.is_set() or _cancel_requested(
                get_db(), task_id, claim_token
            ):
                raise TaskCanceled
            retries = take_check_retries(task_id, claim_token, set(futures.values()))
            if retries is None:
                raise TaskCanceled
            if not futures and not retries:
                break
            for retry in retries:
                code = retry["code"]
                item = catalog[code]
                with result_lock:
                    if completed_by_code.pop(code, None) is not None:
                        completed_units -= 1
                    partial_by_code.pop(code, None)
                    base_results[:] = [
                        result for result in base_results if result.get("code") != code
                    ]
                    if not any(selected["code"] == code for selected in check_items):
                        check_items.append(item)
                        total = total_units = len(check_items)
                check_events[code] = CheckCancelEvent(retry["execution"])
                save_snapshot(
                    get_db(), f"等待重新执行：{item['name']}。", current_progress()
                )
                futures[
                    executor.submit(run_item, check_items.index(item) + 1, item)
                ] = code
        with result_lock:
            ordered = _ordered_results(check_items, completed_by_code, {})
        if len(ordered) != total:
            raise RuntimeError("部分检查项未完成")
        return ordered
    except Exception:
        cancel_event.set()
        for event in check_events.values():
            event.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        if cancel_stop is not None:
            cancel_stop.set()
        if cancel_thread is not None:
            cancel_thread.join(timeout=2)
        heartbeat_stop.set()
        heartbeat.join(timeout=2)
        executor.shutdown(wait=True, cancel_futures=True)


def _run_sensitive_terms_check(
    app, document_text: str, issue_output_limit: int
) -> dict:
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
        return build_sensitive_terms_invalid_report(
            source_path=terms_path, error=str(exc)
        )
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
        return build_common_terms_invalid_report(
            source_path=terms_path,
            error=str(exc),
        )
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
