import logging
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler

logger = logging.getLogger(__name__)
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] pid=%(process)d %(message)s"


def _configure_logging(app):
    for names, config_key in (
        (("app", "werkzeug"), "LOG_FILE"),
        (("app.tasks",), "TASK_LOG_FILE"),
        (("app.models",), "LLM_LOG_FILE"),
    ):
        log_file = Path(app.config[config_key])
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = None
        for name in names:
            target_logger = logging.getLogger(name)
            target_logger.setLevel(logging.INFO)
            if not _has_log_file_handler(target_logger, log_file):
                if file_handler is None:
                    file_handler = ConcurrentRotatingFileHandler(
                        log_file,
                        maxBytes=5 * 1024 * 1024,
                        backupCount=2,
                        encoding="utf-8",
                    )
                    file_handler.setLevel(logging.INFO)
                    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
                target_logger.addHandler(file_handler)
            _ensure_console_handler(target_logger)
            target_logger.propagate = False

    logger.info(
        "本地日志已启用 app=%s task=%s llm=%s",
        app.config["LOG_FILE"],
        app.config["TASK_LOG_FILE"],
        app.config["LLM_LOG_FILE"],
    )


def _has_log_file_handler(target_logger, log_file: Path) -> bool:
    return any(
        isinstance(handler, ConcurrentRotatingFileHandler)
        and Path(handler.baseFilename) == log_file
        for handler in target_logger.handlers
    )


def _ensure_console_handler(target_logger):
    formatter = logging.Formatter(LOG_FORMAT)
    for handler in target_logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            handler.setLevel(logging.INFO)
            handler.setFormatter(formatter)
            return
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    target_logger.addHandler(console_handler)
