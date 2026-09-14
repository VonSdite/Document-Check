import logging

from flask import abort, flash, redirect, request, url_for

from app.infrastructure.files import describe_failures
from app.persistence.connection import get_db, now_text
from app.persistence.settings import delete_task_record
from app.tasks.files import (
    _image_folder,
    _remove_empty_directory,
    _remove_uploaded_files,
    _task_upload_paths,
)
from app.tasks.retries import TaskRetryError, request_task_retry
from app.web.auth import (
    _auth_mode,
    _current_user_identity,
    _mode_subject_filter,
    _platform_enabled,
)
from app.web.common import _safe_next_path
from app.web.constants import (
    BULK_DELETABLE_TASK_STATUSES,
    DELETABLE_TASK_STATUSES,
    MAX_BULK_DELETE_TASKS,
)
from app.web.submission import _task_list_endpoint

logger = logging.getLogger(__name__)


def _task_detail_selection(lightweight, include_revision=True):
    columns = (
        "t.id, t.status, t.task_type, t.progress, t.claim_token, t.finished_at"
        if lightweight
        else "t.*, live.result_json AS live_result_json, live.summary AS live_summary"
    )
    if not include_revision:
        return columns
    return (
        columns
        + ", live.updated_at AS live_updated_at, length(live.result_json) AS live_result_size, length(t.result_json) AS result_size"
    )


def _get_task_or_404(task_id: int, *, lightweight=False, include_revision=True):
    selection = _task_detail_selection(lightweight, include_revision)
    join_ip_usernames = _auth_mode() == "ip"
    ip_username_join = (
        "LEFT JOIN ip_usernames iu ON iu.ip = t.ip" if join_ip_usernames else ""
    )
    owner_name_expr = (
        "COALESCE(NULLIF(iu.username, ''), NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '')"
        if join_ip_usernames
        else "COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '')"
    )
    clauses = ["t.id = ?"]
    params: list[object] = [task_id]
    if _platform_enabled():
        mode_clause, mode_params = _mode_subject_filter("t")
        clauses.append(mode_clause)
        params.extend(mode_params)
    task = (
        get_db()
        .execute(
            f"""
        SELECT {selection},
               {owner_name_expr} AS current_owner_name,
               {owner_name_expr} AS current_username,
               COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
        FROM tasks t
        LEFT JOIN task_live_results live ON live.task_id = t.id
        {ip_username_join}
        WHERE {" AND ".join(clauses)}
        """,
            tuple(params),
        )
        .fetchone()
    )
    if task is None:
        abort(404)
    return _task_with_live_result(task)


def _get_user_task(task_id: int, *, lightweight=False, include_revision=True):
    selection = _task_detail_selection(lightweight, include_revision)
    identity = _current_user_identity()
    task = (
        get_db()
        .execute(
            f"""
        SELECT {selection},
               COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_owner_name,
               COALESCE(NULLIF(t.owner_name_snapshot, ''), NULLIF(t.username_snapshot, ''), '') AS current_username,
               COALESCE(t.owner_subject, 'ip:' || t.ip) AS effective_owner_subject
        FROM tasks t
        LEFT JOIN task_live_results live ON live.task_id = t.id
        WHERE t.id = ? AND t.owner_subject = ?
        """,
            (task_id, identity.subject),
        )
        .fetchone()
    )
    if task is None:
        abort(404)
    return _task_with_live_result(task)


def _task_with_live_result(task) -> dict:
    value = dict(task)
    if value.get("status") in {"running", "canceling"}:
        if value.get("live_result_json") is not None:
            value["result_json"] = value["live_result_json"]
        if value.get("status") == "running" and value.get("live_summary") is not None:
            value["summary"] = value["live_summary"]
    value.pop("live_result_json", None)
    value.pop("live_summary", None)
    return value


def _get_user_task_or_local_admin(
    task_id: int, *, lightweight=False, include_revision=True
):
    if not _platform_enabled():
        return _get_task_or_404(
            task_id, lightweight=lightweight, include_revision=include_revision
        )
    return _get_user_task(
        task_id, lightweight=lightweight, include_revision=include_revision
    )


