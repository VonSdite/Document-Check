"""在一次任务执行内共享模型鉴权失败，停止后续调用及并发请求等待。"""

import logging
import threading
import time

from app.models.client import LLMError

logger = logging.getLogger(__name__)


class _RequestStopEvent:
    def __init__(self, canceled, access_failed):
        self.canceled = canceled
        self.access_failed = access_failed
        self.execution = getattr(canceled, "execution", 0)

    def is_set(self):
        return self.canceled.is_set() or self.access_failed.is_set()

    def set(self):
        self.canceled.set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = 0.05 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                break
            self.access_failed.wait(min(0.05, remaining))
        return self.is_set()


class TaskModelAccess:
    def __init__(self, task_id):
        self.task_id = task_id
        self.failed = threading.Event()
        self.lock = threading.Lock()
        self.message = ""
        self.status_code = None

    def ensure_available(self):
        if self.failed.is_set():
            raise LLMError(
                "本任务的模型鉴权失败，已停止后续模型请求。"
                f"请更新 API Key 或权限，待任务结束后重试。原因：{self.message}",
                retryable=False,
                status_code=self.status_code,
                authentication_failed=True,
            )

    def run(self, request, *, cancel_event, check_canceled, **kwargs):
        def ensure_active():
            check_canceled()
            self.ensure_available()

        ensure_active()
        try:
            return request(
                **kwargs,
                cancel_event=_RequestStopEvent(cancel_event, self.failed),
                check_canceled=ensure_active,
            )
        except LLMError as exc:
            if exc.authentication_failed:
                with self.lock:
                    if not self.failed.is_set():
                        self.message = str(exc)
                        self.status_code = exc.status_code
                        self.failed.set()
                        logger.warning(
                            "任务模型鉴权失败，停止本任务后续模型请求和并发等待 task_id=%s error=%s",
                            self.task_id,
                            exc,
                        )
            raise
