from dataclasses import dataclass

from flask import current_app, request, session

from .db import get_ip_username


class AuthenticationRequired(Exception):
    pass


SAML_USER_SESSION_KEY = "saml_user"


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
    if auth_config.get("mode") == "saml":
        identity = _identity_from_saml_session(ip)
        if identity is not None:
            return identity
        if require_sso:
            raise AuthenticationRequired("未收到 SSO 用户信息")
    return UserIdentity(subject=ip_subject(ip), display_name=get_ip_username(ip), source="ip", ip=ip)


def _identity_from_trusted_header(auth_config: dict, ip: str) -> UserIdentity | None:
    header_config = auth_config.get("trusted_header", {})
    user_id_header = str(header_config.get("user_id") or "").strip()
    if not user_id_header:
        return None

    user_id = _header_value(user_id_header)
    if not user_id:
        return None

    display_name = _header_value(header_config.get("username")) or user_id
    return UserIdentity(subject=f"trusted_header:{user_id}", display_name=display_name, source="trusted_header", ip=ip)


def _identity_from_saml_session(ip: str) -> UserIdentity | None:
    saml_user = session.get(SAML_USER_SESSION_KEY)
    if not isinstance(saml_user, dict):
        return None
    user_id = str(saml_user.get("user_id") or "").strip()
    if not user_id:
        return None
    display_name = str(saml_user.get("username") or "").strip() or user_id
    return UserIdentity(subject=f"saml:{user_id}", display_name=display_name, source="saml", ip=ip)


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
    if subject.startswith("trusted_header:"):
        return subject[15:]
    if subject.startswith("saml:"):
        return subject[5:]
    return subject
