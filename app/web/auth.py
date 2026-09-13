import hmac
from functools import wraps

from flask import (
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.identity.saml import SamlConfigError, create_saml_auth, saml_sp_metadata
from app.identity.service import (
    SAML_USER_SESSION_KEY,
    AuthenticationRequired,
    UserIdentity,
    current_identity,
    subject_label,
)
from app.persistence.settings import get_ip_username, owner_subject_from_ip
from app.web.common import _current_relative_url, _row_value, _safe_next_path
from app.web.constants import CONSOLE_USER_ENDPOINTS


def register_auth_routes(app):
    admin_prefix = app.config["ADMIN_URL"]

    @app.before_request
    def require_saml_user_session():
        if (
            not _platform_enabled()
            or not _saml_mode_enabled()
            or not _needs_saml_user_session(request.endpoint)
        ):
            return None
        if _has_saml_user_session():
            return None
        return redirect(url_for("saml_login", next=_current_relative_url()))

    @app.get("/auth/saml/login")
    def saml_login():
        if not _saml_mode_enabled():
            abort(404)
        try:
            auth = create_saml_auth()
            redirect_url = auth.login(
                return_to=_safe_next_path(request.args.get("next"))
            )
        except SamlConfigError as error:
            abort(503, description=str(error))
        except Exception:
            current_app.logger.exception("生成 SAML 登录请求失败")
            abort(503, description="SAML 登录配置无效，请联系管理员。")
        session["saml_request_id"] = auth.get_last_request_id()
        return redirect(redirect_url)

    @app.post("/auth/saml/acs")
    def saml_acs():
        if not _saml_mode_enabled():
            abort(404)
        try:
            auth = create_saml_auth()
            request_id = session.pop("saml_request_id", None)
            auth.process_response(request_id=request_id)
        except SamlConfigError as error:
            abort(503, description=str(error))
        except Exception:
            current_app.logger.exception("处理 SAML 回调失败")
            abort(401, description="SAML 登录失败，请重新从公司统一入口访问。")

        if auth.get_errors() or not auth.is_authenticated():
            current_app.logger.warning(
                "SAML 回调校验失败：%s", ", ".join(auth.get_errors())
            )
            abort(401, description="SAML 登录失败，请重新从公司统一入口访问。")

        user_id, username = _saml_user_from_response(auth)
        if not user_id:
            abort(
                401, description="SAML 响应缺少用户 ID，请联系管理员检查 SSO 属性映射。"
            )
        session[SAML_USER_SESSION_KEY] = {
            "user_id": user_id,
            "username": username or user_id,
        }
        return redirect(_safe_next_path(request.form.get("RelayState")))

    @app.get("/auth/saml/metadata")
    def saml_metadata():
        if not _saml_mode_enabled():
            abort(404)
        try:
            metadata = saml_sp_metadata()
        except SamlConfigError as error:
            abort(503, description=str(error))
        except Exception:
            current_app.logger.exception("生成 SAML metadata 失败")
            abort(503, description="SAML SP metadata 配置无效，请联系管理员。")
        return Response(metadata, mimetype="application/samlmetadata+xml")

    @app.post("/auth/saml/logout")
    def saml_logout():
        if not _saml_mode_enabled():
            abort(404)
        session.pop(SAML_USER_SESSION_KEY, None)
        session.pop("saml_request_id", None)
        return redirect(url_for("user_tasks"))

    @app.route(f"{admin_prefix}/login", methods=["GET", "POST"])
    def admin_login():
        if not _platform_enabled():
            return redirect(url_for("user_tasks"))
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
        if not _platform_enabled():
            return redirect(url_for("user_tasks"))
        flash("管理员已退出。", "success")
        return redirect(url_for("admin_login"))


def _identity_label(identity: UserIdentity) -> str:
    if identity.display_name:
        return f"{identity.subject}-{identity.display_name}"
    return identity.label


def _platform_enabled() -> bool:
    return bool(current_app.config.get("PLATFORM", True))


def _auth_mode() -> str:
    auth_config = current_app.config.get("AUTH", {})
    if not isinstance(auth_config, dict):
        return "ip"
    return str(auth_config.get("mode") or "ip").strip().lower()


def _mode_subject_prefix() -> str:
    mode = _auth_mode()
    if mode == "trusted_header":
        return "trusted_header:"
    if mode == "saml":
        return "saml:"
    return "ip:"


def _owner_subject_expr(table_alias: str = "t") -> str:
    prefix = f"{table_alias}." if table_alias else ""
    return f"COALESCE({prefix}owner_subject, 'ip:' || {prefix}ip)"


def _mode_subject_filter(table_alias: str = "t") -> tuple[str, tuple[str]]:
    return f"instr({_owner_subject_expr(table_alias)}, ?) = 1", (
        _mode_subject_prefix(),
    )


def _ip_username_management_enabled() -> bool:
    return _auth_mode() == "ip"


def _saml_mode_enabled() -> bool:
    return _auth_mode() == "saml"


def _is_user_endpoint(endpoint: str | None) -> bool:
    return bool(endpoint and endpoint.startswith("user_"))


def _needs_saml_user_session(endpoint: str | None) -> bool:
    if _is_user_endpoint(endpoint):
        return True
    return bool(endpoint in CONSOLE_USER_ENDPOINTS and session.get("admin_logged_in"))


def _has_saml_user_session() -> bool:
    saml_user = session.get(SAML_USER_SESSION_KEY)
    return isinstance(saml_user, dict) and bool(
        str(saml_user.get("user_id") or "").strip()
    )


def _saml_user_from_response(auth) -> tuple[str, str]:
    saml_config = current_app.config.get("AUTH", {}).get("saml", {})
    user_id_attribute = str(saml_config.get("user_id_attribute") or "").strip()
    username_attribute = str(saml_config.get("username_attribute") or "").strip()
    attributes = auth.get_attributes() or {}
    friendly_attributes = (
        getattr(auth, "get_friendlyname_attributes", lambda: {})() or {}
    )

    if user_id_attribute:
        user_id = _saml_attribute_value(
            attributes, user_id_attribute
        ) or _saml_attribute_value(friendly_attributes, user_id_attribute)
    else:
        user_id = str(auth.get_nameid() or "").strip()
    username = ""
    if username_attribute:
        username = _saml_attribute_value(
            attributes, username_attribute
        ) or _saml_attribute_value(friendly_attributes, username_attribute)
    return user_id, username or user_id


def _saml_attribute_value(attributes: dict, name: str) -> str:
    value = attributes.get(name) if isinstance(attributes, dict) else None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def _current_user_identity() -> UserIdentity:
    try:
        return current_identity(require_sso=True)
    except AuthenticationRequired:
        abort(401, description="未收到 SSO 用户信息，请通过公司统一入口访问。")


def _console_user_identity() -> UserIdentity:
    if _platform_enabled():
        return _current_user_identity()
    return current_identity()


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
        if not _platform_enabled():
            return view(*args, **kwargs)
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped
