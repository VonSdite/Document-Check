import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .db import get_db, get_setting, now_text
from .documents import DocumentReadError, extract_text, split_text_chunks
from .llm import LLMError, run_check
from .task_types import (
    CONSISTENCY_CHECK_ITEM,
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    document_groups_from_meta,
)


class TaskCanceled(Exception):
    pass


DEFAULT_CHECK_ITEM_CONCURRENCY = 1
DEFAULT_DOCUMENT_CHUNK_CHARS = 12000
MIN_DOCUMENT_CHUNK_CHARS = 2000
DOCUMENT_CHUNK_INPUT_RATIO = 0.5
MAX_DOCUMENT_CHUNKS = 60
CHUNKED_CHECK_ITEM_LIMIT = 8


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
            SELECT id, ip
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
            running_for_ip = db.execute(
                "SELECT COUNT(*) AS total FROM tasks WHERE status = 'running' AND ip = ?",
                (task["ip"],),
            ).fetchone()["total"]
            if running_for_ip >= user_limit:
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
                    "任务开始 task_id=%s ip=%s file=%s model=%s/%s",
                    task_id,
                    task["ip"],
                    task["original_filename"],
                    task["provider_name"],
                    task["model_name"],
                )
                if task["cancel_requested"]:
                    _mark_canceled(db, task_id)
                    return

                task_type = task["task_type"] or DOCUMENT_TASK_TYPE
                if task_type == CONSISTENCY_TASK_TYPE:
                    document_text = _extract_consistency_document_text(self.app, task)
                    check_items = [CONSISTENCY_CHECK_ITEM]
                    max_workers = 1
                else:
                    upload_path = Path(self.app.config["UPLOAD_FOLDER"]) / task["stored_filename"]
                    document_text = extract_text(upload_path, task["file_type"]).strip()
                    check_ids = json.loads(task["checks_json"])
                    placeholders = ",".join("?" for _ in check_ids)
                    check_items = [
                        dict(row)
                        for row in db.execute(
                            f"""
                            SELECT *
                            FROM check_items
                            WHERE id IN ({placeholders}) AND enabled = 1
                            ORDER BY sort_order ASC, id ASC
                            """,
                            tuple(check_ids),
                        ).fetchall()
                    ]
                    max_workers = max(
                        1,
                        int(get_setting("check_item_concurrency", DEFAULT_CHECK_ITEM_CONCURRENCY)),
                    )

                if not document_text:
                    raise RuntimeError("未能从文档中提取到可检查文本")
                if task_type != DOCUMENT_TASK_TYPE and len(document_text) > task["max_input_chars"]:
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
                    stream_trace_enabled=bool(get_setting("llm_stream_trace_enabled", False)),
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
        raise RuntimeError("一致性检查缺少文档组信息")

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
    text_chunks = _text_chunks_for_task(task, document_text)
    chunk_count = len(text_chunks)
    total_units = max(1, total * chunk_count)
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
            chunk_outputs = []

            def save_partial(content: str, summary: str, *, force: bool = False):
                nonlocal last_stream_write
                if cancel_event.is_set() or _cancel_requested(db, task_id):
                    raise TaskCanceled
                content = content.strip()
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

                now = time.monotonic()
                if not force and content and now - last_stream_write < 1.2:
                    return
                last_stream_write = now
                if not content and not had_partial:
                    return
                save_snapshot(db, summary, current_progress())

            if chunk_count == 1:
                content = run_check(
                    api_base=task["api_base"],
                    api_key=task["api_key"],
                    proxy_mode=task["proxy_mode"],
                    proxy=task["proxy"],
                    ssl_verify=bool(task["ssl_verify"]),
                    request_timeout=task["request_timeout"],
                    model_name=task["model_name"],
                    check_name=item["name"],
                    prompt=item["prompt"],
                    document_text=text_chunks[0]["text"],
                    on_content=lambda content: save_partial(content, f"正在并发检查：{item['name']}"),
                    task_id=task_id,
                    stream_trace_enabled=stream_trace_enabled,
                )
                progress = mark_unit_completed()
            else:
                for chunk in text_chunks:
                    chunk_summary = f"正在检查：{item['name']}（片段 {chunk['index']}/{chunk_count}）"

                    def on_chunk_content(content: str, current_chunk=chunk):
                        partial_outputs = [
                            *chunk_outputs,
                            {
                                "index": current_chunk["index"],
                                "total": chunk_count,
                                "label": current_chunk["label"],
                                "result": content,
                            },
                        ]
                        save_partial(_format_chunked_result(partial_outputs, chunk_count), chunk_summary)

                    chunk_content = run_check(
                        api_base=task["api_base"],
                        api_key=task["api_key"],
                        proxy_mode=task["proxy_mode"],
                        proxy=task["proxy"],
                        ssl_verify=bool(task["ssl_verify"]),
                        request_timeout=task["request_timeout"],
                        model_name=task["model_name"],
                        check_name=f"{item['name']}（片段 {chunk['index']}/{chunk_count}）",
                        prompt=_chunked_prompt(item["prompt"], chunk),
                        document_text=_chunked_document_text(chunk),
                        on_content=on_chunk_content,
                        task_id=task_id,
                        stream_trace_enabled=stream_trace_enabled,
                    )
                    chunk_outputs.append(
                        {
                            "index": chunk["index"],
                            "total": chunk_count,
                            "label": chunk["label"],
                            "result": chunk_content,
                        }
                    )
                    progress = mark_unit_completed()
                    save_partial(_format_chunked_result(chunk_outputs, chunk_count), chunk_summary, force=True)
                content = _format_chunked_result(chunk_outputs, chunk_count)

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


