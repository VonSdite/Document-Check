import json
from typing import Callable, Optional

import requests


class LLMError(Exception):
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
        "stream": True,
    }

    try:
        with requests.Session() as session:
            session.trust_env = proxy_mode == "system"
            request_kwargs = {
                "headers": headers,
                "json": payload,
                "timeout": request_timeout,
                "stream": True,
            }
            if proxy_mode == "custom":
                if not proxy:
                    raise LLMError("自定义代理模式需要填写代理地址")
                session.trust_env = False
                request_kwargs["proxies"] = {"http": proxy, "https": proxy}
            elif proxy_mode != "system":
                session.trust_env = False
            response = session.post(endpoint, **request_kwargs)
            return _read_stream_response(response, on_delta)
    except requests.ReadTimeout as exc:
        raise LLMError(f"模型服务处理超时：已连接到服务，但 {request_timeout} 秒内没有返回结果") from exc
    except requests.RequestException as exc:
        raise LLMError(f"模型服务请求失败：{exc}") from exc


def _read_stream_response(response, on_delta: Optional[Callable[[str], None]]) -> str:
    if response.status_code >= 400:
        body = response.text[:1000]
        raise LLMError(f"模型服务返回 {response.status_code}：{body}")

    parts = []
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
            raise LLMError("模型服务返回了非 JSON 内容") from exc

        delta = _extract_content_delta(data)
        if not delta:
            continue
        parts.append(delta)
        if on_delta:
            on_delta(delta)

    content = "".join(parts).strip()
    if not content:
        raise LLMError("模型服务没有返回可用内容")
    return content


def _extract_content_delta(data: dict) -> str:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""

    delta = choice.get("delta")
    if isinstance(delta, dict) and delta.get("content") is not None:
        return str(delta["content"])

    message = choice.get("message")
    if isinstance(message, dict) and message.get("content") is not None:
        return str(message["content"])

    text = choice.get("text")
    if text is not None:
        return str(text)
    return ""
