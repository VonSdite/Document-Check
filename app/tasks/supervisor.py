import json
import multiprocessing
import os
import time
import uuid
from functools import partial
from pathlib import Path

import portalocker

from app.persistence.connection import get_db, now_text
from app.persistence.settings import get_setting
from app.tasks.runner import TaskRunner
from app.tasks.runtime.artifacts import cleanup_expired_task_files
from app.tasks.runtime.state import _mark_canceled, _task_lease_deadline_text

SUPERVISOR_POLL_SECONDS = 2
SUPERVISOR_HEARTBEAT_STALE_SECONDS = 10
SUPERVISOR_SHUTDOWN_GRACE_SECONDS = 10
REPORT_STATS_REFRESH_INTERVAL_SECONDS = 2
TASK_FILE_CLEANUP_INTERVAL_SECONDS = 3600
SUPERVISOR_STATE_FILENAME = "task-supervisor.json"
SUPERVISOR_LOCK_FILENAME = "task-supervisor.lock"


class TaskSupervisor:
    def __init__(self, app, *, process_context=None):
        self.app = app
        self.max_processes = max(1, int(app.config.get("MAX_TASK_PROCESSES", 4)))
        self._process_context = process_context or multiprocessing.get_context("spawn")
        self._active: dict[int, tuple[object, str]] = {}
        self._last_task_file_cleanup = 0.0
        self._last_report_stats_refresh = 0.0

    def run(self, stop_event, *, parent_pid: int | None = None) -> None:
        lock_file = _acquire_supervisor_lock(self.app)
        self.app.logger.info(
            "任务调度进程启动 pid=%s max_task_processes=%s",
            os.getpid(),
            self.max_processes,
        )
        try:
            _write_supervisor_state(self.app, self._state("running"))
            while not stop_event.is_set() and _parent_is_alive(parent_pid):
                try:
                    self.run_once()
                except Exception:
                    self.app.logger.exception("任务调度循环异常")
                _write_supervisor_state(self.app, self._state("running"))
                for _ in range(int(SUPERVISOR_POLL_SECONDS * 10)):
                    if stop_event.is_set():
                        break
                    time.sleep(0.1)
        finally:
            self._shutdown_active_processes()
            _remove_supervisor_state(self.app, os.getpid())
            lock_file.release()
            self.app.logger.info("任务调度进程退出 pid=%s", os.getpid())

    def run_once(self) -> None:
        self._reap_finished_processes()
        with self.app.app_context():
            self._cleanup_task_files_if_due()
            self._refresh_report_stats_if_due()
            self._launch_available_tasks()

    def _state(self, status: str) -> dict:
        return {
            "pid": os.getpid(),
            "status": status,
            "heartbeat_at": time.time(),
            "active_tasks": len(self._active),
            "max_task_processes": self.max_processes,
        }

    def _launch_available_tasks(self) -> None:
        process_slots = self.max_processes - len(self._active)
        if process_slots <= 0:
            return
        claimed_tasks = self._claim_available_tasks(process_slots)
        for task_id, claim_token in claimed_tasks:
            process = self._process_context.Process(
                target=partial(
                    run_claimed_task,
                    root_dir=self.app.config.get("ROOT_DIR"),
                ),
                args=(task_id, claim_token),
                name=f"task-{task_id}",
            )
            try:
                process.start()
            except Exception:
                self.app.logger.exception("任务进程启动失败 task_id=%s", task_id)
                _recover_owned_task(get_db(), task_id, claim_token)
                continue
            self._active[task_id] = (process, claim_token)
            self.app.logger.info(
                "任务进程已启动 task_id=%s pid=%s active=%s/%s",
                task_id,
                process.pid,
                len(self._active),
                self.max_processes,
            )

    def _claim_available_tasks(
        self, max_claims: int | None = None
    ) -> list[tuple[int, str]]:
        db = get_db()
        claimed_tasks: list[tuple[int, str]] = []
        recovered_count = 0
        canceled_count = 0
        claim_limit = (
            self.max_processes if max_claims is None else max(0, int(max_claims))
        )
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

            global_limit = min(
                self.max_processes,
                max(1, _setting_int("global_concurrency", 3)),
            )
            user_limit = max(1, _setting_int("user_concurrency", 1))
            running_total = db.execute(
                "SELECT COUNT(*) AS total FROM tasks WHERE status IN ('running', 'canceling')"
            ).fetchone()["total"]
            slots = min(claim_limit, global_limit - running_total)
            if slots > 0:
                queued = db.execute(
                    """
                    WITH RECURSIVE queued_owners(owner_subject) AS (
                        SELECT MIN(owner_subject) FROM tasks WHERE status = 'queued'
                        UNION ALL
                        SELECT (
                            SELECT MIN(owner_subject) FROM tasks
                            WHERE status = 'queued' AND owner_subject > owners.owner_subject
                        )
                        FROM queued_owners owners
                        WHERE owners.owner_subject IS NOT NULL
                    ),
                    running_by_owner AS (
                        SELECT owner_subject, COUNT(*) AS running_count
                        FROM tasks
                        WHERE status IN ('running', 'canceling')
                        GROUP BY owner_subject
                    ),
                    eligible_owners AS MATERIALIZED (
                        SELECT owners.owner_subject,
                               COALESCE(r.running_count, 0) AS running_for_user
                        FROM queued_owners owners
                        LEFT JOIN running_by_owner r ON r.owner_subject = owners.owner_subject
                        WHERE owners.owner_subject IS NOT NULL
                          AND COALESCE(r.running_count, 0) < ?
                    ),
                    ranked_candidates AS (
                        SELECT queued.id, queued.owner_subject, queued.created_at,
                               owners.running_for_user,
                               ROW_NUMBER() OVER (
                                   PARTITION BY queued.owner_subject ORDER BY queued.id
                               ) AS owner_queue_position
                        FROM eligible_owners owners
                        JOIN tasks queued ON queued.id IN (
                            SELECT id FROM tasks INDEXED BY idx_tasks_status_owner
                            WHERE status = 'queued' AND owner_subject = owners.owner_subject
                            ORDER BY id LIMIT ?
                        )
                    )
                    SELECT id, owner_subject, running_for_user
                    FROM ranked_candidates
                    WHERE owner_queue_position <= ? - running_for_user
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (user_limit, min(user_limit, slots), user_limit, slots),
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
                        (
                            claim_token,
                            _task_lease_deadline_text(),
                            now,
                            now,
                            task["id"],
                        ),
                    )
                    if claimed.rowcount == 1:
                        claimed_tasks.append((task["id"], claim_token))
                        running_by_owner[owner_subject] = running_for_user + 1
            db.commit()
        except Exception:
            db.rollback()
            raise

        if recovered_count:
            self.app.logger.warning(
                "已回收租约过期的运行任务 count=%s", recovered_count
            )
        if canceled_count:
            self.app.logger.warning(
                "已结束租约过期的取消中任务 count=%s", canceled_count
            )
        return claimed_tasks

    def _reap_finished_processes(self) -> None:
        for task_id, (process, claim_token) in list(self._active.items()):
            if process.is_alive():
                continue
            process.join()
            self._active.pop(task_id, None)
            with self.app.app_context():
                recovered = _recover_owned_task(get_db(), task_id, claim_token)
            if recovered:
                self.app.logger.warning(
                    "任务进程异常退出，任务已重新排队 task_id=%s pid=%s exit_code=%s",
                    task_id,
                    process.pid,
                    process.exitcode,
                )

    def _refresh_report_stats_if_due(self) -> None:
        now = time.monotonic()
        if (
            now - self._last_report_stats_refresh
            < REPORT_STATS_REFRESH_INTERVAL_SECONDS
        ):
            return
        self._last_report_stats_refresh = now
        try:
            from app.reporting.statistics import refresh_stale_report_stats_batch

            refreshed = refresh_stale_report_stats_batch()
            if refreshed:
                self.app.logger.info("后台刷新报告统计缓存 count=%s", refreshed)
        except Exception:
            self.app.logger.exception("后台刷新报告统计缓存失败")

    def _cleanup_task_files_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_task_file_cleanup < TASK_FILE_CLEANUP_INTERVAL_SECONDS:
            return
        self._last_task_file_cleanup = now
        cleanup_expired_task_files(self.app)

    def _shutdown_active_processes(self) -> None:
        if not self._active:
            return
        _write_supervisor_state(self.app, self._state("stopping"))
        deadline = time.monotonic() + SUPERVISOR_SHUTDOWN_GRACE_SECONDS
        while self._active and time.monotonic() < deadline:
            self._reap_finished_processes()
            if self._active:
                time.sleep(0.1)

        for process, _claim_token in self._active.values():
            if process.is_alive():
                process.terminate()
        for process, _claim_token in self._active.values():
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)

        with self.app.app_context():
            db = get_db()
            for task_id, (_process, claim_token) in self._active.items():
                _recover_owned_task(db, task_id, claim_token)
        self._active.clear()


def run_claimed_task(
    task_id: int, claim_token: str, *, root_dir: Path | None = None
) -> None:
    from app.infrastructure.runtime import create_task_app

    TaskRunner(create_task_app(root_dir)).run(task_id, claim_token)


def supervisor_state_path(app) -> Path:
    return Path(app.instance_path) / SUPERVISOR_STATE_FILENAME


def supervisor_is_ready(app, *, now: float | None = None) -> bool:
    try:
        state = json.loads(supervisor_state_path(app).read_text(encoding="utf-8"))
        pid = int(state["pid"])
        heartbeat_at = float(state["heartbeat_at"])
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False
    if state.get("status") != "running":
        return False
    if (
        time.time() if now is None else now
    ) - heartbeat_at > SUPERVISOR_HEARTBEAT_STALE_SECONDS:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def wait_for_supervisor(app, process, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_is_alive(process):
            return False
        if supervisor_is_ready(app):
            try:
                state = json.loads(
                    supervisor_state_path(app).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                pass
            else:
                if int(state.get("pid") or 0) == process.pid:
                    return True
        time.sleep(0.05)
    return False


def _process_is_alive(process) -> bool:
    poll = getattr(process, "poll", None)
    if callable(poll):
        return poll() is None
    return process.is_alive()


def _setting_int(key: str, default: int) -> int:
    try:
        return int(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def _recover_owned_task(db, task_id: int, claim_token: str) -> bool:
    row = db.execute(
        "SELECT status, cancel_requested FROM tasks WHERE id = ? AND claim_token = ?",
        (task_id, claim_token),
    ).fetchone()
    if row is None or row["status"] not in {"running", "canceling"}:
        return False
    if row["cancel_requested"] or row["status"] == "canceling":
        _mark_canceled(db, task_id, claim_token)
        return False

    updated = db.execute(
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
        WHERE id = ? AND status = 'running' AND claim_token = ?
        """,
        (now_text(), task_id, claim_token),
    )
    if updated.rowcount == 1:
        db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task_id,))
    db.commit()
    return updated.rowcount == 1


def _acquire_supervisor_lock(app):
    lock_path = Path(app.instance_path) / SUPERVISOR_LOCK_FILENAME
    lock_file = portalocker.Lock(
        str(lock_path),
        mode="a+",
        timeout=0,
        fail_when_locked=True,
        encoding="utf-8",
    )
    try:
        lock_file.acquire()
    except portalocker.exceptions.AlreadyLocked as exc:
        lock_file.release()
        raise RuntimeError("已有任务调度进程正在运行") from exc
    return lock_file


def _write_supervisor_state(app, state: dict) -> None:
    path = supervisor_state_path(app)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(
        json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def _remove_supervisor_state(app, pid: int) -> None:
    path = supervisor_state_path(app)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if int(state.get("pid") or 0) == pid:
        path.unlink(missing_ok=True)


def _parent_is_alive(parent_pid: int | None) -> bool:
    return parent_pid is None or os.getppid() == parent_pid
