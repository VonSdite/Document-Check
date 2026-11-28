"""为前端浏览器验证提供隔离的示例工作区。"""

import atexit
import io
import sys
import zipfile
from pathlib import Path

from flask import Response, g, redirect, request, session

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.contracts.task_types import DOCUMENT_TASK_TYPE, TASK_TYPE_LABELS  # noqa: E402
from app.identity.models import UserIdentity  # noqa: E402
from app.persistence.connection import get_db  # noqa: E402
from app.persistence.permissions import (  # noqa: E402
    register_subject,
    replace_subject_permissions,
)
from app.persistence.settings import set_ip_username  # noqa: E402
from tests.test_routes import AdminSettingsRouteTest  # noqa: E402


def main():
    fixture = AdminSettingsRouteTest()
    fixture.setUp()
    fixture.app.config["TEMPLATES_AUTO_RELOAD"] = True
    fixture.app.jinja_env.auto_reload = True
    atexit.register(fixture.tearDown)
    fixture._configure_provider(api_key="example-frontend-key")
    fixture._insert_report_task()
    for task_type, filename in [
        ("document_check", "VX120 安装手册.pdf"),
        ("consistency_check", "安装指南与产品规格对照"),
        ("language_consistency_check", "产品手册中英文对照.docx"),
        ("image_check", "设备接线示意图.pdf"),
        ("video_check", "设备安装操作.mp4"),
    ]:
        fixture._insert_task(task_type=task_type, original_filename=filename)
    with fixture.app.app_context():
        db = get_db()
        db.execute(
            "UPDATE tasks SET owner_subject='ip:127.0.0.1', owner_source='ip', "
            "owner_name_snapshot='演示用户', created_at='2026-11-21 10:30:00', "
            "updated_at='2026-11-21 10:30:00'"
        )
        db.execute(
            "UPDATE user_model_providers SET name='示例模型服务', request_timeout=600"
        )
        db.commit()
        for ip, name in [("192.0.2.10", "示例用户甲"), ("192.0.2.11", "示例用户乙")]:
            register_subject(f"ip:{ip}")
            set_ip_username(ip, name)
    fixture._insert_suppression_rules(24)
    review_task_id = None

    @fixture.app.get("/__frontend__/skill-package")
    def skill_package_fixture():
        output = io.BytesIO()
        version = (
            "更新后的接口标准"
            if request.args.get("version") == "2"
            else "请求字段应与示例一致"
        )
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "SKILL.md",
                "---\nname: 接口资料检查\n---\n依据 references/标准.md 检查接口文档。",
            )
            archive.writestr("references/标准.md", version)
        return Response(output.getvalue(), mimetype="application/zip")

    @fixture.app.post("/__frontend__/review-fixture")
    def create_review_fixture():
        nonlocal review_task_id
        task_type = request.args.get("task_type", DOCUMENT_TASK_TYPE)
        if task_type not in TASK_TYPE_LABELS:
            return {"error": "Unknown fixture task type"}, 400
        if review_task_id is None:
            review_task_id = fixture._insert_report_task()
        db = get_db()
        db.execute(
            "UPDATE tasks SET owner_subject=?, task_type=? WHERE id=?",
            (
                "ip:127.0.0.1"
                if request.args.get("mode") == "ip"
                else "cookie_session:review-author",
                task_type,
                review_task_id,
            ),
        )
        db.commit()
        for name in ("review-author", "final-reviewer"):
            subject = "cookie_session:" + name
            register_subject(subject)
            replace_subject_permissions(subject, {"reports.final_review"})
        return {"task_id": review_task_id}

    @fixture.app.delete("/__frontend__/review-fixture")
    def remove_review_fixture():
        nonlocal review_task_id
        with get_db() as db:
            db.execute(
                "DELETE FROM report_suppression_rules WHERE source_task_id=?",
                (review_task_id,),
            )
            db.execute("DELETE FROM tasks WHERE id=?", (review_task_id,))
            db.execute(
                "DELETE FROM user_identities WHERE subject IN (?, ?)",
                ("cookie_session:review-author", "cookie_session:final-reviewer"),
            )
        review_task_id = None
        session.clear()
        return {"ok": True}

    @fixture.app.before_request
    def review_test_identity():
        name = session.get("review_test_identity")
        if name:
            g.user_identity = UserIdentity(
                subject="cookie_session:" + name,
                display_name=name,
                source="cookie_session",
                ip="127.0.0.1",
            )

    @fixture.app.get("/__frontend__/review-identity/<name>")
    def review_login(name):
        if name not in {"review-author", "final-reviewer"}:
            return "Unknown fixture identity", 404
        if review_task_id is None:
            return "Review fixture is not ready", 409
        session.clear()
        session["review_test_identity"] = name
        return redirect(f"/tasks/{review_task_id}/reviews")

    (Path(fixture.app.config["UPLOAD_FOLDER"]) / "stored.txt").write_text(
        "产品额定电压为 220V。安装说明中的额定电压为 380V。", encoding="utf-8"
    )

    @fixture.app.get("/__frontend__/admin")
    def login():
        session["admin_logged_in"] = True
        return redirect("/admin/tasks")

    @fixture.app.get("/__frontend__/user")
    def logout():
        session.clear()
        return redirect("/")

    fixture.app.run(host="127.0.0.1", port=8768, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
