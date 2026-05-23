import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask

from .config import load_local_config
from .db import init_db, seed_defaults
from .formatting import render_markdown
from .routes import register_routes
from .tasks import TaskScheduler


def create_app():
    root_dir = _runtime_root_dir()
    local_config = load_local_config(root_dir, default_platform=not getattr(sys, "frozen", False))

    app = Flask(__name__, instance_path=str(root_dir / "instance"), instance_relative_config=True)
    app.config.update(
        SECRET_KEY=local_config["secret_key"],
        PLATFORM=local_config["platform"],
        ADMIN_USERNAME=local_config["admin"]["username"],
        ADMIN_PASSWORD=local_config["admin"]["password"],
        ADMIN_URL=local_config["admin_url"],
        LISTEN_HOST=local_config["server"]["host"] if local_config["platform"] else "127.0.0.1",
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


def _runtime_root_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _configure_logging(app):
    log_file = Path(app.config["LOG_FILE"])
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = None

    for name in ("app", "werkzeug"):
        target_logger = logging.getLogger(name)
        target_logger.setLevel(logging.INFO)
        if not _has_log_file_handler(target_logger, log_file):
            if file_handler is None:
                file_handler = RotatingFileHandler(
                    log_file,
                    maxBytes=5 * 1024 * 1024,
                    backupCount=2,
                    encoding="utf-8",
                )
                file_handler.setLevel(logging.INFO)
                file_handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
                )
            target_logger.addHandler(file_handler)
        _ensure_console_handler(target_logger)
        target_logger.propagate = False

    app.logger.info("本地日志已启用：%s", log_file)


def _has_log_file_handler(target_logger, log_file: Path) -> bool:
    return any(
        isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_file
        for handler in target_logger.handlers
    )


def _ensure_console_handler(target_logger):
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    for handler in target_logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, RotatingFileHandler):
            handler.setLevel(logging.INFO)
            handler.setFormatter(formatter)
            return
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    target_logger.addHandler(console_handler)
