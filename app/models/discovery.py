from typing import Any
from urllib.parse import urlparse

import requests


class ModelDiscoveryError(Exception):
    pass


def fetch_models(
    *,
    api_base: str,
    api_key: str = "",
    proxy_mode: str = "direct",
    proxy: str = "",
    ssl_verify: bool = False,
    request_timeout: int = 30,
) -> list[str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    candidates = _build_model_endpoint_candidates(api_base)
    errors = []
    with requests.Session() as session:
        session.trust_env = proxy_mode == "system"
        request_kwargs = {
            "headers": headers,
            "timeout": _fetch_timeout(request_timeout),
            "verify": ssl_verify,
            "allow_redirects": False,
        }
        if proxy_mode == "custom":
            if not proxy:
                raise ModelDiscoveryError("自定义代理模式需要填写代理地址")
            session.trust_env = False
            request_kwargs["proxies"] = {"http": proxy, "https": proxy}
        elif proxy_mode != "system":
            session.trust_env = False

        for url in candidates:
            response = None
            try:
                response = session.get(url, **request_kwargs)
                if response.status_code >= 400:
                    errors.append(f"{url} 返回 {response.status_code}")
                    continue
                models = _extract_models_from_payload(response.json())
                if models:
                    return models
                errors.append(f"{url} 未返回模型")
            except requests.RequestException as exc:
                errors.append(f"{url} 请求失败：{exc}")
            except ValueError as exc:
                errors.append(f"{url} 返回的 JSON 无法解析：{exc}")
            finally:
                if response is not None:
                    response.close()

    raise ModelDiscoveryError("；".join(errors) or "拉取模型失败")


def _fetch_timeout(request_timeout: int) -> int:
    try:
        value = int(request_timeout)
    except (TypeError, ValueError):
        value = 30
    return max(5, min(value, 60))


def _build_model_endpoint_candidates(api_base: str) -> list[str]:
    cleaned_api = str(api_base or "").strip().rstrip("/")
    parsed = urlparse(cleaned_api)
    if not parsed.scheme or not parsed.netloc:
        raise ModelDiscoveryError("API 地址必须是完整 URL")
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ModelDiscoveryError("API 地址必须使用 http:// 或 https://")

    root = f"{parsed.scheme.lower()}://{parsed.netloc}"
    base_path = _build_model_endpoint_base_path(parsed.path.rstrip("/"))
    base_url = f"{root}{base_path}"
    return [f"{base_url}/v1/models", f"{base_url}/models"]


def _build_model_endpoint_base_path(path: str) -> str:
    normalized_path = path.rstrip("/")
    if not normalized_path:
        return ""

    lower_path = normalized_path.lower()
    known_suffixes = (
        "/v1/chat/completions",
        "/chat/completions",
        "/v1/completions",
        "/completions",
        "/v1/responses",
        "/responses",
        "/v1/messages",
        "/messages",
        "/v1/models",
        "/models",
        "/v1",
    )
    for suffix in known_suffixes:
        if lower_path.endswith(suffix):
            return normalized_path[: -len(suffix)].rstrip("/")
    return normalized_path


def _extract_models_from_payload(payload: Any) -> list[str]:
    items = None
    if isinstance(payload, dict):
        items = payload.get("data")
        if items is None and isinstance(payload.get("models"), list):
            items = payload.get("models")
    elif isinstance(payload, list):
        items = payload

    if not isinstance(items, list):
        return []

    models = []
    seen = set()
    for item in items:
        if isinstance(item, dict):
            model = item.get("id") or item.get("name")
        else:
            model = item
        model_name = str(model).strip() if model is not None else ""
        if not model_name or model_name in seen:
            continue
        seen.add(model_name)
        models.append(model_name)
    return models
