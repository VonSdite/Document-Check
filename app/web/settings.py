import json
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
    url_for,
)

from app.checks.catalog import (
    _check_item_code_prefix,
    _check_item_task_type,
    _check_items_for_task_type,
    _next_check_item_sort_order,
    _reorder_check_items,
)
from app.contracts.limits import MAX_ISSUE_OUTPUT_LIMIT, normalize_issue_output_limit
from app.contracts.task_types import (
    CONSISTENCY_TASK_TYPE,
    DOCUMENT_TASK_TYPE,
    IMAGE_TASK_TYPE,
    LANGUAGE_CONSISTENCY_TASK_TYPE,
    VIDEO_TASK_TYPE,
    task_type_label,
)
from app.documents.images import DEFAULT_PDF_PAGE_IMAGE_MAX_PAGES
from app.infrastructure.config import save_network_config
from app.infrastructure.network import suppress_insecure_request_warning
from app.persistence.connection import get_db, now_text
from app.persistence.defaults import (
    default_check_item_codes,
    reset_default_check_item_prompt,
)
from app.persistence.settings import (
    get_bool_setting,
    get_setting,
    set_ip_username,
    set_setting,
)
from app.tasks.files import _int_setting
from app.tasks.runtime.artifacts import (
    cleanup_task_file_cache,
    task_file_cache_snapshot,
    task_file_cache_snapshot_async,
)
from app.tasks.submission import _consistency_task_title
from app.web.auth import _ip_username_management_enabled, admin_required
from app.web.common import _max_task_processes, _wants_json_response
from app.web.constants import (
    CHECK_ITEM_CONCURRENCY_DEFAULT,
    ISSUE_OUTPUT_LIMIT_DEFAULT,
    TASK_FILE_RETENTION_DAYS_DEFAULT,
)


