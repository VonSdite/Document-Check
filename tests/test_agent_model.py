import json
import unittest
from unittest.mock import patch

from app.models.agent import AgentModel, IncompleteResponse, ToolsUnsupported
from app.models.client import LLMError
from tests.test_llm import FakeResponse, FakeSession


class AgentModelTest(unittest.TestCase):
    def test_stream_activity_reports_thinking_and_output_only_on_phase_changes(self):
        model = self.make_model()
        phases = []
        model.on_activity = lambda phase, attempt: phases.append((phase, attempt))
        model._collect(
            [
                {"choices": [{"delta": {"reasoning_content": "核实原文"}}]},
                {"choices": [{"delta": {"reasoning_content": "继续核实"}}]},
                {"choices": [{"delta": {"content": "{"}}]},
                {"choices": [{"delta": {"content": "}"}, "finish_reason": "stop"}]},
            ],
            "test-stream",
        )
        self.assertEqual(phases, [("thinking", None), ("output", None)])

    def make_model(self):
        self.reservations, self.completed = [], []
        return AgentModel(
            {
                "api_base": "http://model.test/v1/chat/completions",
                "api_key": None,
                "model_name": "test",
            },
            before_request=self.reservations.append,
            after_request=lambda *args: self.completed.append(args),
            ensure_active=lambda: None,
        )

    def test_stream_accumulates_fragmented_tool_arguments_and_usage(self):
        model = self.make_model()
        model.connection["max_output_tokens"] = 512
        model.capabilities["output_token_parameter"] = "max_tokens"
        frames = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {
                                        "name": "read_context",
                                        "arguments": '{"source_id":',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"a","radius":1}'},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 15, "completion_tokens": 6}},
        ]
        session = FakeSession(
            [
                FakeResponse(
                    lines=["data: " + json.dumps(f) for f in frames] + ["data: [DONE]"]
                )
            ]
        )
        with patch("app.models.agent._pooled_http_session", return_value=session):
            turn = model.turn([], schema=None, tools=[{"type": "function"}])
        call = turn.tool_calls[0]
        self.assertEqual(
            json.loads(call["function"]["arguments"]), {"source_id": "a", "radius": 1}
        )
        self.assertEqual(turn.usage["prompt_tokens"], 15)
        self.assertEqual(len(self.reservations), 1)
        payload = session.calls[0][1]["json"]
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertTrue(session.responses == [])

    def test_schema_unsupported_falls_back_without_losing_validation_contract(self):
        session = FakeSession(
            [
                FakeResponse(
                    status_code=400, text="response_format json_schema unsupported"
                ),
                FakeResponse(
                    data={
                        "choices": [
                            {
                                "message": {"content": '{"items":[]}'},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                    headers={"Content-Type": "application/json"},
                ),
            ]
        )
        model = self.make_model()
        with patch("app.models.agent._pooled_http_session", return_value=session):
            turn = model.turn([], schema={"type": "object"})
        self.assertEqual(json.loads(turn.content), {"items": []})
        self.assertEqual(model.capabilities["schema"], "json_object")
        self.assertEqual(len(self.reservations), 2)

    def test_native_tools_rejection_is_explicit(self):
        model = self.make_model()
        session = FakeSession(
            [FakeResponse(status_code=400, text="tools not supported")]
        )
        with (
            patch("app.models.agent._pooled_http_session", return_value=session),
            self.assertRaises(ToolsUnsupported),
        ):
            model.turn([], schema=None, tools=[{"type": "function"}])
        self.assertFalse(model.capabilities["native_tools"])

    def test_tool_history_rejection_is_explicit_without_tools_in_final_call(self):
        model = self.make_model()
        session = FakeSession(
            [FakeResponse(status_code=400, text="role 'tool' not allowed")]
        )
        with (
            patch("app.models.agent._pooled_http_session", return_value=session),
            self.assertRaises(ToolsUnsupported),
        ):
            model.turn(
                [{"role": "tool", "tool_call_id": "read", "content": "{}"}],
                schema={"type": "object"},
            )
        self.assertFalse(model.capabilities["native_tools"])

    def test_optional_parameter_fallbacks_are_cached_for_following_calls(self):
        success = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        for parameter, message in (
            (
                "temperature",
                "temperature does not support 0; only default is supported",
            ),
            ("stream_options", "stream_options not supported"),
        ):
            with self.subTest(parameter=parameter):
                model = self.make_model()
                session = FakeSession(
                    [
                        FakeResponse(status_code=400, text=message),
                        FakeResponse(
                            data=success, headers={"Content-Type": "application/json"}
                        ),
                        FakeResponse(
                            data=success, headers={"Content-Type": "application/json"}
                        ),
                    ]
                )
                with patch(
                    "app.models.agent._pooled_http_session", return_value=session
                ):
                    model.turn([], schema=None)
                    model.turn([], schema=None)
                self.assertEqual(len(self.reservations), 3)
                self.assertIn(parameter, session.calls[0][1]["json"])
                self.assertNotIn(parameter, session.calls[-1][1]["json"])
                for _, kwargs in session.calls:
                    self.assertNotIn("max_completion_tokens", kwargs["json"])
                    self.assertNotIn("max_tokens", kwargs["json"])

    def test_truncated_response_preserves_actual_usage_and_error(self):
        model = self.make_model()
        session = FakeSession(
            [
                FakeResponse(
                    data={
                        "choices": [
                            {
                                "message": {"content": "{unfinished"},
                                "finish_reason": "length",
                            }
                        ],
                        "usage": {"prompt_tokens": 15, "completion_tokens": 20},
                    },
                    headers={"Content-Type": "application/json"},
                )
            ]
        )
        with (
            patch("app.models.agent._pooled_http_session", return_value=session),
            self.assertRaises(IncompleteResponse) as raised,
        ):
            model.turn([], schema=None)
        self.assertEqual(len(self.completed), 1)
        self.assertEqual(self.completed[0][1].usage["completion_tokens"], 20)
        self.assertIn("length", self.completed[0][3])
        self.assertEqual(raised.exception.finish_reason, "length")
        self.assertIn("上游默认", str(raised.exception))
        self.assertIn("截断", str(raised.exception))

    def test_long_thinking_and_content_stream_complete_with_actual_usage(self):
        model = self.make_model()
        events = []
        model.on_output = events.append
        content = "正文" * 530000
        frames = [
            {"choices": [{"delta": {"reasoning_content": "思考" * 35000}}]},
            {"choices": [{"delta": {"reasoning_content": "继续" * 35000}}]},
            *[
                {"choices": [{"delta": {"content": content[start : start + 65536]}}]}
                for start in range(0, len(content), 65536)
            ],
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {"prompt_tokens": 200, "completion_tokens": 50000},
            },
        ]
        session = FakeSession(
            [
                FakeResponse(
                    lines=["data: " + json.dumps(frame) for frame in frames]
                    + ["data: [DONE]"]
                )
            ]
        )
        with patch("app.models.agent._pooled_http_session", return_value=session):
            turn = model.turn([], schema=None)
        self.assertEqual(turn.content, content)
        self.assertEqual(turn.finish_reason, "stop")
        self.assertEqual(turn.usage["completion_tokens"], 50000)
        self.assertIsNone(self.completed[0][3])
        self.assertEqual(
            sum(len(event["text"]) for event in events if event["kind"] == "thinking"),
            140000,
        )
        payload = session.calls[0][1]["json"]
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("max_completion_tokens", payload)

    def test_interrupted_tool_metadata_preserves_finish_reason_and_usage(self):
        for reason in ("length", "content_filter", "tool_calls"):
            with self.subTest(reason=reason):
                model = self.make_model()
                frames = [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": '{"source_id":'},
                                        }
                                    ]
                                },
                                "finish_reason": reason,
                            }
                        ]
                    },
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 15, "completion_tokens": 20},
                    },
                ]
                session = FakeSession(
                    [
                        FakeResponse(
                            lines=["data: " + json.dumps(frame) for frame in frames]
                            + ["data: [DONE]"]
                        )
                    ]
                )
                error_type = LLMError if reason == "tool_calls" else IncompleteResponse
                with (
                    patch(
                        "app.models.agent._pooled_http_session", return_value=session
                    ),
                    self.assertRaises(error_type) as raised,
                ):
                    model.turn([], schema=None, tools=[{"type": "function"}])
                self.assertEqual(len(self.completed), 1)
                if reason == "tool_calls":
                    self.assertIn("缺少标识或函数名", str(raised.exception))
                else:
                    self.assertEqual(raised.exception.finish_reason, reason)
                    self.assertEqual(
                        self.completed[0][1].usage["completion_tokens"], 20
                    )

    def test_refusal_truncation_and_unfinished_stream_do_not_become_empty_success(self):
        for frames in (
            [
                {
                    "choices": [
                        {"message": {"refusal": "refused"}, "finish_reason": "stop"}
                    ]
                }
            ],
            [
                {
                    "choices": [
                        {"message": {"content": '{"items":'}, "finish_reason": "length"}
                    ]
                }
            ],
            [{"choices": [{"delta": {"content": "{}"}}]}],
        ):
            with self.subTest(frames=frames):
                model = self.make_model()
                session = FakeSession(
                    [
                        FakeResponse(
                            lines=["data: " + json.dumps(f) for f in frames]
                            + ["data: [DONE]"]
                        )
                    ]
                )
                with (
                    patch(
                        "app.models.agent._pooled_http_session", return_value=session
                    ),
                    self.assertRaises(IncompleteResponse),
                ):
                    model.turn([], schema=None)


if __name__ == "__main__":
    unittest.main()
