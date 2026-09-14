import logging
import os
import subprocess
from pathlib import Path

from app.bootstrap.diagnostics import run_entrypoint
from app.infrastructure.subprocesses import PROJECT_ROOT, python_module_command

logger = logging.getLogger("app.run")


SUPERVISOR_STOP_TIMEOUT_SECONDS = 20


def main() -> None:
    from app.bootstrap.factory import create_app
    from app.bootstrap.server import serve_application
    from app.infrastructure.network import access_urls
    from app.web.observability import log_startup_self_check

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
            extra={"console_notice": True},
        )
        for url in access_urls(host, port):
            logger.info("可访问地址：%s", url, extra={"console_notice": True})
        serve_application(app, host, port)
    finally:
        _stop_task_supervisor(supervisor)


def _start_task_supervisor(app):
    from app.tasks.supervisor import wait_for_supervisor

    command, environment = python_module_command(
        "app.bootstrap.supervisor", root_dir=app.config["ROOT_DIR"]
    )
    supervisor = subprocess.Popen(
        [
            *command,
            "--parent-pid",
            str(os.getpid()),
            "--watch-stdin",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        start_new_session=False,
    )
    if wait_for_supervisor(app, supervisor):
        return supervisor

    exit_code = supervisor.poll()
    reason = "等待就绪超时" if exit_code is None else f"子进程退出，退出码={exit_code}"
    _stop_task_supervisor(supervisor)
    task_log = Path(app.instance_path) / "logs" / "task.log"
    raise RuntimeError(
        f"任务调度进程启动失败：{reason}，启动 PID={supervisor.pid}。"
        f"请查看控制台前面的子进程异常和任务日志：{task_log}"
    )


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
    run_entrypoint(main)
