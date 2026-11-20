from urllib.parse import urlsplit


def normalize_chat_completions_endpoint(api_base: str) -> str:
    """将服务根地址或 API 基础路径补全为 Chat Completions 请求地址。"""
    endpoint = str(api_base or "").strip()
    error = "API 地址必须是有效的 http:// 或 https:// URL。"
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or any(char.isspace() for char in endpoint)
        ):
            raise ValueError(error)
        # 访问端口属性以校验端口格式和范围。
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(error) from exc

    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1/chat/completions"
    elif path.endswith("/chat/completions"):
        pass
    elif path.endswith("/chat"):
        path += "/completions"
    else:
        path += "/chat/completions"
    return parsed._replace(path=path).geturl()
