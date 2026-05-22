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
_EXECUTION_BOUNDARY = """执行边界：
1. 只依据提供的文档内容，不补全文档外信息。
2. 不输出思考过程、推理链、草稿或分析计划。
3. 优先输出明确问题；不确定请标注“需人工确认”。
4. 没有发现问题时只给出简短结论。
5. 单次回复最多列出 20 条问题。"""


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
    force_disable_thinking: bool = False,
    check_name: str,
    prompt: str,
    document_text: str,
    on_delta: Optional[Callable[[str], None]] = None,
    on_content: Optional[Callable[[str], None]] = None,
    task_id: Optional[int] = None,
    stream_trace_enabled: bool = False,
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
                    f"{_EXECUTION_BOUNDARY}\n\n"
                    f"检查提示词：\n{prompt}\n\n"
                    f"待检查文档：\n{document_text}"
                ),
            },
        ],
        "temperature": 0.2,
    }
    if force_disable_thinking:
        _disable_thinking_in_payload(payload)

    logger.info(
        "LLM 请求开始 request_id=%s task_id=%s endpoint=%s model=%s check=%s proxy_mode=%s ssl_verify=%s timeout=%s force_disable_thinking=%s prompt_chars=%s document_chars=%s",
        request_id,
        task_id or "-",
        endpoint,
        model_name,
        check_name,
        proxy_mode,
        ssl_verify,
        request_timeout,
        force_disable_thinking,
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
                stream_trace_enabled=stream_trace_enabled,
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
    stream_trace_enabled: bool,
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
                if stream_trace_enabled:
                    logger.info(
                        "LLM 流式定位请求发送 request_id=%s task_id=%s attempt=%s endpoint=%s timeout=%s proxy_mode=%s ssl_verify=%s",
                        request_id,
                        task_id or "-",
                        attempt,
                        endpoint,
                        request_timeout,
                        proxy_mode,
                        ssl_verify,
                    )
                started_at = time.monotonic()
                response = session.post(endpoint, json=stream_payload, stream=True, **request_kwargs)
                if stream_trace_enabled:
                    headers = getattr(response, "headers", {}) or {}
                    logger.info(
                        "LLM 流式定位响应建立 request_id=%s task_id=%s attempt=%s status=%s content_type=%s elapsed_ms=%s",
                        request_id,
                        task_id or "-",
                        attempt,
                        getattr(response, "status_code", "-"),
                        headers.get("content-type") or headers.get("Content-Type") or "-",
                        int((time.monotonic() - started_at) * 1000),
                    )
                content = _read_stream_response(
                    response,
                    on_delta,
                    request_id=request_id,
                    task_id=task_id,
                    attempt=attempt,
                    stream_trace_enabled=stream_trace_enabled,
                )
                logger.info(
                    "LLM 请求完成 request_id=%s task_id=%s attempt=%s mode=stream output_chars=%s",
                    request_id,
                    task_id or "-",
                    attempt,
                    len(content),
                )
                return content
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
    endpoint = str(api_base or "").strip().rstrip("/")
    if not endpoint.startswith(("http://", "https://")) or not endpoint.endswith("/chat/completions"):
        raise LLMError(
            "模型提供商 API 地址必须填写完整的 OpenAI Chat Completions 请求地址，"
            "例如 https://api.example.com/v1/chat/completions"
        )
    return endpoint


def _disable_thinking_in_payload(payload: dict):
    payload["enable_thinking"] = False
    payload["chat_template_kwargs"] = {"enable_thinking": False}


def _read_stream_response(
    response,
    on_delta: Optional[Callable[[str], None]],
    *,
    request_id: str = "-",
    task_id: Optional[int] = None,
    attempt: Optional[int] = None,
    stream_trace_enabled: bool = False,
) -> str:
    _force_utf8_response(response)
    _raise_for_http_error(response, request_id=request_id, task_id=task_id)
    if stream_trace_enabled:
        logger.info(
            "LLM 流式定位开始读取 request_id=%s task_id=%s attempt=%s",
            request_id,
            task_id or "-",
            attempt or "-",
        )

    return _read_stream_lines(
        response.iter_lines(decode_unicode=True),
        on_delta,
        request_id=request_id,
        task_id=task_id,
        attempt=attempt,
        stream_trace_enabled=stream_trace_enabled,
    )


def _read_stream_lines(
    lines,
    on_delta: Optional[Callable[[str], None]],
    *,
    request_id: str = "-",
    task_id: Optional[int] = None,
    attempt: Optional[int] = None,
    stream_trace_enabled: bool = False,
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
            if stream_trace_enabled:
                logger.info(
                    "LLM 流式定位收到结束标记 request_id=%s task_id=%s attempt=%s frames=%s",
                    request_id,
                    task_id or "-",
                    attempt or "-",
                    diagnostics.frames,
                )
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
        if stream_trace_enabled:
            logger.info(
                "LLM 流式定位收到响应chunk request_id=%s task_id=%s attempt=%s frame=%s %s raw=%s",
                request_id,
                task_id or "-",
                attempt or "-",
                diagnostics.frames,
                _chat_frame_trace(data),
                _short_text(line, 600),
            )
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
    if error:
        if isinstance(error, str):
            return _short_text(error)
        if isinstance(error, dict):
            message = error.get("message") or error.get("msg") or error.get("code") or error
            return _short_text(message)
        return _short_text(error)
    if data.get("success") is False:
        message = data.get("message") or data.get("msg") or data.get("errorCode") or data.get("code") or data
        return _short_text(message)
    return ""


def _chat_frame_trace(data: dict) -> str:
    if not isinstance(data, dict):
        return f"type={type(data).__name__}"

    parts = []
    if data.get("object"):
        parts.append(f"object={_short_text(data['object'], 80)}")

    choices = data.get("choices")
    if isinstance(choices, list):
        roles = set()
        finish_reasons = set()
        content_chars = 0
        reasoning_chars = 0
        reasoning_fields = set()
        tool_call_chunks = 0
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                finish_reasons.add(str(finish_reason))
            for message in (choice.get("delta"), choice.get("message")):
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role:
                    roles.add(str(role))
                content = message.get("content")
                if isinstance(content, str):
                    content_chars += len(content)
                reasoning_field, reasoning_text = _extract_reasoning(message)
                if reasoning_text:
                    reasoning_fields.add(reasoning_field)
                    reasoning_chars += len(reasoning_text)
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    tool_call_chunks += 1

        parts.append(f"choices={len(choices)}")
        parts.append(f"content_delta_chars={content_chars}")
        parts.append(f"reasoning_delta_chars={reasoning_chars}")
        if reasoning_fields:
            parts.append(f"reasoning_fields={','.join(sorted(reasoning_fields))}")
        if roles:
            parts.append(f"roles={','.join(sorted(roles))}")
        if finish_reasons:
            parts.append(f"finish_reasons={','.join(sorted(finish_reasons))}")
        if tool_call_chunks:
            parts.append(f"tool_call_chunks={tool_call_chunks}")
    else:
        parts.append("choices=-")

    if isinstance(data.get("usage"), dict):
        parts.append("usage=1")
    return " ".join(parts)


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
