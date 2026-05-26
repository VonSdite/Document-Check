import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .db import get_bool_setting, get_db, get_setting, now_text
from .documents import DocumentReadError, extract_text, format_document_text
from .images import default_image_folder, image_items_from_meta, image_path_from_item, image_to_data_url
from .llm import LLMError, run_check, run_multimodal_document_check
from .network import outbound_network_config
from .task_types import CONSISTENCY_TASK_TYPE, DOCUMENT_TASK_TYPE, IMAGE_TASK_TYPE, document_groups_from_meta


class TaskCanceled(Exception):
    pass


DEFAULT_CHECK_ITEM_CONCURRENCY = 1
DEFAULT_IMAGE_CHECK_BATCH_SIZE = 4
MAX_IMAGE_CHECK_BATCH_SIZE = 4
IMAGE_CONTEXT_NEIGHBOR_PAGES = 1
IMAGE_DOCUMENT_CONTEXT_MAX_CHARS = 20000


class TaskScheduler:
    def __init__(self, app):
        self.app = app
        self._stop_event = threading.Event()
        self._launcher = threading.Thread(target=self._loop, daemon=True, name="task-launcher")

    def start(self):
        self._launcher.start()

    def stop(self):
        self._stop_event.set()
        self._launcher.join(timeout=3)

    def _loop(self):
        with self.app.app_context():
            db = get_db()
            db.execute(
                """
                UPDATE tasks
                SET status = 'queued',
                    progress = 0,
                    result_json = NULL,
                    summary = NULL,
                    error = NULL,
                    updated_at = ?,
                    started_at = NULL
                WHERE status = 'running'
                """,
                (now_text(),),
            )
            db.commit()

        while not self._stop_event.is_set():
            try:
                with self.app.app_context():
                    self._launch_available_tasks()
            except Exception:
                self.app.logger.exception("任务调度循环异常")
            self._stop_event.wait(2)

    def _launch_available_tasks(self):
        db = get_db()
        global_limit = max(1, int(get_setting("global_concurrency", 3)))
        user_limit = max(1, int(get_setting("user_concurrency", 1)))

        running_total = db.execute(
            "SELECT COUNT(*) AS total FROM tasks WHERE status = 'running'"
        ).fetchone()["total"]
        slots = global_limit - running_total
        if slots <= 0:
            return

        queued = db.execute(
            """
            SELECT id, ip, COALESCE(owner_subject, 'ip:' || ip) AS owner_subject
            FROM tasks
            WHERE status = 'queued'
            ORDER BY created_at ASC, id ASC
            LIMIT 50
            """
        ).fetchall()

        launched = 0
        for task in queued:
            if launched >= slots:
                break
            running_for_user = db.execute(
                """
                SELECT COUNT(*) AS total
                FROM tasks
                WHERE status = 'running' AND COALESCE(owner_subject, 'ip:' || ip) = ?
                """,
                (task["owner_subject"],),
            ).fetchone()["total"]
            if running_for_user >= user_limit:
                continue

            db.execute(
                """
                UPDATE tasks
                SET status = 'running', progress = 1, started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now_text(), now_text(), task["id"]),
            )
            db.commit()
            worker = threading.Thread(
                target=self._run_task,
                args=(task["id"],),
                daemon=True,
                name=f"task-worker-{task['id']}",
            )
            worker.start()
            launched += 1

    def _run_task(self, task_id: int):
        with self.app.app_context():
            db = get_db()
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                return

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
                if task["cancel_requested"]:
                    _mark_canceled(db, task_id)
                    return

                task_type = task["task_type"] or DOCUMENT_TASK_TYPE
                max_workers = max(
                    1,
                    int(get_setting("check_item_concurrency", DEFAULT_CHECK_ITEM_CONCURRENCY)),
                )
                if task_type == IMAGE_TASK_TYPE:
                    image_items = image_items_from_meta(_task_value(task, "document_meta_json"))
                    if not image_items:
                        raise RuntimeError("未能从文档中提取到可检查图片")
                    document_text = _task_value(task, "document_text") or ""
                    if not document_text:
                        document_text = f"file: {task['original_filename']}\n\nextracted_images: {len(image_items)}"
                    if len(document_text) > task["max_input_chars"]:
                        raise RuntimeError(
                            f"图文检查上下文 {len(document_text)} 字，超过当前模型文本上限 {task['max_input_chars']} 字"
                        )
                    check_items = _task_check_items(db, task, IMAGE_TASK_TYPE)
                    if not check_items:
                        raise RuntimeError("没有可执行的图片检查项")
                    results = _run_image_check_items_concurrently(
                        self.app,
                        task,
                        check_items,
                        image_items,
                        document_text,
                        max_workers=max_workers,
                        stream_trace_enabled=get_bool_setting("llm_stream_trace_enabled", False),
                    )
                else:
                    if task_type == CONSISTENCY_TASK_TYPE:
                        document_text = _task_value(task, "document_text") or _extract_consistency_document_text(self.app, task)
                        check_items = _task_check_items(db, task, CONSISTENCY_TASK_TYPE)
                    else:
                        document_text = _task_value(task, "document_text")
                        if not document_text:
                            upload_path = Path(self.app.config["UPLOAD_FOLDER"]) / task["stored_filename"]
                            document_text = format_document_text(
                                task["original_filename"],
                                extract_text(upload_path, task["file_type"]),
                            )
                        check_items = _document_check_items(db, task)

                    if not document_text:
                        raise RuntimeError("未能从文档中提取到可检查文本")
                    if len(document_text) > task["max_input_chars"]:
                        raise RuntimeError(
                            f"文档文本 {len(document_text)} 字，超过当前模型文本上限 {task['max_input_chars']} 字"
                        )

                    if not check_items:
                        raise RuntimeError("没有可执行的检查项")

                    results = _run_check_items_concurrently(
                        self.app,
                        task,
                        check_items,
                        document_text,
                        max_workers=max_workers,
                        stream_trace_enabled=get_bool_setting("llm_stream_trace_enabled", False),
                    )
                if _cancel_requested(db, task_id):
                    raise TaskCanceled

                summary = _build_summary(results)
                db.execute(
                    """
                    UPDATE tasks
                    SET status = 'completed',
                        progress = 100,
                        result_json = ?,
                        summary = ?,
                        error = NULL,
                        updated_at = ?,
                        finished_at = ?
                    WHERE id = ? AND status = 'running' AND cancel_requested = 0
                    """,
                    (json.dumps(results, ensure_ascii=False), summary, now_text(), now_text(), task_id),
                )
                db.commit()
                self.app.logger.info("任务完成 task_id=%s checks=%s", task_id, len(results))
            except TaskCanceled:
                self.app.logger.info("任务取消 task_id=%s", task_id)
                _mark_canceled(db, task_id)
            except (DocumentReadError, LLMError, RuntimeError) as exc:
                self.app.logger.warning("任务失败 task_id=%s error=%s", task_id, exc)
                _mark_failed(db, task_id, str(exc), results)
            except Exception as exc:
                self.app.logger.exception("任务执行异常：%s", task_id)
                _mark_failed(db, task_id, f"任务执行异常：{exc}", results)


def _extract_consistency_document_text(app, task) -> str:
    groups = document_groups_from_meta(task["document_meta_json"])
    if not groups:
        raise RuntimeError("多文档对照检查缺少文档组信息")

    upload_folder = Path(app.config["UPLOAD_FOLDER"])
    sections = []
    for group in groups:
        label = group["label"]
        group_parts = [f"# {label}"]
        for index, file_info in enumerate(group["files"], start=1):
            stored_filename = Path(str(file_info.get("stored_filename") or "")).name
            file_type = str(file_info.get("file_type") or "").lower()
            original_filename = str(file_info.get("original_filename") or stored_filename or f"文档{index}")
            if not stored_filename or not file_type:
                raise RuntimeError(f"{label}第 {index} 个文档信息不完整")
            upload_path = upload_folder / stored_filename
            if not upload_path.is_file():
                raise RuntimeError(f"{label}“{original_filename}”已删除，无法检查")
            text = extract_text(upload_path, file_type).strip()
            if not text:
                raise RuntimeError(f"{label}“{original_filename}”未能提取到可检查文本")
            group_parts.append(f"## {label}{index}：{original_filename}\n{text}")
        sections.append("\n\n".join(group_parts))
    return "\n\n".join(sections).strip()


def _document_check_items(db, task) -> list[dict]:
    return _task_check_items(db, task, DOCUMENT_TASK_TYPE)


def _task_check_items(db, task, task_type: str) -> list[dict]:
    snapshot = _check_items_from_snapshot(_task_value(task, "checks_snapshot_json"))
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
    except json.JSONDecodeError:
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


def _task_value(task, key: str):
    if hasattr(task, "keys") and key in task.keys():
        return task[key]
    if isinstance(task, dict):
        return task.get(key)
    return None


def _run_check_items_concurrently(
    app,
    task,
    check_items: list[dict],
    document_text: str,
    *,
    max_workers: int,
    stream_trace_enabled: bool,
) -> list[dict]:
    task_id = task["id"]
    total = len(check_items)
    total_units = max(1, total)
    completed_units = 0
    completed_by_code: dict[str, dict] = {}
    partial_by_code: dict[str, dict] = {}
    result_lock = threading.Lock()
    save_lock = threading.Lock()
    cancel_event = threading.Event()
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
        ),
        daemon=True,
        name=f"task-heartbeat-{task_id}",
    )
    with save_lock:
        db = get_db()
        _update_progress(db, task_id, 5)
    heartbeat.start()

    def save_snapshot(db, summary: str, progress: int):
        with result_lock:
            snapshot = _ordered_results(check_items, completed_by_code, partial_by_code)
        with save_lock:
            _save_intermediate_results(db, task_id, snapshot, summary, progress)

    def current_progress() -> int:
        with result_lock:
            units = completed_units
        return 5 + int(units / total_units * 85)

    def mark_unit_completed() -> int:
        nonlocal completed_units
        with result_lock:
            completed_units += 1
            return 5 + int(completed_units / total_units * 85)

    def run_item(index: int, item: dict) -> dict:
        with app.app_context():
            db = get_db()
            if cancel_event.is_set() or _cancel_requested(db, task_id):
                raise TaskCanceled

            app.logger.info(
                "任务检查项开始 task_id=%s item=%s index=%s/%s",
                task_id,
                item["name"],
                index,
                total,
            )
            last_stream_write = 0.0
            def save_partial(content: str, summary: str, *, force: bool = False):
                nonlocal last_stream_write
                content = content.strip()
                now = time.monotonic()
                if not force and content and now - last_stream_write < 1.2:
                    return
                if cancel_event.is_set() or _cancel_requested(db, task_id):
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
                if not content and not had_partial:
                    return
                save_snapshot(db, summary, current_progress())

            network = outbound_network_config()
            content = run_check(
                api_base=task["api_base"],
                api_key=task["api_key"],
                proxy_mode=network["proxy_mode"],
                proxy=network["proxy"],
                ssl_verify=network["ssl_verify"],
                request_timeout=task["request_timeout"],
                model_name=task["model_name"],
                force_disable_thinking=_task_flag(task, "force_disable_thinking"),
                check_name=item["name"],
                prompt=item["prompt"],
                document_text=document_text,
                on_content=lambda content: save_partial(content, f"正在并发检查：{item['name']}"),
                task_id=task_id,
                stream_trace_enabled=stream_trace_enabled,
            )
            progress = mark_unit_completed()

            if cancel_event.is_set() or _cancel_requested(db, task_id):
                raise TaskCanceled

            result = {
                "code": item["code"],
                "name": item["name"],
                "result": content,
            }
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


def _run_image_check_items_concurrently(
    app,
    task,
    check_items: list[dict],
    image_items: list[dict],
    document_text: str,
    *,
    max_workers: int,
    stream_trace_enabled: bool,
) -> list[dict]:
    task_id = task["id"]
    total = len(check_items)
    checkable_image_items, skipped_image_items = _split_checkable_image_items(image_items)
    image_batches = _image_batches(checkable_image_items, _image_check_batch_size())
    batch_count = len(image_batches)
    total_units = max(1, total * max(1, batch_count))
    completed_units = 0
    completed_by_code: dict[str, dict] = {}
    partial_by_code: dict[str, dict] = {}
    result_lock = threading.Lock()
    save_lock = threading.Lock()
    cancel_event = threading.Event()
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
        ),
        daemon=True,
        name=f"task-image-heartbeat-{task_id}",
    )
    with save_lock:
        db = get_db()
        _update_progress(db, task_id, 5)
    heartbeat.start()

    def save_snapshot(db, summary: str, progress: int):
        with result_lock:
            snapshot = _ordered_results(check_items, completed_by_code, partial_by_code)
        with save_lock:
            _save_intermediate_results(db, task_id, snapshot, summary, progress)

    def current_progress() -> int:
        with result_lock:
            units = completed_units
        return 5 + int(units / total_units * 85)

    def mark_unit_completed() -> int:
        nonlocal completed_units
        with result_lock:
            completed_units += 1
            return 5 + int(completed_units / total_units * 85)

    def run_item(index: int, item: dict) -> dict:
        with app.app_context():
            db = get_db()
            if cancel_event.is_set() or _cancel_requested(db, task_id):
                raise TaskCanceled

            app.logger.info(
                "任务图文联合检查项开始 task_id=%s item=%s index=%s/%s images=%s skipped_images=%s batches=%s",
                task_id,
                item["name"],
                index,
                total,
                len(checkable_image_items),
                len(skipped_image_items),
                batch_count,
            )
            batch_results = []
            last_stream_write = 0.0

            def save_partial(current_batch: dict | None, content: str, summary: str, *, force: bool = False):
                nonlocal last_stream_write
                content = content.strip()
                now = time.monotonic()
                if not force and content and now - last_stream_write < 1.2:
                    return
                if cancel_event.is_set() or _cancel_requested(db, task_id):
                    raise TaskCanceled

                result_text = _format_multimodal_image_check_result(
                    batch_results,
                    current_batch=current_batch if content else None,
                    current_content=content,
                )
                with result_lock:
                    if result_text:
                        partial_by_code[item["code"]] = {
                            "code": item["code"],
                            "name": item["name"],
                            "result": result_text,
                        }
                    elif not batch_results:
                        partial_by_code.pop(item["code"], None)

                last_stream_write = now
                save_snapshot(db, summary, current_progress())

            network = outbound_network_config()
            image_folder = _task_image_folder(app)
            if not image_batches:
                result = {
                    "code": item["code"],
                    "name": item["name"],
                    "result": _format_multimodal_image_check_result([], skipped_images=skipped_image_items),
                }
                progress = mark_unit_completed()
                with result_lock:
                    completed_by_code[item["code"]] = result
                    partial_by_code.pop(item["code"], None)
                    completed_count = len(completed_by_code)
                save_snapshot(
                    db,
                    f"已完成 {completed_count}/{total} 个图文检查项，继续检查中。",
                    progress,
                )
                app.logger.info(
                    "任务图文联合检查项跳过 task_id=%s item=%s skipped_images=%s",
                    task_id,
                    item["name"],
                    len(skipped_image_items),
                )
                return result

            for batch_index, batch in enumerate(image_batches, start=1):
                if cancel_event.is_set() or _cancel_requested(db, task_id):
                    raise TaskCanceled
                multimodal_images = _multimodal_image_inputs(image_folder, batch)
                batch_document_text = _document_text_for_image_batch(document_text, batch)
                current_batch = {
                    "batch_index": batch_index,
                    "batch_count": batch_count,
                    "images": batch,
                }

                content = run_multimodal_document_check(
                    api_base=task["api_base"],
                    api_key=task["api_key"],
                    proxy_mode=network["proxy_mode"],
                    proxy=network["proxy"],
                    ssl_verify=network["ssl_verify"],
                    request_timeout=task["request_timeout"],
                    model_name=task["model_name"],
                    force_disable_thinking=_task_flag(task, "force_disable_thinking"),
                    check_name=item["name"],
                    prompt=item["prompt"],
                    document_text=batch_document_text,
                    image_items=multimodal_images,
                    batch_index=batch_index,
                    batch_count=batch_count,
                    on_content=lambda content, current=current_batch: save_partial(
                        current,
                        content,
                        f"正在进行图文联合检查：{item['name']} / 批次 {current['batch_index']}/{current['batch_count']}",
                    ),
                    task_id=task_id,
                    stream_trace_enabled=stream_trace_enabled,
                )
                batch_results.append(
                    {
                        "batch_index": batch_index,
                        "batch_count": batch_count,
                        "images": batch,
                        "content": content,
                    }
                )
                progress = mark_unit_completed()
                save_partial(
                    None,
                    "",
                    f"已完成 {item['name']}：{batch_index}/{batch_count} 个图文批次",
                    force=True,
                )
                save_snapshot(
                    db,
                    f"已完成 {index - 1}/{total} 个图文检查项，正在检查第 {index} 项。",
                    progress,
                )

            result = {
                "code": item["code"],
                "name": item["name"],
                "result": _format_multimodal_image_check_result(batch_results, skipped_images=skipped_image_items),
            }
            with result_lock:
                completed_by_code[item["code"]] = result
                partial_by_code.pop(item["code"], None)
                completed_count = len(completed_by_code)
            save_snapshot(
                db,
                f"已完成 {completed_count}/{total} 个图文检查项，继续检查中。",
                current_progress(),
            )
            app.logger.info(
                "任务图文联合检查项完成 task_id=%s item=%s images=%s skipped_images=%s batches=%s output_chars=%s",
                task_id,
                item["name"],
                len(checkable_image_items),
                len(skipped_image_items),
                len(batch_results),
                len(result["result"]),
            )
            return result

    executor = ThreadPoolExecutor(max_workers=max(1, min(max_workers, total)), thread_name_prefix=f"task-image-check-{task_id}")
    futures = []
    try:
        futures = [executor.submit(run_item, index, item) for index, item in enumerate(check_items, start=1)]
        for future in as_completed(futures):
            future.result()
        with result_lock:
            ordered = _ordered_results(check_items, completed_by_code, {})
        if len(ordered) != total:
            raise RuntimeError("部分图文检查项未完成")
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


def _image_check_batch_size() -> int:
    try:
        return max(1, min(MAX_IMAGE_CHECK_BATCH_SIZE, int(get_setting("image_check_batch_size", DEFAULT_IMAGE_CHECK_BATCH_SIZE))))
    except (TypeError, ValueError):
        return DEFAULT_IMAGE_CHECK_BATCH_SIZE


def _image_batches(image_items: list[dict], batch_size: int) -> list[list[dict]]:
    return [image_items[index : index + batch_size] for index in range(0, len(image_items), batch_size)]


def _document_text_for_image_batch(document_text: str, image_items: list[dict]) -> str:
    text = str(document_text or "").strip()
    if not text:
        return ""

    pages = sorted(
        {
            page
            for image in image_items
            for page in _page_numbers_from_image(image)
        }
    )
    if not pages:
        return _trim_document_context(text, IMAGE_DOCUMENT_CONTEXT_MAX_CHARS)

    page_sections = _page_sections_from_document_text(text)
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


def _page_numbers_from_image(image: dict) -> list[int]:
    value = f"{image.get('position') or ''} {image.get('filename') or ''}"
    pages = []
    for match in re.finditer(r"page0*(\d+)", value, flags=re.IGNORECASE):
        try:
            pages.append(int(match.group(1)))
        except ValueError:
            continue
    return pages


def _page_sections_from_document_text(document_text: str) -> list[tuple[int, str]]:
    text = str(document_text or "")
    body = text.split("\n\nextracted_images:", 1)[0]
    matches = list(re.finditer(r"(?m)^\[第(\d+)页\]\s*$", body))
    sections = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        try:
            page = int(match.group(1))
        except ValueError:
            continue
        sections.append((page, body[match.start() : next_start].strip()))
    return sections


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
        if not image_path.is_file():
            raise RuntimeError(f"提取图片“{image.get('filename') or image_index}”已删除，无法检查")
        mime_type = str(image.get("mime_type") or "")
        if not mime_type.startswith("image/"):
            raise RuntimeError(f"提取图片“{image.get('filename') or image_index}”不是可识别的图片格式")
        inputs.append(
            {
                "index": image_index,
                "name": str(image.get("filename") or f"image-{image_index:04d}"),
                "position": str(image.get("position") or ""),
                "mime_type": mime_type,
                "data_url": image_to_data_url(image_path, mime_type),
            }
        )
    return inputs


def _format_multimodal_image_check_result(
    batch_results: list[dict],
    *,
    current_batch: dict | None = None,
    current_content: str = "",
    skipped_images: list[dict] | None = None,
) -> str:
    parts = []
    for item in batch_results:
        parts.append(_format_image_batch_result(item, item["content"]))
    if current_batch is not None and current_content:
        parts.append(_format_image_batch_result(current_batch, current_content))
    if skipped_images:
        parts.append(_format_skipped_image_result(skipped_images))
    if current_batch is None:
        summary = _format_image_check_issue_summary(batch_results, skipped_images or [])
        if summary:
            parts.append(summary)
    return "\n\n".join(parts).strip()


def _format_image_batch_result(batch: dict, content: str) -> str:
    batch_index = int(batch.get("batch_index") or 1)
    batch_count = int(batch.get("batch_count") or 1)
    images = batch.get("images") or []
    title = "### 图文联合检查结果" if batch_count <= 1 else f"### 图文联合检查结果（批次 {batch_index}/{batch_count}）"
    image_lines = []
    for image in images:
        filename = str(image.get("filename") or image.get("id") or "图片")
        position = str(image.get("position") or "未标注")
        image_lines.append(f"- {filename}（位置：{position}）")
    image_list = "\n".join(image_lines) if image_lines else "- 未记录图片"
    return f"{title}\n\n覆盖图片：\n{image_list}\n\n{str(content or '').strip()}"


def _format_skipped_image_result(skipped_images: list[dict]) -> str:
    image_lines = []
    for image in skipped_images:
        filename = str(image.get("filename") or image.get("id") or "图片")
        position = str(image.get("position") or "未标注")
        mime_type = str(image.get("mime_type") or "未知格式")
        reason = str(image.get("skip_reason") or "已跳过")
        image_lines.append(f"- {filename}（位置：{position}，格式：{mime_type}，原因：{reason}）")
    return "### 已跳过的图片\n\n以下提取图片不是可识别图片格式，已跳过，不影响其他图片继续检查：\n" + "\n".join(image_lines)


def _format_image_check_issue_summary(batch_results: list[dict], skipped_images: list[dict]) -> str:
    issues = []
    manual = []
    for batch in batch_results:
        batch_label = _image_batch_label(batch)
        for line in _summary_candidate_lines(str(batch.get("content") or "")):
            normalized = _normalize_summary_line(line)
            if not normalized or _summary_line_is_negative(normalized):
                continue
            entry = f"{batch_label} {normalized}"
            if _summary_line_needs_manual(normalized):
                manual.append(entry)
            elif _summary_line_is_issue(normalized):
                issues.append(entry)
    for image in skipped_images:
        filename = str(image.get("filename") or image.get("id") or "图片")
        position = str(image.get("position") or "未标注")
        manual.append(f"{filename}（位置：{position}）提取后不是可识别图片格式，已跳过，需人工确认是否影响检查。")

    issues = _dedupe_limited(issues, 30)
    manual = _dedupe_limited(manual, 30)
    if not issues and not manual:
        return "### 检查汇总\n\n明确问题：未汇总到明确问题。\n\n需人工确认：未汇总到需人工确认项。"

    issue_text = "\n".join(f"- {item}" for item in issues) if issues else "- 未汇总到明确问题。"
    manual_text = "\n".join(f"- {item}" for item in manual) if manual else "- 未汇总到需人工确认项。"
    return f"### 检查汇总\n\n#### 明确问题\n{issue_text}\n\n#### 需人工确认\n{manual_text}"


def _image_batch_label(batch: dict) -> str:
    batch_index = int(batch.get("batch_index") or 1)
    batch_count = int(batch.get("batch_count") or 1)
    if batch_count > 1:
        return f"批次 {batch_index}/{batch_count}："
    return "批次 1："


def _summary_candidate_lines(content: str) -> list[str]:
    lines = []
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[-*]?\s*(覆盖图片|图片名称|图片位置|输出要求|详细问题列表)[:：]?$", line):
            continue
        lines.append(line)
    return lines


def _normalize_summary_line(line: str) -> str:
    value = re.sub(r"^\s*[-*]\s*", "", str(line or "").strip())
    value = re.sub(r"^\s*\d+[.)、]\s*", "", value)
    value = re.sub(r"\s+", " ", value)
    return value[:240].strip()


def _summary_line_is_negative(line: str) -> bool:
    return any(marker in line for marker in ("未发现", "没有发现", "无明显", "未见明显", "不存在明显", "未汇总到"))


def _summary_line_needs_manual(line: str) -> bool:
    return any(marker in line for marker in ("需人工确认", "需要人工确认", "无法确认", "无法判断", "不确定", "证据不足", "看不清", "分辨率不足"))


def _summary_line_is_issue(line: str) -> bool:
    return any(
        marker in line
        for marker in ("发现", "问题", "风险", "不一致", "不匹配", "冲突", "错误", "异常", "不符合", "缺失", "错位", "不对应")
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


def _cancel_requested(db, task_id: int) -> bool:
    row = db.execute(
        "SELECT cancel_requested FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return bool(row and row["cancel_requested"])


def _update_progress(db, task_id: int, progress: int):
    db.execute(
        "UPDATE tasks SET progress = ?, updated_at = ? WHERE id = ? AND status = 'running'",
        (progress, now_text(), task_id),
    )
    db.commit()


def _save_intermediate_results(db, task_id: int, results: list[dict], summary: str, progress: int):
    db.execute(
        """
        UPDATE tasks
        SET result_json = ?,
            summary = ?,
            progress = MAX(progress, ?),
            updated_at = ?
        WHERE id = ? AND status = 'running'
        """,
        (json.dumps(results, ensure_ascii=False), summary, progress, now_text(), task_id),
    )
    db.commit()


def _progress_heartbeat(
    app,
    task_id: int,
    stop_event: threading.Event,
    start: int,
    end: int,
    timeout_seconds: int,
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
                "SELECT status, progress FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] != "running":
                return
            if row["progress"] >= progress:
                continue
            _update_progress(db, task_id, progress)


def _mark_canceled(db, task_id: int):
    db.execute(
        """
        UPDATE tasks
        SET status = 'canceled', progress = 0, updated_at = ?, finished_at = ?
        WHERE id = ?
        """,
        (now_text(), now_text(), task_id),
    )
    db.commit()


def _mark_failed(db, task_id: int, error: str, results: list[dict] | None = None):
    existing = db.execute("SELECT result_json, summary FROM tasks WHERE id = ?", (task_id,)).fetchone()
    result_json = existing["result_json"] if existing else None
    summary = existing["summary"] if existing else None
    if not result_json and results:
        result_json = json.dumps(results, ensure_ascii=False)
        summary = f"已完成 {len(results)} 个检查项，后续检查失败。"
    db.execute(
        """
        UPDATE tasks
        SET status = 'failed',
            error = ?,
            result_json = ?,
            summary = ?,
            updated_at = ?,
            finished_at = ?
        WHERE id = ? AND status = 'running'
        """,
        (error, result_json, summary, now_text(), now_text(), task_id),
    )
    db.commit()


def _build_summary(results: list[dict]) -> str:
    names = "、".join(item["name"] for item in results)
    return f"已完成 {len(results)} 个检查项：{names}"
