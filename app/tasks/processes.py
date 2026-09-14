"""任务进程身份与跨平台退出处理。"""

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field

import psutil

from app.infrastructure.subprocesses import PROJECT_ROOT, python_module_command

logger = logging.getLogger(__name__)


class TaskWorkerProcess:
    """通过独立模块启动任务，持续读取输出并记录业务就绪信号。"""

    def __init__(self, task_id: int, claim_token: str, *, root_dir=None):
        self.task_id = task_id
        self.claim_token = claim_token
        self.root_dir = root_dir
        self._process = None
        self._reader = None
        self.ready = threading.Event()
        self.phase = "created"

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    @property
    def exitcode(self):
        return self._process.poll() if self._process is not None else None

    def start(self) -> None:
        command, environment = python_module_command(
            "app.bootstrap.task", root_dir=self.root_dir
        )
        self._process = subprocess.Popen(
            [*command, str(self.task_id)],
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            close_fds=True,
        )
        self._reader = threading.Thread(
            target=self._read_output, daemon=True, name=f"task-start-{self.task_id}"
        )
        self._reader.start()

    def _read_output(self) -> None:
        with self._process.stdout as output:
            for line in output:
                message = line.rstrip()
                if message == "DOCUMENTCHECK_BOOTSTRAP":
                    self.phase = "waiting_permission"
                    logger.info(
                        "任务入口已进入 task_id=%s pid=%s", self.task_id, self.pid
                    )
                elif message == "DOCUMENTCHECK_INITIALIZING":
                    self.phase = "initializing"
                    logger.info(
                        "任务运行环境初始化 task_id=%s pid=%s", self.task_id, self.pid
                    )
                elif message == "DOCUMENTCHECK_READY":
                    self.phase = "ready"
                    self.ready.set()
                    logger.info(
                        "任务业务已就绪 task_id=%s pid=%s", self.task_id, self.pid
                    )
                elif message:
                    logger.info(
                        "任务进程输出 task_id=%s pid=%s message=%s",
                        self.task_id,
                        self.pid,
                        message,
                    )

    def allow_start(self) -> None:
        try:
            self._process.stdin.write(
                json.dumps({"start": True, "claim_token": self.claim_token}) + "\n"
            )
            self._process.stdin.flush()
        finally:
            self.close_start()

    def close_start(self) -> None:
        if self._process is not None and self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:
                pass

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def join(self, timeout=None) -> None:
        if self._process is None:
            return
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return
        if self._reader is not None and self._reader.ident is not None:
            self._reader.join(timeout=1)
        elif self._process.stdout is not None:
            self._process.stdout.close()

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()


@dataclass
class TaskProcess:
    process: object
    claim_token: str
    create_time: float | None = None
    startup_at: float = field(default_factory=time.monotonic)
    startup_error: str | None = None
    stop_requested_at: float | None = None
    terminate_sent_at: float | None = None
    descendants: list = field(default_factory=list)

    def snapshot(self, task_id: int) -> dict:
        return {
            "task_id": task_id,
            "claim_token": self.claim_token,
            "pid": self.process.pid,
            "create_time": self.create_time,
            "startup_error": self.startup_error,
            "descendants": [
                {"pid": process.pid, "create_time": process.create_time()}
                for process in self.descendants
            ],
        }


def process_identity(pid: int) -> float:
    return psutil.Process(pid).create_time()


def matching_process(record: dict):
    """PID 与创建时间共同标识进程，避免操作复用同一 PID 的进程。"""
    if record.get("create_time") is None:
        return None
    try:
        process = psutil.Process(int(record["pid"]))
        if process.create_time() == float(record["create_time"]):
            return process
    except psutil.NoSuchProcess:
        pass
    return None


def process_is_running(process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def collect_process_tree(process) -> list:
    """记录任务及其 ffmpeg 等子进程，以便等待退出和恢复管理。"""
    try:
        descendants = process.children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    return [*reversed(descendants), process]


def signal_processes(processes, *, kill: bool = False) -> None:
    for process in processes:
        try:
            if kill:
                process.kill()
            else:
                process.terminate()
        except psutil.NoSuchProcess:
            pass
