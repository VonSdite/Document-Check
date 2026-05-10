import json
from typing import Optional

import requests


class LLMError(Exception):
    pass


def run_check(
    *,
    api_base: str,
    api_key: Optional[str],
    proxy_mode: str = "direct",
    proxy: Optional[str] = None,
    model_name: str,
    check_name: str,
    prompt: str,
    document_text: str,
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
                "json": payload,
                "timeout": 180,
            }
            if proxy_mode == "custom":
                if not proxy:
                    raise LLMError("自定义代理模式需要填写代理地址")
                session.trust_env = False
                request_kwargs["proxies"] = {"http": proxy, "https": proxy}
            elif proxy_mode != "system":
                session.trust_env = False
            response = session.post(endpoint, **request_kwargs)
    except requests.ReadTimeout as exc:
        raise LLMError("模型服务处理超时：已连接到服务，但 180 秒内没有返回结果") from exc
    except requests.RequestException as exc:
        raise LLMError(f"模型服务请求失败：{exc}") from exc

    if response.status_code >= 400:
        body = response.text[:1000]
        raise LLMError(f"模型服务返回 {response.status_code}：{body}")

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise LLMError("模型服务返回了非 JSON 内容") from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("模型服务响应格式不符合 OpenAI Chat Completions 规范") from exc

    return str(content).strip()