def register_settings_routes(app):
    admin_prefix = app.config["ADMIN_URL"]

    @app.route(f"{admin_prefix}/prompts", methods=["GET", "POST"])
    @admin_required
    def admin_prompts():
        return redirect(url_for("admin_settings"))

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
        db = get_db()
        if request.method == "POST":
            action = request.form.get("action", "concurrency")
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
                except ValueError:
                    flash(
                        "任务设置必须是整数，任务文件保留天数可为 0，其余必须为正整数。",
                        "error",
                    )
                    return redirect(url_for("admin_settings"))
                max_task_processes = _max_task_processes()
                if not 1 <= global_concurrency <= max_task_processes:
                    flash(
                        f"系统同时执行任务数必须在 1 到 {max_task_processes} 之间。",
                        "error",
                    )
                    return redirect(url_for("admin_settings"))
                set_setting("global_concurrency", global_concurrency)
                set_setting("user_concurrency", user_concurrency)
                set_setting("check_item_concurrency", check_item_concurrency)
                set_setting("image_page_check_max_pages", image_page_check_max_pages)
                set_setting("issue_output_limit", issue_output_limit)
                set_setting("task_file_retention_days", task_file_retention_days)
                flash("任务设置已保存。", "success")
                return redirect(url_for("admin_settings"))

            if action == "diagnostics":
                llm_stream_trace_enabled = (
                    request.form.get("llm_stream_trace_enabled") == "on"
                )
                set_setting("llm_stream_trace_enabled", llm_stream_trace_enabled)
                if _wants_json_response():
                    return {"llm_stream_trace_enabled": llm_stream_trace_enabled}
                flash("定位日志设置已保存。", "success")
                return redirect(url_for("admin_settings"))

            if action == "network":
                proxy_mode = request.form.get("proxy_mode", "direct")
                proxy = request.form.get("proxy", "")
                if proxy_mode == "custom" and not str(proxy or "").strip():
                    flash("自定义代理模式需要填写代理地址。", "error")
                    return redirect(url_for("admin_settings"))
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
                return redirect(url_for("admin_settings"))

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
                    db.execute(
                        "UPDATE report_suppression_rules SET enabled = 1, updated_at = ? WHERE id = ?",
                        (now_text(), int(rule_id)),
                    )
                    db.commit()
                    flash("误报忽略规则已启用。", "success")
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
                name = request.form.get("name", "").strip()
                description = request.form.get("description", "").strip()
                prompt = request.form.get("prompt", "").strip()
                enabled = 1 if request.form.get("enabled") == "on" else 0
                if not name or not prompt:
                    if _wants_json_response():
                        return {
                            "ok": False,
                            "error": "检查项名称和提示词不能为空。",
                        }, 400
                    flash("检查项名称和提示词不能为空。", "error")
                    return redirect(url_for("admin_settings"))
                now = now_text()
                cursor = db.execute(
                    """
                    INSERT INTO check_items(task_type, code, name, description, prompt, enabled, sort_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_type,
                        f"{_check_item_code_prefix(task_type)}-{uuid.uuid4().hex}",
                        name,
                        description,
                        prompt,
                        enabled,
                        _next_check_item_sort_order(db, task_type),
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
                            "_check_item_rows.html", item=item, is_builtin=False
                        ),
                    }
                flash("扩展检查项已创建。", "success")
                return redirect(url_for("admin_settings"))

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
                    return redirect(url_for("admin_settings"))
                _reorder_check_items(db, item_ids, task_type)
                db.commit()
                if request.headers.get("X-Requested-With") == "fetch":
                    return Response(status=204)
                flash("检查项顺序已保存。", "success")
                return redirect(url_for("admin_settings"))

            if action == "delete_check_item":
                item_id = request.form.get("item_id")
                if not item_id or not item_id.isdigit():
                    if _wants_json_response():
                        return {"ok": False, "error": "检查项不存在，无法删除。"}, 400
                    flash("检查项不存在，无法删除。", "error")
                    return redirect(url_for("admin_settings"))
                item = db.execute(
                    "SELECT code FROM check_items WHERE id = ?", (item_id,)
                ).fetchone()
                if item is None:
                    if _wants_json_response():
                        return {"ok": False, "error": "检查项不存在，无法删除。"}, 404
                    flash("检查项不存在，无法删除。", "error")
                    return redirect(url_for("admin_settings"))
                if item["code"] in default_check_item_codes():
                    if _wants_json_response():
                        return {"ok": False, "error": "内置检查项不能删除。"}, 400
                    flash("内置检查项不能删除。", "error")
                    return redirect(url_for("admin_settings"))
                db.execute("DELETE FROM check_items WHERE id = ?", (item_id,))
                db.commit()
                if _wants_json_response():
                    return {
                        "ok": True,
                        "message": "扩展检查项已删除。",
                        "item_id": int(item_id),
                    }
                flash("扩展检查项已删除。", "success")
                return redirect(url_for("admin_settings"))

            if action == "prompt" and request.form.get("reset_prompt") == "1":
                item_id = request.form.get("item_id")
                if not item_id or not item_id.isdigit():
                    flash("检查项不存在，无法重置。", "error")
                    return redirect(url_for("admin_settings"))
                if not reset_default_check_item_prompt(int(item_id)):
                    flash("该检查项没有默认提示词可重置。", "error")
                    return redirect(url_for("admin_settings"))
                flash("检查项提示词已重置为默认内容。", "success")
                return redirect(url_for("admin_settings"))

            if action != "prompt":
                flash("未知设置操作。", "error")
                return redirect(url_for("admin_settings"))

            item_id = request.form.get("item_id")
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            prompt = request.form.get("prompt", "").strip()
            enabled = 1 if request.form.get("enabled") == "on" else 0
            if not item_id or not item_id.isdigit() or not name or not prompt:
                flash("检查项名称和提示词不能为空。", "error")
                return redirect(url_for("admin_settings"))
            if (
                db.execute(
                    "SELECT 1 FROM check_items WHERE id = ?", (item_id,)
                ).fetchone()
                is None
            ):
                flash("检查项不存在，无法保存。", "error")
                return redirect(url_for("admin_settings"))
            db.execute(
                """
                UPDATE check_items
                SET name = ?, description = ?, prompt = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, description, prompt, enabled, now_text(), item_id),
            )
            db.commit()
            flash("检查项提示词已保存。", "success")
            return redirect(url_for("admin_settings"))

        document_check_items = _check_items_for_task_type(db, DOCUMENT_TASK_TYPE)
        consistency_check_items = _check_items_for_task_type(db, CONSISTENCY_TASK_TYPE)
        language_consistency_check_items = _check_items_for_task_type(
            db, LANGUAGE_CONSISTENCY_TASK_TYPE
        )
        image_check_items = _check_items_for_task_type(db, IMAGE_TASK_TYPE)
        video_check_items = _check_items_for_task_type(db, VIDEO_TASK_TYPE)
        settings_tab = _settings_tab()
        report_suppression_keyword, report_suppression_status = (
            _report_suppression_filter_values(request.args)
        )
        return render_template(
            "admin_settings.html",
            check_item_groups=[
                {
                    "task_type": DOCUMENT_TASK_TYPE,
                    "tab_title": "单文档检查",
                    "title": "单文档检查提示词",
                    "description": "内置检查项不可删除；扩展检查项可新增、停用或删除。",
                    "new_title": "新增单文档检查项",
                    "name_placeholder": "例如：术语一致性检查",
                    "description_placeholder": "用于向用户说明该检查项的范围",
                    "prompt_placeholder": "描述该检查项的审查角色、关注范围和输出要求",
                    "items": document_check_items,
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
                    "items": consistency_check_items,
                    "default_check_codes": default_check_item_codes(
                        CONSISTENCY_TASK_TYPE
                    ),
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
                    "items": language_consistency_check_items,
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
                    "items": image_check_items,
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
                    "items": video_check_items,
                    "default_check_codes": default_check_item_codes(VIDEO_TASK_TYPE),
                },
            ],
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
            task_file_retention_days=get_setting(
                "task_file_retention_days", TASK_FILE_RETENTION_DAYS_DEFAULT
            ),
            network=current_app.config["NETWORK"],
            llm_stream_trace_enabled=get_bool_setting(
                "llm_stream_trace_enabled", False
            ),
            settings_tab=settings_tab,
            ip_username_management_enabled=_ip_username_management_enabled(),
            ip_username_rows=_ip_username_rows()
            if _ip_username_management_enabled()
            else [],
            report_suppression_rules=_report_suppression_rule_rows(),
            report_suppression_keyword=report_suppression_keyword,
            report_suppression_status=report_suppression_status,
        )


