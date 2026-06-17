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
_CONTENT_CALLBACK_INTERVAL = 0.25
DEFAULT_ISSUE_OUTPUT_LIMIT = 20
_STRUCTURED_REPORT_OUTPUT_CONTRACT = """结构化输出要求：
1. 只输出一个 JSON 对象，不要使用 Markdown、代码块、表格或解释性前后缀。
2. JSON 对象格式必须为：{"summary":"...", "items":[{"status":"issue|suggestion|non_issue","category":"...","location":"...","excerpt":"...","description":"...","impact":"...","suggestion":"..."}]}。
3. status 规则：能从证据明确判定的问题填 "issue"；证据不足、需人工确认、疑似、建议补充或不确定项填 "suggestion"；明确不是问题或无需修改填 "non_issue"。
4. 每个 items 元素只描述一个问题、建议或非问题；没有发现问题时 items 为空数组，summary 写简短结论。
5. 所有字段使用中文字符串；未知或不适用字段填空字符串；不要输出 null。"""
_EXECUTION_BOUNDARY_TEMPLATE = """执行边界：
1. 只依据提供的文档内容，不补全文档外信息。
2. 不输出思考过程、推理链、草稿或分析计划。
3. 优先输出明确问题；不确定请标注“需人工确认”。
4. 文档文本由解析器抽取得到，换行、分页、表格分隔符、行首行尾空白可能与原版版式不同；除非同一原文行内明确可见连续空格或异常空格，不要把解析换行/分页造成的空白判为“多余空格”。
5. 输出明确问题或建议时，每条只描述一个问题或建议。
6. 没有发现问题时不要编造条目。
7. {issue_output_limit_instruction}

{structured_report_output_contract}"""
_IMAGE_EXECUTION_BOUNDARY_TEMPLATE = """执行边界：
1. 只依据当前图片和提供的图片位置信息进行检查，不补全图片外信息。
2. 不输出思考过程、推理链、草稿或分析计划。
3. 优先输出明确问题；不确定请标注“需人工确认”。
4. 输出明确问题或建议时，每条只描述一个问题或建议。
5. 没有发现问题时不要编造条目。
6. {issue_output_limit_instruction}

{structured_report_output_contract}"""
_MULTIMODAL_DOCUMENT_EXECUTION_BOUNDARY_TEMPLATE = """执行边界：
1. 可以综合文档文本、图片清单、图片位置和本次提供的图片内容进行检查，尤其关注图文是否对应。
2. 只依据提供的文档上下文和图片内容，不补全文档外信息。
3. 不输出思考过程、推理链、草稿或分析计划。
4. 优先输出明确问题；不确定请标注“需人工确认”。
5. 输出问题时尽量引用图片名称、图片位置或文档中的文字线索。
6. 判断图文不对应时，必须同时给出明确文档线索和图片可见证据。
7. 不要仅凭文件名、页码、图片顺序或未提供的上下文断言图片插入错位；证据不足时写“需人工确认”。
8. 输出明确问题或建议时，请使用可拆分的编号条目，每条只描述一个问题或建议。
9. {issue_output_limit_instruction}"""


def _normalized_issue_output_limit(value) -> int:
    if value is None:
        return DEFAULT_ISSUE_OUTPUT_LIMIT
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_ISSUE_OUTPUT_LIMIT


def _issue_output_limit_instruction(value, subject: str) -> str:
    limit = _normalized_issue_output_limit(value)
    if limit <= 0:
        return f"{subject}不限制问题条数；请完整列出可确认的问题，并按严重程度和证据明确程度排序。"
    return f"{subject}最多列出 {limit} 条问题；如果超过 {limit} 条，优先列出影响最大、证据最明确的问题。"


def _execution_boundary(issue_output_limit=DEFAULT_ISSUE_OUTPUT_LIMIT) -> str:
    return _EXECUTION_BOUNDARY_TEMPLATE.format(
        issue_output_limit_instruction=_issue_output_limit_instruction(issue_output_limit, "单次回复"),
        structured_report_output_contract=_STRUCTURED_REPORT_OUTPUT_CONTRACT,
    )


