import hmac
import json
import os
import re
import uuid
from functools import wraps
from pathlib import Path

from flask import (
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    Response,
    send_file,
    session,
    url_for,
)

from .config import load_local_config, save_local_config
from .db import (
    default_check_item_codes,
    get_db,
    get_setting,
    now_text,
    reset_default_check_item_prompt,
    set_setting,
)
from .documents import DocumentReadError, allowed_file, extension_of, extract_text


STATUS_LABELS = {
    "queued": "排队中",
    "running": "检查中",
    "completed": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}
TASKS_PER_PAGE = 20
PROXY_MODES = {"direct", "system", "custom"}
PROVIDER_TIMEOUT_DEFAULT = 3600
PROVIDER_TIMEOUT_MIN = 30
PROVIDER_TIMEOUT_MAX = 7200
PROVIDER_INPUT_LIMIT_DEFAULT = 60000
PROVIDER_INPUT_LIMIT_MIN = 5000
PROVIDER_INPUT_LIMIT_MAX = 200000
INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f/\\<>:"|?*]+')


def register_routes(app):
    app.add_template_global(STATUS_LABELS, "STATUS_LABELS")
    app.add_template_global(lambda: app.config["ADMIN_URL"], "admin_url")

    @app.context_processor
    def inject_globals():
        ip = client_ip()
        user = get_ip_user(ip)
        username = user["username"] if user and user["username"] else ""
        return {
            "status_labels": STATUS_LABELS,
            "nav_identity": f"{ip}-{username}" if username else ip,
        }

    @app.route("/", methods=["GET", "POST"])
    def user_tasks():
        ip = client_ip()
        user = get_ip_user(ip)
        if request.method == "POST":
            if user and user["is_disabled"]:
                flash("当前 IP 已被管理员禁用，不能提交任务。", "error")
                return redirect(url_for("user_tasks"))
            return create_task_for_ip(ip, user, admin_created=False)
        page = _page_arg()
        total = get_db().execute(
            "SELECT COUNT(*) AS total FROM tasks WHERE ip = ?",
            (ip,),
        ).fetchone()["total"]
        page = _bounded_page(page, total, TASKS_PER_PAGE)
        rows = get_db().execute(
            """
            SELECT *
            FROM tasks
            WHERE ip = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (ip, TASKS_PER_PAGE, (page - 1) * TASKS_PER_PAGE),
        ).fetchall()
        stats = _task_stats_for_where("ip = ?", (ip,))
        return render_template(
            "user_tasks.html",
            ip=ip,
            user=user,
            tasks=rows,
            stats=stats,
            pagination=_pagination(page, total, TASKS_PER_PAGE),
            check_items=get_enabled_check_items(),
            models=get_enabled_models(),
        )

    @app.route("/tasks/new", methods=["GET", "POST"])
    def user_new_task():
        ip = client_ip()
        user = get_ip_user(ip)
        if user and user["is_disabled"]:
            flash("当前 IP 已被管理员禁用，不能提交任务。", "error")
            return redirect(url_for("user_tasks"))
        if request.method == "POST":
            return create_task_for_ip(ip, user, admin_created=False)
        return redirect(url_for("user_tasks"))

    @app.get("/tasks/<int:task_id>")
    def user_task_detail(task_id):
        task = _get_user_task(task_id)
        return render_template("task_detail.html", mode="user", task=task, results=_task_results(task))

    @app.get("/tasks/<int:task_id>/export")
    def user_export_task(task_id):
        task = _get_user_task(task_id)
        return _export_task_report(task)

    @app.get("/tasks/<int:task_id>/document")
    def user_download_task_document(task_id):
        task = _get_user_task(task_id)
        return _download_task_document(task, "user_task_detail")

    @app.post("/tasks/<int:task_id>/cancel")
    def user_cancel_task(task_id):
        task = _get_user_task(task_id)
        _cancel_task(task)
        flash("已提交取消请求。", "success")
        return redirect(_task_action_redirect("user_tasks"))

    @app.post("/tasks/<int:task_id>/delete")
    def user_delete_task(task_id):
        task = _get_user_task(task_id)
        if _delete_task(task):
            flash("任务已删除。", "success")
        return redirect(url_for("user_tasks"))

    admin_prefix = app.config["ADMIN_URL"]

    @app.route(f"{admin_prefix}/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            ok = hmac.compare_digest(username, current_app.config["ADMIN_USERNAME"]) and hmac.compare_digest(
                password, current_app.config["ADMIN_PASSWORD"]
            )
            if ok:
                session["admin_logged_in"] = True
                flash("管理员已登录。", "success")
                return redirect(url_for("admin_tasks"))
            flash("账号或密码不正确。", "error")
        return render_template("admin_login.html")

    @app.post(f"{admin_prefix}/logout")
    def admin_logout():
        session.pop("admin_logged_in", None)
        flash("管理员已退出。", "success")
        return redirect(url_for("admin_login"))

    @app.get(admin_prefix)
    @admin_required
    def admin_dashboard():
        return redirect(url_for("admin_tasks"))

    @app.route(f"{admin_prefix}/tasks", methods=["GET", "POST"])
    @admin_required
    def admin_tasks():
        if request.method == "POST":
            ip = client_ip()
            return create_task_for_ip(ip, get_ip_user(ip), admin_created=True)

        status = request.args.get("status", "")
        ip = request.args.get("ip", "").strip()
        page = _page_arg()
        params = []
        clauses = []
        if status:
            clauses.append("t.status = ?")
            params.append(status)
        if ip:
            clauses.append("t.ip LIKE ?")
            params.append(f"%{ip}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = get_db().execute(
            f"SELECT COUNT(*) AS total FROM tasks t {where}",
            tuple(params),
        ).fetchone()["total"]
        page = _bounded_page(page, total, TASKS_PER_PAGE)
        rows = get_db().execute(
            f"""
            SELECT t.*, u.username AS current_username
            FROM tasks t
            LEFT JOIN ip_users u ON u.ip = t.ip
            {where}
            ORDER BY t.created_at DESC, t.id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params + [TASKS_PER_PAGE, (page - 1) * TASKS_PER_PAGE]),
        ).fetchall()
        return render_template(
            "admin_tasks.html",
            tasks=rows,
            status=status,
            ip=ip,
            pagination=_pagination(page, total, TASKS_PER_PAGE),
            totals=_admin_totals(),
            global_concurrency=get_setting("global_concurrency", 3),
            user_concurrency=get_setting("user_concurrency", 1),
            check_items=get_enabled_check_items(),
            models=get_enabled_models(),
        )

    @app.route(f"{admin_prefix}/tasks/new", methods=["GET", "POST"])
    @admin_required
    def admin_new_task():
        if request.method == "POST":
            ip = client_ip()
            return create_task_for_ip(ip, get_ip_user(ip), admin_created=True)
        return redirect(url_for("admin_tasks"))

    @app.get(f"{admin_prefix}/tasks/<int:task_id>")
    @admin_required
    def admin_task_detail(task_id):
        task = _get_task_or_404(task_id)
        return render_template("task_detail.html", mode="admin", task=task, results=_task_results(task))

    @app.get(f"{admin_prefix}/tasks/<int:task_id>/export")
    @admin_required
    def admin_export_task(task_id):
        task = _get_task_or_404(task_id)
        return _export_task_report(task)

    @app.get(f"{admin_prefix}/tasks/<int:task_id>/document")
    @admin_required
    def admin_download_task_document(task_id):
        task = _get_task_or_404(task_id)
        return _download_task_document(task, "admin_task_detail")

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/cancel")
    @admin_required
    def admin_cancel_task(task_id):
        task = _get_task_or_404(task_id)
        _cancel_task(task)
        flash("已提交取消请求。", "success")
        return redirect(_task_action_redirect("admin_tasks"))

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/delete")
    @admin_required
    def admin_delete_task(task_id):
        task = _get_task_or_404(task_id)
        if _delete_task(task):
            flash("任务已删除。", "success")
        return redirect(url_for("admin_tasks"))

    @app.route(f"{admin_prefix}/users", methods=["GET", "POST"])
    @admin_required
    def admin_users():
        db = get_db()
        if request.method == "POST":
            action = request.form.get("action", "save")
            ip = request.form.get("ip", "").strip()
            if not ip:
                flash("IP 不能为空。", "error")
                return redirect(url_for("admin_users"))
            if action == "delete":
                db.execute("DELETE FROM ip_users WHERE ip = ?", (ip,))
                db.commit()
                flash("用户标识已删除。", "success")
                return redirect(url_for("admin_users"))

            username = request.form.get("username", "").strip()
            if request.form.get("permission_submitted") == "1":
                is_disabled = 0 if request.form.get("is_enabled") == "1" else 1
            else:
                is_disabled = 0
            db.execute(
                """
                INSERT INTO ip_users(ip, username, is_disabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ip) DO UPDATE SET
                    username = excluded.username,
                    is_disabled = excluded.is_disabled,
                    updated_at = excluded.updated_at
                """,
                (ip, username, is_disabled, now_text(), now_text()),
            )
            db.commit()
            flash("用户标识已保存。", "success")
            return redirect(url_for("admin_users"))

        users = db.execute(
            """
            SELECT u.*,
                   COUNT(t.id) AS task_count,
                   SUM(CASE WHEN t.status = 'running' THEN 1 ELSE 0 END) AS running_count
            FROM ip_users u
            LEFT JOIN tasks t ON t.ip = u.ip
            GROUP BY u.ip
            ORDER BY u.updated_at DESC
            """
        ).fetchall()
        ips = db.execute(
            """
            SELECT t.ip,
                   MAX(t.username_snapshot) AS username_snapshot,
                   COUNT(*) AS task_count,
                   MAX(t.created_at) AS last_task_at
            FROM tasks t
            LEFT JOIN ip_users u ON u.ip = t.ip
            WHERE u.ip IS NULL
            GROUP BY t.ip
            ORDER BY last_task_at DESC
            """
        ).fetchall()
        return render_template("admin_users.html", users=users, ips=ips)

    @app.route(f"{admin_prefix}/models", methods=["GET", "POST"])
    @admin_required
    def admin_models():
        providers = _load_providers()
        if request.method == "POST":
            action = request.form.get("action", "save")
            provider_id = request.form.get("provider_id")
            if action == "delete" and provider_id:
                _save_providers([provider for provider in providers if provider["id"] != provider_id])
                flash("模型提供商已删除。", "success")
                return redirect(url_for("admin_models"))

            name = request.form.get("name", "").strip()
            api_base = request.form.get("api_base", "").strip()
            api_key = request.form.get("api_key", "").strip()
            proxy_mode = request.form.get("proxy_mode", "direct")
            proxy = request.form.get("proxy", "").strip()
            try:
                request_timeout = int(request.form.get("request_timeout", str(PROVIDER_TIMEOUT_DEFAULT)))
            except ValueError:
                flash("超时时间必须是整数秒。", "error")
                return redirect(url_for("admin_models"))
            try:
                max_input_chars = int(request.form.get("max_input_chars", str(PROVIDER_INPUT_LIMIT_DEFAULT)))
            except ValueError:
                flash("文本上限必须是整数。", "error")
                return redirect(url_for("admin_models"))
            is_active = 1 if request.form.get("is_active") == "on" else 0
            models_text = request.form.get("models", "")
            model_names = list(dict.fromkeys(line.strip() for line in models_text.splitlines() if line.strip()))
            if proxy_mode not in PROXY_MODES:
                proxy_mode = "direct"
            if proxy_mode == "custom" and not proxy:
                flash("自定义代理模式需要填写代理地址。", "error")
                return redirect(url_for("admin_models"))
            if proxy_mode != "custom":
                proxy = ""
            if not name or not api_base:
                flash("提供商名称和 API 地址不能为空。", "error")
                return redirect(url_for("admin_models"))
            if request_timeout < PROVIDER_TIMEOUT_MIN or request_timeout > PROVIDER_TIMEOUT_MAX:
                flash(f"超时时间需在 {PROVIDER_TIMEOUT_MIN}-{PROVIDER_TIMEOUT_MAX} 秒之间。", "error")
                return redirect(url_for("admin_models"))
            if max_input_chars < PROVIDER_INPUT_LIMIT_MIN or max_input_chars > PROVIDER_INPUT_LIMIT_MAX:
                flash(f"文本上限需在 {PROVIDER_INPUT_LIMIT_MIN}-{PROVIDER_INPUT_LIMIT_MAX} 字之间。", "error")
                return redirect(url_for("admin_models"))
            if not model_names:
                flash("至少需要填写一个模型名称。", "error")
                return redirect(url_for("admin_models"))

            now = now_text()
            if provider_id:
                existing = next((provider for provider in providers if provider["id"] == provider_id), None)
                if existing is None:
                    flash("模型提供商不存在。", "error")
                    return redirect(url_for("admin_models"))
                saved_provider = _provider_config(
                    provider_id=provider_id,
                    name=name,
                    api_base=api_base,
                    api_key=api_key,
                    proxy_mode=proxy_mode,
                    proxy=proxy,
                    request_timeout=request_timeout,
                    max_input_chars=max_input_chars,
                    is_active=bool(is_active),
                    models=model_names,
                    created_at=existing.get("created_at") or now,
                    updated_at=now,
                )
                providers = [saved_provider if provider["id"] == provider_id else provider for provider in providers]
            else:
                saved_provider = _provider_config(
                    provider_id=uuid.uuid4().hex,
                    name=name,
                    api_base=api_base,
                    api_key=api_key,
                    proxy_mode=proxy_mode,
                    proxy=proxy,
                    request_timeout=request_timeout,
                    max_input_chars=max_input_chars,
                    is_active=bool(is_active),
                    models=model_names,
                    created_at=now,
                    updated_at=now,
                )
                providers.append(saved_provider)
            _save_providers(providers)
            flash("模型提供商已保存。", "success")
            return redirect(url_for("admin_models"))

        providers = sorted(providers, key=lambda provider: (provider["updated_at"], provider["id"]), reverse=True)
        models_by_provider = {
            provider["id"]: [{"model_name": model_name, "enabled": True} for model_name in sorted(provider["models"])]
            for provider in providers
        }
        return render_template("admin_models.html", providers=providers, models_by_provider=models_by_provider)

    @app.route(f"{admin_prefix}/prompts", methods=["GET", "POST"])
    @admin_required
    def admin_prompts():
        return redirect(url_for("admin_settings"))

    @app.route(f"{admin_prefix}/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings():
        db = get_db()
        if request.method == "POST":
            action = request.form.get("action", "concurrency")
            if action == "concurrency":
                try:
                    global_concurrency = max(1, int(request.form.get("global_concurrency", "3")))
                    user_concurrency = max(1, int(request.form.get("user_concurrency", "1")))
                except ValueError:
                    flash("并发度必须是正整数。", "error")
                    return redirect(url_for("admin_settings"))
                set_setting("global_concurrency", global_concurrency)
                set_setting("user_concurrency", user_concurrency)
                flash("并发设置已保存。", "success")
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
            if not item_id or not name or not prompt:
                flash("检查项名称和提示词不能为空。", "error")
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

        items = db.execute("SELECT * FROM check_items ORDER BY sort_order ASC, id ASC").fetchall()
        return render_template(
            "admin_settings.html",
            items=items,
            default_check_codes=default_check_item_codes(),
            global_concurrency=get_setting("global_concurrency", 3),
            user_concurrency=get_setting("user_concurrency", 1),
        )


def client_ip() -> str:
    return request.remote_addr or "0.0.0.0"


def get_ip_user(ip: str):
    return get_db().execute("SELECT * FROM ip_users WHERE ip = ?", (ip,)).fetchone()


def get_enabled_check_items():
    return get_db().execute(
        "SELECT * FROM check_items WHERE enabled = 1 ORDER BY sort_order ASC, id ASC"
    ).fetchall()


def get_enabled_models():
    models = []
    for provider in _load_providers():
        if not provider["is_active"]:
            continue
        for model_name in provider["models"]:
            models.append(_model_option(provider, model_name))
    return sorted(models, key=lambda model: (model["provider_name"], model["model_name"]))


def _load_providers() -> list[dict]:
    return load_local_config(Path(current_app.config["ROOT_DIR"]))["providers"]


def _save_providers(providers: list[dict]):
    root_dir = Path(current_app.config["ROOT_DIR"])
    config = load_local_config(root_dir)
    config["providers"] = providers
    save_local_config(root_dir, config)


def _provider_config(
    *,
    provider_id: str,
    name: str,
    api_base: str,
    api_key: str,
    proxy_mode: str,
    proxy: str,
    request_timeout: int,
    max_input_chars: int,
    is_active: bool,
    models: list[str],
    created_at: str,
    updated_at: str,
) -> dict:
    return {
        "id": provider_id,
        "name": name,
        "api_base": api_base,
        "api_key": api_key,
        "proxy_mode": proxy_mode,
        "proxy": proxy,
        "request_timeout": request_timeout,
        "max_input_chars": max_input_chars,
        "is_active": is_active,
        "models": models,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _model_option(provider: dict, model_name: str) -> dict:
    return {
        "id": f"{provider['id']}:{model_name}",
        "provider_id": provider["id"],
        "provider_name": provider["name"],
        "model_name": model_name,
        "api_base": provider["api_base"],
        "api_key": provider["api_key"],
        "proxy_mode": provider["proxy_mode"],
        "proxy": provider["proxy"],
        "request_timeout": provider["request_timeout"],
        "max_input_chars": provider["max_input_chars"],
    }


def _find_enabled_model(model_id: str) -> dict | None:
    if ":" not in model_id:
        return None
    provider_id, model_name = model_id.split(":", 1)
    for provider in _load_providers():
        if provider["id"] != provider_id or not provider["is_active"]:
            continue
        if model_name not in provider["models"]:
            return None
        return _model_option(provider, model_name)
    return None


def _admin_totals() -> dict:
    db = get_db()
    return {
        "tasks": db.execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"],
        "queued": db.execute("SELECT COUNT(*) AS total FROM tasks WHERE status = 'queued'").fetchone()["total"],
        "running": db.execute("SELECT COUNT(*) AS total FROM tasks WHERE status = 'running'").fetchone()["total"],
        "completed": db.execute("SELECT COUNT(*) AS total FROM tasks WHERE status = 'completed'").fetchone()["total"],
        "ips": db.execute("SELECT COUNT(DISTINCT ip) AS total FROM tasks").fetchone()["total"],
    }


def create_task_for_ip(ip: str, user, *, admin_created: bool):
    db = get_db()
    if not admin_created:
        current_user = get_ip_user(ip)
        if current_user and current_user["is_disabled"]:
            flash("当前 IP 已被管理员禁用，不能提交任务。", "error")
            return redirect(url_for("user_tasks"))

    upload = request.files.get("document")
    if upload is None or not upload.filename:
        flash("请选择要上传的文档。", "error")
        return _back_to_task_form(admin_created)
    if not allowed_file(upload.filename):
        flash("仅支持 docx、pdf、txt、md、html 文件。", "error")
        return _back_to_task_form(admin_created)

    check_ids = [int(value) for value in request.form.getlist("checks") if value.isdigit()]
    if not check_ids:
        flash("请至少选择一个检查项。", "error")
        return _back_to_task_form(admin_created)

    model_id = request.form.get("model_id", "")
    model = _find_enabled_model(model_id)
    if model is None:
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created)

    file_type = extension_of(upload.filename)
    original_filename = _clean_upload_filename(upload.filename, file_type)
    created_at = now_text()
    stored_filename, destination = _upload_destination(original_filename, ip, created_at, file_type)
    upload.save(destination)
    file_size = os.path.getsize(destination)
    try:
        document_text = extract_text(destination, file_type).strip()
    except DocumentReadError as exc:
        _remove_uploaded_file(destination)
        flash(f"文档读取失败：{exc}", "error")
        return _back_to_task_form(admin_created)
    if not document_text:
        _remove_uploaded_file(destination)
        flash("未能从文档中提取到可检查文本。", "error")
        return _back_to_task_form(admin_created)
    if len(document_text) > model["max_input_chars"]:
        _remove_uploaded_file(destination)
        flash(f"文档文本 {len(document_text)} 字，超过当前模型文本上限 {model['max_input_chars']} 字。", "error")
        return _back_to_task_form(admin_created)

    username = user["username"] if user and user["username"] else None
    db.execute(
        """
        INSERT INTO tasks(
            ip, username_snapshot, original_filename, stored_filename, file_type, file_size,
            checks_json, provider_name, model_name, api_base, api_key, proxy_mode, proxy,
            request_timeout, max_input_chars,
            status, progress, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
        """,
        (
            ip,
            username,
            original_filename,
            stored_filename,
            file_type,
            file_size,
            json.dumps(check_ids, ensure_ascii=False),
            model["provider_name"],
            model["model_name"],
            model["api_base"],
            model["api_key"],
            model["proxy_mode"],
            model["proxy"],
            model["request_timeout"],
            model["max_input_chars"],
            created_at,
            created_at,
        ),
    )
    db.commit()
    if admin_created:
        return redirect(url_for("admin_tasks"))
    return redirect(url_for("user_tasks"))


def _back_to_task_form(admin_created: bool):
    if admin_created:
        return redirect(url_for("admin_tasks"))
    return redirect(url_for("user_tasks"))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def _get_task_or_404(task_id: int):
    task = get_db().execute(
        """
        SELECT t.*, u.username AS current_username
        FROM tasks t
        LEFT JOIN ip_users u ON u.ip = t.ip
        WHERE t.id = ?
        """,
        (task_id,),
    ).fetchone()
    if task is None:
        abort(404)
    return task


def _get_user_task(task_id: int):
    task = get_db().execute(
        """
        SELECT t.*, u.username AS current_username
        FROM tasks t
        LEFT JOIN ip_users u ON u.ip = t.ip
        WHERE t.id = ? AND t.ip = ?
        """,
        (task_id, client_ip()),
    ).fetchone()
    if task is None:
        abort(404)
    return task


def _cancel_task(task):
    if task["status"] in {"completed", "failed", "canceled"}:
        return
    db = get_db()
    db.execute(
        """
        UPDATE tasks
        SET cancel_requested = 1,
            status = 'canceled',
            progress = 0,
            updated_at = ?,
            finished_at = ?
        WHERE id = ? AND status NOT IN ('completed', 'failed', 'canceled')
        """,
        (now_text(), now_text(), task["id"]),
    )
    db.commit()


def _delete_task(task):
    if task["status"] == "running":
        flash("运行中的任务不能直接删除，请先取消后再删除。", "error")
        return False
    db = get_db()
    upload_path = _task_upload_path(task)
    if upload_path.exists():
        upload_path.unlink()
    db.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
    db.commit()
    return True


def _task_action_redirect(default_endpoint: str):
    next_url = request.form.get("next", "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return url_for(default_endpoint)


def _download_task_document(task, fallback_endpoint: str):
    upload_path = _task_upload_path(task)
    if not upload_path.is_file():
        flash("文档已删除，无法下载。", "error")
        return redirect(request.referrer or url_for(fallback_endpoint, task_id=task["id"]))
    return send_file(
        upload_path,
        as_attachment=True,
        download_name=task["original_filename"],
    )


def _remove_uploaded_file(path: Path):
    if path.exists():
        path.unlink()


def _task_upload_path(task) -> Path:
    return Path(current_app.config["UPLOAD_FOLDER"]) / Path(task["stored_filename"]).name


def _clean_upload_filename(filename: str, file_type: str) -> str:
    name = Path(filename.replace("\\", "/")).name.strip()
    name = _safe_filename_part(name, f"document.{file_type}")
    if "." not in name:
        name = f"{name}.{file_type}"
    return name


def _upload_destination(original_filename: str, ip: str, created_at: str, file_type: str) -> tuple[str, Path]:
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    timestamp = re.sub(r"\D", "", created_at) or now_text().replace("-", "").replace(":", "").replace(" ", "")
    stem = _limit_utf8_bytes(_safe_filename_part(Path(original_filename).stem, "document"), 140)
    ip_part = _limit_utf8_bytes(_safe_filename_part(ip, "0.0.0.0"), 80)
    stored_filename = f"{stem}_{ip_part}_{timestamp}.{file_type}"
    destination = upload_dir / stored_filename
    if not destination.exists():
        return stored_filename, destination

    for index in range(2, 1000):
        candidate = f"{stem}_{ip_part}_{timestamp}-{index}.{file_type}"
        destination = upload_dir / candidate
        if not destination.exists():
            return candidate, destination
    raise RuntimeError("无法保存上传文档，请稍后再试")


def _safe_filename_part(value: str, fallback: str) -> str:
    value = INVALID_FILENAME_CHARS.sub("_", value).strip(" ._")
    value = re.sub(r"_+", "_", value)
    return value or fallback


def _limit_utf8_bytes(value: str, max_bytes: int) -> str:
    while len(value.encode("utf-8")) > max_bytes:
        value = value[:-1]
    return value or "document"


def _task_results(task):
    if not task["result_json"]:
        return []
    try:
        return json.loads(task["result_json"])
    except json.JSONDecodeError:
        return []


def _export_task_report(task):
    app_css = (Path(current_app.static_folder) / "app.css").read_text(encoding="utf-8")
    html = render_template(
        "task_report_export.html",
        task=task,
        results=_task_results(task),
        app_css=app_css,
    )
    filename = f"document-check-report-{task['id']}.html"
    return Response(
        html,
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _page_arg() -> int:
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return 1
    return max(1, page)


def _bounded_page(page: int, total: int, per_page: int) -> int:
    pages = max(1, (total + per_page - 1) // per_page)
    return min(max(1, page), pages)


def _pagination(page: int, total: int, per_page: int) -> dict:
    pages = max(1, (total + per_page - 1) // per_page)
    return {
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < pages,
        "prev_page": max(1, page - 1),
        "next_page": min(pages, page + 1),
        "start": 0 if total == 0 else (page - 1) * per_page + 1,
        "end": min(total, page * per_page),
    }


def _task_stats_for_where(where: str, params: tuple) -> dict:
    stats = {"total": 0, "queued": 0, "running": 0, "completed": 0, "failed": 0, "canceled": 0}
    rows = get_db().execute(
        f"SELECT status, COUNT(*) AS total FROM tasks WHERE {where} GROUP BY status",
        params,
    ).fetchall()
    for row in rows:
        count = row["total"]
        stats["total"] += count
        if row["status"] in stats:
            stats[row["status"]] = count
    return stats