def _report_suppression_filter_values(values) -> tuple[str, str]:
    keyword = str(values.get("rule_keyword") or "").strip()[:200]
    status = str(values.get("rule_status") or "").strip()
    if status not in {"candidate", "enabled"}:
        status = ""
    return keyword, status


def _report_suppression_rules_redirect():
    keyword, status = _report_suppression_filter_values(request.form)
    url_values = {"_anchor": "report-suppression-rules"}
    if keyword:
        url_values["rule_keyword"] = keyword
    if status:
        url_values["rule_status"] = status
    return redirect(url_for("admin_settings", **url_values))


def _report_suppression_rule_rows() -> list[dict]:
    rows = (
        get_db()
        .execute(
            """
        SELECT r.*, c.name AS check_name
        FROM report_suppression_rules r
        LEFT JOIN check_items c ON c.code = r.check_code
        ORDER BY r.enabled ASC, r.updated_at DESC, r.id DESC
        """
        )
        .fetchall()
    )
    result = []
    for row in rows:
        snapshot = _parse_report_suppression_item_json(row["item_json"])
        result.append(
            {
                "id": row["id"],
                "enabled": bool(row["enabled"]),
                "task_type": row["task_type"],
                "task_type_label": task_type_label(row["task_type"]),
                "check_code": row["check_code"],
                "check_name": row["check_name"] or row["check_code"],
                "reason": row["reason"] or "",
                "hit_count": row["hit_count"],
                "last_hit_at": row["last_hit_at"] or "",
                "source_task_id": row["source_task_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "item": snapshot,
            }
        )
    return result


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