def _image_execution_boundary(issue_output_limit=DEFAULT_ISSUE_OUTPUT_LIMIT) -> str:
    return _IMAGE_EXECUTION_BOUNDARY_TEMPLATE.format(
        issue_output_limit_instruction=_issue_output_limit_instruction(issue_output_limit, "单张图片回复"),
        structured_report_output_contract=_STRUCTURED_REPORT_OUTPUT_CONTRACT,
    )


def _multimodal_document_execution_boundary(issue_output_limit=DEFAULT_ISSUE_OUTPUT_LIMIT) -> str:
    return _MULTIMODAL_DOCUMENT_EXECUTION_BOUNDARY_TEMPLATE.format(
        issue_output_limit_instruction=_issue_output_limit_instruction(issue_output_limit, "单次回复")
    )


_EXECUTION_BOUNDARY = _execution_boundary()
_IMAGE_EXECUTION_BOUNDARY = _image_execution_boundary()
_MULTIMODAL_DOCUMENT_EXECUTION_BOUNDARY = _multimodal_document_execution_boundary()


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
    issue_output_limit: int | None = DEFAULT_ISSUE_OUTPUT_LIMIT,
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
                    f"{_execution_boundary(issue_output_limit)}\n\n"
                    f"检查提示词：\n{prompt}\n\n"
                    f"待检查文档：\n{document_text}"
                ),
            },
        ],
        "temperature": 0.2,
    }
    if force_disable_thinking:
        _disable_thinking_in_payload(payload, api_base=api_base, model_name=model_name)

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

    return _run_payload_with_retries(
        endpoint=endpoint,
        headers=headers,
        payload=payload,
        proxy_mode=proxy_mode,
        proxy=proxy,
        ssl_verify=ssl_verify,
        request_timeout=request_timeout,
        on_delta=on_delta,
        on_content=on_content,
        request_id=request_id,
        task_id=task_id,
        stream_trace_enabled=stream_trace_enabled,
    )


def run_image_check(
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
    image_name: str,
    image_position: str,
    image_data_url: str,
    issue_output_limit: int | None = DEFAULT_ISSUE_OUTPUT_LIMIT,
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

    image_data_url = str(image_data_url or "").strip()
    if not image_data_url.startswith("data:image/"):
        raise LLMError("图片数据格式无效，无法发送给多模态模型。")

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": "你是文档智能门禁系统中的图片审查助手。请严格基于用户提供的图片进行检查，输出中文结果。",
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"检查项：{check_name}\n\n"
                            f"{_image_execution_boundary(issue_output_limit)}\n\n"
                            f"图片名称：{image_name}\n"
                            f"图片位置：{image_position or '未标注'}\n\n"
                            f"检查提示词：\n{prompt}"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_data_url,
                        },
                    },
                ],
            },
        ],
        "temperature": 0.2,
    }
    if force_disable_thinking:
        _disable_thinking_in_payload(payload, api_base=api_base, model_name=model_name)

    logger.info(
        "LLM 图片请求开始 request_id=%s task_id=%s endpoint=%s model=%s check=%s image=%s position=%s proxy_mode=%s ssl_verify=%s timeout=%s force_disable_thinking=%s prompt_chars=%s data_url_chars=%s",
        request_id,
        task_id or "-",
        endpoint,
        model_name,
        check_name,
        image_name,
        image_position or "-",
        proxy_mode,
        ssl_verify,
        request_timeout,
        force_disable_thinking,
        len(prompt),
        len(image_data_url),
    )

    return _run_payload_with_retries(
        endpoint=endpoint,
        headers=headers,
        payload=payload,
        proxy_mode=proxy_mode,
        proxy=proxy,
        ssl_verify=ssl_verify,
        request_timeout=request_timeout,
        on_delta=on_delta,
        on_content=on_content,
        request_id=request_id,
        task_id=task_id,
        stream_trace_enabled=stream_trace_enabled,
    )


