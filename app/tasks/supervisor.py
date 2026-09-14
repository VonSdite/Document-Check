import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

import portalocker
import psutil

from app.persistence.connection import get_db, now_text
from app.persistence.settings import get_setting
from app.tasks.processes import (
    TaskProcess,
    TaskWorkerProcess,
    collect_process_tree,
    matching_process,
    process_identity,
    process_is_running,
    signal_processes,
)
from app.tasks.runtime.artifacts import cleanup_expired_task_files
from app.tasks.runtime.state import (
    TASK_LEASE_RENEW_INTERVAL_SECONDS,
    _mark_canceled,
    _mark_failed,
    _task_lease_deadline_text,
)

logger = logging.getLogger(__name__)


SUPERVISOR_POLL_SECONDS = 2
SUPERVISOR_HEARTBEAT_STALE_SECONDS = 10
SUPERVISOR_SHUTDOWN_GRACE_SECONDS = 10
TASK_CANCEL_GRACE_SECONDS = 10
TASK_TERMINATE_GRACE_SECONDS = 3
TASK_STARTUP_TIMEOUT_SECONDS = 60
REPORT_STATS_REFRESH_INTERVAL_SECONDS = 2
TASK_FILE_CLEANUP_INTERVAL_SECONDS = 3600
SUPERVISOR_STATE_FILENAME = "task-supervisor.json"
SUPERVISOR_LOCK_FILENAME = "task-supervisor.lock"


