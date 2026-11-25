import json
import unicodedata
import uuid
from ipaddress import ip_address

from flask import (
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app.checks.catalog import (
    _check_item_code_prefix,
    _check_item_task_type,
    _check_items_for_task_type,
    _next_check_item_sort_order,
    _reorder_check_items,
)
from app.contracts.limits import (
    MAX_DOCUMENT_CHECK_ROUNDS,
    MAX_ISSUE_OUTPUT_LIMIT,
    normalize_issue_output_limit,
)
from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
    task_type_label,
)
from app.documents.images import DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES
from app.documents.skill_packages import (
    load_skill_package,
    skill_archive_path,
    skill_metadata,
    store_skill_package,
)
from app.infrastructure.config import save_network_config
from app.infrastructure.network import suppress_insecure_request_warning
from app.persistence.check_item_pool import (
    LOCAL_CHECK_CODES,
    document_check_pool,
    profile_check_ids,
    save_profile_check_items,
)
from app.persistence.check_profiles import (
    check_profiles,
    copy_check_profile,
    save_check_profile,
)
from app.persistence.connection import get_db, now_text
from app.persistence.defaults import (
    default_check_item_codes,
    reset_default_check_item_prompt,
)
from app.persistence.settings import (
    document_check_round_settings,
    get_bool_setting,
    get_setting,
    set_ip_username,
    set_setting,
)
from app.reporting.suppression import (
    VERIFIED_RULE_SQL,
    enable_suppression_rule,
    refresh_suppression_reviews,
    suppression_evidence,
)
from app.tasks.files import _int_setting
from app.tasks.runtime.artifacts import (
    cleanup_task_file_cache,
    task_file_cache_snapshot,
    task_file_cache_snapshot_async,
)
from app.tasks.submission import _consistency_task_title
from app.web.auth import (
    _ip_username_management_enabled,
    admin_required,
    permission_required,
)
from app.web.common import _max_task_processes, _wants_json_response
from app.web.constants import (
    CHECK_ITEM_CONCURRENCY_DEFAULT,
    ISSUE_OUTPUT_LIMIT_DEFAULT,
    TASK_FILE_RETENTION_DAYS_DEFAULT,
)
from app.web.task_lists import _bounded_page, _page_arg, _pagination, _per_page_arg


