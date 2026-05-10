import json
import threading
import time
from pathlib import Path

from .db import get_db, get_setting, now_text
from .documents import DocumentReadError, extract_text, trim_for_model
from .llm import LLMError, run_check


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
        global_limit = max(1, int(get_setting("global_concurrency", 5)))
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
                if task["cancel_requested"]:
                    _mark_canceled(db, task_id)
                    return

                provider = db.execute(
                    "SELECT * FROM providers WHERE id = ? AND is_active = 1",
                    (task["provider_id"],),
                ).fetchone()
                if provider is None:
                    raise RuntimeError("任务使用的模型提供商不存在或已停用")

                model = db.execute(
                    "SELECT * FROM provider_models WHERE id = ? AND enabled = 1",
                    (task["model_id"],),
                ).fetchone()
                if model is None:
                    raise RuntimeError("任务使用的模型不存在或已停用")

                upload_path = Path(self.app.config["UPLOAD_FOLDER"]) / task["stored_filename"]
                document_text = extract_text(upload_path, task["file_type"]).strip()
                if not document_text:
                    raise RuntimeError("未能从文档中提取到可检查文本")
                document_text = trim_for_model(document_text, limit=provider["max_input_chars"])

                check_ids = json.loads(task["checks_json"])
                placeholders = ",".join("?" for _ in check_ids)
                check_items = db.execute(
                    f"""
                    SELECT *
                    FROM check_items
                    WHERE id IN ({placeholders}) AND enabled = 1
                    ORDER BY sort_order ASC, id ASC
                    """,
                    tuple(check_ids),
                ).fetchall()
                if not check_items:
                    raise RuntimeError("没有可执行的检查项")

                total = len(check_items)
                for index, item in enumerate(check_items, start=1):
                    if _cancel_requested(db, task_id):
                        _mark_canceled(db, task_id)
                        return

                    progress = 5 + int((index - 1) / total * 85)
                    next_progress = 5 + int(index / total * 85)
                    _update_progress(db, task_id, progress)
                    current_parts = []
                    last_stream_write = 0.0

                    def on_delta(delta: str):
                        nonlocal last_stream_write
                        current_parts.append(delta)
                        if time.monotonic() - last_stream_write < 1.2:
                            return
                        last_stream_write = time.monotonic()
                        content = "".join(current_parts).strip()
                        if content:
                            _save_stream_result(db, task_id, results, item, content, progress)

                    heartbeat_stop = threading.Event()
                    heartbeat = threading.Thread(
                        target=_progress_heartbeat,
                        args=(
                            self.app,
                            task_id,
                            heartbeat_stop,
                            progress,
                            max(progress, next_progress - 1),
                            provider["request_timeout"],
                        ),
                        daemon=True,
                        name=f"task-heartbeat-{task_id}-{index}",
                    )
                    heartbeat.start()
                    try:
                        content = run_check(
                            api_base=provider["api_base"],
                            api_key=provider["api_key"],
                            proxy_mode=provider["proxy_mode"],
                            proxy=provider["proxy"],
                            request_timeout=provider["request_timeout"],
                            model_name=model["model_name"],
                            check_name=item["name"],
                            prompt=item["prompt"],
                            document_text=document_text,
                            on_delta=on_delta,
                        )
                    finally:
                        heartbeat_stop.set()
                        heartbeat.join(timeout=2)

                    results.append(
                        {
                            "code": item["code"],
                            "name": item["name"],
                            "result": content,
                        }
                    )
                    _save_intermediate_results(
                        db,
                        task_id,
                        results,
                        f"已完成 {len(results)} 个检查项，继续检查中。",
                        next_progress,
                    )

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
                    WHERE id = ?
                    """,
                    (json.dumps(results, ensure_ascii=False), summary, now_text(), now_text(), task_id),
                )
                db.commit()
            except (DocumentReadError, LLMError, RuntimeError) as exc:
                _mark_failed(db, task_id, str(exc), results)
            except Exception as exc:
                self.app.logger.exception("任务执行异常：%s", task_id)
                _mark_failed(db, task_id, f"任务执行异常：{exc}", results)


def _cancel_requested(db, task_id: int) -> bool:
    row = db.execute(
        "SELECT cancel_requested FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return bool(row and row["cancel_requested"])


def _update_progress(db, task_id: int, progress: int):
    db.execute(
        "UPDATE tasks SET progress = ?, updated_at = ? WHERE id = ?",
        (progress, now_text(), task_id),
    )
    db.commit()


def _save_stream_result(db, task_id: int, completed: list[dict], item, content: str, progress: int):
    results = completed + [
        {
            "code": item["code"],
            "name": item["name"],
            "result": content,
        }
    ]
    _save_intermediate_results(db, task_id, results, f"正在检查：{item['name']}", progress)


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
        WHERE id = ?
        """,
        (error, result_json, summary, now_text(), now_text(), task_id),
    )
    db.commit()


def _build_summary(results: list[dict]) -> str:
    names = "、".join(item["name"] for item in results)
    return f"已完成 {len(results)} 个检查项：{names}"
