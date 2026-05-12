import json
from typing import Callable, Optional

import requests


class LLMError(Exception):
    pass


class _EmptyContentError(LLMError):
    pass


def run_check(
    *,
    api_base: str,
    api_key: Optional[str],
    proxy_mode: str = "direct",
    proxy: Optional[str] = None,
    request_timeout: int = 3600,
    model_name: str,
    check_name: str,
    prompt: str,
    document_text: str,
    on_delta: Optional[Callable[[str], None]] = None,
) -> str:
    api_base = api_base.rstrip("/")
    endpoint = api_base
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": "你是文档智能门禁系统中的审查助手。请严格基于用户提供的文档内容进行检查，输出中文结果。",
            },
            {
                "role": "user",
                "content": (
                    f"检查项：{check_name}\n\n"
                    f"检查提示词：\n{prompt}\n\n"
                    f"待检查文档：\n{document_text}"
                ),
            },
        ],
        "temperature": 0.2,
    }

    try:
        with requests.Session() as session:
            session.trust_env = proxy_mode == "system"
            request_kwargs = {
                "headers": headers,
                "timeout": request_timeout,
            }
            if proxy_mode == "custom":
                if not proxy:
                    raise LLMError("自定义代理模式需要填写代理地址")
                session.trust_env = False
                request_kwargs["proxies"] = {"http": proxy, "https": proxy}
            elif proxy_mode != "system":
                session.trust_env = False

            stream_payload = dict(payload)
            stream_payload["stream"] = True
            response = session.post(endpoint, json=stream_payload, stream=True, **request_kwargs)
            try:
                return _read_stream_response(response, on_delta)
            except _EmptyContentError as stream_exc:
                _close_response(response)
                non_stream_payload = dict(payload)
                non_stream_payload["stream"] = False
                response = session.post(endpoint, json=non_stream_payload, stream=False, **request_kwargs)
                try:
                    return _read_json_response(response, on_delta)
                except _EmptyContentError as non_stream_exc:
                    raise LLMError(
                        f"{stream_exc}；已自动改用非流式重试，仍未获得可输出文本：{non_stream_exc}"
                    ) from non_stream_exc
    except requests.ReadTimeout as exc:
        raise LLMError(f"模型服务处理超时：已连接到服务，但 {request_timeout} 秒内没有返回结果") from exc
    except requests.RequestException as exc:
        raise LLMError(f"模型服务请求失败：{exc}") from exc