def register_settings_routes(app):
    admin_prefix = app.config["ADMIN_URL"]

    @app.route(f"{admin_prefix}/prompts", methods=["GET", "POST"])
    @admin_required
    def admin_prompts():
        return redirect(_settings_url())

    @app.get(f"{admin_prefix}/settings/task-cache")
    @admin_required
    def admin_task_file_cache():
        app = current_app._get_current_object()
        if request.args.get("background") == "1":
            snapshot, ready = task_file_cache_snapshot_async(app)
        else:
            snapshot = task_file_cache_snapshot(app)
            ready = True
        items = []
        for item in snapshot["items"]:
            row = dict(item)
            if row["task_type"] in {
                CONSISTENCY_TASK_TYPE,
                LANGUAGE_CONSISTENCY_TASK_TYPE,
            }:
                row["title"] = _consistency_task_title(row)
            else:
                row["title"] = str(row["original_filename"] or f"任务 #{row['id']}")
            row["task_type_label"] = task_type_label(row["task_type"])
            row["report_url"] = url_for("admin_task_detail", task_id=row["id"])
            row.pop("document_meta_json", None)
            items.append(row)
        snapshot["items"] = items
        snapshot["ready"] = ready
        return snapshot

    @app.post(f"{admin_prefix}/settings/task-cache/cleanup")
    @admin_required
    def admin_cleanup_task_file_cache():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("task_ids"), list):
            return {"ok": False, "error": "请选择需要清理的任务。"}, 400
        task_ids = []
        for value in data["task_ids"]:
            try:
                task_id = int(value)
            except (TypeError, ValueError):
                continue
            if task_id > 0 and task_id not in task_ids:
                task_ids.append(task_id)
        if not task_ids:
            return {"ok": False, "error": "请选择需要清理的任务。"}, 400
        return {"ok": True, **cleanup_task_file_cache(current_app, task_ids)}

    @app.route(f"{admin_prefix}/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings():
        return settings_response()

    @app.route(f"{admin_prefix}/rules", methods=["GET", "POST"])
    @admin_required
    def admin_rules():
        return settings_response(rules_only=True)

    @app.route("/rules", methods=["GET", "POST"])
    @permission_required("rules.manage", page=True)
    def user_rules():
        return settings_response(rules_only=True)

    def settings_response(rules_only=False):
        db = get_db()
        rule_actions = {
            "report_suppression_rule",
            "create_check_item",
            "reorder_check_items",
            "delete_check_item",
            "prompt",
            "save_profile",
            "copy_profile",
        }
        profile_id = (
            request.form.get("profile_id", request.args.get("profile_id", "default"))
            if rules_only
            else "default"
        )
        allowed_task_types = (
            {DOCUMENT_TASK_TYPE}
            if rules_only
            else {
                CONSISTENCY_TASK_TYPE,
                LANGUAGE_CONSISTENCY_TASK_TYPE,
                IMAGE_TASK_TYPE,
                VIDEO_TASK_TYPE,
            }
        )
        profile = db.execute(
            "SELECT * FROM check_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if rules_only and profile is None:
            abort(400, description="检查方案不存在，请重新选择。")
        if request.method == "GET" and rules_only and "download_skill" in request.args:
            item = db.execute(
                "SELECT * FROM check_items WHERE id = ? AND task_type = ? AND pool_item_id = id AND check_method = 'skill'",
                (request.args.get("download_skill", type=int), DOCUMENT_TASK_TYPE),
            ).fetchone()
            if item is None:
                abort(404)
            package = skill_metadata(item["skill_package_json"])
            try:
                archive_path = skill_archive_path(package.get("sha256"))
            except ValueError:
                abort(404)
            if not archive_path.is_file():
                abort(
                    404,
                    description="Skill 原始文件包不可用，已保存的检查内容仍可执行。",
                )
            return send_file(
                archive_path,
                as_attachment=True,
                download_name=package.get("filename") or "skill.zip",
            )
        if request.method == "POST" and request.form.get("action") in rule_actions:
            action = request.form.get("action")
            if not rules_only and (
                request.form.get("profile_id", "default") != "default"
                or action in {"save_profile", "copy_profile"}
            ):
                abort(400, description="单文档检查方案请在检查规则页面管理。")
            if action in {"create_check_item", "reorder_check_items"}:
                if (
                    _check_item_task_type(request.form.get("task_type"))
                    not in allowed_task_types
                ):
                    abort(400, description="该检查方式不属于当前页面的管理范围。")
            item_id = request.form.get("item_id")
            if action in {"prompt", "delete_check_item"} and item_id:
                item = db.execute(
                    "SELECT task_type, profile_id, pool_item_id, id FROM check_items WHERE id = ?",
                    (item_id,),
                ).fetchone()
                if (
                    item is None
                    or item["task_type"] not in allowed_task_types
                    or (rules_only and item["pool_item_id"] != item["id"])
                    or (not rules_only and item["profile_id"] != "default")
                ):
                    abort(400, description="检查项不属于当前页面或方案。")
            if action == "report_suppression_rule":
                rule_id = request.form.get("rule_id")
                if rule_id:
                    rule = db.execute(
                        "SELECT task_type FROM report_suppression_rules WHERE id = ? AND profile_id = ?",
                        (rule_id, profile_id),
                    ).fetchone()
                    if rule is None or rule["task_type"] not in allowed_task_types:
                        abort(400, description="误报规则不属于当前页面或方案。")
            if action in {"save_profile", "copy_profile"}:
                try:
                    if action == "copy_profile":
                        profile_id = copy_check_profile(
                            profile_id, request.form.get("name", "")
                        )
                    else:
                        if request.form.get("save_checks") == "1":
                            save_profile_check_items(
                                profile_id, request.form.getlist("profile_checks")
                            )
                        save_check_profile(
                            profile_id,
                            request.form.get("name", ""),
                            request.form.get("description", "").strip(),
                            request.form.get("enabled") == "on",
                        )
                    db.commit()
                except ValueError as exc:
                    db.rollback()
                    flash(str(exc), "error")
                else:
                    flash("检查方案已保存。", "success")
                return redirect(url_for(_rules_endpoint(), profile_id=profile_id))
        if request.method == "POST":
            action = request.form.get("action", "concurrency")
            if rules_only and action not in rule_actions:
                abort(403, description="运行设置仅限超级管理员修改。")
            if action == "concurrency":
                try:
                    global_concurrency = int(
                        request.form.get("global_concurrency", "3")
                    )
                    user_concurrency = max(
                        1, int(request.form.get("user_concurrency", "1"))
                    )
                    check_item_concurrency = max(
                        1,
                        int(
                            request.form.get(
                                "check_item_concurrency",
                                str(CHECK_ITEM_CONCURRENCY_DEFAULT),
                            )
                        ),
                    )
                    image_page_check_max_pages = max(
                        1,
                        int(
                            request.form.get(
                                "image_page_check_max_pages",
                                str(DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES),
                            )
                        ),
                    )
                    issue_output_limit = normalize_issue_output_limit(
                        int(
                            request.form.get(
                                "issue_output_limit", str(ISSUE_OUTPUT_LIMIT_DEFAULT)
                            )
                        )
                    )
                    task_file_retention_days = max(
                        0,
                        int(
                            request.form.get(
                                "task_file_retention_days",
                                str(TASK_FILE_RETENTION_DAYS_DEFAULT),
                            )
                        ),
                    )
                    round_settings = document_check_round_settings()
                    document_check_rounds = int(
                        request.form.get(
                            "document_check_rounds", round_settings["default"]
                        )
                    )
                    document_check_max_rounds = int(
                        request.form.get(
                            "document_check_max_rounds", round_settings["maximum"]
                        )
                    )
                except ValueError:
                    flash(
                        "任务设置必须是整数，任务文件保留天数可为 0，其余必须为正整数。",
                        "error",
                    )
                    return redirect(_settings_url())
                max_task_processes = _max_task_processes()
                if (
                    not 1
                    <= document_check_rounds
                    <= document_check_max_rounds
                    <= MAX_DOCUMENT_CHECK_ROUNDS
                ):
                    flash(
                        f"单文档检查轮数需满足：1 ≤ 默认轮数 ≤ 最大轮数 ≤ {MAX_DOCUMENT_CHECK_ROUNDS}。",
                        "error",
                    )
                    return redirect(_settings_url())
                if not 1 <= global_concurrency <= max_task_processes:
                    flash(
                        f"系统同时执行任务数必须在 1 到 {max_task_processes} 之间。",
                        "error",
                    )
                    return redirect(_settings_url())
                set_setting("global_concurrency", global_concurrency)
                set_setting("user_concurrency", user_concurrency)
                set_setting("check_item_concurrency", check_item_concurrency)
                set_setting("image_page_check_max_pages", image_page_check_max_pages)
                set_setting("issue_output_limit", issue_output_limit)
                set_setting("task_file_retention_days", task_file_retention_days)
                set_setting("document_check_rounds", document_check_rounds)
                set_setting("document_check_max_rounds", document_check_max_rounds)
                flash("任务设置已保存。", "success")
                return redirect(_settings_url())

            if action == "diagnostics":
                llm_stream_trace_enabled = (
                    request.form.get("llm_stream_trace_enabled") == "on"
                )
                set_setting("llm_stream_trace_enabled", llm_stream_trace_enabled)
                if _wants_json_response():
                    return {"llm_stream_trace_enabled": llm_stream_trace_enabled}
                flash("定位日志设置已保存。", "success")
                return redirect(_settings_url())

            if action == "network":
                proxy_mode = request.form.get("proxy_mode", "direct")
                proxy = request.form.get("proxy", "")
                if proxy_mode == "custom" and not str(proxy or "").strip():
                    flash("自定义代理模式需要填写代理地址。", "error")
                    return redirect(_settings_url())
                network = save_network_config(
                    current_app.config["ROOT_DIR"],
                    {
                        "proxy_mode": proxy_mode,
                        "proxy": proxy,
                        "ssl_verify": request.form.get("ssl_verify") == "on",
                    },
                )
                current_app.config["NETWORK"] = network
                suppress_insecure_request_warning(network["ssl_verify"])
                flash("系统出站网络配置已保存。", "success")
                return redirect(_settings_url())

            if action == "ip_username":
                if not _ip_username_management_enabled():
                    abort(404)
                ip = request.form.get("ip", "").strip()
                username = request.form.get("username", "").strip()
                if not _valid_ip(ip):
                    if _wants_json_response():
                        return {"ok": False, "error": "请输入有效的 IP 地址。"}, 400
                    flash("请输入有效的 IP 地址。", "error")
                    return redirect(url_for("admin_settings", tab="ip_users"))
                set_ip_username(ip, username)
                if _wants_json_response():
                    return {"ok": True, "ip": ip, "username": username}
                flash(
                    "IP 用户名已保存。" if username else "IP 用户名已清除。", "success"
                )
                return redirect(url_for("admin_settings", tab="ip_users"))

            if action == "report_suppression_rule":
                rule_id = request.form.get("rule_id", "")
                operation = request.form.get("operation", "")
                if not rule_id.isdigit():
                    flash("误报忽略规则不存在。", "error")
                    return _report_suppression_rules_redirect()
                if operation == "enable":
                    if enable_suppression_rule(int(rule_id)):
                        flash("误报忽略规则已启用。", "success")
                    else:
                        flash("仅有有效终审确认的误报条目可启用。", "error")
                    return _report_suppression_rules_redirect()
                if operation == "disable":
                    db.execute(
                        "UPDATE report_suppression_rules SET enabled = 0, updated_at = ? WHERE id = ?",
                        (now_text(), int(rule_id)),
                    )
                    db.commit()
                    flash("误报忽略规则已停用。", "success")
                    return _report_suppression_rules_redirect()
                if operation == "delete":
                    db.execute(
                        "DELETE FROM report_suppression_rules WHERE id = ?",
                        (int(rule_id),),
                    )
                    db.commit()
                    flash("误报忽略规则已删除。", "success")
                    return _report_suppression_rules_redirect()
                flash("未知误报忽略规则操作。", "error")
                return _report_suppression_rules_redirect()

            if action == "create_check_item":
                task_type = _check_item_task_type(request.form.get("task_type"))
                enabled = 1 if request.form.get("enabled") == "on" else 0
                try:
                    name, description, prompt, check_method, package_json = (
                        _check_item_definition(task_type)
                    )
                except ValueError as exc:
                    if _wants_json_response():
                        return {"ok": False, "error": str(exc)}, 400
                    flash(str(exc), "error")
                    return redirect(_settings_url())
                now = now_text()
                cursor = db.execute(
                    """
                    INSERT INTO check_items(profile_id, task_type, code, name, description, prompt, check_method, skill_package_json, enabled, sort_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "default" if rules_only else profile_id,
                        task_type,
                        f"{_check_item_code_prefix(task_type)}-{uuid.uuid4().hex}",
                        name,
                        description,
                        prompt,
                        check_method,
                        package_json,
                        enabled,
                        _next_check_item_sort_order(db, task_type, profile_id),
                        now,
                        now,
                    ),
                )
                db.commit()
                if _wants_json_response():
                    item = db.execute(
                        "SELECT * FROM check_items WHERE id = ?", (cursor.lastrowid,)
                    ).fetchone()
                    return {
                        "ok": True,
                        "message": "扩展检查项已创建，可继续添加。",
                        "item_id": int(cursor.lastrowid),
                        "html": render_template(
                            "_check_item_rows.html",
                            item=_check_item_view(item),
                            is_builtin=False,
                            rules_only=rules_only,
                            selected_profile=dict(profile) if profile else {},
                        ),
                    }
                flash("扩展检查项已创建。", "success")
                return redirect(_settings_url())

            if action == "reorder_check_items":
                task_type = _check_item_task_type(request.form.get("task_type"))
                item_ids = [
                    int(value)
                    for value in request.form.getlist("item_ids")
                    if value.isdigit()
                ]
                if not item_ids:
                    if request.headers.get("X-Requested-With") == "fetch":
                        return Response("检查项顺序不能为空。", status=400)
                    flash("检查项顺序不能为空。", "error")
                    return redirect(_settings_url())
                valid_ids = {
                    row["id"]
                    for row in _check_items_for_task_type(db, task_type, profile_id)
                }
                if not set(item_ids).issubset(valid_ids):
                    abort(400, description="排序包含其他方案或检查方式的检查项。")
                _reorder_check_items(db, item_ids, task_type, profile_id)
                db.commit()
                if request.headers.get("X-Requested-With") == "fetch":
                    return Response(status=204)
                flash("检查项顺序已保存。", "success")
                return redirect(_settings_url())

            if action == "delete_check_item":
                item_id = request.form.get("item_id")
                if not item_id or not item_id.isdigit():
                    if _wants_json_response():
                        return {"ok": False, "error": "检查项不存在，无法删除。"}, 400
                    flash("检查项不存在，无法删除。", "error")
                    return redirect(_settings_url())
                item = db.execute(
                    "SELECT code, template_code FROM check_items WHERE id = ?",
                    (item_id,),
                ).fetchone()
                if item is None:
                    if _wants_json_response():
                        return {"ok": False, "error": "检查项不存在，无法删除。"}, 404
                    flash("检查项不存在，无法删除。", "error")
                    return redirect(_settings_url())
                if (
                    item["template_code"] or item["code"]
                ) in default_check_item_codes():
                    if _wants_json_response():
                        return {"ok": False, "error": "内置检查项不能删除。"}, 400
                    flash("内置检查项不能删除。", "error")
                    return redirect(_settings_url())
                db.execute("DELETE FROM check_items WHERE id = ?", (item_id,))
                db.commit()
                if _wants_json_response():
                    return {
                        "ok": True,
                        "message": "扩展检查项已删除。",
                        "item_id": int(item_id),
                    }
                flash("扩展检查项已删除。", "success")
                return redirect(_settings_url())

            if action == "prompt" and request.form.get("reset_prompt") == "1":
                item_id = request.form.get("item_id")
                if not item_id or not item_id.isdigit():
                    flash("检查项不存在，无法重置。", "error")
                    return redirect(_settings_url())
                if not reset_default_check_item_prompt(int(item_id)):
                    flash("该检查项没有默认提示词可重置。", "error")
                    return redirect(_settings_url())
                flash("检查项提示词已重置为默认内容。", "success")
                return redirect(_settings_url())

            if action != "prompt":
                flash("未知设置操作。", "error")
                return redirect(_settings_url())

            item_id = request.form.get("item_id")
            enabled = 1 if request.form.get("enabled") == "on" else 0
            if not item_id or not item_id.isdigit():
                flash("检查项名称和提示词不能为空。", "error")
                return redirect(_settings_url())
            current = db.execute(
                "SELECT * FROM check_items WHERE id = ?", (item_id,)
            ).fetchone()
            if current is None:
                flash("检查项不存在，无法保存。", "error")
                return redirect(_settings_url())
            try:
                name, description, prompt, check_method, package_json = (
                    _check_item_definition(current["task_type"], current)
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(_settings_url())
            db.execute(
                """
                UPDATE check_items
                SET name = ?, description = ?, prompt = ?, check_method = ?, skill_package_json = ?, enabled = ?,
                    default_selected = CASE WHEN task_type = 'document_check' THEN default_selected ELSE ? END, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    description,
                    prompt,
                    check_method,
                    package_json,
                    enabled,
                    1 if request.form.get("default_selected", "on") == "on" else 0,
                    now_text(),
                    item_id,
                ),
            )
            db.commit()
            flash("检查项已保存。", "success")
            return redirect(_settings_url())

        check_item_groups = [
            {
                "task_type": DOCUMENT_TASK_TYPE,
                "tab_title": "单文档检查",
                "title": "单文档检查项",
                "description": "内置检查项不可删除；扩展检查项可新增、停用或删除。",
                "new_title": "新增单文档检查项",
                "name_placeholder": "例如：术语一致性检查",
                "description_placeholder": "用于向用户说明该检查项的范围",
                "prompt_placeholder": "描述该检查项的审查角色、关注范围和输出要求",
                "default_check_codes": default_check_item_codes(DOCUMENT_TASK_TYPE),
            },
            {
                "task_type": CONSISTENCY_TASK_TYPE,
                "tab_title": "多文档对照",
                "title": "多文档对照提示词",
                "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交多文档对照任务时可多选。",
                "new_title": "新增多文档对照项",
                "name_placeholder": "例如：关键参数一致性检查",
                "description_placeholder": "用于说明该多文档对照项的比对范围",
                "prompt_placeholder": "描述素材与资料的比对规则、关注范围和输出要求",
                "default_check_codes": default_check_item_codes(CONSISTENCY_TASK_TYPE),
            },
            {
                "task_type": LANGUAGE_CONSISTENCY_TASK_TYPE,
                "tab_title": "跨语种检查",
                "title": "跨语种检查提示词",
                "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交跨语种检查任务时可多选。",
                "new_title": "新增跨语种检查项",
                "name_placeholder": "例如：翻译缺失与事实差异检查",
                "description_placeholder": "用于说明该跨语种检查项的范围",
                "prompt_placeholder": "描述两份不同语种文档的比对规则、关注范围和中文输出要求",
                "default_check_codes": default_check_item_codes(
                    LANGUAGE_CONSISTENCY_TASK_TYPE
                ),
            },
            {
                "task_type": IMAGE_TASK_TYPE,
                "tab_title": "图片检查",
                "title": "图片检查提示词",
                "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交图片检查任务时可多选。",
                "new_title": "新增图片检查项",
                "name_placeholder": "例如：端子标识完整性检查",
                "description_placeholder": "用于说明该图片检查项的范围",
                "prompt_placeholder": "描述图片审查角色、关注范围、判断规则和输出要求",
                "default_check_codes": default_check_item_codes(IMAGE_TASK_TYPE),
            },
            {
                "task_type": VIDEO_TASK_TYPE,
                "tab_title": "视频检查",
                "title": "视频检查提示词",
                "description": "内置检查项不可删除；扩展检查项可新增、停用或删除，提交视频检查任务时可多选。",
                "new_title": "新增视频检查项",
                "name_placeholder": "例如：安装力矩与工具使用检查",
                "description_placeholder": "用于说明该视频检查项的范围",
                "prompt_placeholder": "描述视频质检角色、关注范围、判断规则和输出要求",
                "default_check_codes": default_check_item_codes(VIDEO_TASK_TYPE),
            },
        ]
        check_item_groups = [
            {
                **group,
                "items": [
                    _check_item_view(item)
                    for item in _check_items_for_task_type(
                        db, group["task_type"], profile_id
                    )
                ],
            }
            for group in check_item_groups
            if group["task_type"] in allowed_task_types
        ]
        settings_tab = "general" if rules_only else _settings_tab()
        report_suppression_keyword, report_suppression_status = (
            _report_suppression_filter_values(request.args)
        )
        report_suppression_rules, report_suppression_pagination = (
            _report_suppression_rule_rows(
                report_suppression_keyword,
                report_suppression_status,
                profile_id=profile_id,
                document_only=rules_only,
            )
        )
        return render_template(
            "admin_rules.html" if rules_only else "admin_settings.html",
            profiles=check_profiles() if rules_only else [],
            selected_profile=dict(profile) if profile else {},
            pool_items=document_check_pool(db) if rules_only else [],
            selected_pool_ids=profile_check_ids(profile_id) if rules_only else set(),
            rules_tab="items" if request.args.get("tab") == "items" else "profiles",
            rules_only=rules_only,
            active_nav="rules" if rules_only else "settings",
            check_item_groups=check_item_groups,
            global_concurrency=min(
                _max_task_processes(),
                max(1, _int_setting("global_concurrency", 3)),
            ),
            max_task_processes=_max_task_processes(),
            user_concurrency=get_setting("user_concurrency", 1),
            check_item_concurrency=get_setting(
                "check_item_concurrency", CHECK_ITEM_CONCURRENCY_DEFAULT
            ),
            image_page_check_max_pages=get_setting(
                "image_page_check_max_pages", DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES
            ),
            issue_output_limit=get_setting(
                "issue_output_limit", ISSUE_OUTPUT_LIMIT_DEFAULT
            ),
            max_issue_output_limit=MAX_ISSUE_OUTPUT_LIMIT,
            document_round_settings=document_check_round_settings(),
            max_document_check_rounds=MAX_DOCUMENT_CHECK_ROUNDS,
            task_file_retention_days=get_setting(
                "task_file_retention_days", TASK_FILE_RETENTION_DAYS_DEFAULT
            ),
            network=current_app.config["NETWORK"],
            llm_stream_trace_enabled=get_bool_setting(
                "llm_stream_trace_enabled", False
            ),
            settings_tab=settings_tab,
            ip_username_management_enabled=not rules_only
            and _ip_username_management_enabled(),
            ip_username_rows=_ip_username_rows()
            if not rules_only and _ip_username_management_enabled()
            else [],
            report_suppression_rules=report_suppression_rules,
            report_suppression_pagination=report_suppression_pagination,
            report_suppression_keyword=report_suppression_keyword,
            report_suppression_status=report_suppression_status,
        )


def _report_suppression_filter_values(values) -> tuple[str, str]:
    keyword = str(values.get("rule_keyword") or "").strip()[:200]
    status = str(values.get("rule_status") or "").strip()
    if status not in {"candidate", "enabled", "awaiting_final"}:
        status = ""
    return keyword, status


def _report_suppression_rules_redirect():
    keyword, status = _report_suppression_filter_values(request.form)
    url_values = {
        "_anchor": "report-suppression-rules",
    }
    if request.endpoint in {"admin_rules", "user_rules"}:
        url_values["profile_id"] = request.form.get("profile_id", "default")
    if keyword:
        url_values["rule_keyword"] = keyword
    if status:
        url_values["rule_status"] = status
    for field in ("page", "per_page"):
        value = request.form.get(field, type=int)
        if value and value > 0:
            url_values[field] = value
    return redirect(url_for(_settings_endpoint(), **url_values))


def _report_suppression_rule_rows(
    keyword: str, status: str, *, profile_id: str, document_only: bool
) -> tuple[list[dict], dict]:
    refresh_suppression_reviews()
    db = get_db()
    clauses = [
        "r.profile_id = ?",
        "r.task_type = ?" if document_only else "r.task_type != ?",
    ]
    params = [profile_id, DOCUMENT_TASK_TYPE]
    if status == "awaiting_final":
        clauses.append(f"NOT {VERIFIED_RULE_SQL}")
    elif status:
        clauses.append(f"r.enabled = ? AND {VERIFIED_RULE_SQL}")
        params.append(int(status == "enabled"))
    if keyword:
        terms = unicodedata.normalize("NFKC", keyword).lower().split()

        def matches(check_name, task_type, item_json, reason):
            item = _parse_report_suppression_item_json(item_json)
            text = " ".join(
                str(value or "-")
                for value in (
                    check_name,
                    task_type_label(task_type),
                    item.get("category"),
                    item.get("location"),
                    item.get("description") or item.get("text"),
                    reason,
                )
            )
            text = " ".join(unicodedata.normalize("NFKC", text).lower().split())
            return all(term in text for term in terms)

        db.create_function("report_rule_matches", 4, matches, deterministic=True)
        clauses.append(
            "report_rule_matches(COALESCE(NULLIF(c.name, ''), r.check_code), "
            "r.task_type, r.item_json, r.reason)"
        )
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    source = """FROM report_suppression_rules r LEFT JOIN check_items c ON c.code = r.check_code
        AND c.task_type = r.task_type AND ((r.task_type = 'document_check' AND c.pool_item_id = c.id)
        OR (r.task_type != 'document_check' AND c.profile_id = r.profile_id))"""
    total = db.execute(f"SELECT COUNT(*) {source} {where}", params).fetchone()[0]
    per_page = _per_page_arg()
    page = _bounded_page(_page_arg(), total, per_page)
    rows = db.execute(
        f"""
        SELECT r.*, c.name AS check_name, {VERIFIED_RULE_SQL} AS review_confirmed,
            EXISTS (SELECT 1 FROM tasks WHERE tasks.id = r.source_task_id) AS source_task_exists
        {source} {where}
        ORDER BY r.enabled ASC, r.updated_at DESC, r.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    evidence = suppression_evidence([row["id"] for row in rows])
    result = []
    for row in rows:
        snapshot = _parse_report_suppression_item_json(row["item_json"])
        result.append(
            {
                "id": row["id"],
                "enabled": bool(row["enabled"] and row["review_confirmed"]),
                "review_confirmed": bool(row["review_confirmed"]),
                "evidence": evidence.get(row["id"], []),
                "task_type": row["task_type"],
                "task_type_label": task_type_label(row["task_type"]),
                "check_code": row["check_code"],
                "check_name": row["check_name"] or row["check_code"],
                "reason": row["reason"] or "",
                "hit_count": row["hit_count"],
                "last_hit_at": row["last_hit_at"] or "",
                "source_task_id": row["source_task_id"],
                "source_task_exists": bool(row["source_task_exists"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "item": snapshot,
            }
        )
    return result, _pagination(page, total, per_page)


def _parse_report_suppression_item_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _settings_tab() -> str:
    tab = request.args.get("tab", "general").strip()
    if tab == "ip_users" and _ip_username_management_enabled():
        return tab
    return "general"


def _valid_ip(value: str) -> bool:
    try:
        ip_address(str(value or "").strip())
    except ValueError:
        return False
    return True


def _ip_username_rows():
    return (
        get_db()
        .execute(
            """
        WITH known_ips AS (
            SELECT ip
            FROM tasks
            WHERE ip IS NOT NULL
              AND ip != ''
              AND instr(COALESCE(owner_subject, 'ip:' || ip), 'ip:') = 1
            UNION
            SELECT ip FROM ip_usernames
        )
        SELECT
            k.ip,
            COALESCE(u.username, '') AS username,
            COUNT(t.id) AS task_count,
            MAX(t.created_at) AS last_task_at
        FROM known_ips k
        LEFT JOIN ip_usernames u ON u.ip = k.ip
        LEFT JOIN tasks t
            ON t.ip = k.ip
           AND instr(COALESCE(t.owner_subject, 'ip:' || t.ip), 'ip:') = 1
        GROUP BY k.ip, u.username
        ORDER BY COALESCE(MAX(t.created_at), '') DESC, k.ip ASC
        """
        )
        .fetchall()
    )


def _settings_endpoint():
    if request.endpoint in {"admin_rules", "user_rules"}:
        return request.endpoint
    return "admin_settings"


def _rules_endpoint():
    return "admin_rules" if request.endpoint.startswith("admin_") else "user_rules"


def _check_item_view(item):
    return {**dict(item), "skill_package": skill_metadata(item["skill_package_json"])}


def _check_item_definition(task_type, current=None):
    current = dict(current) if current is not None else {}
    method = request.form.get("check_method", current.get("check_method", "prompt"))
    if method not in {"prompt", "skill"}:
        raise ValueError("请选择提示词或 Skill 包检查方式。")
    if method == "skill" and (
        task_type != DOCUMENT_TASK_TYPE
        or (current.get("template_code") or current.get("code")) in LOCAL_CHECK_CODES
    ):
        raise ValueError(
            "Skill 包适用于单文档的模型检查项，本地规则检查保持当前执行方式。"
        )
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    upload = request.files.get("skill_package")
    package = None
    if method == "skill":
        if upload and upload.filename:
            package = load_skill_package(upload)
            prompt = package.prompt
            package_json = json.dumps(package.metadata, ensure_ascii=False)
            name = name or package.metadata["name"]
            if not current:
                description = description or package.metadata["description"]
        elif current.get("check_method") == "skill":
            prompt = current["prompt"]
            package_json = current["skill_package_json"]
        else:
            raise ValueError("请选择包含 SKILL.md 和参考资料的 Skill 文件包。")
        if not name:
            raise ValueError("请输入检查项名称，或在 SKILL.md 中填写 name。")
    else:
        if upload and upload.filename:
            raise ValueError("上传 Skill 文件包时，请选择 Skill 包检查方式。")
        prompt = request.form.get("prompt", "").strip()
        package_json = "{}"
        if not name or not prompt:
            raise ValueError("检查项名称和提示词不能为空。")
    if package is not None:
        try:
            store_skill_package(package)
        except OSError as exc:
            raise ValueError("Skill 文件包保存失败，请检查本地存储。") from exc
    return name, description, prompt, method, package_json


def _settings_url():
    if request.endpoint in {
        "admin_rules",
        "user_rules",
        "admin_prompts",
    }:
        return url_for(
            _rules_endpoint(),
            tab=request.form.get("tab", request.args.get("tab")),
            profile_id=request.form.get(
                "profile_id", request.args.get("profile_id", "default")
            ),
        )
    return url_for(_settings_endpoint())
