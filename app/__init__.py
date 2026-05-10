import os
from pathlib import Path

from flask import Flask

from .config import load_local_config
from .db import init_db, seed_defaults
from .formatting import render_markdown
from .routes import register_routes
from .tasks import TaskScheduler


def create_app():
    root_dir = Path(__file__).resolve().parent.parent
    local_config = load_local_config(root_dir)

    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=local_config["secret_key"],
        ADMIN_USERNAME=local_config["admin"]["username"],
        ADMIN_PASSWORD=local_config["admin"]["password"],
        ADMIN_URL=local_config["admin_url"],
        LISTEN_HOST=local_config["server"]["host"],
        LISTEN_PORT=local_config["server"]["port"],
        ROOT_DIR=root_dir,
        DATABASE=str(root_dir / "instance" / "document_check.sqlite3"),
        UPLOAD_FOLDER=str(root_dir / "instance" / "uploads"),
        MAX_CONTENT_LENGTH=50 * 1024 * 1024,
    )

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    with app.app_context():
        init_db()
        seed_defaults()

    register_routes(app)
    app.add_template_filter(render_markdown, "markdown")

    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        scheduler = TaskScheduler(app)
        scheduler.start()
        app.extensions["task_scheduler"] = scheduler

    return app
