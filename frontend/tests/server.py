"""为前端浏览器验证提供隔离的示例工作区。"""

import atexit
import sys
from pathlib import Path

from flask import redirect, session

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.persistence.connection import get_db  # noqa: E402
from tests.test_routes import AdminSettingsRouteTest  # noqa: E402


def main():
    fixture = AdminSettingsRouteTest()
    fixture.setUp()
    fixture.app.config["TEMPLATES_AUTO_RELOAD"] = True
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
    fixture._insert_suppression_rules(24)
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
