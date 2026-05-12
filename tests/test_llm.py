import json
import unittest
from unittest.mock import patch

from app import llm


class FakeResponse:
    def __init__(self, *, lines=None, data=None, status_code=200, text=None):
        self._lines = lines or []
        self._data = data
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(data or {}, ensure_ascii=False)
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)

    def json(self):
        if self._data is None:
            raise json.JSONDecodeError("empty", self.text, 0)
        return self._data

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def post(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        return self.responses.pop(0)


class LLMResponseParsingTest(unittest.TestCase):
    def test_reads_stream_chat_completion_content(self):
        response = FakeResponse(
            lines=[
                'data: {"choices":[{"delta":{"role":"assistant"}}]}',
                'data: {"choices":[{"delta":{"content":"检查"}}]}',
                'data: {"choices":[{"delta":{"content":"完成"}}]}',
                "data: [DONE]",
            ]
        )

        result = llm._read_stream_response(response, None)

        self.assertEqual(result, "检查完成")

    def test_does_not_treat_responses_api_events_as_chat_completion_content(self):
        response = FakeResponse(
            lines=[
                'data: {"type":"response.output_text.delta","delta":"检查"}',
                'data: {"type":"response.output_text.delta","delta":"完成"}',
                "data: [DONE]",
            ]
        )

        with self.assertRaisesRegex(llm.LLMError, "OpenAI Chat Completions"):
            llm._read_stream_response(response, None)

    def test_reports_reasoning_without_content(self):
        response = FakeResponse(
            lines=[
                'data: {"choices":[{"delta":{"reasoning_content":"分析中"}}]}',
                'data: {"choices":[{"finish_reason":"stop","delta":{}}]}',
                "data: [DONE]",
            ]
        )

        with self.assertRaisesRegex(llm.LLMError, "reasoning_content"):
            llm._read_stream_response(response, None)

    def test_raises_service_error_from_200_json(self):
        response = FakeResponse(
            lines=[
                'data: {"error":{"message":"model not found"}}',
            ]
        )

        with self.assertRaisesRegex(llm.LLMError, "model not found"):
            llm._read_stream_response(response, None)

    def test_falls_back_to_non_stream_when_stream_has_no_content(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
                        "data: [DONE]",
                    ]
                ),
                FakeResponse(
                    data={
                        "choices": [
                            {"message": {"role": "assistant", "content": "非流式结果"}}
                        ]
                    }
                ),
            ]
        )
        chunks = []

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_check(
                api_base="http://example.test/v1",
                api_key="key",
                model_name="test-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
                on_delta=chunks.append,
            )

        self.assertEqual(result, "非流式结果")
        self.assertEqual(chunks, ["非流式结果"])
        self.assertEqual(len(fake_session.calls), 2)
        self.assertTrue(fake_session.calls[0][1]["json"]["stream"])
        self.assertEqual(fake_session.calls[0][1]["json"]["stream_options"], {"include_usage": True})
        self.assertFalse(fake_session.calls[1][1]["json"]["stream"])
        self.assertNotIn("stream_options", fake_session.calls[1][1]["json"])
        self.assertTrue(fake_session.calls[0][1]["stream"])
        self.assertFalse(fake_session.calls[1][1]["stream"])


if __name__ == "__main__":
    unittest.main()
