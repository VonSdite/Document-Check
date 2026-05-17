import json
import logging
import time
import uuid
from typing import Callable, Optional

import requests


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_REASONING_FIELDS = ("reasoning", "reasoning_content", "reasoning_details", "reasoning_text", "reasoning_opaque")
_MAX_RETRIES = 2


class LLMError(Exception):
    pass


class _EmptyContentError(LLMError):
    pass


class _StreamParseError(LLMError):
    pass


def run_check(
    *,
    api_base: str,
    api_key: Optional[str],
    proxy_mode: str = "direct",
    proxy: Optional[str] = None,
    ssl_verify: bool = False,
    request_timeout: int = 3600,
    model_name: str,
    check_name: str,
    prompt: str,
    document_text: str,
    on_delta: Optional[Callable[[str], None]] = None,
    on_content: Optional[Callable[[str], None]] = None,
    task_id: Optional[int] = None,
) -> str:
    request_id = uuid.uuid4().hex[:12]
    endpoint = _chat_completions_endpoint(api_base)

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

    logger.info(
        "LLM 请求开始 request_id=%s task_id=%s endpoint=%s model=%s check=%s proxy_mode=%s ssl_verify=%s timeout=%s prompt_chars=%s document_chars=%s",
        request_id,
        task_id or "-",
        endpoint,
        model_name,
        check_name,
        proxy_mode,
        ssl_verify,
        request_timeout,
        len(prompt),
        len(document_text),
    )

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 2):
        attempt_content = ""

        def on_attempt_delta(delta: str):
            nonlocal attempt_content
            attempt_content += delta
            if on_content:
                on_content(attempt_content)

        try:
            content = _run_check_attempt(
                endpoint=endpoint,
                headers=headers,
                payload=payload,
                proxy_mode=proxy_mode,
                proxy=proxy,
                ssl_verify=ssl_verify,
                request_timeout=request_timeout,
                on_delta=on_attempt_delta if on_content else None,
                request_id=request_id,
                task_id=task_id,
                attempt=attempt,
            )
            if on_content and not attempt_content and content:
                on_content(content)
            if on_delta:
                on_delta(content)
            return content
        except LLMError as exc:
            last_error = exc
            if on_content and attempt_content:
                on_content("")
            if attempt > _MAX_RETRIES:
                raise
            delay_seconds = attempt
            logger.warning(
                "LLM 请求出错，准备重试 request_id=%s task_id=%s attempt=%s/%s delay=%ss error=%s",
                request_id,
                task_id or "-",
                attempt,
                _MAX_RETRIES + 1,
                delay_seconds,
                exc,
            )
            time.sleep(delay_seconds)
    raise last_error or LLMError("模型服务请求失败")


