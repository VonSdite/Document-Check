"""超级管理员的用户权限设置页面。"""

import hmac
import logging
import secrets

from flask import abort, flash, redirect, render_template, request, session, url_for

from app.contracts.user_domains import MAX_DOMAIN_NAME_LENGTH, MAX_USER_DOMAINS
from app.identity.permissions import ASSIGNABLE_PERMISSIONS, normalize_permissions
from app.persistence.permissions import permission_users, replace_subject_permissions
from app.persistence.user_domains import (
    add_user_domains,
    product_domains,
    replace_user_domains,
    user_domains,
)
from app.web.auth import admin_required
from app.web.common import _wants_json_response
from app.web.task_lists import _page_arg, _pagination, _per_page_arg

logger = logging.getLogger(__name__)


def register_permission_routes(app):
    @app.route(f"{app.config['ADMIN_URL']}/permissions", methods=["GET", "POST"])
    @admin_required
    def admin_permissions():
        keyword = str(request.values.get("keyword") or "").strip()[:200]
        domain = str(request.values.get("domain") or "").strip()
        if domain and domain != "unassigned":
            if not domain.isascii() or not domain.isdecimal() or len(domain) > 18:
                abort(400, description="领域筛选无效。")
        if request.method == "POST":
            token = session.get("permission_csrf_token", "")
            supplied = request.form.get("csrf_token", "")
            if not token or not hmac.compare_digest(token.encode(), supplied.encode()):
                abort(400, description="页面验证已失效，请刷新后保存。")
            subject = str(request.form.get("subject") or "").strip()
            action = request.form.get("action", "permissions")
            if action == "add_domains":
                try:
                    assignments = add_user_domains(
                        request.form.getlist("subjects"),
                        request.form.getlist("domains"),
                    )
                except ValueError as exc:
                    if _wants_json_response():
                        return {"error": str(exc)}, 400
                    abort(400, description=str(exc))
                if assignments is None:
                    if _wants_json_response():
                        return {"error": "部分用户已不存在，请刷新后重新选择。"}, 404
                    abort(404, description="部分用户已不存在，请刷新后重新选择。")
                logger.info("用户领域已批量添加 subjects=%s", list(assignments))
                if _wants_json_response():
                    return {
                        "users": [
                            {"subject": key, "domains": value}
                            for key, value in assignments.items()
                        ],
                        "available_domains": product_domains(),
                    }
                flash(f"已为 {len(assignments)} 位用户添加领域。", "success")
                return redirect(
                    url_for(
                        "admin_permissions",
                        keyword=keyword,
                        domain=domain,
                        page=request.form.get("page", "1"),
                        per_page=request.form.get("per_page", "20"),
                    )
                )
            if action == "domains":
                try:
                    found = replace_user_domains(
                        subject, request.form.getlist("domains")
                    )
                except ValueError as exc:
                    if _wants_json_response():
                        return {"error": str(exc)}, 400
                    abort(400, description=str(exc))
                if not found:
                    abort(404, description="用户不存在。")
                domains = user_domains([subject])[subject]
                logger.info("用户领域已更新 subject=%s domains=%s", subject, domains)
                if _wants_json_response():
                    return {
                        "subject": subject,
                        "domains": domains,
                        "available_domains": product_domains(),
                    }
                flash("领域已保存。", "success")
                return redirect(
                    url_for(
                        "admin_permissions",
                        keyword=keyword,
                        domain=domain,
                        page=request.form.get("page", "1"),
                        per_page=request.form.get("per_page", "20"),
                    )
                )
            if action != "permissions":
                abort(400, description="未知的用户设置操作。")
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
            if _wants_json_response():
                return {"subject": subject, "permissions": sorted(permissions)}
            flash("用户权限已保存。", "success")
            return redirect(
                url_for(
                    "admin_permissions",
                    keyword=keyword,
                    domain=domain,
                    page=request.form.get("page", "1"),
                    per_page=request.form.get("per_page", "20"),
                )
            )
        per_page = _per_page_arg()
        users, page, total = permission_users(
            keyword=keyword, page=_page_arg(), per_page=per_page, domain=domain
        )
        token = session.setdefault("permission_csrf_token", secrets.token_urlsafe(32))
        return render_template(
            "admin_permissions.html",
            users=users,
            permissions=ASSIGNABLE_PERMISSIONS,
            keyword=keyword,
            domain=domain,
            domains=product_domains(),
            domain_name_limit=MAX_DOMAIN_NAME_LENGTH,
            user_domain_limit=MAX_USER_DOMAINS,
            csrf_token=token,
            pagination=_pagination(page, total, per_page),
            active_nav="permissions",
        )