def _text_chunks_for_task(task, document_text: str) -> list[dict]:
    task_type = task["task_type"] or DOCUMENT_TASK_TYPE
    if task_type != DOCUMENT_TASK_TYPE:
        return [{"index": 1, "total": 1, "label": "全文", "text": document_text}]
    chunks = split_text_chunks(document_text, _document_chunk_chars(task["max_input_chars"]))
    if len(chunks) > MAX_DOCUMENT_CHUNKS:
        raise RuntimeError(
            f"文档拆分后需要 {len(chunks)} 个片段，超过当前系统上限 {MAX_DOCUMENT_CHUNKS} 个；"
            "请先按章节拆分文档后再提交。"
        )
    return chunks


def _document_chunk_chars(max_input_chars: int) -> int:
    try:
        input_chars = int(max_input_chars)
    except (TypeError, ValueError):
        input_chars = DEFAULT_DOCUMENT_CHUNK_CHARS
    chunk_chars = int(input_chars * DOCUMENT_CHUNK_INPUT_RATIO)
    return max(MIN_DOCUMENT_CHUNK_CHARS, min(DEFAULT_DOCUMENT_CHUNK_CHARS, chunk_chars))


def _chunked_prompt(prompt: str, chunk: dict) -> str:
    return f"""{prompt}

长文档分块执行要求：
1. 当前是第 {chunk['index']}/{chunk['total']} 个片段，只检查当前片段。
2. 不要推断其他片段内容，不要要求补充全文。
3. 本片段最多输出 {CHUNKED_CHECK_ITEM_LIMIT} 条最重要的问题。
4. 找不到问题时只写“本片段未发现明显问题”。
5. 不输出思考过程、推理链或草稿。"""


def _chunked_document_text(chunk: dict) -> str:
    return (
        f"[长文档片段 {chunk['index']}/{chunk['total']}]\n"
        f"[片段线索] {chunk['label']}\n\n"
        f"{chunk['text']}"
    )


def _format_chunked_result(outputs: list[dict], total_chunks: int) -> str:
    parts = [
        f"长文档已分为 {total_chunks} 个片段分别检查。"
        "以下结果按片段排列，跨片段一致性问题建议结合原文人工复核。"
    ]
    for output in outputs:
        result = str(output.get("result") or "").strip() or "模型未返回内容"
        parts.append(
            f"## 片段 {output['index']}/{total_chunks}：{output['label']}\n{result}"
        )
    return "\n\n".join(parts)


def _ordered_results(check_items: list[dict], completed: dict[str, dict], partial: dict[str, dict]) -> list[dict]:
    results = []
    for item in check_items:
        result = completed.get(item["code"]) or partial.get(item["code"])
        if result:
            results.append(result)
    return results


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