def _run_check_attempt(
    *,
    endpoint: str,
    headers: dict,
    payload: dict,
    proxy_mode: str,
    proxy: Optional[str],
    ssl_verify: bool,
    request_timeout: int,
    on_delta: Optional[Callable[[str], None]],
    request_id: str,
    task_id: Optional[int],
    attempt: int,
) -> str:
    try:
        with requests.Session() as session:
            session.trust_env = proxy_mode == "system"
            request_kwargs = {
                "headers": headers,
                "timeout": request_timeout,
                "verify": ssl_verify,
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
            stream_payload["stream_options"] = {"include_usage": True}
            response = None
            try:
                response = session.post(endpoint, json=stream_payload, stream=True, **request_kwargs)
                content = _read_stream_response(response, on_delta, request_id=request_id, task_id=task_id)
                logger.info(
                    "LLM 请求完成 request_id=%s task_id=%s attempt=%s mode=stream output_chars=%s",
                    request_id,
                    task_id or "-",
                    attempt,
                    len(content),
                )
                return content
            except (_EmptyContentError, _StreamParseError) as stream_exc:
                _close_response(response)
                logger.warning(
                    "LLM 流式响应不可用，改用 OpenAI Chat 非流式重试 request_id=%s task_id=%s attempt=%s reason=%s",
                    request_id,
                    task_id or "-",
                    attempt,
                    stream_exc,
                )

                non_stream_payload = dict(payload)
                non_stream_payload["stream"] = False
                response = session.post(endpoint, json=non_stream_payload, stream=False, **request_kwargs)
                try:
                    content = _read_json_response(response, on_delta, request_id=request_id, task_id=task_id)
                    logger.info(
                        "LLM 请求完成 request_id=%s task_id=%s attempt=%s mode=non_stream_retry output_chars=%s",
                        request_id,
                        task_id or "-",
                        attempt,
                        len(content),
                    )
                    return content
                except (_EmptyContentError, _StreamParseError) as non_stream_exc:
                    raise LLMError(
                        f"{stream_exc}；已自动改用 OpenAI Chat 非流式重试，仍未获得可输出文本：{non_stream_exc}"
                    ) from non_stream_exc
            finally:
                _close_response(response)
    except requests.ReadTimeout as exc:
        logger.warning(
            "LLM 请求超时 request_id=%s task_id=%s attempt=%s timeout=%s",
            request_id,
            task_id or "-",
            attempt,
            request_timeout,
        )
        raise LLMError(f"模型服务处理超时：已连接到服务，但 {request_timeout} 秒内没有返回结果") from exc
    except requests.RequestException as exc:
        logger.warning("LLM 请求失败 request_id=%s task_id=%s attempt=%s error=%s", request_id, task_id or "-", attempt, exc)
        raise LLMError(f"模型服务请求失败：{exc}") from exc


def _chat_completions_endpoint(api_base: str) -> str:
    endpoint = api_base.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint = f"{endpoint}/chat/completions"
    return endpoint


def _read_stream_response(
    response,
    on_delta: Optional[Callable[[str], None]],
    *,
    request_id: str = "-",
    task_id: Optional[int] = None,
) -> str:
    _force_utf8_response(response)
    _raise_for_http_error(response, request_id=request_id, task_id=task_id)

    return _read_stream_lines(
        response.iter_lines(decode_unicode=True),
        on_delta,
        request_id=request_id,
        task_id=task_id,
    )


def _read_stream_lines(
    lines,
    on_delta: Optional[Callable[[str], None]],
    *,
    request_id: str = "-",
    task_id: Optional[int] = None,
) -> str:
    parts = []
    diagnostics = _OpenAIChatDiagnostics()
    for raw_line in lines:
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
            diagnostics.done = True
            break

        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning(
                "LLM 流式帧不是 JSON request_id=%s task_id=%s frame=%s",
                request_id,
                task_id or "-",
                _short_text(line, 1000),
            )
            raise _StreamParseError(f"模型服务返回了非 JSON 流式分片：{_short_text(line)}") from exc

        service_error = _extract_service_error(data)
        if service_error:
            logger.warning(
                "LLM 流式帧返回错误 request_id=%s task_id=%s error=%s raw=%s",
                request_id,
                task_id or "-",
                service_error,
                _short_text(line, 1000),
            )
            raise LLMError(f"模型服务返回错误：{service_error}")

        diagnostics.observe(data, raw=line)
        delta = _extract_chat_content(data)
        if not delta:
            continue
        parts.append(delta)
        if on_delta:
            on_delta(delta)

    content = "".join(parts).strip()
    logger.info(
        "LLM 流式响应结束 request_id=%s task_id=%s %s",
        request_id,
        task_id or "-",
        diagnostics.log_summary(),
    )
    if not content:
        logger.warning(
            "LLM 流式响应没有 assistant content request_id=%s task_id=%s samples=%s",
            request_id,
            task_id or "-",
            diagnostics.samples,
        )
        raise _EmptyContentError(_empty_content_message(diagnostics))
    return content


def _read_json_response(
    response,
    on_delta: Optional[Callable[[str], None]],
    *,
    request_id: str = "-",
    task_id: Optional[int] = None,
) -> str:
    _force_utf8_response(response)
    _raise_for_http_error(response, request_id=request_id, task_id=task_id)
    try:
        data = response.json()
    except ValueError as exc:
        body = _short_text(response.text, 1000)
        if _looks_like_stream_body(response.text):
            logger.warning(
                "LLM 非流式响应实际为流式分片 request_id=%s task_id=%s body=%s",
                request_id,
                task_id or "-",
                body,
            )
            return _read_stream_lines(
                response.text.splitlines(),
                on_delta,
                request_id=request_id,
                task_id=task_id,
            )
        logger.warning("LLM 非流式响应不是 JSON request_id=%s task_id=%s body=%s", request_id, task_id or "-", body)
        raise LLMError(f"模型服务返回了非 JSON 内容：{body}") from exc

    service_error = _extract_service_error(data)
    if service_error:
        logger.warning("LLM 非流式响应返回错误 request_id=%s task_id=%s error=%s", request_id, task_id or "-", service_error)
        raise LLMError(f"模型服务返回错误：{service_error}")

    diagnostics = _OpenAIChatDiagnostics()
    diagnostics.observe(data, raw=json.dumps(data, ensure_ascii=False))
    content = _extract_chat_content(data).strip()
    logger.info(
        "LLM 非流式响应结束 request_id=%s task_id=%s %s",
        request_id,
        task_id or "-",
        diagnostics.log_summary(),
    )
    if not content:
        logger.warning(
            "LLM 非流式响应没有 assistant content request_id=%s task_id=%s samples=%s",
            request_id,
            task_id or "-",
            diagnostics.samples,
        )
        raise _EmptyContentError(_empty_content_message(diagnostics))
    if on_delta:
        on_delta(content)
    return content


def _raise_for_http_error(response, *, request_id: str = "-", task_id: Optional[int] = None):
    if response.status_code >= 400:
        body = _short_text(response.text, 1500)
        logger.warning(
            "LLM HTTP 错误 request_id=%s task_id=%s status=%s body=%s",
            request_id,
            task_id or "-",
            response.status_code,
            body,
        )
        raise LLMError(f"模型服务返回 {response.status_code}：{body}")


def _force_utf8_response(response):
    response.encoding = "utf-8"


def _looks_like_stream_body(value: str) -> bool:
    text = str(value or "").lstrip()
    return (
        text.startswith("data:")
        or "\ndata:" in text
        or '"object":"chat.completion.chunk"' in text
        or ('"delta"' in text and '"choices"' in text)
    )


def _close_response(response):
    close = getattr(response, "close", None)
    if close:
        close()


class _OpenAIChatDiagnostics:
    def __init__(self):
        self.frames = 0
        self.done = False
        self.content_chunks = 0
        self.content_chars = 0
        self.reasoning_chunks = 0
        self.reasoning_chars = 0
        self.reasoning_fields = set()
        self.tool_call_chunks = 0
        self.finish_reasons = set()
        self.usage = None
        self.response_id = ""
        self.response_model = ""
        self.samples = []

    def observe(self, data: dict, *, raw: str):
        self.frames += 1
        if len(self.samples) < 3:
            self.samples.append(_short_text(raw, 1000))
        if not isinstance(data, dict):
            return

        if data.get("id"):
            self.response_id = str(data["id"])
        if data.get("model"):
            self.response_model = str(data["model"])
        if isinstance(data.get("usage"), dict):
            self.usage = _short_text(json.dumps(data["usage"], ensure_ascii=False), 1000)

        choices = data.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                self.finish_reasons.add(str(finish_reason))
            self._observe_message(choice.get("delta"))
            self._observe_message(choice.get("message"))

    def _observe_message(self, value):
        if not isinstance(value, dict):
            return
        content = value.get("content")
        if isinstance(content, str) and content:
            self.content_chunks += 1
            self.content_chars += len(content)

        reasoning_field, reasoning_text = _extract_reasoning(value)
        if reasoning_text:
            self.reasoning_fields.add(reasoning_field)
            self.reasoning_chunks += 1
            self.reasoning_chars += len(reasoning_text)

        tool_calls = value.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            self.tool_call_chunks += 1

    def log_summary(self) -> str:
        return (
            f"frames={self.frames} done={self.done} content_chunks={self.content_chunks} "
            f"content_chars={self.content_chars} reasoning_chunks={self.reasoning_chunks} "
            f"reasoning_chars={self.reasoning_chars} reasoning_fields={','.join(sorted(self.reasoning_fields)) or '-'} "
            f"tool_call_chunks={self.tool_call_chunks} "
            f"finish_reasons={','.join(sorted(self.finish_reasons)) or '-'} "
            f"usage={self.usage or '-'} response_id={self.response_id or '-'} response_model={self.response_model or '-'}"
        )


def _extract_chat_content(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""

    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content

    message = choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content

    return ""


def _extract_reasoning(message: dict) -> tuple[str, str]:
    for field in _REASONING_FIELDS:
        value = message.get(field)
        if isinstance(value, str) and value:
            return field, value
        if isinstance(value, list) and value:
            return field, _short_text(value, 1000)
    return "", ""


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


def _empty_content_message(diagnostics: _OpenAIChatDiagnostics) -> str:
    details = ["按 OpenAI Chat Completions 结构解析后没有得到 choices[0].delta.content 或 choices[0].message.content"]
    if diagnostics.reasoning_chunks:
        fields = ",".join(sorted(diagnostics.reasoning_fields)) or "reasoning"
        details.append(f"服务返回了 {fields} 字段 {diagnostics.reasoning_chunks} 段/{diagnostics.reasoning_chars} 字")
    if diagnostics.tool_call_chunks:
        details.append("服务返回了 tool_calls 而不是文本")
    if diagnostics.finish_reasons:
        details.append(f"finish_reason={','.join(sorted(diagnostics.finish_reasons))}")
    if diagnostics.usage:
        details.append(f"usage={diagnostics.usage}")
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
