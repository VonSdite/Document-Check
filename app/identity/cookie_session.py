"""通过外部用户信息接口验证 Cookie，按有效期和故障宽限期缓存身份。"""

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass

import requests
from flask import current_app

logger = logging.getLogger(__name__)


class CookieSessionExpired(Exception):
    """外部接口确认会话失效。"""


class CookieSessionTransient(Exception):
    """外部接口暂时不可用。"""


class CookieSessionNoCache(Exception):
    """外部接口不可用且没有宽限期内的身份缓存。"""


@dataclass
class _CacheEntry:
    user_info: dict | None
    expires_at: float
    discard_at: float
    retry_at: float = 0
    error: Exception | None = None


_CACHE_LIMIT = 4096
_RETRY_SECONDS = 5
_CACHE_CLEANUP_SECONDS = 60
_cache: OrderedDict[str, _CacheEntry] = OrderedDict()
_inflight: dict[str, Future] = {}
_cache_lock = threading.Lock()
_next_cleanup = 0.0


def resolve_userinfo(
    session_config: dict, cookie: str
) -> tuple[dict | None, Exception | None]:
    cookie_key = _cache_key(cookie)
    with _cache_lock:
        now = time.monotonic()
        _prune_cache(now)
        cached = _cache.get(cookie_key)
        if cached is not None and now >= cached.discard_at:
            del _cache[cookie_key]
            cached = None
        if cached is not None:
            _cache.move_to_end(cookie_key)
            if now < cached.expires_at:
                return cached.user_info, None
            if now < cached.retry_at:
                return cached.user_info, cached.error
        flight = _inflight.get(cookie_key)
        owns_query = flight is None
        if owns_query:
            flight = Future()
            _inflight[cookie_key] = flight

    if not owns_query:
        return flight.result()

    try:
        userinfo_url = str(session_config.get("userinfo_url") or "").strip()
        if userinfo_url:
            user_info, error = _fetch_userinfo(session_config, userinfo_url, cookie)
        else:
            user_info, error = None, CookieSessionTransient("userinfo_url 未配置")
        with _cache_lock:
            now = time.monotonic()
            entry = _entry_for_result(session_config, cached, user_info, error, now)
            if _inflight.get(cookie_key) is flight:
                _cache[cookie_key] = entry
                _cache.move_to_end(cookie_key)
                while len(_cache) > _CACHE_LIMIT:
                    _cache.popitem(last=False)
            result = (entry.user_info, entry.error)
        flight.set_result(result)
        return result
    except BaseException as error:
        flight.set_exception(error)
        raise
    finally:
        with _cache_lock:
            if _inflight.get(cookie_key) is flight:
                del _inflight[cookie_key]


def _entry_for_result(session_config, cached, user_info, error, now):
    if user_info is not None:
        ttl = int(session_config.get("cache_ttl", 600))
        grace = int(session_config.get("cache_grace", 600))
        return _CacheEntry(user_info, now + ttl, now + ttl + grace)
    if isinstance(error, CookieSessionExpired):
        return _CacheEntry(None, now, now + _RETRY_SECONDS, now + _RETRY_SECONDS, error)
    if cached is not None and cached.user_info is not None and now < cached.discard_at:
        return _CacheEntry(
            cached.user_info,
            cached.expires_at,
            cached.discard_at,
            min(now + _RETRY_SECONDS, cached.discard_at),
            CookieSessionTransient(f"宽限期降级：{error}"),
        )
    return _CacheEntry(
        None,
        now,
        now + _RETRY_SECONDS,
        now + _RETRY_SECONDS,
        CookieSessionNoCache(f"无可用缓存：{error}"),
    )


def _prune_cache(now: float) -> None:
    global _next_cleanup
    if now < _next_cleanup:
        return
    for key in [key for key, entry in _cache.items() if now >= entry.discard_at]:
        del _cache[key]
    _next_cleanup = now + _CACHE_CLEANUP_SECONDS


def clear_cache_for_cookie(cookie: str) -> None:
    cookie_key = _cache_key(cookie)
    with _cache_lock:
        _cache.pop(cookie_key, None)
        _inflight.pop(cookie_key, None)


