from dataclasses import dataclass

from flask import current_app, request


class AuthenticationRequired(Exception):
    pass


@dataclass(frozen=True)
class UserIdentity:
    subject: str
    display_name: str
    source: str
    ip: str

    @property
    def label(self) -> str:
        return self.display_name or subject_label(self.subject)


def current_identity(*, require_sso: bool = False) -> UserIdentity:
    ip = request.remote_addr or "0.0.0.0"
    auth_config = current_app.config.get("AUTH", {})
    if auth_config.get("mode") == "trusted_header":
        identity = _identity_from_trusted_header(auth_config, ip)
        if identity is not None:
            return identity
        if require_sso:
            raise AuthenticationRequired("未收到 SSO 用户信息")
    return UserIdentity(subject=ip_subject(ip), display_name="", source="ip", ip=ip)


def _identity_from_trusted_header(auth_config: dict, ip: str) -> UserIdentity | None:
    header_config = auth_config.get("trusted_header", {})
    user_header = str(header_config.get("user") or "").strip()
    if not user_header:
        return None

    user_id = _header_value(user_header)
    if not user_id:
        return None

    display_name = _header_value(header_config.get("name")) or _header_value(header_config.get("email")) or user_id
    return UserIdentity(subject=f"sso:{user_id}", display_name=display_name, source="sso", ip=ip)


def _header_value(header_name) -> str:
    header_name = str(header_name or "").strip()
    if not header_name:
        return ""
    return str(request.headers.get(header_name) or "").strip()


def ip_subject(ip: str) -> str:
    return f"ip:{str(ip or '0.0.0.0').strip() or '0.0.0.0'}"


def subject_label(subject: str) -> str:
    subject = str(subject or "").strip()
    if subject.startswith("ip:"):
        return subject[3:]
    if subject.startswith("sso:"):
        return subject[4:]
    return subject
