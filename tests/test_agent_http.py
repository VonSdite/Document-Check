import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.contracts.agent import AgentOptions
from app.documents.index import DocumentIndex
from app.persistence.checkpoints import MemoryCheckpointStore
from app.tasks.runtime.evidence_agent import EvidenceAgent
from scripts.evaluate_checks import run_case
from tests.test_evidence_agent import (
    candidate,
    decision,
    discovery,
    empty_plan,
    input_context,
)


class AgentHTTPTest(unittest.TestCase):
    def setUp(self):
        self.payloads = []
        payloads = self.payloads
        self.behavior = {
            "truncate_plan": False,
            "truncate_discovery": False,
            "truncate_review": False,
            "long_thinking": False,
            "per_source_candidates": False,
            "omit_last_source": False,
        }
        behavior = self.behavior

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                payloads.append(payload)
                context = input_context(payload["messages"])
                schema = (
                    payload.get("response_format", {})
                    .get("json_schema", {})
                    .get("schema", {})
                )
                title = schema.get("title")
                if title == "Discovery" and behavior.get("discovery_barrier"):
                    behavior["discovery_barrier"].wait(timeout=5)
                reason = "stop"
                if payload.get("tools") and isinstance(
                    payload.get("tool_choice"), dict
                ):
                    ref = context["candidates"][0]["evidence"][0]
                    message = {
                        "tool_calls": [
                            {
                                "id": "read-1",
                                "type": "function",
                                "function": {
                                    "name": "read_context",
                                    "arguments": json.dumps(
                                        {"source_id": ref["source_id"], "radius": 1}
                                    ),
                                },
                            }
                        ]
                    }
                    reason = "tool_calls"
                else:
                    if title == "CheckPlan":
                        data = empty_plan()
                        if behavior["truncate_plan"]:
                            behavior["truncate_plan"] = False
                            reason = "length"
                    elif title == "Discovery":
                        sources = context["sources"]
                        groups = (
                            [[source] for source in sources]
                            if behavior["per_source_candidates"]
                            else [sources]
                        )
                        data = discovery(
                            context,
                            candidates=[
                                candidate(
                                    [
                                        {"source_id": s["id"], "quote": s["text"]}
                                        for s in group
                                    ]
                                )
                                for group in groups
                            ],
                        )
                        if behavior["truncate_discovery"] and len(sources) > 1:
                            reason = "length"
                        if behavior["omit_last_source"] and len(sources) > 1:
                            data["covered_source_ids"].pop()
                    elif context.get("candidates"):
                        data = {
                            "actions": [],
                            "decisions": [
                                decision(c["id"], c["evidence"])
                                for c in context["candidates"]
                            ],
                        }
                        if (
                            behavior["truncate_review"]
                            and len(context["candidates"]) > 1
                        ):
                            reason = "length"
                    else:
                        data = {"summary": "未发现问题", "items": []}
                    message = {"content": json.dumps(data, ensure_ascii=False)}
                    if reason == "length":
                        message["content"] = '{"requirements":['
                frames = [
                    {"choices": [{"index": 0, "delta": message}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                    },
                ]
                if behavior["long_thinking"]:
                    behavior["long_thinking"] = False
                    frames.insert(
                        0,
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"reasoning_content": "思考" * 70000},
                                }
                            ]
                        },
                    )
                raw = (
                    "\n".join(
                        "data: " + json.dumps(frame, ensure_ascii=False)
                        for frame in frames
                    )
                    + "\ndata: [DONE]\n\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = {
            "api_base": f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions",
            "model_name": "test",
            "api_key": None,
            "max_input_chars": 80000,
            "request_timeout": 10,
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_native_tool_roundtrip_and_validated_report_over_real_http(self):
        index = DocumentIndex(
            [
                {
                    "filename": "a.txt",
                    "text": "设备A 电压220 V\n设备A 电压110 V",
                    "text_complete": True,
                }
            ]
        )
        agent = EvidenceAgent(
            index=index,
            item={
                "code": "custom",
                "name": "参数检查",
                "prompt": "核对同一设备的电压。",
            },
            task_type="document_check",
            connection=self.connection,
            options=AgentOptions(protocol="native", input_price=1.0, output_price=2.0),
            store=MemoryCheckpointStore(),
            ensure_active=lambda: None,
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["report"]["items"]), 1)
        self.assertTrue(result["metadata"]["capabilities"]["native_tools"])
        self.assertTrue(
            any(message["role"] == "tool" for message in self.payloads[-1]["messages"])
        )
        self.assertEqual(result["metadata"]["model_calls"], 4)
        self.assertTrue(result["metadata"]["usage_complete"])
        self.assertAlmostEqual(result["metadata"]["cost"], 0.00056)

    def test_baseline_evaluation_records_real_api_usage(self):
        case = {
            "id": "baseline",
            "documents": [{"filename": "a.txt", "text": "正常内容"}],
            "check": {"code": "custom", "name": "检查", "prompt": "检查内容。"},
        }
        result = run_case(case, "standard", self.connection, Path.cwd())
        self.assertEqual(result["error"], "")
        self.assertTrue(result["metadata"]["usage_complete"])
        self.assertEqual(result["metadata"]["known_input_tokens"], 100)
        self.assertEqual(result["metadata"]["known_output_tokens"], 20)

    def test_parallel_discovery_over_http_keeps_coverage_report_and_usage(self):
        self.behavior["discovery_barrier"] = threading.Barrier(3)
        self.behavior["per_source_candidates"] = True
        agent = EvidenceAgent(
            index=DocumentIndex(
                [
                    {
                        "filename": "a.txt",
                        "text": "\n".join(
                            f"设备{n}：" + "参数正常。" * 120 for n in range(3)
                        ),
                        "text_complete": True,
                    }
                ]
            ),
            item={"code": "custom", "name": "检查", "prompt": "核实参数。"},
            task_type="document_check",
            connection=self.connection,
            options=AgentOptions(protocol="structured", chunk_chars=1000),
            store=MemoryCheckpointStore(),
            ensure_active=lambda: None,
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertTrue(result["metadata"]["coverage"]["all_checked"])
        self.assertEqual(len(result["report"]["items"]), 3)
        self.assertEqual(result["metadata"]["model_calls"], 5)
        self.assertEqual(result["metadata"]["known_input_tokens"], 500)
        self.assertEqual(result["metadata"]["known_output_tokens"], 100)
        self.assertTrue(result["metadata"]["usage_complete"])

    def test_plan_length_retry_over_http_preserves_usage_and_output_cap(self):
        self.behavior["truncate_plan"] = True
        options = AgentOptions.model_validate(
            {"protocol": "structured", "max_output_tokens": 2048, "max_tokens": 10000}
        )
        agent = EvidenceAgent(
            index=DocumentIndex(
                [
                    {
                        "filename": "a.txt",
                        "text": "设备A 电压220 V\n设备A 电压110 V",
                        "text_complete": True,
                    }
                ]
            ),
            item={
                "code": "custom",
                "name": "参数检查",
                "prompt": "核对同一设备的电压。",
            },
            task_type="document_check",
            connection=self.connection,
            options=options,
            store=MemoryCheckpointStore(),
            ensure_active=lambda: None,
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["report"]["items"]), 1)
        self.assertEqual(result["metadata"]["model_calls"], 4)
        self.assertTrue(result["metadata"]["usage_complete"])
        self.assertEqual(result["metadata"]["known_output_tokens"], 80)
        self.assertIn("length", result["metadata"]["requests"][0]["error"])
        for payload in self.payloads:
            self.assertNotIn("max_completion_tokens", payload)
            self.assertNotIn("max_tokens", payload)

    def test_long_thinking_over_http_completes_and_records_actual_usage(self):
        self.behavior["long_thinking"] = True
        agent = EvidenceAgent(
            index=DocumentIndex(
                [{"filename": "a.txt", "text": "设备A电压220V", "text_complete": True}]
            ),
            item={"code": "custom", "name": "检查", "prompt": "核实参数。"},
            task_type="document_check",
            connection=self.connection,
            options=AgentOptions(protocol="structured"),
            store=MemoryCheckpointStore(),
            ensure_active=lambda: None,
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["report"]["items"]), 1)
        self.assertTrue(result["metadata"]["usage_complete"])
        self.assertEqual(result["metadata"]["model_calls"], 3)
        self.assertEqual(result["metadata"]["known_output_tokens"], 60)

    def test_missing_last_source_recovers_over_http_and_preserves_usage(self):
        self.behavior["omit_last_source"] = True
        self.behavior["per_source_candidates"] = True
        agent = EvidenceAgent(
            index=DocumentIndex(
                [
                    {
                        "filename": "a.txt",
                        "text": "电压220V\n电压110V",
                        "text_complete": True,
                    }
                ]
            ),
            item={"code": "custom", "name": "检查", "prompt": "核实参数。"},
            task_type="document_check",
            connection=self.connection,
            options=AgentOptions(protocol="structured"),
            store=MemoryCheckpointStore(),
            ensure_active=lambda: None,
        )
        result = agent.run()
        self.assertEqual(result["error"], "")
        self.assertEqual(len(result["report"]["items"]), 2)
        self.assertTrue(result["metadata"]["coverage"]["all_checked"])
        self.assertEqual(result["metadata"]["model_calls"], 4)
        self.assertEqual(result["metadata"]["known_output_tokens"], 80)
        self.assertTrue(result["metadata"]["usage_complete"])
        batches = [
            input_context(p["messages"])["sources"]
            for p in self.payloads
            if p.get("response_format", {})
            .get("json_schema", {})
            .get("schema", {})
            .get("title")
            == "Discovery"
        ]
        self.assertEqual([len(batch) for batch in batches], [2, 1])
        self.assertEqual(batches[1][0]["id"], batches[0][-1]["id"])

    def test_discovery_and_review_length_recovery_over_http_preserves_usage(self):
        for stage in ("discovery", "review"):
            with self.subTest(stage=stage):
                self.behavior["truncate_discovery"] = stage == "discovery"
                self.behavior["truncate_review"] = stage == "review"
                self.behavior["per_source_candidates"] = stage == "review"
                agent = EvidenceAgent(
                    index=DocumentIndex(
                        [
                            {
                                "filename": "a.txt",
                                "text": "电压220V\n电压110V",
                                "text_complete": True,
                            }
                        ]
                    ),
                    item={"code": "custom", "name": "检查", "prompt": "核实参数。"},
                    task_type="document_check",
                    connection=self.connection,
                    options=AgentOptions(protocol="structured"),
                    store=MemoryCheckpointStore(),
                    ensure_active=lambda: None,
                )
                result = agent.run()
                self.assertEqual(result["error"], "")
                self.assertTrue(result["metadata"]["coverage"]["all_checked"])
                self.assertEqual(len(result["report"]["items"]), 2)
                self.assertEqual(result["metadata"]["model_calls"], 5)
                self.assertEqual(result["metadata"]["known_output_tokens"], 100)
                self.assertTrue(result["metadata"]["usage_complete"])
                self.assertEqual(
                    sum(
                        "length" in request["error"]
                        for request in result["metadata"]["requests"]
                    ),
                    1,
                )
                for payload in self.payloads:
                    self.assertNotIn("max_completion_tokens", payload)
                    self.assertNotIn("max_tokens", payload)


if __name__ == "__main__":
    unittest.main()
