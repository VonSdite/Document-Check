import logging
import os
from logging.handlers import RotatingFileHandler
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
        LOG_FILE=str(root_dir / "instance" / "logs" / "app.log"),
        MAX_CONTENT_LENGTH=50 * 1024 * 1024,
    )

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    _configure_logging(app)

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


def _configure_logging(app):
    log_file = Path(app.config["LOG_FILE"])
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = None

    for name in ("app", "werkzeug"):
        target_logger = logging.getLogger(name)
        target_logger.setLevel(logging.INFO)
        if _has_log_file_handler(target_logger, log_file):
            continue
        if file_handler is None:
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
            )
            target_logger.addHandler(file_handler)
            continue
        target_logger.addHandler(file_handler)

    app.logger.info("本地日志已启用：%s", log_file)


def _has_log_file_handler(target_logger, log_file: Path) -> bool:
    return any(
        isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_file
        for handler in target_logger.handlers
    )
