import hmac
import logging
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.identity.service import (
    AuthenticationRequired,
    UserIdentity,
    current_identity,
    subject_label,
)
from app.persistence.settings import get_ip_username, owner_subject_from_ip
from app.web.common import _current_relative_url, _row_value, _safe_next_path
from app.web.constants import CONSOLE_USER_ENDPOINTS

logger = logging.getLogger(__name__)


def register_auth_routes(app):
    admin_prefix = app.config["ADMIN_URL"]

    @app.before_request
    def require_cookie_session_login():
        if not _cookie_session_mode_enabled() or not _needs_cookie_session_login(
            request.endpoint
        ):
            return None
        try:
            current_identity()
        except AuthenticationRequired:
            return _login_required_response()
        return None

    @app.route(f"{admin_prefix}/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            ok = hmac.compare_digest(
                username, current_app.config["ADMIN_USERNAME"]
            ) and hmac.compare_digest(password, current_app.config["ADMIN_PASSWORD"])
            if ok:
                session["admin_logged_in"] = True
                flash("管理员已登录。", "success")
                return redirect(url_for("admin_dashboard"))
            flash("账号或密码不正确。", "error")
        return render_template("admin_login.html")

    @app.post(f"{admin_prefix}/logout")
    def admin_logout():
        session.pop("admin_logged_in", None)
        flash("管理员已退出。", "success")
        return redirect(url_for("admin_login"))


def _identity_label(identity: UserIdentity) -> str:
    if identity.source == "cookie_session":
        return identity.label
    if identity.display_name:
        return f"{identity.subject}-{identity.display_name}"
    return identity.label


def _auth_mode() -> str:
    auth_config = current_app.config.get("AUTH", {})
    if not isinstance(auth_config, dict):
        return "ip"
    return str(auth_config.get("mode") or "ip").strip().lower()


def _ip_username_management_enabled() -> bool:
    return _auth_mode() == "ip"


def _cookie_session_mode_enabled() -> bool:
    return _auth_mode() == "cookie_session"


def _cookie_session_login_url() -> str:
    auth_config = current_app.config.get("AUTH", {})
    if not isinstance(auth_config, dict):
        return ""
    session_config = auth_config.get("cookie_session", {})
    if not isinstance(session_config, dict):
        return ""
    return str(session_config.get("login_url") or "").strip()


def _login_required_response():
    login_url = _cookie_session_login_url()
    message = "未收到有效登录信息，请通过公司统一入口访问。"
    is_fetch = request.headers.get("X-Requested-With") == "fetch" or request.is_json
    target = _safe_next_path(
        request.headers.get("X-Return-To"), _current_relative_url()
    )
    if login_url:
        parts = urlsplit(login_url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "redirect"
        ]
        query.append(("redirect", target))
        login_url = urlunsplit(parts._replace(query=urlencode(query)))
    if is_fetch:
        headers = {"X-Login-URL": login_url} if login_url else {}
        return {"error": message, "login_url": login_url}, 401, headers
    if login_url:
        return redirect(login_url)
    abort(401, description=message)


def _is_user_endpoint(endpoint: str | None) -> bool:
    return bool(endpoint and endpoint.startswith("user_"))


def _needs_cookie_session_login(endpoint: str | None) -> bool:
    # 用户端点必须登录；console 用户端点仅在管理员已登录时要求用户身份。
    if _is_user_endpoint(endpoint):
        return True
    return bool(endpoint in CONSOLE_USER_ENDPOINTS and session.get("admin_logged_in"))


def _current_user_identity() -> UserIdentity:
    try:
        return current_identity()
    except AuthenticationRequired:
        abort(401, description="未收到有效登录信息，请通过公司统一入口访问。")


def _owner_display(task) -> str:
    ip = str(_row_value(task, "ip") or "").strip()
    subject = (
        _row_value(task, "effective_owner_subject")
        or _row_value(task, "owner_subject")
        or owner_subject_from_ip(ip)
    )
    subject = str(subject)
    if subject.startswith("ip:"):
        current_ip_username = _row_value(task, "current_ip_username")
        if current_ip_username:
            return str(current_ip_username)
        if not _row_value(task, "ip_username_lookup_complete", False):
            current_ip_username = get_ip_username(ip or subject[3:])
            if current_ip_username:
                return current_ip_username
    current_owner_name = _row_value(task, "current_owner_name")
    if current_owner_name:
        return str(current_owner_name)
    owner_name_snapshot = _row_value(task, "owner_name_snapshot")
    if owner_name_snapshot:
        return str(owner_name_snapshot)
    username_snapshot = _row_value(task, "username_snapshot")
    if username_snapshot:
        return str(username_snapshot)
    return subject_label(subject)


def _owner_meta(task) -> str:
    ip = str(_row_value(task, "ip") or "").strip()
    subject = (
        _row_value(task, "effective_owner_subject")
        or _row_value(task, "owner_subject")
        or owner_subject_from_ip(ip)
    )
    subject = str(subject)
    if subject.startswith("ip:"):
        subject_ip = subject[3:].strip()
        display = _owner_display(task)
        if display and display not in {subject_ip, ip}:
            return f"IP {ip or subject_ip}"
        return ""
    if subject and ip:
        return f"{subject} · IP {ip}"
    if subject:
        return subject
    if ip:
        return f"IP {ip}"
    return ""


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        if _cookie_session_mode_enabled():
            try:
                current_identity()
            except AuthenticationRequired:
                pass
        return view(*args, **kwargs)

    return wrapped
