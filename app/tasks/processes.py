"""任务进程身份与跨平台退出处理。"""

from dataclasses import dataclass, field

import psutil


@dataclass
class TaskProcess:
    process: object
    claim_token: str
    create_time: float | None = None
    stop_requested_at: float | None = None
    terminate_sent_at: float | None = None
    descendants: list = field(default_factory=list)

    def snapshot(self, task_id: int) -> dict:
        return {
            "task_id": task_id,
            "claim_token": self.claim_token,
            "pid": self.process.pid,
            "create_time": self.create_time,
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
