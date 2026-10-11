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
        self.behavior = {"truncate_plan": False}
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
                        data = discovery(
                            context,
                            candidates=[
                                candidate(
                                    [
                                        {"source_id": s["id"], "quote": s["text"]}
                                        for s in context["sources"]
                                    ]
                                )
                            ],
                        )
                    elif context.get("candidates"):
                        c = context["candidates"][0]
                        data = {
                            "actions": [],
                            "decisions": [decision(c["id"], c["evidence"])],
                        }
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

    def test_plan_length_retry_over_http_preserves_usage_and_output_cap(self):
        self.behavior["truncate_plan"] = True
        options = AgentOptions(protocol="structured", max_output_tokens=2048)
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
        self.assertTrue(all(p["max_completion_tokens"] == 2048 for p in self.payloads))


if __name__ == "__main__":
    unittest.main()
