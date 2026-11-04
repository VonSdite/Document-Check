import json
import unittest
from unittest.mock import call, patch

from app.models import client as llm
from tests.test_llm import FakeResponse, FakeSession


def completed_response():
    return FakeResponse(
        lines=[
            'data: {"choices":[{"delta":{"content":"检查完成"}}]}',
            "data: [DONE]",
        ]
    )


class LLMRetryPolicyTest(unittest.TestCase):
    def setUp(self):
        llm._reset_http_session_pools()
        self.addCleanup(llm._reset_http_session_pools)
        self.session = FakeSession([])
        session_patch = patch.object(llm.requests, "Session", return_value=self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        wait_patch = patch.object(llm, "_wait_before_retry")
        self.wait = wait_patch.start()
        self.addCleanup(wait_patch.stop)

    def run_check(self, **kwargs):
        return llm.run_check(
            **{
                "api_base": "https://example.test/v1/chat/completions",
                "api_key": "key",
                "model_name": "test-model",
                "check_name": "规范性",
                "prompt": "检查",
                "document_text": "文档",
                **kwargs,
            }
        )

    def test_permanent_http_errors_stop_without_wait_or_retry_activity(self):
        for status in (400, 401, 402, 403, 404, 405, 413, 422, 501, 505, 511):
            with self.subTest(status=status):
                response = FakeResponse(status_code=status, text="request rejected")
                self.session.responses = [response]
                self.session.calls.clear()
                phases, events = [], []
                with self.assertRaises(llm.LLMError) as caught:
                    self.run_check(
                        on_activity=lambda phase, attempt: phases.append(phase),
                        on_output=events.append,
                    )
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(len(self.session.calls), 1)
                self.assertTrue(response.closed)
                self.assertNotIn("retrying", phases)
                self.assertEqual([event["kind"] for event in events], ["start", "end"])
        self.wait.assert_not_called()

    def test_transient_http_errors_retry_with_existing_backoff(self):
        for status in (408, 409, 425, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                responses = [
                    FakeResponse(status_code=status, text="temporary service failure"),
                    FakeResponse(status_code=status, text="temporary service failure"),
                    completed_response(),
                ]
                self.session.responses = list(responses)
                self.session.calls.clear()
                self.wait.reset_mock()
                self.assertEqual(self.run_check(), "检查完成")
                self.assertEqual(len(self.session.calls), 3)
                self.assertEqual(
                    self.wait.call_args_list, [call(2, None, None), call(4, None, None)]
                )
                self.assertTrue(all(response.closed for response in responses))

    def test_http_200_permanent_service_errors_stop_in_sse_and_json(self):
        errors = (
            {"error": {"code": "invalid_api_key", "message": "服务拒绝请求"}},
            {"error": {"code": "InvalidApiKey", "message": "服务拒绝请求"}},
            {"error": {"type": "permission_error", "message": "服务拒绝请求"}},
            {"error": {"type": "invalid_request_error", "message": "服务拒绝请求"}},
            {"error": {"code": "context_length_exceeded", "message": "服务拒绝请求"}},
            {
                "success": False,
                "errorCode": "insufficient_balance",
                "message": "服务拒绝请求",
            },
            {"error": "model not found"},
            {"error": "The model `missing-model` does not exist"},
            {"error": "forbidden"},
            {"error": "invalid_api_key"},
            {"error": {"message": "模型不存在"}},
            {"success": False, "code": 401, "message": "鉴权失败"},
        )
        for error in errors:
            for prefix in ("data: ", ""):
                with self.subTest(error=error, prefix=prefix):
                    response = FakeResponse(lines=[prefix + json.dumps(error)])
                    self.session.responses = [response]
                    self.session.calls.clear()
                    with self.assertRaises(llm.LLMError) as caught:
                        self.run_check()
                    self.assertFalse(caught.exception.retryable)
                    self.assertEqual(len(self.session.calls), 1)
                    self.assertTrue(response.closed)
        self.wait.assert_not_called()

    def test_business_code_401_with_timeout_message_remains_retryable(self):
        self.session.responses = [
            FakeResponse(
                lines=[
                    'data: {"code":401,"success":false,"errorCode":201001,"message":"调用模型服务失败：模型调用超时，请稍后再试"}'
                ]
            ),
            completed_response(),
        ]
        self.assertEqual(self.run_check(), "检查完成")
        self.assertEqual(len(self.session.calls), 2)
        self.wait.assert_called_once_with(2, None, None)

    def test_permanent_service_codes_override_429_and_500(self):
        for status, code in ((429, "insufficient_quota"), (500, "invalid_api_key")):
            with self.subTest(status=status, code=code):
                response = FakeResponse(
                    status_code=status,
                    text=json.dumps(
                        {"error": {"message": "服务拒绝请求" * 400, "code": code}}
                    ),
                )
                self.session.responses = [response]
                self.session.calls.clear()
                with self.assertRaises(llm.LLMError) as caught:
                    self.run_check()
                self.assertFalse(caught.exception.retryable)
                self.assertNotIn("降低系统同时执行任务数", str(caught.exception))
                self.assertEqual(len(self.session.calls), 1)
        self.wait.assert_not_called()

    def test_parameter_fallbacks_change_payload_once_then_stop_on_permanent_error(self):
        self.session.responses = [
            FakeResponse(
                status_code=400,
                text='{"error":{"message":"response_format json_object is not supported"}}',
            ),
            FakeResponse(
                status_code=422,
                text='{"error":{"message":"max_completion_tokens is not supported"}}',
            ),
            FakeResponse(status_code=400, text='{"error":{"message":"invalid model"}}'),
        ]
        with self.assertRaisesRegex(llm.LLMError, "invalid model"):
            self.run_check(model_name="deepseek-v4-flash")
        self.assertEqual(len(self.session.calls), 3)
        first, second, third = [kwargs["json"] for _, kwargs in self.session.calls]
        self.assertIn("response_format", first)
        self.assertNotIn("response_format", second)
        self.assertIn("max_completion_tokens", second)
        self.assertNotIn("max_completion_tokens", third)
        self.assertIn("max_tokens", third)
        self.wait.assert_not_called()

    def test_authentication_error_does_not_trigger_parameter_fallback(self):
        self.session.responses = [
            FakeResponse(
                status_code=401,
                text='{"error":{"message":"invalid API key for response_format json_object"}}',
            )
        ]
        with self.assertRaises(llm.LLMError):
            self.run_check(model_name="deepseek-v4-flash")
        self.assertEqual(len(self.session.calls), 1)
        self.wait.assert_not_called()

    def test_rejected_compatibility_request_stops_after_one_fallback(self):
        self.session.responses = [
            FakeResponse(
                status_code=400,
                text='{"error":{"message":"response_format json_object is not supported"}}',
            )
            for _ in range(2)
        ]
        with self.assertRaises(llm.LLMError):
            self.run_check(model_name="deepseek-v4-flash", max_completion_tokens=None)
        self.assertEqual(len(self.session.calls), 2)
        self.assertNotIn("response_format", self.session.calls[1][1]["json"])
        self.wait.assert_not_called()

    def test_request_configuration_errors_stop_without_retry(self):
        exceptions = llm.requests.exceptions
        errors = (
            exceptions.InvalidURL("invalid URL"),
            exceptions.InvalidProxyURL("invalid proxy"),
            exceptions.MissingSchema("missing scheme"),
            exceptions.InvalidHeader("invalid header"),
            exceptions.InvalidJSONError("invalid JSON"),
            exceptions.TooManyRedirects("redirect loop"),
            exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"),
        )
        for error in errors:
            with self.subTest(error=error):
                with (
                    patch.object(self.session, "post", side_effect=error) as post,
                    self.assertRaises(llm.LLMError) as caught,
                ):
                    self.run_check()
                self.assertFalse(caught.exception.retryable)
                post.assert_called_once()
        self.wait.assert_not_called()

    def test_transient_network_errors_retry_then_recover(self):
        exceptions = llm.requests.exceptions
        for error in (
            exceptions.ReadTimeout("read timeout"),
            exceptions.ConnectTimeout("connect timeout"),
            exceptions.ConnectionError("connection reset"),
            exceptions.ChunkedEncodingError("connection broken"),
            exceptions.SSLError("unexpected EOF"),
        ):
            with self.subTest(error=error):
                self.wait.reset_mock()
                response = completed_response()
                with patch.object(
                    self.session, "post", side_effect=[error, response]
                ) as post:
                    self.assertEqual(self.run_check(), "检查完成")
                self.assertEqual(post.call_count, 2)
                self.assertTrue(response.closed)
                self.wait.assert_called_once_with(2, None, None)

    def test_missing_proxy_configuration_stops_before_sending(self):
        with self.assertRaisesRegex(llm.LLMError, "需要填写代理地址") as caught:
            self.run_check(proxy_mode="custom", proxy=None)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.session.calls, [])
        self.wait.assert_not_called()

    def test_permanent_failure_keeps_earlier_partial_output(self):
        self.session.responses = [
            FakeResponse(
                lines=['data: {"choices":[{"delta":{"content":"部分正文"}}]}']
            ),
            FakeResponse(
                status_code=401, text='{"error":{"message":"invalid API key"}}'
            ),
        ]
        with self.assertRaises(llm.LLMIncompleteResponseError) as caught:
            self.run_check()
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.partial_content, "部分正文")
        self.assertIn("401", str(caught.exception))
        self.assertEqual(len(self.session.calls), 2)
        self.wait.assert_called_once_with(2, None, None)

    def test_image_and_multimodal_requests_share_permanent_error_policy(self):
        common = {
            "api_base": "https://example.test/v1/chat/completions",
            "api_key": "key",
            "model_name": "test-model",
            "check_name": "图片检查",
            "prompt": "检查",
        }
        requests = (
            (
                llm.run_image_check,
                {
                    "image_name": "test.png",
                    "image_position": "第1页",
                    "image_data_url": "data:image/png;base64,AA==",
                },
            ),
            (
                llm.run_multimodal_document_check,
                {
                    "document_text": "文档",
                    "image_items": [{"data_url": "data:image/png;base64,AA=="}],
                },
            ),
        )
        for run, kwargs in requests:
            with self.subTest(entry=run.__name__):
                self.session.responses = [
                    FakeResponse(status_code=403, text="forbidden")
                ]
                self.session.calls.clear()
                with self.assertRaises(llm.LLMError) as caught:
                    run(**common, **kwargs)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(len(self.session.calls), 1)
        self.wait.assert_not_called()
