"""专项 skill 编辑、报告案例录入与独立验证任务入口。"""

import json

from flask import abort, flash, redirect, render_template, request, url_for

from app.checks.skills import (
    CASE_FIELDS,
    SKILL_FIELDS,
    check_snapshot,
    compile_skill_prompt,
    delete_case,
    get_skill,
    get_skill_case,
    record_case_validation,
    save_case,
    save_skill,
    select_skill_examples,
    selected_examples,
    skill_cases,
    skill_definition,
)
from app.contracts.task_types import DOCUMENT_TASK_TYPE
from app.models.service import get_enabled_models
from app.persistence.connection import get_db
from app.reporting.service import _raw_task_results, _result_report_items
from app.tasks.files import _remove_uploaded_files
from app.tasks.submission import submit_skill_case_validation
from app.web.auth import (
    _current_user_identity,
    has_permission,
    is_superadmin,
    permission_required,
)
from app.web.constants import STATUS_LABELS
from app.web.task_actions import _get_task_or_404, _get_user_task


def register_skill_routes(app):
    @app.route("/rules/skills/<int:skill_id>", methods=["GET", "POST"])
    @permission_required("rules.manage", page=True)
    def user_skill(skill_id):
        return skill_response(skill_id)

    @app.route(
        f"{app.config['ADMIN_URL']}/rules/skills/<int:skill_id>",
        methods=["GET", "POST"],
    )
    @permission_required("rules.manage", page=True)
    def admin_skill(skill_id):
        return skill_response(skill_id)


def _report_source(values):
    task_id = values.get("source_task_id")
    if not task_id:
        return None, {}
    if not str(task_id).isdigit():
        abort(400, description="来源任务编号无效。")
    task = (_get_task_or_404 if is_superadmin() else _get_user_task)(int(task_id))
    if task["task_type"] != DOCUMENT_TASK_TYPE:
        abort(400, description="专项案例使用单文档任务证据。")
    code = values.get("source_result_code", "")
    item_id = values.get("source_item_id", "")
    result = next((r for r in _raw_task_results(task) if r.get("code") == code), None)
    report_item = next(
        (item for item in _result_report_items(result or {}) if item["id"] == item_id),
        None,
    )
    if report_item is None:
        abort(404, description="来源报告条目不存在。")
    source = {
        "source_task_id": int(task_id),
        "source_result_code": code,
        "source_item_id": item_id,
    }
    prefill = {
        "title": report_item.get("category") or "待确认的问题案例",
        "reference": task["original_filename"],
        "location": report_item.get("location", ""),
        "evidence": report_item.get("excerpt", ""),
        "expected_result": report_item.get("description", ""),
        "document_text": task.get("document_text") or "",
        "case_kind": "positive",
    }
    return source, prefill


