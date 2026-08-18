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
    def assert_all_thinking_disable_flags(self, payload):
        self.assertIs(payload["enable_thinking"], False)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(
            payload["chat_template_kwargs"],
            {
                "enable_thinking": False,
                "thinking": False,
            },
        )

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
        self.assertEqual(
            fake_session.calls[0][1]["json"]["max_completion_tokens"],
            llm._MAX_COMPLETION_TOKENS,
        )

    def test_document_check_prompt_clamps_issue_output_limit_to_hard_max(self):
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
        self.assertIn("单次回复最多列出 30 条问题", user_content)
        self.assertIn("如果超过 30 条", user_content)
        self.assertIn("只保留最有可能成立的问题", user_content)
        self.assertIn("只输出一个 JSON 对象", user_content)
        self.assertIn('"status":"issue|suggestion|non_issue"', user_content)
        self.assertIn('"severity":"critical|high|medium|low"', user_content)
        self.assertIn('"confidence":"high|medium|low"', user_content)
        self.assertIn("先按 status", user_content)
        self.assertIn("再按 confidence", user_content)
        self.assertIn("输出前合并重复问题", user_content)

    def test_document_check_prompt_restores_hard_limit_for_zero_value(self):
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
        self.assertIn("单次回复最多列出 30 条问题", user_content)
        self.assertNotIn("不限制问题条数", user_content)

    def test_deepseek_run_check_requests_json_object_response_format(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"{\\"summary\\":\\"完成\\",\\"items\\":[]}"}}]}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_check(
                api_base="https://llm.example.test/v1/chat/completions",
                api_key="key",
                model_name="deepseekv4flash",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, '{"summary":"完成","items":[]}')
        payload = fake_session.calls[0][1]["json"]
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_glm_run_check_requests_json_object_response_format(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"{\\"summary\\":\\"完成\\",\\"items\\":[]}"}}]}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_check(
                api_base="https://llm.example.test/v1/chat/completions",
                api_key="key",
                model_name="GLM-4-Flash",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, '{"summary":"完成","items":[]}')
        payload = fake_session.calls[0][1]["json"]
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_bigmodel_endpoint_requests_json_object_response_format(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"{\\"summary\\":\\"完成\\",\\"items\\":[]}"}}]}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_check(
                api_base="https://open.bigmodel.cn/api/paas/v4/chat/completions",
                api_key="key",
                model_name="company-default-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, '{"summary":"完成","items":[]}')
        payload = fake_session.calls[0][1]["json"]
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_json_object_response_format_falls_back_when_provider_rejects_it(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    status_code=400,
                    text='{"error":{"message":"response_format json_object is not supported"}}',
                ),
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"{\\"summary\\":\\"降级完成\\",\\"items\\":[]}"}}]}',
                        "data: [DONE]",
                    ]
                ),
            ]
        )

        with (
            patch.object(llm.requests, "Session", return_value=fake_session),
            patch.object(llm.time, "sleep") as sleep,
        ):
            result = llm.run_check(
                api_base="https://llm.example.test/v1/chat/completions",
                api_key="key",
                model_name="deepseek-v4-flash",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, '{"summary":"降级完成","items":[]}')
        self.assertEqual(len(fake_session.calls), 2)
        self.assertEqual(fake_session.calls[0][1]["json"]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", fake_session.calls[1][1]["json"])
        sleep.assert_not_called()

    def test_output_token_limit_falls_back_to_max_tokens_when_unsupported(self):
        fake_session = FakeSession(
            [
                FakeResponse(
                    status_code=400,
                    text='{"error":{"message":"max_completion_tokens is not supported"}}',
                ),
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"完成"}}]}',
                        "data: [DONE]",
                    ]
                ),
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_check(
                api_base="http://example.test/v1/chat/completions",
                api_key="key",
                model_name="test-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, "完成")
        first_payload = fake_session.calls[0][1]["json"]
        second_payload = fake_session.calls[1][1]["json"]
        self.assertEqual(first_payload["max_completion_tokens"], llm._MAX_COMPLETION_TOKENS)
        self.assertNotIn("max_completion_tokens", second_payload)
        self.assertEqual(second_payload["max_tokens"], llm._MAX_COMPLETION_TOKENS)

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

    def test_stops_reasoning_only_stream_when_budget_is_exceeded(self):
        consumed = []

        def lines():
            for line in (
                'data: {"choices":[{"delta":{"reasoning":"第一段"}}]}',
                'data: {"choices":[{"delta":{"reasoning":"第二段"}}]}',
                'data: {"choices":[{"delta":{"content":"不应读取"}}]}',
            ):
                consumed.append(line)
                yield line

        response = FakeResponse(lines=lines())
        with (
            patch.object(llm, "_REASONING_ONLY_CHUNK_LIMIT", 2),
            patch.object(llm, "_REASONING_ONLY_CHAR_LIMIT", 10_000),
            self.assertRaisesRegex(llm.LLMError, "已提前终止本次流式响应"),
        ):
            llm._read_stream_response(response, None)

        self.assertEqual(len(consumed), 2)

    def test_reasoning_budget_does_not_stop_stream_after_content_begins(self):
        response = FakeResponse(
            lines=[
                'data: {"choices":[{"delta":{"content":"正文"}}]}',
                'data: {"choices":[{"delta":{"reasoning":"后续分析"}}]}',
                "data: [DONE]",
            ]
        )

        with (
            patch.object(llm, "_REASONING_ONLY_CHUNK_LIMIT", 1),
            patch.object(llm, "_REASONING_ONLY_CHAR_LIMIT", 1),
        ):
            result = llm._read_stream_response(response, None)

        self.assertEqual(result, "正文")

    def test_disabled_thinking_uses_strict_reasoning_budget(self):
        consumed = []

        def lines():
            for line in (
                'data: {"choices":[{"delta":{"reasoning":"第一段"}}]}',
                'data: {"choices":[{"delta":{"reasoning":"第二段"}}]}',
                'data: {"choices":[{"delta":{"content":"不应读取"}}]}',
            ):
                consumed.append(line)
                yield line

        response = FakeResponse(lines=lines())
        with (
            patch.object(llm, "_DISABLED_THINKING_REASONING_ONLY_CHUNK_LIMIT", 2),
            patch.object(llm, "_DISABLED_THINKING_REASONING_ONLY_CHAR_LIMIT", 10_000),
            self.assertLogs("app.llm", level="WARNING") as logs,
            self.assertRaisesRegex(llm.LLMError, "忽略了关闭思考设置"),
        ):
            llm._read_stream_response(response, None, thinking_disabled=True)

        self.assertEqual(len(consumed), 2)
        joined_logs = "\n".join(logs.output)
        self.assertIn("thinking_disabled=True", joined_logs)
        self.assertIn("limit_chunks=2", joined_logs)

    def test_disabled_thinking_budget_does_not_stop_stream_after_content_begins(self):
        response = FakeResponse(
            lines=[
                'data: {"choices":[{"delta":{"content":"正文"}}]}',
                'data: {"choices":[{"delta":{"reasoning":"后续分析"}}]}',
                "data: [DONE]",
            ]
        )

        with (
            patch.object(llm, "_DISABLED_THINKING_REASONING_ONLY_CHUNK_LIMIT", 1),
            patch.object(llm, "_DISABLED_THINKING_REASONING_ONLY_CHAR_LIMIT", 1),
        ):
            result = llm._read_stream_response(response, None, thinking_disabled=True)

        self.assertEqual(result, "正文")

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

    def test_stops_repeated_stream_without_retry(self):
        repeated = "重复内容" * 20
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        f'data: {{"choices":[{{"delta":{{"content":"{repeated}"}}}}]}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            with self.assertRaisesRegex(llm.LLMError, "疑似重复输出"):
                llm.run_check(
                    api_base="http://example.test/v1/chat/completions",
                    api_key="key",
                    model_name="test-model",
                    check_name="规范性",
                    prompt="检查",
                    document_text="文档",
                )

        self.assertEqual(len(fake_session.calls), 1)

    def test_stops_stream_when_content_character_limit_is_exceeded(self):
        first = "a" * 12
        second = "b" * 12
        fake_session = FakeSession(
            [
                FakeResponse(
                    lines=[
                        f'data: {{"choices":[{{"delta":{{"content":"{first}"}}}}]}}',
                        f'data: {{"choices":[{{"delta":{{"content":"{second}"}}}}]}}',
                        "data: [DONE]",
                    ]
                )
            ]
        )

        with (
            patch.object(llm.requests, "Session", return_value=fake_session),
            patch.object(llm, "_MAX_STREAM_CONTENT_CHARS", 20),
        ):
            with self.assertRaisesRegex(llm.LLMError, "超过字符上限"):
                llm.run_check(
                    api_base="http://example.test/v1/chat/completions",
                    api_key="key",
                    model_name="test-model",
                    check_name="规范性",
                    prompt="检查",
                    document_text="文档",
                )

        self.assertEqual(len(fake_session.calls), 1)

    def test_deepseek_reasoning_only_retry_automatically_disables_thinking(self):
        first_response = FakeResponse(
            lines=[
                'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}',
                'data: {"choices":[{"delta":{"reasoning":"持续分析"}}]}',
            ]
        )
        fake_session = FakeSession(
            [
                first_response,
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"降级重试成功"}}]}',
                        "data: [DONE]",
                    ]
                ),
            ]
        )

        with (
            patch.object(llm.requests, "Session", return_value=fake_session),
            patch.object(llm.time, "sleep") as sleep,
            self.assertLogs("app.llm", level="WARNING") as logs,
        ):
            result = llm.run_check(
                api_base="https://llm.example.test/v1/chat/completions",
                api_key="key",
                model_name="DeepSeek-V4-Flash-H200",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, "降级重试成功")
        self.assertTrue(first_response.closed)
        self.assertEqual(len(fake_session.calls), 2)
        first_payload = fake_session.calls[0][1]["json"]
        second_payload = fake_session.calls[1][1]["json"]
        self.assertNotIn("enable_thinking", first_payload)
        self.assertNotIn("chat_template_kwargs", first_payload)
        self.assertNotIn("thinking", first_payload)
        self.assert_all_thinking_disable_flags(second_payload)
        self.assertIn("下一次重试自动关闭思考", "\n".join(logs.output))
        sleep.assert_called_once_with(1)

    def test_force_disabled_thinking_stops_reasoning_quickly_and_retries(self):
        consumed = []

        def first_lines():
            for line in (
                'data: {"choices":[{"delta":{"reasoning":"第一段"}}]}',
                'data: {"choices":[{"delta":{"reasoning":"第二段"}}]}',
                'data: {"choices":[{"delta":{"content":"不应读取"}}]}',
            ):
                consumed.append(line)
                yield line

        first_response = FakeResponse(lines=first_lines())
        second_response = FakeResponse(
            lines=[
                'data: {"choices":[{"delta":{"content":"重试成功"}}]}',
                "data: [DONE]",
            ]
        )
        fake_session = FakeSession([first_response, second_response])

        with (
            patch.object(llm.requests, "Session", return_value=fake_session),
            patch.object(llm, "_DISABLED_THINKING_REASONING_ONLY_CHUNK_LIMIT", 2),
            patch.object(llm, "_DISABLED_THINKING_REASONING_ONLY_CHAR_LIMIT", 10_000),
            patch.object(llm.time, "sleep") as sleep,
        ):
            result = llm.run_check(
                api_base="https://llm.example.test/v1/chat/completions",
                api_key="key",
                model_name="DeepSeek-V4-Flash-H200",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
                force_disable_thinking=True,
            )

        self.assertEqual(result, "重试成功")
        self.assertEqual(len(consumed), 2)
        self.assertTrue(first_response.closed)
        self.assertTrue(second_response.closed)
        self.assertEqual(len(fake_session.calls), 2)
        self.assert_all_thinking_disable_flags(fake_session.calls[0][1]["json"])
        self.assert_all_thinking_disable_flags(fake_session.calls[1][1]["json"])
        sleep.assert_called_once_with(1)

    def test_generic_reasoning_only_retry_does_not_add_deepseek_thinking_flags(self):
        fake_session = FakeSession(
            [
                FakeResponse(lines=['data: {"choices":[{"delta":{"reasoning":"分析"}}]}']),
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"重试成功"}}]}',
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
                api_base="https://llm.example.test/v1/chat/completions",
                api_key="key",
                model_name="generic-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, "重试成功")
        self.assertNotIn("enable_thinking", fake_session.calls[0][1]["json"])
        self.assertNotIn("enable_thinking", fake_session.calls[1][1]["json"])
        self.assertNotIn("chat_template_kwargs", fake_session.calls[0][1]["json"])
        self.assertNotIn("chat_template_kwargs", fake_session.calls[1][1]["json"])
        self.assertNotIn("thinking", fake_session.calls[0][1]["json"])
        self.assertNotIn("thinking", fake_session.calls[1][1]["json"])

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

    def test_force_disable_thinking_adds_all_fallback_flags_for_unknown_provider(self):
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
        self.assertEqual(
            payload["chat_template_kwargs"],
            {"enable_thinking": False, "thinking": False},
        )
        self.assertIs(payload["enable_thinking"], False)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["reasoning_effort"], "none")

    def test_force_disable_thinking_adds_all_payload_flags_for_deepseek(self):
        fake_session = FakeSession(
            [
                FakeResponse(lines=['data: {"choices":[{"delta":{"content":"完成"}}]}', "data: [DONE]"]),
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_check(
                api_base="https://api.deepseek.com/v1/chat/completions",
                api_key="key",
                model_name="deepseek-v4-flash",
                force_disable_thinking=True,
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, "完成")
        payload = fake_session.calls[0][1]["json"]
        self.assert_all_thinking_disable_flags(payload)

    def test_force_disable_thinking_classifies_models_but_adds_all_payload_flags(self):
        cases = (
            ("DeepSeek-V4-Flash-H200", "deepseek"),
            ("qwen3.5-plus", "qwen"),
            ("glm-4.7", "glm"),
            ("glm-4.5v", "glm"),
            ("kimi-k2.6", "kimi"),
            ("MiniMax-M3", "minimax"),
            ("gpt-5.6-sol", "openai"),
        )

        for model_name, adapter in cases:
            with self.subTest(model_name=model_name):
                api_base = "https://proxy.example.test/v1/chat/completions"
                self.assertEqual(
                    llm._thinking_payload_adapter(api_base, model_name),
                    adapter,
                )
                payload = {}

                llm._disable_thinking_in_payload(
                    payload,
                    api_base=api_base,
                    model_name=model_name,
                )

                self.assert_all_thinking_disable_flags(payload)

    def test_force_disable_thinking_uses_all_fallback_flags_without_confirmed_official_disable(self):
        cases = (
            "deepseek-reasoner",
            "DeepSeek-R1-0528",
            "qwq-plus",
            "qvq-max",
            "qwen3.7-max-preview",
            "glm-4.1v-thinking-flash",
            "kimi-k3",
            "kimi-k2.7-code-highspeed",
            "MiniMax-M2.7",
            "o3",
            "gpt-5.5",
        )

        for model_name in cases:
            with self.subTest(model_name=model_name):
                payload = {}
                llm._disable_thinking_in_payload(
                    payload,
                    api_base="https://proxy.example.test/v1/chat/completions",
                    model_name=model_name,
                )
                self.assertEqual(
                    payload,
                    {
                        "enable_thinking": False,
                        "thinking": {"type": "disabled"},
                        "reasoning_effort": "none",
                        "chat_template_kwargs": {
                            "enable_thinking": False,
                            "thinking": False,
                        },
                    },
                )

    def test_force_disable_thinking_uses_all_fallback_flags_for_other_known_models(self):
        model_names = (
            "deepseek-chat",
            "qwen2.5-72b-instruct",
            "qwen3-235b-a22b-instruct-2507",
            "glm-4-flash",
            "moonshot-v1-128k",
            "MiniMax-Text-01",
            "gpt-4.1",
        )

        for model_name in model_names:
            with self.subTest(model_name=model_name):
                payload = {"keep": True}

                llm._disable_thinking_in_payload(
                    payload,
                    api_base="https://proxy.example.test/v1/chat/completions",
                    model_name=model_name,
                )

                self.assertEqual(
                    payload,
                    {
                        "keep": True,
                        "enable_thinking": False,
                        "thinking": {"type": "disabled"},
                        "reasoning_effort": "none",
                        "chat_template_kwargs": {
                            "enable_thinking": False,
                            "thinking": False,
                        },
                    },
                )

    def test_reasoning_effort_none_counts_as_disabled_thinking(self):
        self.assertTrue(llm._thinking_disabled_in_payload({"reasoning_effort": "none"}))

    def test_force_disable_thinking_adds_all_payload_flags_for_dingpan(self):
        fake_session = FakeSession(
            [
                FakeResponse(lines=['data: {"choices":[{"delta":{"content":"完成"}}]}', "data: [DONE]"]),
            ]
        )

        with patch.object(llm.requests, "Session", return_value=fake_session):
            result = llm.run_check(
                api_base="https://dingpan.digitalpower.huawei.com/v1/chat/completions",
                api_key="key",
                model_name="MiniMax-M2.7",
                force_disable_thinking=True,
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, "完成")
        payload = fake_session.calls[0][1]["json"]
        self.assert_all_thinking_disable_flags(payload)

    def test_force_disable_thinking_uses_codeagent_adapter_for_snapengine_hosts(self):
        hosts = (
            "snapengine.cida.cce.pro-szv-g.dragon.tools.huawei.com",
            "snapengine.cida.cce.pro-szv-y.dragon.tools.huawei.com",
            "snapengine.codemate.cce.prod-kwe-g.dragon.tools.huawei.com",
            "snapengine.codemate.cce.prod-kwe-y.dragon.tools.huawei.com",
        )

        for host in hosts:
            with self.subTest(host=host):
                self.assertEqual(
                    llm._thinking_payload_adapter(
                        f"https://{host}/v1/chat/completions",
                        "MiniMax-M2.7",
                    ),
                    "codeagent",
                )

                fake_session = FakeSession(
                    [
                        FakeResponse(lines=['data: {"choices":[{"delta":{"content":"完成"}}]}', "data: [DONE]"]),
                    ]
                )

                with patch.object(llm.requests, "Session", return_value=fake_session):
                    result = llm.run_check(
                        api_base=f"https://{host}/v1/chat/completions",
                        api_key="key",
                        model_name="MiniMax-M2.7",
                        force_disable_thinking=True,
                        check_name="规范性",
                        prompt="检查",
                        document_text="文档",
                    )

                self.assertEqual(result, "完成")
                payload = fake_session.calls[0][1]["json"]
                self.assert_all_thinking_disable_flags(payload)

    def test_dingpan_reasoning_only_retry_uses_chat_template_thinking_flag(self):
        fake_session = FakeSession(
            [
                FakeResponse(lines=['data: {"choices":[{"delta":{"reasoning":"持续分析"}}]}']),
                FakeResponse(
                    lines=[
                        'data: {"choices":[{"delta":{"content":"降级重试成功"}}]}',
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
                api_base="https://dingpan.digitalpower.huawei.com/v1/chat/completions",
                api_key="key",
                model_name="qwen-model",
                check_name="规范性",
                prompt="检查",
                document_text="文档",
            )

        self.assertEqual(result, "降级重试成功")
        first_payload = fake_session.calls[0][1]["json"]
        second_payload = fake_session.calls[1][1]["json"]
        self.assertNotIn("chat_template_kwargs", first_payload)
        self.assert_all_thinking_disable_flags(second_payload)

    def test_force_disable_thinking_replaces_existing_chat_template_kwargs(self):
        payload = {"chat_template_kwargs": {"some_option": "keep"}}

        llm._disable_thinking_in_payload(
            payload,
            api_base="https://dingpan.digitalpower.huawei.com/v1/chat/completions",
            model_name="qwen-model",
        )

        self.assert_all_thinking_disable_flags(payload)

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
        self.assertIn("单次回复最多列出 30 条问题", content[0]["text"])
        self.assertIn("只输出一个 JSON 对象", content[0]["text"])
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
