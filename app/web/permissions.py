"""超级管理员的用户权限设置页面。"""

import hmac
import logging
import secrets

from flask import abort, flash, redirect, render_template, request, session, url_for

from app.identity.permissions import ASSIGNABLE_PERMISSIONS, normalize_permissions
from app.persistence.permissions import permission_users, replace_subject_permissions
from app.web.auth import admin_required
from app.web.task_lists import _page_arg, _pagination, _per_page_arg

logger = logging.getLogger(__name__)


def register_permission_routes(app):
    @app.route(f"{app.config['ADMIN_URL']}/permissions", methods=["GET", "POST"])
    @admin_required
    def admin_permissions():
        source = str(request.values.get("source") or "").strip()
        if source not in {"ip", "cookie_session"}:
            source = ""
        keyword = str(request.values.get("keyword") or "").strip()[:200]
        if request.method == "POST":
            token = session.get("permission_csrf_token", "")
            supplied = request.form.get("csrf_token", "")
            if not token or not hmac.compare_digest(token.encode(), supplied.encode()):
                abort(400, description="页面验证已失效，请刷新后保存。")
            subject = str(request.form.get("subject") or "").strip()
            try:
                permissions = normalize_permissions(
                    set(request.form.getlist("permissions"))
                )
            except ValueError as exc:
                abort(400, description=str(exc))
            if not replace_subject_permissions(subject, permissions):
                abort(404, description="用户不存在。")
            logger.info(
                "用户管理权限已更新 subject=%s permissions=%s",
                subject,
                sorted(permissions),
            )
            flash("用户权限已保存。", "success")
            return redirect(
                url_for(
                    "admin_permissions",
                    source=source,
                    keyword=keyword,
                    page=request.form.get("page", "1"),
                    per_page=request.form.get("per_page", "20"),
                )
            )
        per_page = _per_page_arg()
        users, page, total = permission_users(
            source=source, keyword=keyword, page=_page_arg(), per_page=per_page
        )
        token = session.setdefault("permission_csrf_token", secrets.token_urlsafe(32))
        return render_template(
            "admin_permissions.html",
            users=users,
            permissions=ASSIGNABLE_PERMISSIONS,
            source=source,
            keyword=keyword,
            csrf_token=token,
            pagination=_pagination(page, total, per_page),
            active_nav="permissions",
        )
