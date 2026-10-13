"""媒体批次有界并行，流式快照与结果合并由所属执行线程处理。"""

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from app.checks.agent_policy import DEFAULT_AGENT_PARALLELISM
from app.models.client import LLMRequestCanceled
from app.tasks.runtime.agent_batches import BatchCancelEvent


def run_media_batches(
    app, batches, execute, on_content, on_result, ensure_active, cancel_event, gate
):
    updates = {}
    update_lock = threading.Lock()
    stopped = threading.Event()
    canceled = BatchCancelEvent(cancel_event, stopped)

    def emit(number, args):
        with update_lock:
            updates[number] = args

    def run(number, batch):
        with app.app_context():
            while not gate.acquire(timeout=0.1):
                if canceled.is_set():
                    raise LLMRequestCanceled("媒体批次已停止")
            try:
                if canceled.is_set():
                    raise LLMRequestCanceled("媒体批次已停止")
                return execute(
                    number, batch, lambda *args: emit(number, args), canceled
                )
            finally:
                gate.release()

    jobs = iter(enumerate(batches, start=1))
    with ThreadPoolExecutor(
        max_workers=DEFAULT_AGENT_PARALLELISM, thread_name_prefix="media-batch"
    ) as pool:
        pending = {}
        completed = set()

        def submit():
            job = next(jobs, None)
            if job is not None:
                pending[pool.submit(run, *job)] = job

        for _ in range(DEFAULT_AGENT_PARALLELISM):
            submit()
        try:
            while pending:
                ensure_active()
                with update_lock:
                    events = list(updates.items())
                    updates.clear()
                for number, args in events:
                    if number not in completed:
                        on_content(*args)
                done, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    number, batch = pending.pop(future)
                    on_result(number, batch, future.result())
                    completed.add(number)
                    submit()
        finally:
            stopped.set()
            for future in pending:
                future.cancel()
