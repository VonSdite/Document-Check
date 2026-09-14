import logging
from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler

from app.infrastructure.config import DEFAULT_CONSOLE_LOG_LEVEL

logger = logging.getLogger(__name__)
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] pid=%(process)d %(message)s"


def _configure_logging(app):
    configure_process_logging(
        {
            "app.log": app.config["LOG_FILE"],
            "task.log": app.config["TASK_LOG_FILE"],
            "llm.log": app.config["LLM_LOG_FILE"],
        },
        console_level=app.config.get("CONSOLE_LOG_LEVEL", DEFAULT_CONSOLE_LOG_LEVEL),
    )
    logger.info(
        "本地日志已启用 app=%s task=%s llm=%s",
        app.config["LOG_FILE"],
        app.config["TASK_LOG_FILE"],
        app.config["LLM_LOG_FILE"],
    )


def configure_process_logging(log_files, *, console_level=DEFAULT_CONSOLE_LOG_LEVEL):
    for names, filename in (
        (("app", "werkzeug", "uvicorn"), "app.log"),
        (("app.tasks",), "task.log"),
        (("app.models",), "llm.log"),
    ):
        log_file = Path(log_files[filename])
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
            _ensure_console_handler(target_logger, level=console_level)
            target_logger.propagate = False

    # Uvicorn 的运行事件统一交给父 logger，访问日志由 Web 层记录。
    for name in ("uvicorn.error", "uvicorn.asgi"):
        target_logger = logging.getLogger(name)
        target_logger.handlers.clear()
        target_logger.setLevel(logging.INFO)
        target_logger.propagate = True


def _has_log_file_handler(target_logger, log_file: Path) -> bool:
    return any(
        isinstance(handler, ConcurrentRotatingFileHandler)
        and Path(handler.baseFilename) == log_file
        for handler in target_logger.handlers
    )


class ConsoleLogFilter(logging.Filter):
    def __init__(self, level):
        super().__init__()
        self.level = logging.getLevelNamesMapping()[level]

    def filter(self, record):
        return record.levelno >= self.level or bool(
            getattr(record, "console_notice", False)
        )


def _ensure_console_handler(
    target_logger, *, level=DEFAULT_CONSOLE_LOG_LEVEL, log_format=LOG_FORMAT
):
    console_handler = None
    for handler in target_logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            console_handler = handler
            break
    if console_handler is None:
        console_handler = logging.StreamHandler()
        target_logger.addHandler(console_handler)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))
    for existing in console_handler.filters[:]:
        if isinstance(existing, ConsoleLogFilter):
            console_handler.removeFilter(existing)
    console_handler.addFilter(ConsoleLogFilter(level))
