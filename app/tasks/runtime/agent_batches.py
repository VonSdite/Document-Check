"""并行请求隔离模型状态，执行线程统一保存结果与用量。"""

import copy
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from queue import Empty, Queue

from app.models.client import LLMError, LLMRequestCanceled


class LimitedModel:
    def __init__(self, model, gate, ensure_active):
        self.model = model
        self.gate = gate
        self.ensure_active = ensure_active

    def turn(self, *args, **kwargs):
        while not self.gate.acquire(timeout=0.1):
            self.ensure_active()
        try:
            self.ensure_active()
            return self.model.turn(*args, **kwargs)
        finally:
            self.gate.release()


class BatchCancelEvent:
    def __init__(self, original, stopped):
        self.original = original
        self.stopped = stopped

    def is_set(self):
        return self.stopped.is_set() or bool(self.original and self.original.is_set())

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self.stopped.wait(0.1 if remaining is None else min(0.1, remaining))
        return True


class RequestBridge:
    def __init__(self, events, stopped, connection):
        self.events = events
        self.stopped = stopped
        self.connection = connection
        self.request_index = None

    def call(self, action, *args):
        reply = Future()
        self.events.put((action, args, reply))
        while True:
            if self.stopped.is_set():
                raise LLMRequestCanceled("并行请求已停止")
            try:
                return reply.result(timeout=0.1)
            except TimeoutError:
                if reply.done():
                    return reply.result()
                continue

    def before_request(self, payload):
        self.request_index, remaining = self.call("before", payload)
        self.connection["remaining_seconds"] = remaining

    def after_request(self, payload, turn, duration, error):
        self.call("after", payload, turn, duration, error, self.request_index)


def run_batch_requests(agent, requests, on_result):
    events = Queue()
    stopped = threading.Event()

    def execute(contract, instruction, context):
        worker = copy.copy(agent)
        worker.connection = {
            **agent.connection,
            "cancel_event": BatchCancelEvent(
                agent.connection.get("cancel_event"), stopped
            ),
        }
        bridge = RequestBridge(events, stopped, worker.connection)
        initial_capabilities = dict(agent.state["capabilities"])
        capabilities = dict(initial_capabilities)

        def ensure_active():
            if worker.connection["cancel_event"].is_set():
                raise LLMRequestCanceled("并行请求已停止")

        worker.external_ensure_active = ensure_active
        worker.model = LimitedModel(
            agent.model_factory(
                worker.connection,
                before_request=bridge.before_request,
                after_request=bridge.after_request,
                ensure_active=worker.ensure_active,
                on_output=agent.on_output,
                on_activity=agent.on_activity,
                capabilities=capabilities,
            ),
            agent.request_gate,
            worker.ensure_active,
        )
        with agent.request_context():
            result = worker._ask(contract, instruction, context)
        changes = {
            key: value
            for key, value in capabilities.items()
            if initial_capabilities.get(key) != value
        }
        return result, changes

    failures = {}
    with ThreadPoolExecutor(
        max_workers=agent.parallelism, thread_name_prefix="evidence-batch"
    ) as executor:
        pending = {
            executor.submit(execute, *request): key for key, request in requests.items()
        }
        next_active_check = 0.0
        try:
            while pending:
                if time.monotonic() >= next_active_check:
                    agent.ensure_active()
                    next_active_check = time.monotonic() + 0.25
                while True:
                    try:
                        action, args, reply = events.get_nowait()
                    except Empty:
                        break
                    try:
                        if action == "before":
                            agent.before_request(*args)
                            answer = (
                                len(agent.state["requests"]) - 1,
                                agent.connection["remaining_seconds"],
                            )
                        else:
                            agent.after_request(*args[:-1], request_index=args[-1])
                            answer = None
                    except Exception as exc:
                        reply.set_exception(exc)
                    else:
                        reply.set_result(answer)
                finished, _ = wait(pending, timeout=0.01, return_when=FIRST_COMPLETED)
                for future in finished:
                    key = pending.pop(future)
                    try:
                        result, capabilities = future.result()
                    except LLMError as exc:
                        failures[key] = exc
                    else:
                        on_result(key, result, capabilities)
        finally:
            stopped.set()
            for future in pending:
                future.cancel()
    return failures
