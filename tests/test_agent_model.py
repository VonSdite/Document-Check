import json
import unittest
from unittest.mock import patch

from app.models.agent import AgentModel, IncompleteResponse, ToolsUnsupported
from tests.test_llm import FakeResponse, FakeSession


class AgentModelTest(unittest.TestCase):
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
