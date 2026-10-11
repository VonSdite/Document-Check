"""兼容 Chat Completions 的结构化输出和工具调用适配。"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import requests

from app.models.client import (
    LLMError,
    _apply_reasoning_effort,
    _chat_completions_endpoint,
    _close_response,
    _disable_thinking_in_payload,
    _extract_chat_content,
    _extract_reasoning,
    _extract_service_error,
    _force_utf8_response,
    _is_output_token_limit_unsupported_error,
    _pooled_http_session,
    _raise_for_http_error,
    _raise_if_request_canceled,
    _start_response_cancel_watcher,
    _wait_before_retry,
)

logger = logging.getLogger(__name__)


class ToolsUnsupported(LLMError):
    pass


class IncompleteResponse(LLMError):
    pass


@dataclass
class AgentTurn:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict | None = None
    finish_reason: str = ""


class AgentModel:
    def __init__(
        self,
        connection: dict,
        *,
        before_request,
        after_request,
        ensure_active,
        on_output=None,
        on_activity=None,
        capabilities=None,
    ):
        self.connection = connection
        self.before_request = before_request
        self.after_request = after_request
        self.ensure_active = ensure_active
        self.on_output = on_output
        self.on_activity = on_activity
        self.capabilities = capabilities if capabilities is not None else {}
        self.capabilities.setdefault("schema", "json_schema")

    def turn(
        self, messages, *, schema, tools=None, force_tool=None, max_output_tokens=8192
    ):
        connection = self.connection
        payload = {
            "model": connection["model_name"],
            "messages": messages,
            self.capabilities.get(
                "output_token_parameter", "max_completion_tokens"
            ): max_output_tokens,
        }
        if self.capabilities.get("temperature") is not False:
            payload["temperature"] = 0
        _apply_reasoning_effort(payload, connection.get("reasoning_effort"))
        if connection.get("force_disable_thinking"):
            _disable_thinking_in_payload(
                payload,
                api_base=connection["api_base"],
                model_name=connection["model_name"],
            )
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = (
                {"type": "function", "function": {"name": force_tool}}
                if force_tool
                else "auto"
            )
        elif schema:
            mode = self.capabilities.get("schema", "json_schema")
            if mode == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "document_check",
                        "strict": True,
                        "schema": schema,
                    },
                }
            elif mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
        last_error = None
        retries = 0
        while True:
            self.ensure_active()
            self.before_request(payload)
            started = time.monotonic()
            request_id = uuid.uuid4().hex
            if self.on_activity:
                self.on_activity("waiting", retries + 1)
            if self.on_output:
                self.on_output(
                    {"kind": "start", "stream": request_id, "attempt": retries + 1}
                )
            turn = None
            try:
                turn = self._request(payload, request_id)
                if turn.finish_reason in {"length", "content_filter"}:
                    raise IncompleteResponse(
                        f"模型响应未完整完成：{turn.finish_reason}"
                    )
                if not turn.content and not turn.tool_calls:
                    raise IncompleteResponse("模型未返回可校验的正文或工具调用")
                self.after_request(payload, turn, time.monotonic() - started, None)
                return turn
            except IncompleteResponse as exc:
                self.after_request(payload, turn, time.monotonic() - started, str(exc))
                raise
            except LLMError as exc:
                last_error = exc
                self.after_request(payload, None, time.monotonic() - started, str(exc))
                lower = str(exc).lower()
                incompatible = any(
                    word in lower
                    for word in (
                        "unsupported",
                        "not support",
                        "not allowed",
                        "不支持",
                        "unknown",
                        "invalid",
                        "400",
                        "422",
                    )
                )
                if (
                    (
                        tools
                        or any(message.get("role") == "tool" for message in messages)
                    )
                    and any(
                        word in lower
                        for word in (
                            "tools",
                            "tool_choice",
                            "function calling",
                            "tool calling",
                            "tool_call_id",
                            "role 'tool'",
                            'role "tool"',
                        )
                    )
                    and incompatible
                ):
                    self.capabilities["native_tools"] = False
                    raise ToolsUnsupported("模型服务不支持原生工具调用") from exc
                if (
                    incompatible
                    and "response_format" in payload
                    and any(
                        word in lower
                        for word in (
                            "response_format",
                            "json_schema",
                            "json_object",
                            "json mode",
                        )
                    )
                ):
                    if payload["response_format"]["type"] == "json_schema":
                        payload["response_format"] = {"type": "json_object"}
                        self.capabilities["schema"] = "json_object"
                    else:
                        payload.pop("response_format")
                        self.capabilities["schema"] = "prompt"
                    continue
                if incompatible and "temperature" in lower and "temperature" in payload:
                    payload.pop("temperature")
                    self.capabilities["temperature"] = False
                    continue
                if (
                    incompatible
                    and "stream_options" in lower
                    and self.capabilities.get("stream_usage") is not False
                ):
                    self.capabilities["stream_usage"] = False
                    continue
                if (
                    "max_completion_tokens" in payload
                    and _is_output_token_limit_unsupported_error(exc)
                ):
                    payload["max_tokens"] = payload.pop("max_completion_tokens")
                    self.capabilities["output_token_parameter"] = "max_tokens"
                    continue
                if retries >= 2 or not any(
                    word in lower
                    for word in (
                        "429",
                        "500",
                        "502",
                        "503",
                        "504",
                        "超时",
                        "timeout",
                        "连接",
                        "connection",
                    )
                ):
                    raise
                retries += 1
                _wait_before_retry(
                    retries * 2, connection.get("cancel_event"), self.ensure_active
                )
            finally:
                if self.on_output:
                    self.on_output(
                        {"kind": "end", "stream": request_id, "attempt": retries + 1}
                    )
            if retries > 2:
                raise last_error

    def _request(self, payload, request_id):
        connection = self.connection
        endpoint = _chat_completions_endpoint(connection["api_base"])
        headers = {"Content-Type": "application/json"}
        if connection.get("api_key"):
            headers["Authorization"] = f"Bearer {connection['api_key']}"
        request_kwargs = {
            "headers": headers,
            "timeout": min(
                connection.get("request_timeout", 3600),
                connection.get(
                    "remaining_seconds", connection.get("max_seconds", 3600)
                ),
            ),
            "verify": connection.get("ssl_verify", False),
        }
        proxy_mode = connection.get("proxy_mode", "direct")
        if proxy_mode == "custom":
            proxy = connection.get("proxy")
            if not proxy:
                raise LLMError("自定义代理模式需要填写代理地址")
            request_kwargs["proxies"] = {"http": proxy, "https": proxy}
        response, done, watcher = None, None, None
        try:
            with _pooled_http_session(
                endpoint, proxy_mode, connection.get("proxy")
            ) as session:
                response = session.post(
                    endpoint,
                    json={
                        **payload,
                        "stream": True,
                        **(
                            {"stream_options": {"include_usage": True}}
                            if self.capabilities.get("stream_usage") is not False
                            else {}
                        ),
                    },
                    stream=True,
                    **request_kwargs,
                )
                _force_utf8_response(response)
                _raise_for_http_error(response)
                done, watcher = _start_response_cancel_watcher(
                    response,
                    connection.get("cancel_event"),
                    task_id=connection.get("task_id"),
                )
                content_type = str(
                    (getattr(response, "headers", {}) or {}).get("Content-Type")
                    or (getattr(response, "headers", {}) or {}).get("content-type")
                    or ""
                )
                if "application/json" in content_type:
                    frames = [response.json()]
                else:

                    def stream_frames():
                        for line in response.iter_lines(decode_unicode=True):
                            self.ensure_active()
                            if isinstance(line, bytes):
                                line = line.decode("utf-8")
                            if not line or not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                break
                            try:
                                yield json.loads(raw)
                            except ValueError as exc:
                                raise LLMError(
                                    "模型流包含无法解析的 JSON 事件"
                                ) from exc

                    frames = stream_frames()
                return self._collect(frames, request_id)
        except requests.RequestException as exc:
            _raise_if_request_canceled(
                connection.get("cancel_event"), self.ensure_active, poll=True
            )
            raise LLMError(f"模型服务请求失败：{exc}") from exc
        finally:
            if done:
                done.set()
            _close_response(response)
            if watcher:
                watcher.join(timeout=0.2)

    def _collect(self, frames, request_id):
        turn, calls = AgentTurn(), {}
        output_chars, reasoning_chars = 0, 0
        finished = False
        for frame in frames:
            self.ensure_active()
            if not isinstance(frame, dict):
                raise LLMError("模型响应不是 JSON 对象")
            error = _extract_service_error(frame)
            if error:
                raise LLMError(f"模型服务返回错误：{error}")
            if isinstance(frame.get("usage"), dict):
                turn.usage = frame["usage"]
            for choice in frame.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                message = choice.get("delta") or choice.get("message") or {}
                if message.get("refusal"):
                    raise IncompleteResponse("模型拒绝了当前检查请求")
                text = _extract_chat_content({"choices": [{"message": message}]})
                if text:
                    turn.content += text
                    output_chars += len(text)
                    if self.on_output:
                        self.on_output(
                            {"kind": "content", "text": text, "stream": request_id}
                        )
                _, reasoning = _extract_reasoning(message)
                reasoning_chars += len(reasoning)
                if reasoning and self.on_output:
                    self.on_output(
                        {"kind": "thinking", "text": reasoning, "stream": request_id}
                    )
                for position, delta in enumerate(message.get("tool_calls") or []):
                    index = delta.get("index", position)
                    if not isinstance(index, int) or not 0 <= index < 6:
                        raise LLMError("工具调用数量或索引超出限制")
                    call = calls.setdefault(
                        index,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    call["id"] += str(delta.get("id") or "")
                    function = delta.get("function") or {}
                    for key in ("name", "arguments"):
                        value = str(function.get(key) or "")
                        call["function"][key] += value
                        output_chars += len(value)
                if choice.get("finish_reason"):
                    turn.finish_reason = str(choice["finish_reason"])
                    finished = True
            if output_chars > 1048576 or reasoning_chars > 128000:
                raise IncompleteResponse("模型输出超过单次响应限制")
        if not finished:
            raise IncompleteResponse("模型流未返回完成标识")
        turn.tool_calls = [calls[key] for key in sorted(calls)]
        if any(not c["id"] or not c["function"]["name"] for c in turn.tool_calls):
            raise LLMError("工具调用缺少标识或函数名")
        return turn