def run_multimodal_document_check(
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
    image_items: list[dict],
    batch_index: int = 1,
    batch_count: int = 1,
    issue_output_limit: int | None = DEFAULT_ISSUE_OUTPUT_LIMIT,
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

    if not image_items:
        raise LLMError("未提供可发送给多模态模型的图片。")
    normalized_images = []
    for index, image in enumerate(image_items, start=1):
        image_data_url = str(image.get("data_url") or "").strip()
        if not image_data_url.startswith("data:image/"):
            raise LLMError("图片数据格式无效，无法发送给多模态模型。")
        normalized_images.append(
            {
                "index": int(image.get("index") or index),
                "name": str(image.get("name") or image.get("filename") or f"image-{index:04d}"),
                "position": str(image.get("position") or "未标注"),
                "mime_type": str(image.get("mime_type") or ""),
                "data_url": image_data_url,
            }
        )

    content = [
        {
            "type": "text",
            "text": _multimodal_document_prompt_text(
                check_name=check_name,
                prompt=prompt,
                document_text=document_text,
                image_items=normalized_images,
                batch_index=batch_index,
                batch_count=batch_count,
                issue_output_limit=issue_output_limit,
            ),
        }
    ]
    for image in normalized_images:
        content.append(
            {
                "type": "text",
                "text": (
                    f"图片 {image['index']}：{image['name']}\n"
                    f"位置：{image['position']}\n"
                    f"格式：{image['mime_type'] or '未知'}"
                ),
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": image["data_url"],
                },
            }
        )

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": "你是文档智能门禁系统中的多模态审查助手。请严格基于用户提供的文档文本和图片内容进行图文联合检查，输出中文结果。",
            },
            {
                "role": "user",
                "content": content,
            },
        ],
        "temperature": 0.2,
    }
    if force_disable_thinking:
        _disable_thinking_in_payload(payload, api_base=api_base, model_name=model_name)

    data_url_chars = sum(len(image["data_url"]) for image in normalized_images)
    logger.info(
        "LLM 图文联合请求开始 request_id=%s task_id=%s endpoint=%s model=%s check=%s batch=%s/%s images=%s proxy_mode=%s ssl_verify=%s timeout=%s force_disable_thinking=%s prompt_chars=%s document_chars=%s data_url_chars=%s",
        request_id,
        task_id or "-",
        endpoint,
        model_name,
        check_name,
        batch_index,
        batch_count,
        len(normalized_images),
        proxy_mode,
        ssl_verify,
        request_timeout,
        force_disable_thinking,
        len(prompt),
        len(document_text),
        data_url_chars,
    )

    return _run_payload_with_retries(
        endpoint=endpoint,
        headers=headers,
        payload=payload,
        proxy_mode=proxy_mode,
        proxy=proxy,
        ssl_verify=ssl_verify,
        request_timeout=request_timeout,
        on_delta=on_delta,
        on_content=on_content,
        request_id=request_id,
        task_id=task_id,
        stream_trace_enabled=stream_trace_enabled,
    )


def _multimodal_document_prompt_text(
    *,
    check_name: str,
    prompt: str,
    document_text: str,
    image_items: list[dict],
    batch_index: int,
    batch_count: int,
    issue_output_limit: int | None = DEFAULT_ISSUE_OUTPUT_LIMIT,
) -> str:
    image_lines = []
    for image in image_items:
        image_lines.append(
            f"- 图片 {image['index']}: {image['name']}，位置：{image['position']}，格式：{image['mime_type'] or '未知'}"
        )
    batch_label = f"{batch_index}/{batch_count}" if batch_count > 1 else "1/1"
    return (
        f"检查项：{check_name}\n\n"
        f"{_multimodal_document_execution_boundary(issue_output_limit)}\n\n"
        f"当前图片批次：{batch_label}\n\n"
        f"本次可见图片：\n{chr(10).join(image_lines)}\n\n"
        f"检查提示词：\n{prompt}\n\n"
        "待检查文档上下文：\n"
        f"{document_text}"
    )


