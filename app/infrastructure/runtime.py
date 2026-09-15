import os
from pathlib import Path

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from app.infrastructure.config import load_local_config
from app.infrastructure.logging import _configure_logging
from app.persistence.connection import close_db


def create_task_app(root_dir: Path | None = None):
    app = _create_base_app(root_dir)
    # 每个任务应用上下文结束时释放独立数据库连接。
    app.teardown_appcontext(close_db)
    return app


def _create_base_app(root_dir: Path | None = None):
    root_dir = Path(root_dir).resolve() if root_dir is not None else _runtime_root_dir()
    local_config = load_local_config(root_dir)
    server_config = local_config["server"]
    worker_config = local_config["worker"]

    app = Flask(
        "app",
        root_path=str(Path(__file__).resolve().parents[1] / "web"),
        instance_path=str(root_dir / "instance"),
        instance_relative_config=True,
    )
    if server_config["proxy_fix"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    app.config.update(
        SECRET_KEY=local_config["secret_key"],
        ADMIN_USERNAME=local_config["admin"]["username"],
        ADMIN_PASSWORD=local_config["admin"]["password"],
        ADMIN_URL=local_config["admin_url"],
        LISTEN_HOST=server_config["host"],
        LISTEN_PORT=server_config["port"],
        APPLICATION_ROOT=server_config["url_prefix"] or "/",
        PROXY_FIX=server_config["proxy_fix"],
        REAL_IP_HEADER=server_config["real_ip_header"],
        NETWORK=local_config["network"],
        AUTH=local_config["auth"],
        ROOT_DIR=root_dir,
        DATABASE=str(root_dir / "instance" / "document_check.sqlite3"),
        UPLOAD_FOLDER=str(root_dir / "instance" / "uploads"),
        IMAGE_FOLDER=str(root_dir / "instance" / "extracted_images"),
        SENSITIVE_TERMS_PATH=str(root_dir / "instance" / "sensitive_terms.xlsx"),
        COMMON_TERMS_PATH=str(root_dir / "instance" / "common_terms.xlsx"),
        LOG_FILE=str(root_dir / "instance" / "logs" / "app.log"),
        TASK_LOG_FILE=str(root_dir / "instance" / "logs" / "task.log"),
        LLM_LOG_FILE=str(root_dir / "instance" / "logs" / "llm.log"),
        ACCESS_LOG_FILE=str(root_dir / "instance" / "logs" / "access.log"),
        CONSOLE_LOG_LEVEL=local_config["logging"]["console_level"],
        MAX_UPLOAD_MB=server_config["max_upload_mb"],
        MAX_CONTENT_LENGTH=server_config["max_upload_mb"] * 1024 * 1024,
        WEB_WORKERS=server_config["web_workers"],
        WEB_THREADS=server_config["web_threads"],
        MAX_TASK_PROCESSES=worker_config["max_task_processes"],
    )

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["IMAGE_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    _configure_logging(app)

    return app


def _runtime_root_dir() -> Path:
    configured_root = os.environ.get("DOCUMENTCHECK_ROOT_DIR")
    if configured_root:
        return Path(configured_root).resolve()
    return Path(__file__).resolve().parents[2]