def skill_response(skill_id):
    identity = _current_user_identity()
    db = get_db()
    if request.method == "POST" and not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    item = get_skill(db, skill_id)
    if item is None:
        abort(404, description="专项 skill 不存在。")
    endpoint = "admin_skill" if is_superadmin() else "user_skill"
    rules_endpoint = "admin_rules" if is_superadmin() else "user_rules"
    status = 200
    definition = skill_definition(item)
    source, prefill = _report_source({**request.args, **request.form})
    case_form = prefill
    validation_upload_paths = []
    if request.method == "POST":
        action = request.form.get("action")
        try:
            expected_version = request.form.get("version")
            if expected_version and expected_version != str(item["version"]):
                raise ValueError("专项规则已更新，请刷新页面核对最新版本后保存。")
            if action == "save_skill":
                save_skill(db, item, request.form)
                flash("专项规则与版本已保存。", "success")
            elif action == "select_examples":
                select_skill_examples(
                    db, item, request.form.getlist("example_case_ids")
                )
                flash("代表性示例已保存，任务使用当前版本的示例选择。", "success")
            elif action in {
                "add_case",
                "save_case",
                "delete_case",
                "validate_case",
                "review_validation",
            }:
                raw_id = request.form.get("case_id", "")
                case_id = int(raw_id) if str(raw_id).isdigit() else None
                if action != "add_case" and case_id is None:
                    raise ValueError("案例编号无效。")
                if action in {"add_case", "save_case"}:
                    case_id = save_case(
                        db,
                        item,
                        request.form,
                        case_id if action == "save_case" else None,
                        source if action == "add_case" else None,
                    )
                    flash("案例已保存，规则版本已更新。", "success")
                elif action == "delete_case":
                    delete_case(db, item, case_id)
                    flash("案例已删除，规则版本已更新。", "success")
                elif action == "review_validation":
                    raw_task_id = request.form.get("validation_task_id", "")
                    if not str(raw_task_id).isdigit():
                        raise ValueError("验证任务编号无效。")
                    task = (_get_task_or_404 if is_superadmin() else _get_user_task)(
                        int(raw_task_id)
                    )
                    record_case_validation(
                        db, item, case_id, task, request.form, identity.subject
                    )
                    flash("验证复核结论已保存。", "success")
                else:
                    case = get_skill_case(db, skill_id, case_id)
                    pending = None
                    if case["validation_version"] == item["version"]:
                        pending = db.execute(
                            "SELECT owner_subject, status FROM tasks WHERE id=?",
                            (case["validation_task_id"],),
                        ).fetchone()
                    if (
                        pending
                        and pending["owner_subject"] == identity.subject
                        and pending["status"] in {"queued", "running", "canceling"}
                    ):
                        flash("当前案例已有验证任务正在执行。", "success")
                    else:
                        snapshot = check_snapshot(db, item)
                        # 验证案例独立于提示词示例，期望结果作为任务复核资料保存。
                        other_cases = [
                            c for c in skill_cases(db, skill_id) if c["id"] != case_id
                        ]
                        snapshot["prompt"] = compile_skill_prompt(item, other_cases)
                        snapshot["example_case_ids"] = [
                            c["id"] for c in selected_examples(other_cases)
                        ]
                        snapshot["validation_case_id"] = case_id
                        snapshot["task_report_limit"] = 0
                        snapshot["validation_case"] = {
                            key: case["data"].get(key, "")
                            for key in ("title", "kind", "expected_result")
                        }
                        task_id = submit_skill_case_validation(
                            identity,
                            request.form.get("model_id", ""),
                            snapshot,
                            case["data"],
                            saved_paths=validation_upload_paths,
                        )
                        case["data"].pop("validation_review", None)
                        db.execute(
                            "UPDATE skill_cases SET validation_task_id=?, validation_version=?, case_json=? WHERE id=?",
                            (
                                task_id,
                                item["version"],
                                json.dumps(case["data"], ensure_ascii=False),
                                case_id,
                            ),
                        )
                        flash("案例验证任务已创建，可打开报告对照期望结果。", "success")
            elif action == "delete_skill":
                db.execute("DELETE FROM check_items WHERE id=?", (skill_id,))
                db.commit()
                flash("专项 skill 已删除。", "success")
                return redirect(url_for(rules_endpoint, _anchor="specialist-skills"))
            else:
                raise ValueError("专项操作无效。")
            db.commit()
            validation_upload_paths.clear()
            retained_source = (
                source if action not in {"add_case", "save_case"} else None
            )
            focus = {}
            if action in {
                "add_case",
                "save_case",
                "validate_case",
                "review_validation",
            }:
                focus = {"case": case_id, "_anchor": f"case-{case_id}"}
            elif action == "select_examples":
                focus = {"_anchor": "skill-examples"}
            elif action == "delete_case":
                focus = {"_anchor": "skill-cases"}
            return redirect(
                url_for(endpoint, skill_id=skill_id, **(retained_source or {}), **focus)
            )
        except ValueError as error:
            db.rollback()
            flash(str(error), "error")
            status = 400
            if action == "save_skill":
                definition = {key: request.form.get(key, "") for key, _ in SKILL_FIELDS}
                item = {
                    **dict(item),
                    "name": request.form.get("name", ""),
                    "description": request.form.get("description", ""),
                    "enabled": request.form.get("enabled") == "on",
                }
            elif action in {"add_case", "save_case"}:
                case_form = dict(request.form)
        except Exception:
            db.rollback()
            raise
        finally:
            _remove_uploaded_files(validation_upload_paths)
    cases = skill_cases(db, skill_id)
    if status == 400 and request.form.get("action") == "save_case":
        for case in cases:
            if str(case["id"]) == request.form.get("case_id"):
                case["data"] = {**case["data"], **request.form}
                case["data"]["kind"] = request.form.get("case_kind", "positive")
                case["data"]["reviewed"] = request.form.get("reviewed") == "on"
        case_form = {}
    example_ids = {case["id"] for case in selected_examples(cases)}
    validation_counts = {"passed": 0, "failed": 0, "pending": 0}
    for case in cases:
        case["is_example"] = case["id"] in example_ids
        case["focused"] = str(case["id"]) == (
            request.form.get("case_id") if status == 400 else request.args.get("case")
        )
        case["edit_open"] = (
            case["focused"]
            and status == 400
            and request.form.get("action") == "save_case"
        )
        case["validation_url"] = ""
        case["validation_status"] = ""
        case["validation_can_review"] = False
        review = case["data"].get("validation_review")
        review = review if isinstance(review, dict) else {}
        case["validation_review"] = review
        case["validation_form"] = review
        if (
            status == 400
            and request.form.get("action") == "review_validation"
            and request.form.get("case_id") == str(case["id"])
        ):
            case["validation_form"] = {
                "verdict": request.form.get("validation_verdict", ""),
                "note": request.form.get("validation_note", ""),
            }
        case["validation_current"] = (
            bool(case["validation_task_id"])
            and case["validation_version"] == item["version"]
            and review.get("task_id") == case["validation_task_id"]
            and review.get("version") == item["version"]
        )
        if case["validation_task_id"]:
            # 验证报告继续使用任务页面的可见范围。
            visible = db.execute(
                "SELECT owner_subject, status FROM tasks WHERE id=?",
                (case["validation_task_id"],),
            ).fetchone()
            if visible and (
                is_superadmin()
                or has_permission("tasks.view_all")
                or visible["owner_subject"] == identity.subject
            ):
                case["validation_url"] = url_for(
                    "admin_task_detail" if is_superadmin() else "user_task_detail",
                    task_id=case["validation_task_id"],
                )
                case["validation_status"] = STATUS_LABELS.get(
                    visible["status"], visible["status"]
                )
                case["validation_can_review"] = (
                    visible["status"] == "completed"
                    and case["validation_version"] == item["version"]
                )
        verdict = review.get("verdict") if case["validation_current"] else "pending"
        validation_counts[verdict if verdict in validation_counts else "pending"] += 1
    try:
        preview = compile_skill_prompt(
            get_skill(db, skill_id), skill_cases(db, skill_id)
        )
    except ValueError:
        preview = "填写完整规则后可查看执行提示词。"
    return render_template(
        "skill_detail.html",
        skill=item,
        definition=definition,
        skill_fields=SKILL_FIELDS,
        case_fields=CASE_FIELDS,
        cases=cases,
        case_form=case_form,
        source=source or {},
        preview=preview,
        models=get_enabled_models(identity.subject),
        validation_counts=validation_counts,
        rules_endpoint=rules_endpoint,
        active_nav="rules",
    ), status
