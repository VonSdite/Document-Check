import hmac
import json
import os
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
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from .db import get_db, get_setting, now_text, set_setting
from .documents import allowed_file, extension_of


STATUS_LABELS = {
    "queued": "排队中",
    "running": "检查中",
    "completed": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}


def register_routes(app):
    app.add_template_global(STATUS_LABELS, "STATUS_LABELS")
    app.add_template_global(lambda: app.config["ADMIN_URL"], "admin_url")

    @app.context_processor
    def inject_globals():
        return {
            "status_labels": STATUS_LABELS,
        }

    @app.get("/")
    def user_tasks():
        ip = client_ip()
        user = get_ip_user(ip)
        rows = get_db().execute(
            """
            SELECT *
            FROM tasks
            WHERE ip = ?
            ORDER BY created_at DESC, id DESC
            """,
            (ip,),
        ).fetchall()
        stats = _task_stats(rows)
        return render_template("user_tasks.html", ip=ip, user=user, tasks=rows, stats=stats)

    @app.route("/tasks/new", methods=["GET", "POST"])
    def user_new_task():
        ip = client_ip()
        user = get_ip_user(ip)
        if user and user["is_disabled"]:
            flash("当前 IP 已被管理员禁用，不能提交新的检查任务。", "error")
            return redirect(url_for("user_tasks"))
        if request.method == "POST":
            return create_task_for_ip(ip, user, admin_created=False)
        return render_template(
            "task_form.html",
            mode="user",
            ip=ip,
            user=user,
            check_items=get_enabled_check_items(),
            models=get_enabled_models(),
            admin_prefix=current_app.config["ADMIN_URL"],
        )

    @app.get("/tasks/<int:task_id>")
    def user_task_detail(task_id):
        task = _get_user_task(task_id)
        return render_template("task_detail.html", mode="user", task=task, results=_task_results(task))

    @app.post("/tasks/<int:task_id>/cancel")
    def user_cancel_task(task_id):
        task = _get_user_task(task_id)
        _cancel_task(task)
        flash("已提交取消请求。", "success")
        return redirect(url_for("user_task_detail", task_id=task_id))

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
                return redirect(url_for("admin_dashboard"))
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
        db = get_db()
        totals = {
            "tasks": db.execute("SELECT COUNT(*) AS total FROM tasks").fetchone()["total"],
            "running": db.execute("SELECT COUNT(*) AS total FROM tasks WHERE status = 'running'").fetchone()["total"],
            "queued": db.execute("SELECT COUNT(*) AS total FROM tasks WHERE status = 'queued'").fetchone()["total"],
            "users": db.execute("SELECT COUNT(*) AS total FROM ip_users").fetchone()["total"],
            "models": db.execute(
                """
                SELECT COUNT(*) AS total
                FROM provider_models pm
                JOIN providers p ON p.id = pm.provider_id
                WHERE p.is_active = 1 AND pm.enabled = 1
                """
            ).fetchone()["total"],
        }
        recent_tasks = db.execute(
            """
            SELECT *
            FROM tasks
            ORDER BY created_at DESC, id DESC
            LIMIT 8
            """
        ).fetchall()
        return render_template(
            "admin_dashboard.html",
            totals=totals,
            recent_tasks=recent_tasks,
            global_concurrency=get_setting("global_concurrency", 5),
            user_concurrency=get_setting("user_concurrency", 1),
        )

    @app.route(f"{admin_prefix}/tasks", methods=["GET"])
    @admin_required
    def admin_tasks():
        status = request.args.get("status", "")
        ip = request.args.get("ip", "").strip()
        params = []
        clauses = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if ip:
            clauses.append("ip LIKE ?")
            params.append(f"%{ip}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = get_db().execute(
            f"""
            SELECT *
            FROM tasks
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT 200
            """,
            tuple(params),
        ).fetchall()
        return render_template("admin_tasks.html", tasks=rows, status=status, ip=ip)

    @app.route(f"{admin_prefix}/tasks/new", methods=["GET", "POST"])
    @admin_required
    def admin_new_task():
        target_ip = request.form.get("target_ip", client_ip()).strip() if request.method == "POST" else client_ip()
        user = get_ip_user(target_ip)
        if request.method == "POST":
            return create_task_for_ip(target_ip, user, admin_created=True)
        return render_template(
            "task_form.html",
            mode="admin",
            ip=target_ip,
            user=user,
            check_items=get_enabled_check_items(),
            models=get_enabled_models(),
            admin_prefix=admin_prefix,
        )

    @app.get(f"{admin_prefix}/tasks/<int:task_id>")
    @admin_required
    def admin_task_detail(task_id):
        task = _get_task_or_404(task_id)
        return render_template("task_detail.html", mode="admin", task=task, results=_task_results(task))

    @app.post(f"{admin_prefix}/tasks/<int:task_id>/cancel")
    @admin_required
    def admin_cancel_task(task_id):
        task = _get_task_or_404(task_id)
        _cancel_task(task)
        flash("已提交取消请求。", "success")
        return redirect(url_for("admin_task_detail", task_id=task_id))

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
            is_disabled = 1 if request.form.get("is_disabled") == "on" else 0
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
        db = get_db()
        if request.method == "POST":
            action = request.form.get("action", "save")
            provider_id = request.form.get("provider_id")
            if action == "delete" and provider_id:
                db.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
                db.commit()
                flash("模型提供商已删除。", "success")
                return redirect(url_for("admin_models"))

            name = request.form.get("name", "").strip()
            api_base = request.form.get("api_base", "").strip()
            api_key = request.form.get("api_key", "").strip()
            proxy = request.form.get("proxy", "").strip()
            is_active = 1 if request.form.get("is_active") == "on" else 0
            models_text = request.form.get("models", "")
            model_names = [line.strip() for line in models_text.splitlines() if line.strip()]
            if not name or not api_base:
                flash("提供商名称和 API 地址不能为空。", "error")
                return redirect(url_for("admin_models"))
            if not model_names:
                flash("至少需要填写一个模型名称。", "error")
                return redirect(url_for("admin_models"))

            if provider_id:
                db.execute(
                    """
                    UPDATE providers
                    SET name = ?, api_base = ?, api_key = ?, proxy = ?, is_active = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (name, api_base, api_key, proxy, is_active, now_text(), provider_id),
                )
                pid = int(provider_id)
            else:
                cursor = db.execute(
                    """
                    INSERT INTO providers(name, api_base, api_key, proxy, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, api_base, api_key, proxy, is_active, now_text(), now_text()),
                )
                pid = cursor.lastrowid

            existing = {
                row["model_name"]: row["id"]
                for row in db.execute("SELECT id, model_name FROM provider_models WHERE provider_id = ?", (pid,))
            }
            for model_name in model_names:
                if model_name in existing:
                    db.execute(
                        "UPDATE provider_models SET enabled = 1, updated_at = ? WHERE id = ?",
                        (now_text(), existing[model_name]),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO provider_models(provider_id, model_name, display_name, enabled, created_at, updated_at)
                        VALUES (?, ?, ?, 1, ?, ?)
                        """,
                        (pid, model_name, model_name, now_text(), now_text()),
                    )
            for model_name, model_id in existing.items():
                if model_name not in model_names:
                    db.execute(
                        "UPDATE provider_models SET enabled = 0, updated_at = ? WHERE id = ?",
                        (now_text(), model_id),
                    )
            db.commit()
            flash("模型提供商已保存。", "success")
            return redirect(url_for("admin_models"))

        providers = db.execute(
            """
            SELECT *
            FROM providers
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
        models_by_provider = {
            provider["id"]: db.execute(
                """
                SELECT *
                FROM provider_models
                WHERE provider_id = ?
                ORDER BY enabled DESC, model_name ASC
                """,
                (provider["id"],),
            ).fetchall()
            for provider in providers
        }
        return render_template("admin_models.html", providers=providers, models_by_provider=models_by_provider)

    @app.route(f"{admin_prefix}/prompts", methods=["GET", "POST"])
    @admin_required
    def admin_prompts():
        db = get_db()
        if request.method == "POST":
            item_id = request.form.get("item_id")
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            prompt = request.form.get("prompt", "").strip()
            enabled = 1 if request.form.get("enabled") == "on" else 0
            if not item_id or not name or not prompt:
                flash("检查项名称和提示词不能为空。", "error")
                return redirect(url_for("admin_prompts"))
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
            return redirect(url_for("admin_prompts"))

        items = db.execute("SELECT * FROM check_items ORDER BY sort_order ASC, id ASC").fetchall()
        return render_template("admin_prompts.html", items=items)

    @app.route(f"{admin_prefix}/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings():
        if request.method == "POST":
            try:
                global_concurrency = max(1, int(request.form.get("global_concurrency", "5")))
                user_concurrency = max(1, int(request.form.get("user_concurrency", "1")))
            except ValueError:
                flash("并发度必须是正整数。", "error")
                return redirect(url_for("admin_settings"))
            set_setting("global_concurrency", global_concurrency)
            set_setting("user_concurrency", user_concurrency)
            flash("并发设置已保存。", "success")
            return redirect(url_for("admin_settings"))
        return render_template(
            "admin_settings.html",
            global_concurrency=get_setting("global_concurrency", 5),
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
    return get_db().execute(
        """
        SELECT pm.*, p.name AS provider_name
        FROM provider_models pm
        JOIN providers p ON p.id = pm.provider_id
        WHERE p.is_active = 1 AND pm.enabled = 1
        ORDER BY p.name ASC, pm.model_name ASC
        """
    ).fetchall()


def create_task_for_ip(ip: str, user, *, admin_created: bool):
    db = get_db()
    if not admin_created:
        current_user = get_ip_user(ip)
        if current_user and current_user["is_disabled"]:
            flash("当前 IP 已被管理员禁用，不能提交新的检查任务。", "error")
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
    if not model_id.isdigit():
        flash("请选择可用模型。", "error")
        return _back_to_task_form(admin_created)
    model = db.execute(
        """
        SELECT pm.*, p.name AS provider_name, p.id AS provider_id
        FROM provider_models pm
        JOIN providers p ON p.id = pm.provider_id
        WHERE pm.id = ? AND pm.enabled = 1 AND p.is_active = 1
        """,
        (int(model_id),),
    ).fetchone()
    if model is None:
        flash("所选模型不可用，请重新选择。", "error")
        return _back_to_task_form(admin_created)

    file_type = extension_of(upload.filename)
    safe_name = secure_filename(upload.filename)
    original_filename = safe_name if "." in safe_name else f"document.{file_type}"
    stored_filename = f"{uuid.uuid4().hex}.{file_type}"
    destination = Path(current_app.config["UPLOAD_FOLDER"]) / stored_filename
    upload.save(destination)
    file_size = os.path.getsize(destination)
    username = user["username"] if user and user["username"] else None
    db.execute(
        """
        INSERT INTO tasks(
            ip, username_snapshot, original_filename, stored_filename, file_type, file_size,
            checks_json, provider_id, provider_name, model_id, model_name,
            status, progress, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
        """,
        (
            ip,
            username,
            original_filename,
            stored_filename,
            file_type,
            file_size,
            json.dumps(check_ids, ensure_ascii=False),
            model["provider_id"],
            model["provider_name"],
            model["id"],
            model["model_name"],
            now_text(),
            now_text(),
        ),
    )
    db.commit()
    flash("检查任务已创建，系统会按并发设置自动执行。", "success")
    if admin_created:
        return redirect(url_for("admin_tasks"))
    return redirect(url_for("user_tasks"))


def _back_to_task_form(admin_created: bool):
    if admin_created:
        return redirect(url_for("admin_new_task"))
    return redirect(url_for("user_new_task"))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def _get_task_or_404(task_id: int):
    task = get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        abort(404)
    return task


def _get_user_task(task_id: int):
    task = get_db().execute(
        "SELECT * FROM tasks WHERE id = ? AND ip = ?",
        (task_id, client_ip()),
    ).fetchone()
    if task is None:
        abort(404)
    return task


def _cancel_task(task):
    if task["status"] in {"completed", "failed", "canceled"}:
        return
    db = get_db()
    if task["status"] == "queued":
        db.execute(
            """
            UPDATE tasks
            SET cancel_requested = 1, status = 'canceled', updated_at = ?, finished_at = ?
            WHERE id = ?
            """,
            (now_text(), now_text(), task["id"]),
        )
    else:
        db.execute(
            "UPDATE tasks SET cancel_requested = 1, updated_at = ? WHERE id = ?",
            (now_text(), task["id"]),
        )
    db.commit()


def _delete_task(task):
    if task["status"] == "running":
        flash("运行中的任务不能直接删除，请先取消后再删除。", "error")
        return False
    db = get_db()
    upload_path = Path(current_app.config["UPLOAD_FOLDER"]) / task["stored_filename"]
    if upload_path.exists():
        upload_path.unlink()
    db.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
    db.commit()
    return True


def _task_results(task):
    if not task["result_json"]:
        return []
    try:
        return json.loads(task["result_json"])
    except json.JSONDecodeError:
        return []


def _task_stats(rows):
    stats = {"total": len(rows), "queued": 0, "running": 0, "completed": 0, "failed": 0, "canceled": 0}
    for row in rows:
        if row["status"] in stats:
            stats[row["status"]] += 1
    return stats