def _cancel_task(task):
    if task["status"] in DELETABLE_TASK_STATUSES:
        return
    db = get_db()
    now = now_text()
    if task["status"] == "queued":
        canceled = db.execute(
            """
            UPDATE tasks
            SET cancel_requested = 1,
                status = 'canceled',
                progress = 0,
                api_key = NULL,
                retry_check_codes_json = NULL,
                claim_token = NULL,
                lease_expires_at = NULL,
                updated_at = ?,
                finished_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, now, task["id"]),
        )
        if canceled.rowcount == 1:
            db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task["id"],))
    else:
        db.execute(
            """
            UPDATE tasks
            SET cancel_requested = 1,
                status = 'canceling',
                summary = ?,
                updated_at = ?
            WHERE id = ? AND status IN ('running', 'canceling')
            """,
            ("正在取消任务，请等待当前请求退出。", now, task["id"]),
        )
    db.commit()


def _retry_task(task) -> bool:
    try:
        retry_count = request_task_retry(task["id"])
    except TaskRetryError as exc:
        flash(str(exc), "error")
        return False
    flash(f"已重新加入队列，将重跑 {retry_count} 个未完成检查项。", "success")
    return True


def _delete_task(task):
    if task["status"] not in DELETABLE_TASK_STATUSES:
        flash("排队中或运行中的任务不能直接删除，请先取消后再删除。", "error")
        return False
    db = get_db()
    paths = _task_upload_paths(task)
    image_dirs = {path.parent for path in paths if _image_folder() in path.parents}
    failures = _remove_uploaded_files(paths)
    if failures:
        logger.warning(
            "删除任务文件失败 task_id=%s failures=%s",
            task["id"],
            "; ".join(f"{path}: {error}" for path, error in failures),
        )
        flash(
            f"任务文件正被其他程序使用，暂时无法删除：{describe_failures(failures)}。"
            "请关闭正在下载、预览或扫描该文件的程序后稍后重试。",
            "error",
        )
        return False
    for image_dir in image_dirs:
        _remove_empty_directory(image_dir)
    delete_task_record(db, task["id"])
    db.commit()
    return True


def _delete_queued_task(task) -> bool | None:
    db = get_db()
    canceled = db.execute(
        """
        UPDATE tasks
        SET cancel_requested = 1,
            status = 'canceled',
            progress = 0,
            api_key = NULL,
            claim_token = NULL,
            lease_expires_at = NULL,
            updated_at = ?,
            finished_at = ?
        WHERE id = ? AND status = 'queued'
        """,
        (now_text(), now_text(), task["id"]),
    )
    if canceled.rowcount != 1:
        db.rollback()
        return None
    db.execute("DELETE FROM task_live_results WHERE task_id = ?", (task["id"],))
    db.commit()

    canceled_task = dict(task)
    canceled_task["status"] = "canceled"
    return _delete_task(canceled_task)


def _bulk_delete_tasks(task_loader, *, admin_created: bool):
    raw_task_ids = request.form.getlist("task_ids")
    if len(raw_task_ids) > MAX_BULK_DELETE_TASKS:
        flash(f"每次最多批量删除 {MAX_BULK_DELETE_TASKS} 个任务。", "error")
        return redirect(
            _task_action_redirect("admin_tasks" if admin_created else "user_tasks")
        )

    task_ids = []
    for raw_task_id in raw_task_ids:
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            continue
        if task_id > 0 and task_id not in task_ids:
            task_ids.append(task_id)

    if not task_ids:
        flash("请先选择需要删除的任务。", "error")
        return redirect(
            _task_action_redirect("admin_tasks" if admin_created else "user_tasks")
        )

    tasks = [task_loader(task_id) for task_id in task_ids]
    fallback_endpoint = _task_list_endpoint(admin_created, tasks[0]["task_type"])
    redirect_url = _task_action_redirect(fallback_endpoint)
    deleted_count = 0
    queued_deleted_count = 0
    skipped_count = 0
    for task in tasks:
        if task["status"] not in BULK_DELETABLE_TASK_STATUSES:
            skipped_count += 1
            continue
        if task["status"] == "queued":
            deleted = _delete_queued_task(task)
            if deleted is None:
                skipped_count += 1
                continue
            if deleted:
                deleted_count += 1
                queued_deleted_count += 1
            continue
        if _delete_task(task):
            deleted_count += 1

    if deleted_count:
        queued_message = (
            f"，其中 {queued_deleted_count} 个排队任务已取消"
            if queued_deleted_count
            else ""
        )
        flash(f"已批量删除 {deleted_count} 个任务{queued_message}。", "success")
    if skipped_count:
        flash(
            f"已跳过 {skipped_count} 个状态已变化或正在运行的任务，请先取消后再删除。",
            "error",
        )
    return redirect(redirect_url)


def _task_action_redirect(default_endpoint: str):
    return _safe_next_path(request.form.get("next"), url_for(default_endpoint))
