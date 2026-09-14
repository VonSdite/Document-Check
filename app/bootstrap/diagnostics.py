"""使用轻量入口记录进程初始化与运行期间的未捕获异常。"""

import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def run_entrypoint(callback, *, logger_name="app.run"):
    try:
        return callback()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        _log_process_failure(logger_name, sys.exc_info())
        raise SystemExit(1) from None


def _log_process_failure(logger_name, exc_info):
    root = Path(
        os.environ.get("DOCUMENTCHECK_ROOT_DIR") or Path(__file__).resolve().parents[2]
    ).resolve()
    log_dir = root / "instance" / "logs"
    owner = "app.tasks" if logger_name.startswith("app.tasks") else "app"
    filename = "task.log" if owner == "app.tasks" else "app.log"
    try:
        from app.infrastructure.logging import configure_process_logging

        if not any(
            isinstance(handler, logging.FileHandler)
            and Path(handler.baseFilename) == log_dir / filename
            for handler in logging.getLogger(owner).handlers
        ):
            configure_process_logging(
                {name: log_dir / name for name in ("app.log", "task.log", "llm.log")}
            )
        logging.getLogger(logger_name).critical("进程异常退出", exc_info=exc_info)
    except Exception:
        # 依赖不可用时，标准库为本进程单独保存启动诊断。
        text = (
            f"{datetime.now().isoformat(timespec='seconds')} CRITICAL "
            f"[{logger_name}] pid={os.getpid()} 进程异常退出\n"
            + "".join(traceback.format_exception(*exc_info))
        )
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / f"startup-{os.getpid()}.log").open(
                "a", encoding="utf-8", newline="\n"
            ) as output:
                output.write(text)
        except OSError:
            pass
        sys.stderr.write(text)
