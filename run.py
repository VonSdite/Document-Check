import logging
import os
import subprocess
import sys
from pathlib import Path

from app.bootstrap.factory import create_app
from app.bootstrap.server import serve_application
from app.infrastructure.network import access_urls
from app.tasks.supervisor import wait_for_supervisor
from app.web.observability import log_startup_self_check

logger = logging.getLogger("app.run")


SUPERVISOR_STOP_TIMEOUT_SECONDS = 20


def main() -> None:
    app = create_app()
    host = (
        os.environ.get("HOST", app.config["LISTEN_HOST"])
        if app.config["PLATFORM"]
        else "127.0.0.1"
    )
    port = int(os.environ.get("PORT", app.config["LISTEN_PORT"]))
    supervisor = _start_task_supervisor(app)

    try:
        log_startup_self_check(app)
        logger.info(
            "服务监听：http://%s:%s pid=%s url_prefix=%s proxy_fix=%s "
            "web_server=uvicorn web_workers=%s web_threads=%s max_task_processes=%s",
            host,
            port,
            os.getpid(),
            app.config["APPLICATION_ROOT"],
            app.config["PROXY_FIX"],
            app.config["WEB_WORKERS"],
            app.config["WEB_THREADS"],
            app.config["MAX_TASK_PROCESSES"],
        )
        for url in access_urls(host, port):
            logger.info("可访问地址：%s", url)
        serve_application(app, host, port)
    finally:
        _stop_task_supervisor(supervisor)


def _start_task_supervisor(app):
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app.bootstrap.supervisor",
            "--parent-pid",
            str(os.getpid()),
            "--watch-stdin",
        ],
        cwd=Path(__file__).resolve().parent,
        env={**os.environ, "DOCUMENTCHECK_ROOT_DIR": str(app.config["ROOT_DIR"])},
        stdin=subprocess.PIPE,
        start_new_session=False,
    )
    if wait_for_supervisor(app, supervisor):
        return supervisor

    _stop_task_supervisor(supervisor)
    raise RuntimeError("任务调度进程启动失败")


def _stop_task_supervisor(supervisor) -> None:
    if supervisor.stdin is not None:
        supervisor.stdin.close()
    if supervisor.poll() is not None:
        return
    try:
        supervisor.wait(timeout=SUPERVISOR_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning("任务调度进程未在期限内退出，准备终止 pid=%s", supervisor.pid)
        supervisor.kill()
        supervisor.wait(timeout=3)


if __name__ == "__main__":
    main()
