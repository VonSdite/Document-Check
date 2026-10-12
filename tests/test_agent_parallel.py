import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.contracts.agent import AgentOptions
from app.models.agent import AgentTurn, IncompleteResponse
from app.models.client import LLMError
from app.persistence.checkpoints import CheckpointOwnershipLost, MemoryCheckpointStore
from tests import test_evidence_agent as scripted


def documents(count=6):
    return [
        {
            "filename": "parallel.txt",
            "text": "\n".join(
                f"来源{n}：" + "设备参数正常。" * 85 for n in range(count)
            ),
            "text_complete": True,
        }
    ]


class AgentParallelTest(unittest.TestCase):
    def make_agent(self, handler, **kwargs):
        defaults = dict(
            documents=documents(),
            options=AgentOptions(protocol="structured", chunk_chars=1000),
            code="custom",
        )
        return scripted.EvidenceAgentTest().make_agent(handler, **(defaults | kwargs))

    def test_out_of_order_discovery_matches_serial_report_and_usage(self):
        def execute(parallelism):
            lock = threading.Lock()
            barrier = threading.Barrier(3) if parallelism > 1 else None
            active = peak = 0
            finished = []
            owner_thread = threading.get_ident()

            class OwnerStore(MemoryCheckpointStore):
                def save(self, state):
                    self_thread = threading.get_ident()
                    if self_thread != owner_thread:
                        raise AssertionError("检查点由执行线程统一保存")
                    super().save(state)

            def ensure():
                self.assertEqual(threading.get_ident(), owner_thread)

            def handler(messages, title, tools, force):
                nonlocal active, peak
                context = scripted.input_context(messages)
                if title == "CheckPlan":
                    return scripted.empty_plan()
                if title == "Review":
                    return {
                        "actions": [],
                        "decisions": [
                            scripted.decision(c["id"], c["evidence"])
                            for c in context["candidates"]
                        ],
                    }
                source = context["sources"][0]
                number = source["ordinal"]
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    if barrier:
                        barrier.wait(timeout=3)
                    time.sleep((3 - number % 3) * 0.02)
                    answer = scripted.discovery(
                        context,
                        candidates=[
                            scripted.candidate(
                                [{"source_id": source["id"], "quote": source["text"]}]
                            )
                        ],
                    )
                    return AgentTurn(
                        json.dumps(answer, ensure_ascii=False),
                        usage={
                            "prompt_tokens": 100 + number,
                            "completion_tokens": 50 + number,
                        },
                        finish_reason="stop",
                    )
                finally:
                    with lock:
                        active -= 1
                        finished.append(number)

            result = self.make_agent(
                handler, parallelism=parallelism, store=OwnerStore(), ensure=ensure
            ).run()
            self.assertEqual(result["error"], "")
            self.assertTrue(result["metadata"]["coverage"]["all_checked"])
            self.assertTrue(result["metadata"]["usage_complete"])
            self.assertEqual(result["metadata"]["model_calls"], 9)
            self.assertEqual(result["metadata"]["known_input_tokens"], 915)
            self.assertEqual(result["metadata"]["known_output_tokens"], 465)
            usage = [
                r["input_tokens"]
                for r in result["metadata"]["requests"]
                if r["stage"] == "discover"
            ]
            self.assertEqual(sorted(usage), list(range(100, 106)))
            return result, peak, finished

        serial, serial_peak, _ = execute(1)
        parallel, parallel_peak, finished = execute(3)
        self.assertEqual(serial["report"], parallel["report"])
        self.assertEqual(serial_peak, 1)
        self.assertEqual(parallel_peak, 3)
        self.assertEqual(finished[0], 2)

    def test_failed_first_batch_resumes_cached_later_batches(self):
        store = MemoryCheckpointStore()
        calls = []
        fail = True
        lock = threading.Lock()

        def handler(messages, title, tools, force):
            nonlocal fail
            if title == "CheckPlan":
                return scripted.empty_plan()
            context = scripted.input_context(messages)
            number = context["sources"][0]["ordinal"]
            with lock:
                calls.append(number)
                if number == 0 and fail:
                    fail = False
                    return LLMError("503 服务暂时失败")
            return scripted.discovery(context)

        first = self.make_agent(handler, parallelism=3, store=store).run()
        self.assertIn("503", first["error"])
        self.assertEqual(first["metadata"]["coverage"]["checked_blocks"], 0)
        self.assertEqual(set(store.load()["batch_outputs"]["discover"]), {"1", "2"})
        for parallelism in (1, 3):
            with self.subTest(parallelism=parallelism):
                resumed_store = MemoryCheckpointStore()
                resumed_store.save(store.load())
                count = len(calls)
                result = self.make_agent(
                    handler, parallelism=parallelism, store=resumed_store
                ).run()
                self.assertEqual(result["error"], "")
                self.assertTrue(result["metadata"]["coverage"]["all_checked"])
                self.assertEqual(sorted(calls[count:]), [0, 3, 4, 5])
                self.assertEqual(result["metadata"]["model_calls"], 8)

    def test_parallel_length_recovery_obeys_accumulated_call_budget(self):
        agent = scripted.EvidenceAgentTest().make_agent(
            lambda messages, title, *args: (
                scripted.empty_plan()
                if title == "CheckPlan"
                else IncompleteResponse("输出被截断", finish_reason="length")
            ),
            documents=documents(),
            options=AgentOptions(protocol="structured", chunk_chars=1000, max_calls=4),
            parallelism=3,
        )
        result = agent.run()
        self.assertIn("调用次数预算", result["error"])
        self.assertEqual(result["metadata"]["model_calls"], 4)
        self.assertEqual(result["metadata"]["coverage"]["checked_blocks"], 0)

    def test_recovery_and_resume_keep_later_parallel_outputs(self):
        for mode in ("length", "repair_length", "missing_source"):
            with self.subTest(mode=mode):
                store = MemoryCheckpointStore()
                calls = []
                failed = False

                def handler(messages, title, tools, force):
                    nonlocal failed
                    if title == "CheckPlan":
                        return scripted.empty_plan()
                    context = scripted.input_context(messages)
                    numbers = tuple(s["ordinal"] for s in context["sources"])
                    calls.append(numbers)
                    if numbers == (0, 1):
                        if mode == "missing_source":
                            return {
                                **scripted.discovery(context),
                                "covered_source_ids": [context["sources"][0]["id"]],
                            }
                        if mode == "repair_length" and len(messages) == 3:
                            return AgentTurn(
                                "{",
                                usage={"prompt_tokens": 100, "completion_tokens": 50},
                            )
                        return IncompleteResponse("输出被截断", finish_reason="length")
                    if numbers == (1,) and not failed:
                        failed = True
                        return LLMError("503 服务暂时失败")
                    return scripted.discovery(context)

                source_documents = [
                    {
                        "filename": "a.txt",
                        "text": "\n".join(
                            f"来源{n}：" + "设备参数正常。" * 40 for n in range(6)
                        ),
                        "text_complete": True,
                    }
                ]
                agent = self.make_agent(
                    handler, documents=source_documents, parallelism=3, store=store
                )
                self.assertEqual(len(agent.index.chunks(1000)), 3)
                first = agent.run()
                self.assertIn("503", first["error"])
                self.assertEqual(first["metadata"]["coverage"]["checked_blocks"], 0)
                self.assertEqual(
                    set(store.load()["batch_outputs"]["discover"]), {"1", "2"}
                )
                count = len(calls)
                result = self.make_agent(
                    handler, documents=source_documents, parallelism=3, store=store
                ).run()
                self.assertEqual(result["error"], "")
                self.assertTrue(result["metadata"]["coverage"]["all_checked"])
                self.assertEqual(calls[count:], [(1,)])
                self.assertEqual(calls.count((2, 3)), 1)
                self.assertEqual(calls.count((4, 5)), 1)

    def test_checkpoint_errors_stop_workers_and_release_slots(self):
        for error in (
            TimeoutError("检查点写入超时"),
            CheckpointOwnershipLost("执行权已结束"),
        ):
            with self.subTest(error=type(error).__name__):

                class FailedStore(MemoryCheckpointStore):
                    def save(self, state):
                        if any(r["stage"] == "discover" for r in state["requests"]):
                            raise error
                        super().save(state)

                agent = self.make_agent(
                    lambda messages, title, *args: (
                        scripted.empty_plan()
                        if title == "CheckPlan"
                        else scripted.discovery(scripted.input_context(messages))
                    ),
                    parallelism=3,
                    store=FailedStore(),
                )
                with self.assertRaisesRegex(type(error), str(error)):
                    agent.run()
                self.assertEqual(agent.state["checked_ids"], [])
                for _ in range(3):
                    self.assertTrue(agent.request_gate.acquire(blocking=False))
                for _ in range(3):
                    agent.request_gate.release()

    def test_parallel_cancel_releases_all_request_slots(self):
        canceled = threading.Event()
        barrier = threading.Barrier(3)

        def ensure():
            if canceled.is_set():
                raise RuntimeError("已取消")

        def handler(messages, title, tools, force):
            if title == "CheckPlan":
                return scripted.empty_plan()
            barrier.wait(timeout=3)
            canceled.set()
            return scripted.discovery(scripted.input_context(messages))

        agent = self.make_agent(
            handler, parallelism=3, ensure=ensure, connection={"cancel_event": canceled}
        )
        with self.assertRaisesRegex(RuntimeError, "已取消"):
            agent.run()
        self.assertEqual(agent.state["checked_ids"], [])
        for _ in range(3):
            self.assertTrue(agent.request_gate.acquire(blocking=False))
        for _ in range(3):
            agent.request_gate.release()

    def test_multiple_checks_share_request_concurrency(self):
        gate = threading.BoundedSemaphore(2)
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active = peak = 0

        def handler(messages, title, tools, force):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=3)
                return (
                    scripted.empty_plan()
                    if title == "CheckPlan"
                    else scripted.discovery(scripted.input_context(messages))
                )
            finally:
                with lock:
                    active -= 1

        agents = [
            self.make_agent(handler, parallelism=3, request_gate=gate) for _ in range(2)
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda agent: agent.run(), agents))
        self.assertEqual(peak, 2)
        for result in results:
            self.assertEqual(result["error"], "")
            self.assertTrue(result["metadata"]["coverage"]["all_checked"])

    def test_comparison_runs_in_parallel_after_complete_discovery(self):
        barrier = threading.Barrier(3)
        seen = []
        prompt = "\n".join(f"要求{number}必须说明。" for number in range(6))

        def handler(messages, title, tools, force):
            context = scripted.input_context(messages)
            if title == "CheckPlan":
                return {
                    **scripted.empty_plan(),
                    "requirements": [
                        {
                            "id": str(number),
                            "text": line,
                            "quote": line,
                            "origin": "prompt",
                            "source_id": "",
                            "applicability": "技术手册",
                        }
                        for number, line in enumerate(
                            context["prompt_fragment"].splitlines()
                        )
                    ],
                }
            if title == "Discovery":
                return {
                    **scripted.discovery(context),
                    "observations": [
                        {
                            "requirement_id": r["id"],
                            "status": "not_found",
                            "rationale": "本批未找到",
                            "evidence": [],
                        }
                        for r in context["requirements"]
                    ],
                }
            self.assertEqual(title, "Comparison")
            self.assertTrue(context["coverage"]["all_checked"])
            barrier.wait(timeout=3)
            seen.append(context["requirement"]["id"])
            return {"candidates": []}

        agent = scripted.EvidenceAgentTest().make_agent(
            handler,
            documents=documents(),
            code="compliance",
            item={"code": "compliance", "name": "规范", "prompt": prompt},
            options=AgentOptions(protocol="structured", chunk_chars=1000),
            parallelism=3,
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(seen), 6)
        self.assertEqual(len(set(seen)), 6)
        self.assertTrue(result["metadata"]["coverage"]["all_checked"])

    def test_failed_comparison_resumes_without_repeating_cached_outputs(self):
        store = MemoryCheckpointStore()
        prompt = "\n".join(f"要求{number}必须说明。" for number in range(6))
        seen = []
        failed = False
        lock = threading.Lock()

        def handler(messages, title, tools, force):
            nonlocal failed
            context = scripted.input_context(messages)
            if title == "CheckPlan":
                return {
                    **scripted.empty_plan(),
                    "requirements": [
                        {
                            "id": str(number),
                            "text": line,
                            "quote": line,
                            "origin": "prompt",
                            "source_id": "",
                            "applicability": "技术手册",
                        }
                        for number, line in enumerate(
                            context["prompt_fragment"].splitlines()
                        )
                    ],
                }
            if title == "Discovery":
                return {
                    **scripted.discovery(context),
                    "observations": [
                        {
                            "requirement_id": r["id"],
                            "status": "not_found",
                            "rationale": "本批未找到",
                            "evidence": [],
                        }
                        for r in context["requirements"]
                    ],
                }
            self.assertEqual(title, "Comparison")
            self.assertTrue(context["coverage"]["all_checked"])
            requirement_id = context["requirement"]["id"]
            with lock:
                seen.append(requirement_id)
                if not failed:
                    failed = True
                    return LLMError("503 服务暂时失败")
            return {"candidates": []}

        def make():
            return self.make_agent(
                handler,
                item={"code": "compliance", "name": "规范", "prompt": prompt},
                parallelism=3,
                store=store,
            )

        first = make().run()
        self.assertIn("503", first["error"])
        self.assertEqual(len(store.load()["batch_outputs"]["compare"]), 2)
        failed_id = seen[0]
        completed_ids = set(seen[1:])
        result = make().run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(set(seen)), 6)
        self.assertEqual(seen.count(failed_id), 2)
        for requirement_id in completed_ids:
            self.assertEqual(seen.count(requirement_id), 1)


if __name__ == "__main__":
    unittest.main()
