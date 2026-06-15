import json
import unittest
from unittest.mock import call, patch

from app import llm


class FakeResponse:
    def __init__(self, *, lines=None, data=None, status_code=200, text=None, headers=None):
        self._lines = lines or []
        self._data = data
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(data or {}, ensure_ascii=False)
        self.headers = headers or {"content-type": "text/event-stream"}
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
    def test_requires_full_chat_completions_endpoint(self):
        with self.assertRaisesRegex(llm.LLMError, "chat/completions"):
            llm._chat_completions_endpoint("http://example.test/v1")
        with self.assertRaisesRegex(llm.LLMError, "chat/completions"):
            llm._chat_completions_endpoint("example.test/v1/chat/completions")

    def test_accepts_full_chat_completions_endpoint(self):
        self.assertEqual(
            llm._chat_completions_endpoint("http://example.test/v1/chat/completions/"),
            "http://example.test/v1/chat/completions",
        )

    def test_document_check_prompt_warns_about_extracted_line_break_spaces(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"完成"}}]}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            llm.run_check(
                api_base="http://example.test/v1/chat/completions",
                api_key="key",
                model_name="test-model",
                check_name="规范性",
                prompt="检查多余空格",
                document_text="第一行\n第二行",
            )

        user_content = fake_session.calls[0][1]["json"]["messages"][1]["content"]
        self.assertIn("解析换行/分页造成的空白", user_content)
        self.assertIn("不要把解析换行/分页造成的空白判为“多余空格”", user_content)

    def test_document_check_prompt_uses_configured_issue_output_limit(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"完成"}}]}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            llm.run_check(
                api_base="http://example.test/v1/chat/completions",
                api_key="key",
                model_name="test-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
                issue_output_limit=50,
            )

        user_content = fake_session.calls[0][1]["json"]["messages"][1]["content"]
        self.assertIn("单次回复最多列出 50 条问题", user_content)
        self.assertIn("如果超过 50 条", user_content)

    def test_document_check_prompt_can_disable_issue_output_limit(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"完成"}}]}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            llm.run_check(
                api_base="http://example.test/v1/chat/completions",
                api_key="key",
                model_name="test-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
                issue_output_limit=0,
            )

        user_content = fake_session.calls[0][1]["json"]["messages"][1]["content"]
        self.assertIn("单次回复不限制问题条数", user_content)
        self.assertNotIn("最多列出 20 条问题", user_content)

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

    def test_raises_service_error_from_success_false_json(self):
        response = FakeResponse(
            lines=[
                'data: {"code": 401, "success": false, "errorCode": 201001, "data": null, "message": "调用模型服务失败：模型调用超时，请稍后再试"}',
            ]
        )

        with self.assertRaisesRegex(llm.LLMError, "模型调用超时"):
            llm._read_stream_response(response, None)

    def test_reports_provider_capacity_error_from_http_error_body(self):
        body = {
            "error": {
                "message": (
                    "<503> InternalError.Algo.ModelServingError.ServiceUnavailable: "
                    "Too many requests. Your requests are being throttled due to system capacity limits."
                )
            }
        }
        response = FakeResponse(status_code=500, text=json.dumps(body, ensure_ascii=False))

        with self.assertRaises(llm.LLMError) as context:
            llm._read_stream_response(response, None)
        message = str(context.exception)
        self.assertIn("模型服务繁忙或触发限流", message)
        self.assertIn("降低系统同时执行任务数", message)

    def test_preserves_general_http_error_message(self):
        response = FakeResponse(status_code=400, text='{"error":{"message":"invalid model"}}')

        with self.assertRaisesRegex(llm.LLMError, "模型服务返回 400：invalid model"):
            llm._read_stream_response(response, None)

    def test_retries_stream_when_stream_has_no_content(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
                        "data: [DONE]",
                    ]
                ),
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"流式结果"}}]}',
                        "data: [DONE]",
                    ]
                ),
            ]
        )
        chunks = []

        with (
            patch.object(llm.requests, "Session", return_value=fake_session),
            patch.object(llm.time, "sleep") as sleep,
        ):
            result = llm.run_check(
                api_base="http://example.test/v1/chat/completions",
                api_key="key",
                model_name="test-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
                on_delta=chunks.append,
            )

        self.assertEqual(result, "流式结果")
        self.assertEqual(chunks, ["流式结果"])
        self.assertEqual(len(fake_session.calls), 2)
        self.assertTrue(fake_session.calls[0][1]["json"]["stream"])
        self.assertEqual(fake_session.calls[0][1]["json"]["stream_options"], {"include_usage": True})
        self.assertTrue(fake_session.calls[1][1]["json"]["stream"])
        self.assertEqual(fake_session.calls[1][1]["json"]["stream_options"], {"include_usage": True})
        self.assertTrue(fake_session.calls[0][1]["stream"])
        self.assertTrue(fake_session.calls[1][1]["stream"])
        self.assertFalse(fake_session.calls[0][1]["verify"])
        self.assertFalse(fake_session.calls[1][1]["verify"])
        sleep.assert_called_once_with(1)

    def test_retries_stream_when_stream_frame_is_malformed(self):
        fake_session = FakeSession(
            [
                FakeResponse(lines=['data: {"choices":[{"delta":{"reasoning":"分析中"}']),
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"流式结果"}}]}',
                        "data: [DONE]",
                    ]
                ),
            ]
        )

        with (
            patch.object(llm.requests, "Session", return_value=fake_session),
            patch.object(llm.time, "sleep"),
        ):
            result = llm.run_check(
                api_base="http://example.test/v1/chat/completions",
                api_key="key",
                model_name="test-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, "流式结果")
        self.assertEqual(len(fake_session.calls), 2)
        self.assertTrue(fake_session.calls[0][1]["json"]["stream"])
        self.assertTrue(fake_session.calls[1][1]["json"]["stream"])

    def test_reports_glm_reasoning_field_without_content(self):
        response = FakeResponse(
            lines=[
                'data: {"choices":[{"delta":{"reasoning":"分析中"}}]}',
                'data: {"choices":[{"finish_reason":"stop","delta":{}}]}',
                "data: [DONE]",
            ]
        )

        with self.assertRaisesRegex(llm.LLMError, "reasoning"):
            llm._read_stream_response(response, None)

    def test_reads_plain_stream_chunks_without_data_prefix(self):
        response = FakeResponse(
            lines=(
                '{"object":"chat.completion.chunk","choices":[{"delta":{"content":"检查"}}]}\n'
                '{"object":"chat.completion.chunk","choices":[{"delta":{"content":"完成"}}]}\n'
                "data: [DONE]\n"
            ).splitlines()
        )

        result = llm._read_stream_response(response, None)

        self.assertEqual(result, "检查完成")

    def test_passes_ssl_verify_flag_to_requests(self):
        fake_session = FakeSession(
            [
                FakeResponse(lines=['data: {"choices":[{"delta":{"content":"校验开启"}}]}', "data: [DONE]"]),
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_check(
                api_base="https://example.test/v1/chat/completions",
                api_key="key",
                ssl_verify=True,
                model_name="test-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, "校验开启")
        self.assertEqual(len(fake_session.calls), 1)
        self.assertTrue(fake_session.calls[0][1]["verify"])

    def test_force_disable_thinking_adds_payload_flag(self):
        fake_session = FakeSession(
            [
                FakeResponse(lines=['data: {"choices":[{"delta":{"content":"完成"}}]}', "data: [DONE]"]),
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_check(
                api_base="https://example.test/v1/chat/completions",
                api_key="key",
                model_name="test-model",
                force_disable_thinking=True,
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, "完成")
        payload = fake_session.calls[0][1]["json"]
        self.assertIs(payload["enable_thinking"], False)
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})

    def test_run_image_check_sends_multimodal_chat_content(self):
        fake_session = FakeSession(
            [
                FakeResponse(lines=['data: {"choices":[{"delta":{"content":"图片检查完成"}}]}', "data: [DONE]"]),
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_image_check(
                api_base="https://example.test/v1/chat/completions",
                api_key="key",
                model_name="qwen-vl",
                check_name="图片语种匹配检查",
                prompt="检查图片文字语种是否和文档一致",
                image_name="0001_page001-image001.png",
                image_position="page001-image001",
                image_data_url="data:image/png;base64,AAAA",
                issue_output_limit=12,
            )

        self.assertEqual(result, "图片检查完成")
        payload = fake_session.calls[0][1]["json"]
        content = payload["messages"][1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("图片语种匹配检查", content[0]["text"])
        self.assertIn("单张图片回复最多列出 12 条问题", content[0]["text"])
        self.assertEqual(content[1], {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}})

    def test_run_multimodal_document_check_sends_text_and_multiple_images(self):
        fake_session = FakeSession(
            [
                FakeResponse(lines=['data: {"choices":[{"delta":{"content":"图文检查完成"}}]}', "data: [DONE]"]),
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_multimodal_document_check(
                api_base="https://example.test/v1/chat/completions",
                api_key="key",
                model_name="qwen-vl",
                check_name="图文对应检查",
                prompt="检查图文对应",
                document_text="file: 图纸.pdf\n\n正文提到图 1 是电源接线图",
                image_items=[
                    {
                        "index": 1,
                        "name": "0001_page001-image001.png",
                        "position": "page001-image001",
                        "mime_type": "image/png",
                        "data_url": "data:image/png;base64,AAAA",
                    },
                    {
                        "index": 2,
                        "name": "0002_page002-image001.jpg",
                        "position": "page002-image001",
                        "mime_type": "image/jpeg",
                        "data_url": "data:image/jpeg;base64,BBBB",
                    },
                ],
                batch_index=1,
                batch_count=2,
                issue_output_limit=33,
            )

        self.assertEqual(result, "图文检查完成")
        payload = fake_session.calls[0][1]["json"]
        content = payload["messages"][1]["content"]
        self.assertEqual([item["type"] for item in content], ["text", "text", "image_url", "text", "image_url"])
        self.assertIn("图文对应检查", content[0]["text"])
        self.assertIn("当前图片批次：1/2", content[0]["text"])
        self.assertIn("单次回复最多列出 33 条问题", content[0]["text"])
        self.assertIn("正文提到图 1 是电源接线图", content[0]["text"])
        self.assertIn("0001_page001-image001.png", content[1]["text"])
        self.assertEqual(content[2]["image_url"]["url"], "data:image/png;base64,AAAA")
        self.assertEqual(content[4]["image_url"]["url"], "data:image/jpeg;base64,BBBB")

    def test_retries_llm_errors_twice_before_success(self):
        fake_session = FakeSession(
            [
                FakeResponse(lines=['data: {"error":{"message":"temporary"}}']),
                FakeResponse(lines=['data: {"error":{"message":"temporary again"}}']),
                FakeResponse(lines=['data: {"choices":[{"delta":{"content":"重试成功"}}]}', "data: [DONE]"]),
            ]
        )
        chunks = []

        with (
            patch.object(llm.requests, "Session", return_value=fake_session),
            patch.object(llm.time, "sleep") as sleep,
        ):
            result = llm.run_check(
                api_base="http://example.test/v1/chat/completions",
                api_key="key",
                model_name="test-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
                on_delta=chunks.append,
            )

        self.assertEqual(result, "重试成功")
        self.assertEqual(chunks, ["重试成功"])
        self.assertEqual(len(fake_session.calls), 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])

    def test_stream_trace_logs_request_and_chunks(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        'data: {"object":"chat.completion.chunk","choices":[{"delta":{"role":"assistant","content":""}}]}',
                        'data: {"object":"chat.completion.chunk","choices":[{"delta":{"reasoning":"分析"}}]}',
                        'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"结果"}}]}',
                        "data: [DONE]",
                    ]
                ),
            ]
        )

        with (
            patch.object(llm.requests, "Session", return_value=fake_session),
            self.assertLogs("app.llm", level="INFO") as logs,
        ):
            result = llm.run_check(
                api_base="http://example.test/v1/chat/completions",
                api_key="key",
                model_name="test-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
                stream_trace_enabled=True,
            )

        joined_logs = "\n".join(logs.output)
        self.assertEqual(result, "结果")
        self.assertIn("LLM 流式定位请求发送", joined_logs)
        self.assertIn("LLM 流式定位响应建立", joined_logs)
        self.assertIn("LLM 流式定位开始读取", joined_logs)
        self.assertIn("LLM 流式定位收到响应chunk", joined_logs)
        self.assertIn("reasoning_delta_chars=2", joined_logs)
        self.assertIn("content_delta_chars=2", joined_logs)
        self.assertIn("LLM 流式定位收到结束标记", joined_logs)

    def test_reports_stream_content_snapshots_and_clears_failed_attempt(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"失败前片段"}}]}',
                        'data: {"error":{"message":"temporary"}}',
                    ]
                ),
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"重试"}}]}',
                        'data: {"choices":[{"delta":{"content":"成功"}}]}',
                        "data: [DONE]",
                    ]
                ),
            ]
        )
        snapshots = []

        with (
            patch.object(llm.requests, "Session", return_value=fake_session),
            patch.object(llm.time, "sleep") as sleep,
        ):
            result = llm.run_check(
                api_base="http://example.test/v1/chat/completions",
                api_key="key",
                model_name="test-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
                on_content=snapshots.append,
            )

        self.assertEqual(result, "重试成功")
        self.assertEqual(snapshots, ["失败前片段", "", "重试", "重试成功"])
        self.assertEqual(len(fake_session.calls), 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