def _fetch_userinfo(
    session_config: dict,
    userinfo_url: str,
    cookie: str,
) -> tuple[dict | None, Exception | None]:
    cookie_header_name = str(
        session_config.get("cookie_header_name") or "cookie"
    ).strip()
    timeout = int(session_config.get("timeout") or 3)
    ssl_verify = bool(session_config.get("ssl_verify", False))

    headers = {cookie_header_name: cookie}
    profile_version = time.time_ns()

    for attempt in range(2):
        try:
            with _build_http_session() as http_session:
                response = http_session.get(
                    userinfo_url,
                    headers=headers,
                    timeout=timeout,
                    verify=ssl_verify,
                    allow_redirects=False,
                )
        except requests.RequestException as exc:
            if attempt == 0:
                logger.warning("cookie_session 请求失败，重试一次：%s", exc)
                continue
            return None, CookieSessionTransient(f"请求失败：{exc}")
        user_info, error = _parse_response(response, session_config)
        if user_info is not None:
            user_info["_profile_version"] = profile_version
        return user_info, error
    return None, CookieSessionTransient("请求失败且重试耗尽")


def _build_http_session() -> requests.Session:
    network_config = current_app.config.get("NETWORK", {})
    network = network_config if isinstance(network_config, dict) else {}
    proxy_mode = str(network.get("proxy_mode") or "direct").strip().lower()
    session = requests.Session()
    session.trust_env = proxy_mode == "system"
    if proxy_mode == "custom":
        proxy = str(network.get("proxy") or "").strip()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
    return session


def _parse_response(
    response: requests.Response, session_config: dict
) -> tuple[dict | None, Exception | None]:
    status_code = response.status_code
    if status_code in (401, 403):
        logger.warning("cookie_session 会话失效 status=%s", status_code)
        return None, CookieSessionExpired(f"外部接口返回 {status_code}")

    if not 200 <= status_code < 300:
        return None, CookieSessionTransient(f"外部接口返回 {status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        return None, CookieSessionTransient(f"响应非 JSON：{exc}")

    if not isinstance(payload, dict):
        return None, CookieSessionTransient("响应不是对象")

    user_data, err = _extract_user_data(payload)
    if err is not None:
        return None, err

    user_info = _apply_mapping(user_data, session_config)
    if user_info is None:
        return None, CookieSessionTransient("字段映射未取到 user_id")
    return user_info, None


def _extract_user_data(payload: dict) -> tuple[dict | None, Exception | None]:
    status = str(payload.get("status") or "").strip()
    if status:
        if status in ("401", "403"):
            return None, CookieSessionExpired("外部接口状态表示会话失效")
        if status.lower() != "success":
            return None, CookieSessionTransient(f"外部接口状态异常：{status}")
        data = payload.get("data")
        if isinstance(data, dict):
            return data, None
        return None, CookieSessionTransient("data 不是对象")
    return payload, None


def _apply_mapping(user_data: dict, session_config: dict) -> dict | None:
    field_mapping = session_config.get("field_mapping") or {}
    user_id_field = str(field_mapping.get("user_id") or "").strip()
    if not user_id_field:
        return None
    user_id = str(user_data.get(user_id_field) or "").strip()
    if not user_id:
        return None

    username_field = str(field_mapping.get("username") or "").strip()
    username = (
        str(user_data.get(username_field) or "").strip() if username_field else ""
    )
    username = username or user_id

    avatar = _resolve_avatar(session_config, user_data, user_id)
    employee_field = str(field_mapping.get("employee_number") or "").strip()
    employee_number = (
        str(user_data.get(employee_field) or "").strip() if employee_field else ""
    )

    return {
        "user_id": user_id,
        "username": username,
        "employee_number": employee_number,
        "avatar": avatar,
    }


def _resolve_avatar(session_config: dict, user_data: dict, user_id: str) -> str:
    avatar_template = str(session_config.get("avatar_url_template") or "").strip()
    if not avatar_template:
        return ""

    avatar_field = str(session_config.get("avatar_field") or "").strip()
    token_value = user_id
    if avatar_field:
        field_value = str(user_data.get(avatar_field) or "").strip()
        if field_value:
            token_value = field_value

    if not token_value:
        return ""
    try:
        return avatar_template.replace("{user_id}", token_value)
    except (TypeError, ValueError):
        return ""


def _cache_key(cookie: str) -> str:
    return hashlib.sha256(cookie.encode("utf-8")).hexdigest()