def _run_payload_with_retries(
    *,
    endpoint: str,
    headers: dict,
    payload: dict,
    proxy_mode: str,
    proxy: Optional[str],
    ssl_verify: bool,
    request_timeout: int,
    on_delta: Optional[Callable[[str], None]],
    on_content: Optional[Callable[[str], None]],
    request_id: str,
    task_id: Optional[int],
    stream_trace_enabled: bool,
) -> str:
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 2):
        attempt_parts = []
        last_content_callback = 0.0
        last_content_snapshot = ""

        def emit_attempt_content(*, force: bool = False):
            nonlocal last_content_callback, last_content_snapshot
            if not on_content:
                return
            now = time.monotonic()
            if not force and now - last_content_callback < _CONTENT_CALLBACK_INTERVAL:
                return
            content = "".join(attempt_parts)
            if content == last_content_snapshot:
                return
            last_content_callback = now
            last_content_snapshot = content
            on_content(content)

        def on_attempt_delta(delta: str):
            attempt_parts.append(delta)
            emit_attempt_content()

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
            if on_content and not attempt_parts and content:
                on_content(content)
            elif on_content and content != last_content_snapshot:
                attempt_parts[:] = [content]
                emit_attempt_content(force=True)
            if on_delta:
                on_delta(content)
            return content
        except LLMError as exc:
            last_error = exc
            if on_content and attempt_parts:
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


def test_model_connection(
    *,
    api_base: str,
    api_key: Optional[str],
    proxy_mode: str = "direct",
    proxy: Optional[str] = None,
    ssl_verify: bool = False,
    request_timeout: int = 30,
    model_name: str,
    force_disable_thinking: bool = False,
) -> str:
    endpoint = _chat_completions_endpoint(api_base)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "请只回复 OK。"}],
        "temperature": 0,
        "max_tokens": 16,
    }
    if force_disable_thinking:
        _disable_thinking_in_payload(payload, api_base=api_base, model_name=model_name)

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

            response = session.post(endpoint, json=payload, **request_kwargs)
            _force_utf8_response(response)
            _raise_for_http_error(response)
            try:
                data = response.json()
            except ValueError:
                return "模型服务已返回 200，但响应不是 JSON。"
            service_error = _extract_service_error(data)
            if service_error:
                raise LLMError(f"模型服务返回错误：{service_error}")
            return "模型连通性测试通过。"
    except requests.ReadTimeout as exc:
        raise LLMError(f"模型服务测试超时：{request_timeout} 秒内没有返回结果") from exc
    except requests.RequestException as exc:
        raise LLMError(f"模型服务测试失败：{exc}") from exc


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


def _disable_thinking_in_payload(payload: dict, *, api_base: str = "", model_name: str = ""):
    payload["enable_thinking"] = False
    payload["chat_template_kwargs"] = {"enable_thinking": False}
    if _uses_deepseek_thinking_toggle(api_base, model_name):
        payload["thinking"] = {"type": "disabled"}


def _uses_deepseek_thinking_toggle(api_base: str, model_name: str) -> bool:
    model = str(model_name or "").strip().lower().replace("_", "-")
    model_id = model.rsplit("/", 1)[-1]
    if model_id in {"deepseek-v4-flash", "deepseek-v4-pro"}:
        return True
    if model_id.startswith("deepseek-v4-"):
        return True

    endpoint = str(api_base or "").strip().lower()
    return "api.deepseek.com/" in endpoint and model_id.startswith("deepseek-")


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
        raise LLMError(_http_error_message(response.status_code, body))


def _http_error_message(status_code: int, body: str) -> str:
    detail = _extract_http_error_detail(body) or body
    if _is_provider_capacity_error(status_code, detail):
        return (
            f"模型服务繁忙或触发限流（HTTP {status_code}）：{_short_text(detail, 500)}。"
            "请稍后重试，或在系统设置中降低系统同时执行任务数、单用户同时执行任务数、单任务检查项并发数。"
        )
    return f"模型服务返回 {status_code}：{detail}"


def _extract_http_error_detail(body: str) -> str:
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return ""
    service_error = _extract_service_error(data)
    if service_error:
        return service_error
    if isinstance(data, dict):
        return _short_text(data.get("message") or data.get("msg") or data)
    return _short_text(data)


def _is_provider_capacity_error(status_code: int, detail: str) -> bool:
    if status_code == 429:
        return True
    text = f"{status_code} {detail}".lower()
    markers = (
        "too many requests",
        "throttled",
        "rate limit",
        "ratelimit",
        "serviceunavailable",
        "service unavailable",
        "capacity",
        "overloaded",
        "限流",
        "频率",
        "请求过多",
        "服务繁忙",
        "容量",
        "过载",
    )
    return any(marker in text for marker in markers)


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