class TaskSupervisor:
    def __init__(self, app, *, process_factory=TaskWorkerProcess):
        self.app = app
        self.max_processes = max(1, int(app.config.get("MAX_TASK_PROCESSES", 4)))
        self._process_factory = process_factory
        self._active: dict[int, TaskProcess] = {}
        self._shutting_down = False
        self._next_lease_renew_at = 0.0
        self._last_task_file_cleanup = 0.0
        self._last_report_stats_refresh = 0.0

    def run(self, stop_event, *, parent_pid: int | None = None) -> None:
        lock_file = _acquire_supervisor_lock(self.app)
        logger.info(
            "任务调度进程启动 pid=%s max_task_processes=%s",
            os.getpid(),
            self.max_processes,
        )
        maintenance = None
        recovered_previous = False
        try:
            self._recover_previous_processes()
            recovered_previous = True
            maintenance = threading.Thread(
                target=self._run_maintenance,
                args=(stop_event,),
                daemon=True,
                name="task-maintenance",
            )
            maintenance.start()
            _write_supervisor_state(self.app, self._state("running"))
            while not stop_event.is_set() and _parent_is_alive(parent_pid):
                try:
                    self.run_once()
                    _write_supervisor_state(self.app, self._state("running"))
                except Exception:
                    logger.exception("任务调度循环异常")
                for _ in range(int(SUPERVISOR_POLL_SECONDS * 10)):
                    if stop_event.is_set():
                        break
                    time.sleep(0.1)
        finally:
            stop_event.set()
            try:
                self._shutdown_active_processes()
                if maintenance is not None:
                    maintenance.join(timeout=2)
                if recovered_previous and not self._active:
                    _remove_supervisor_state(self.app, os.getpid())
            finally:
                lock_file.release()
            logger.info("任务调度进程退出 pid=%s", os.getpid())

    def run_once(self) -> None:
        self._reap_finished_processes()
        with self.app.app_context():
            self._maintain_active_processes()
            self._launch_available_tasks()

    def _run_maintenance(self, stop_event) -> None:
        while not stop_event.is_set():
            try:
                with self.app.app_context():
                    self._cleanup_task_files_if_due()
                    self._refresh_report_stats_if_due()
            except Exception:
                logger.exception("任务后台维护失败")
            stop_event.wait(SUPERVISOR_POLL_SECONDS)

    def _state(self, status: str) -> dict:
        return {
            "pid": os.getpid(),
            "status": "stopping" if self._shutting_down else status,
            "heartbeat_at": time.time(),
            "active_tasks": len(self._active),
            "max_task_processes": self.max_processes,
            "task_processes": [
                entry.snapshot(task_id) for task_id, entry in self._active.items()
            ],
        }

    def _launch_available_tasks(self) -> None:
        process_slots = self.max_processes - len(self._active)
        if process_slots <= 0:
            return
        claimed_tasks = self._claim_available_tasks(process_slots)
        for task_id, claim_token in claimed_tasks:
            if task_id in self._active:
                logger.error("任务进程仍受管理，跳过重复启动 task_id=%s", task_id)
                continue
            process = self._process_factory(
                task_id, claim_token, root_dir=self.app.config.get("ROOT_DIR")
            )
            entry = TaskProcess(process, claim_token)
            try:
                process.start()
                self._active[task_id] = entry
                entry.create_time = process_identity(process.pid)
                # 先保存进程身份，再允许子进程读取文件与调用模型。
                _write_supervisor_state(self.app, self._state("running"))
                process.allow_start()
            except Exception as exc:
                logger.exception("任务进程启动失败 task_id=%s", task_id)
                entry.startup_error = f"任务进程启动失败：{type(exc).__name__}：{exc}"
                process.close_start()
                if process.pid is None:
                    _recover_owned_task(
                        get_db(),
                        task_id,
                        claim_token,
                        startup_error=entry.startup_error,
                    )
                else:
                    self._active[task_id] = entry
                    entry.stop_requested_at = (
                        time.monotonic() - TASK_CANCEL_GRACE_SECONDS
                    )
                    self._stop_process_if_due(task_id, entry)
                continue
            finally:
                process.close_start()
            logger.info(
                "任务进程已创建并发送启动许可 task_id=%s pid=%s active=%s/%s",
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
        managed_ids = tuple(self._active)
        unmanaged_clause = (
            f" AND id NOT IN ({','.join('?' for _ in managed_ids)})"
            if managed_ids
            else ""
        )
        managed_clause = (
            f" OR id IN ({','.join('?' for _ in managed_ids)})" if managed_ids else ""
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
                """
                + unmanaged_clause,
                (now, now, now, *managed_ids),
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
                """
                + unmanaged_clause,
                (now, now, *managed_ids),
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

            db.execute(
                "DELETE FROM settings WHERE key >= 'task_activity:' AND key < 'task_activity;' "
                "AND NOT EXISTS (SELECT 1 FROM tasks "
                "WHERE id = CAST(substr(settings.key, 15) AS INTEGER) "
                "AND status IN ('running', 'canceling') "
                "AND claim_token IS json_extract(settings.value, '$.claim_token'))"
            )

            global_limit = min(
                self.max_processes,
                max(1, _setting_int("global_concurrency", 3)),
            )
            user_limit = max(1, _setting_int("user_concurrency", 1))
            running_total = db.execute(
                "SELECT COUNT(*) AS total FROM tasks WHERE status IN ('running', 'canceling')"
                + managed_clause,
                managed_ids,
            ).fetchone()["total"]
            slots = min(claim_limit, global_limit - running_total)
            if slots > 0:
                queued = db.execute(
                    f"""
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
                        WHERE status IN ('running', 'canceling') {managed_clause}
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
                            {unmanaged_clause}
                            ORDER BY id LIMIT ?
                        )
                    )
                    SELECT id, owner_subject, running_for_user
                    FROM ranked_candidates
                    WHERE owner_queue_position <= ? - running_for_user
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (
                        *managed_ids,
                        user_limit,
                        *managed_ids,
                        min(user_limit, slots),
                        user_limit,
                        slots,
                    ),
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
            logger.warning("已回收租约过期的运行任务 count=%s", recovered_count)
        if canceled_count:
            logger.warning("已结束租约过期的取消中任务 count=%s", canceled_count)
        return claimed_tasks

    def _maintain_active_processes(self) -> None:
        db = get_db()
        now = time.monotonic()
        renew = now >= self._next_lease_renew_at
        try:
            for task_id, entry in self._active.items():
                if not entry.process.is_alive():
                    continue
                task = db.execute(
                    "SELECT status, cancel_requested, claim_token FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                owns_task = (
                    task is not None and task["claim_token"] == entry.claim_token
                )
                executing = owns_task and task["status"] in {"running", "canceling"}
                if (
                    executing
                    and not entry.process.ready.is_set()
                    and entry.startup_error is None
                    and now - entry.startup_at >= TASK_STARTUP_TIMEOUT_SECONDS
                ):
                    entry.startup_error = (
                        f"任务进程启动超时（{TASK_STARTUP_TIMEOUT_SECONDS} 秒），"
                        f"阶段={entry.process.phase}，请查看 task.log 和控制台日志"
                    )
                    logger.error(
                        "%s task_id=%s pid=%s",
                        entry.startup_error,
                        task_id,
                        entry.process.pid,
                    )
                    entry.stop_requested_at = now - TASK_CANCEL_GRACE_SECONDS
                if (
                    not executing
                    or task["cancel_requested"]
                    or task["status"] == "canceling"
                    or entry.stop_requested_at is not None
                ):
                    if entry.stop_requested_at is None:
                        entry.stop_requested_at = now
                    self._stop_process_if_due(task_id, entry)
                if renew and executing:
                    db.execute(
                        """
                        UPDATE tasks SET lease_expires_at = ?
                        WHERE id = ? AND claim_token = ?
                          AND status IN ('running', 'canceling')
                        """,
                        (_task_lease_deadline_text(), task_id, entry.claim_token),
                    )
            db.commit()
        except Exception:
            db.rollback()
            raise
        if renew:
            self._next_lease_renew_at = (
                time.monotonic() + TASK_LEASE_RENEW_INTERVAL_SECONDS
            )

    def _stop_process_if_due(self, task_id: int, entry: TaskProcess) -> None:
        now = time.monotonic()
        if (
            entry.stop_requested_at is None
            or now - entry.stop_requested_at < TASK_CANCEL_GRACE_SECONDS
        ):
            return
        if entry.terminate_sent_at is None:
            logger.warning(
                "任务进程退出等待结束，发送终止信号 task_id=%s pid=%s",
                task_id,
                entry.process.pid,
            )
            try:
                process = psutil.Process(entry.process.pid)
                entry.descendants = collect_process_tree(process)
            except psutil.NoSuchProcess:
                pass
            self._save_cleanup_state()
            signal_processes(entry.descendants)
            if entry.process.is_alive():
                entry.process.terminate()
            entry.terminate_sent_at = now
        elif now - entry.terminate_sent_at >= TASK_TERMINATE_GRACE_SECONDS:
            signal_processes(entry.descendants, kill=True)
            if entry.process.is_alive():
                entry.process.kill()

    def _reap_finished_processes(self) -> None:
        for task_id, entry in list(self._active.items()):
            process = entry.process
            if process.is_alive():
                continue
            process.join()
            # 任务的外部工具也退出后，才释放该任务的执行名额。
            if any(process_is_running(child) for child in entry.descendants):
                signal_processes(entry.descendants, kill=True)
                continue
            with self.app.app_context():
                if (
                    not process.ready.is_set()
                    and entry.stop_requested_at is None
                    and not self._shutting_down
                ):
                    entry.startup_error = (
                        f"任务进程在业务就绪前退出，阶段={process.phase}，"
                        f"退出码={process.exitcode}，请查看 task.log 和控制台日志"
                    )
                    logger.error(
                        "%s task_id=%s pid=%s",
                        entry.startup_error,
                        task_id,
                        process.pid,
                    )
                recovered = _recover_owned_task(
                    get_db(),
                    task_id,
                    entry.claim_token,
                    startup_error=entry.startup_error,
                )
            self._active.pop(task_id)
            if recovered:
                logger.warning(
                    "任务进程异常退出，任务已重新排队 task_id=%s pid=%s exit_code=%s",
                    task_id,
                    process.pid,
                    process.exitcode,
                )

    def _recover_previous_processes(self) -> None:
        path = supervisor_state_path(self.app)
        if not path.exists():
            return
        # 状态文件读取失败时保留现场，确保进程身份得到确认后再恢复任务。
        state = json.loads(path.read_text(encoding="utf-8"))
        records = state.get("task_processes", [])
        processes = {}
        for record in records:
            previous = [record, *record.get("descendants", [])]
            descendants = {}
            for identity in previous:
                process = matching_process(identity)
                if process is not None:
                    for child in collect_process_tree(process):
                        descendants[child.pid] = child
            record["descendants"] = [
                {"pid": child.pid, "create_time": child.create_time()}
                for child in descendants.values()
            ]
            processes.update(descendants)
            if descendants:
                logger.warning(
                    "清理遗留任务进程 task_id=%s pids=%s",
                    record["task_id"],
                    list(descendants),
                )
        # 保存清理中的进程身份，使清理中断后仍可继续确认全部进程退出。
        _write_supervisor_state(self.app, state)
        processes = list(processes.values())
        signal_processes(processes)
        _, alive = psutil.wait_procs(processes, timeout=TASK_TERMINATE_GRACE_SECONDS)
        signal_processes(alive, kill=True)
        _, alive = psutil.wait_procs(alive, timeout=TASK_TERMINATE_GRACE_SECONDS)
        if any(process_is_running(process) for process in alive):
            raise RuntimeError("遗留任务进程仍在退出，保留进程记录并停止启动")
        with self.app.app_context():
            for record in records:
                _recover_owned_task(
                    get_db(),
                    int(record["task_id"]),
                    record["claim_token"],
                    startup_error=record.get("startup_error"),
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
                logger.info("后台刷新报告统计缓存 count=%s", refreshed)
        except Exception:
            logger.exception("后台刷新报告统计缓存失败")

    def _cleanup_task_files_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_task_file_cleanup < TASK_FILE_CLEANUP_INTERVAL_SECONDS:
            return
        self._last_task_file_cleanup = now
        cleanup_expired_task_files(self.app)

    def _shutdown_active_processes(self) -> None:
        self._shutting_down = True
        if not self._active:
            return
        self._save_cleanup_state()
        deadline = time.monotonic() + SUPERVISOR_SHUTDOWN_GRACE_SECONDS
        while self._active and time.monotonic() < deadline:
            self._reap_finished_processes()
            if self._active:
                time.sleep(0.1)

        for task_id, entry in self._active.items():
            entry.stop_requested_at = time.monotonic() - TASK_CANCEL_GRACE_SECONDS
            self._stop_process_if_due(task_id, entry)
        deadline = time.monotonic() + TASK_TERMINATE_GRACE_SECONDS * 2 + 1
        while self._active and time.monotonic() < deadline:
            for task_id, entry in self._active.items():
                self._stop_process_if_due(task_id, entry)
            self._reap_finished_processes()
            if self._active:
                time.sleep(0.1)
        # 未确认退出的进程继续保留身份和租约归属，供下次启动处理。
        self._save_cleanup_state()

    def _save_cleanup_state(self) -> None:
        try:
            _write_supervisor_state(
                self.app, self._state("stopping" if self._shutting_down else "running")
            )
        except OSError:
            # 进程退出独立于状态文件写入，内存中继续保留全部待退出进程。
            logger.exception("保存任务进程状态失败，继续执行进程清理")


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
    return pid > 0 and psutil.pid_exists(pid)


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


def _recover_owned_task(
    db, task_id: int, claim_token: str, *, startup_error: str | None = None
) -> bool:
    row = db.execute(
        "SELECT status, cancel_requested FROM tasks WHERE id = ? AND claim_token = ?",
        (task_id, claim_token),
    ).fetchone()
    if row is None or row["status"] not in {"running", "canceling"}:
        return False
    if row["cancel_requested"] or row["status"] == "canceling":
        _mark_canceled(db, task_id, claim_token)
        return False
    if startup_error:
        _mark_failed(db, task_id, startup_error, claim_token=claim_token)
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
        db.execute("DELETE FROM settings WHERE key = ?", (f"task_activity:{task_id}",))
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
    try:
        for delay in (0, 0.05, 0.1, 0.2, 0.4, 0.8):
            if delay:
                time.sleep(delay)
            try:
                temporary_path.write_text(
                    json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.replace(temporary_path, path)
                return
            except OSError as exc:
                if not isinstance(exc, PermissionError) and getattr(
                    exc, "winerror", None
                ) not in {5, 32, 33}:
                    raise
                if delay == 0.8:
                    raise
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("状态临时文件等待下次写入清理 path=%s", temporary_path)


def _remove_supervisor_state(app, pid: int) -> None:
    path = supervisor_state_path(app)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if int(state.get("pid") or 0) == pid:
        path.unlink(missing_ok=True)


def _parent_is_alive(parent_pid: int | None) -> bool:
    return parent_pid is None or (
        os.getppid() == parent_pid and psutil.pid_exists(parent_pid)
    )
