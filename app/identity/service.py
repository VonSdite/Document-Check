import ipaddress
import logging

from flask import current_app, g, request

from app.identity.cookie_session import resolve_userinfo
from app.identity.models import UserIdentity as UserIdentity
from app.identity.models import ip_subject as ip_subject
from app.identity.models import subject_label as subject_label
from app.persistence.settings import (
    get_ip_username,
    migrate_ip_owner_to_subject,
    sync_identity_profile,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class AuthenticationRequired(Exception):
    """cookie_session 模式下未登录或会话失效，需触发登录重定向。"""

    pass


def current_identity() -> UserIdentity:
    if "user_identity" in g:
        if g.user_identity is None:
            raise AuthenticationRequired("未收到有效登录信息")
        return g.user_identity
    ip = (
        _real_ip_header_value("X-Real-IP")
        or _real_ip_header_value("X-Forwarded-For")
        or str(request.remote_addr or "").strip()
        or "0.0.0.0"
    )
    auth_config = current_app.config.get("AUTH", {})
    if auth_config.get("mode") == "cookie_session":
        identity = _identity_from_cookie_session(auth_config, ip)
        g.user_identity = identity
        if identity is not None:
            return identity
        raise AuthenticationRequired("未收到有效登录信息")
    g.user_identity = UserIdentity(
        subject=ip_subject(ip), display_name=get_ip_username(ip), source="ip", ip=ip
    )
    return g.user_identity


def client_ip() -> str:
    header_ip = _real_ip_header_value(current_app.config.get("REAL_IP_HEADER"))
    if header_ip:
        return header_ip
    return str(request.remote_addr or "").strip() or "0.0.0.0"


def _real_ip_header_value(header_name) -> str:
    header_name = str(header_name or "").strip()
    if not header_name:
        return ""
    raw_value = str(request.headers.get(header_name) or "")
    candidate = raw_value.split(",", 1)[0].strip()
    if not candidate:
        return ""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def _identity_from_cookie_session(auth_config: dict, ip: str) -> UserIdentity | None:
    session_config = auth_config.get("cookie_session", {})
    if not isinstance(session_config, dict):
        session_config = {}
    cookie = str(request.headers.get("Cookie") or "").strip()
    if not cookie:
        return None
    user_info, err = resolve_userinfo(session_config, cookie)
    if user_info is None:
        if err is not None:
            logger.warning("cookie_session 身份解析失败：%s", err)
        return None
    user_id = str(user_info.get("user_id") or "").strip()
    if not user_id:
        return None
    username = str(user_info.get("username") or "").strip() or user_id
    avatar = str(user_info.get("avatar") or "").strip()
    subject = f"cookie_session:{user_id}"
    try:
        if migrate_ip_owner_to_subject(ip, subject):
            logger.info("cookie_session 懒迁移完成 ip=%s subject=%s", ip, subject)
    except Exception:
        logger.exception("cookie_session 懒迁移失败 ip=%s subject=%s", ip, subject)
    identity = UserIdentity(
        subject=subject,
        display_name=username,
        source="cookie_session",
        ip=ip,
        avatar=avatar,
        employee_number=str(user_info.get("employee_number") or "").strip(),
        profile_version=int(user_info.get("_profile_version", 0)),
    )
    profile = sync_identity_profile(
        subject,
        {
            "display_name": identity.display_name,
            "employee_number": identity.employee_number,
            "avatar": identity.avatar,
            "label": identity.label,
            "version": identity.profile_version,
        },
    )
    return UserIdentity(
        subject=subject,
        display_name=profile["display_name"],
        source="cookie_session",
        ip=ip,
        avatar=profile["avatar"],
        employee_number=profile["employee_number"],
        profile_version=profile["version"],
    )