def _read_stream_response(response, on_delta: Optional[Callable[[str], None]]) -> str:
    _raise_for_http_error(response)

    parts = []
    diagnostics = _ResponseDiagnostics()
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode("utf-8", errors="replace")
        line = raw_line.strip()
        if not line or line.startswith(":") or line.startswith("event:"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型服务返回了非 JSON 内容：{_short_text(line)}") from exc

        service_error = _extract_service_error(data)
        if service_error:
            raise LLMError(f"模型服务返回错误：{service_error}")

        diagnostics.observe(data)
        delta = _extract_content_delta(data, diagnostics)
        if not delta:
            continue
        parts.append(delta)
        if on_delta:
            on_delta(delta)

    content = "".join(parts).strip()
    if not content:
        raise _EmptyContentError(_empty_content_message(diagnostics))
    return content


def _read_json_response(response, on_delta: Optional[Callable[[str], None]]) -> str:
    _raise_for_http_error(response)
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError(f"模型服务返回了非 JSON 内容：{_short_text(response.text)}") from exc

    service_error = _extract_service_error(data)
    if service_error:
        raise LLMError(f"模型服务返回错误：{service_error}")

    diagnostics = _ResponseDiagnostics()
    diagnostics.observe(data)
    content = _extract_content_delta(data, diagnostics).strip()
    if not content:
        raise _EmptyContentError(_empty_content_message(diagnostics))
    if on_delta:
        on_delta(content)
    return content


def _raise_for_http_error(response):
    if response.status_code >= 400:
        body = response.text[:1000]
        raise LLMError(f"模型服务返回 {response.status_code}：{body}")


def _close_response(response):
    close = getattr(response, "close", None)
    if close:
        close()


class _ResponseDiagnostics:
    def __init__(self):
        self.frames = 0
        self.finish_reasons = set()
        self.response_types = set()
        self.saw_reasoning = False
        self.saw_tool_call = False
        self.refusal = ""

    def observe(self, data: dict):
        if not isinstance(data, dict):
            return
        self.frames += 1
        response_type = data.get("type")
        if response_type:
            self.response_types.add(str(response_type))

        choices = data.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    self.finish_reasons.add(str(finish_reason))
                self._observe_message(choice.get("delta"))
                self._observe_message(choice.get("message"))
                if choice.get("tool_calls"):
                    self.saw_tool_call = True

        self._observe_message(data)

    def _observe_message(self, value):
        if not isinstance(value, dict):
            return
        reasoning = value.get("reasoning_content") or value.get("reasoning")
        if reasoning:
            self.saw_reasoning = True
        refusal = value.get("refusal")
        if refusal and not self.refusal:
            self.refusal = _content_to_text(refusal)
        if value.get("tool_calls"):
            self.saw_tool_call = True


def _extract_content_delta(data: dict, diagnostics: Optional[_ResponseDiagnostics] = None) -> str:
    if not isinstance(data, dict):
        return ""

    response_type = data.get("type")
    if response_type == "response.output_text.delta":
        return _content_to_text(data.get("delta"))
    if response_type == "response.output_text.done":
        return _content_to_text(data.get("text"))
    if response_type in {"response.refusal.delta", "response.refusal.done"} and diagnostics:
        diagnostics.refusal = diagnostics.refusal or _content_to_text(data.get("delta") or data.get("refusal"))
        return ""

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        return _content_to_text(
            data.get("content")
            or data.get("output_text")
            or data.get("message")
            or data.get("output")
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""

    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = _content_to_text(delta.get("content"))
        if content:
            return content
        if diagnostics and delta.get("refusal"):
            diagnostics.refusal = diagnostics.refusal or _content_to_text(delta.get("refusal"))
    elif delta is not None:
        return _content_to_text(delta)

    message = choice.get("message")
    if isinstance(message, dict):
        content = _content_to_text(message.get("content"))
        if content:
            return content
        if diagnostics and message.get("refusal"):
            diagnostics.refusal = diagnostics.refusal or _content_to_text(message.get("refusal"))

    text = choice.get("text")
    if text is not None:
        return _content_to_text(text)
    return ""


def _extract_service_error(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if not error:
        return ""
    if isinstance(error, str):
        return _short_text(error)
    if isinstance(error, dict):
        message = error.get("message") or error.get("msg") or error.get("code") or error
        return _short_text(message)
    return _short_text(error)


def _content_to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_content_to_text(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in {"text", "output_text"}:
            return _content_to_text(value.get("text"))
        if "text" in value:
            return _content_to_text(value.get("text"))
        if "content" in value:
            return _content_to_text(value.get("content"))
        return ""
    return str(value)


def _empty_content_message(diagnostics: _ResponseDiagnostics) -> str:
    details = []
    if diagnostics.saw_reasoning:
        details.append("服务返回了 reasoning_content，但没有返回 assistant content")
    if diagnostics.refusal:
        details.append(f"服务返回拒绝信息：{_short_text(diagnostics.refusal)}")
    if diagnostics.saw_tool_call:
        details.append("服务返回了工具调用而不是文本")
    if diagnostics.finish_reasons:
        details.append(f"finish_reason={','.join(sorted(diagnostics.finish_reasons))}")
    if diagnostics.response_types:
        details.append(f"事件类型={','.join(sorted(diagnostics.response_types))}")
    if diagnostics.frames:
        details.append(f"已收到 {diagnostics.frames} 个 JSON 数据帧")
    else:
        details.append("未收到 JSON 数据帧")
    return f"模型服务没有返回可用内容：{'；'.join(details)}"


def _short_text(value, limit: int = 300) -> str:
    text = str(value).strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."
