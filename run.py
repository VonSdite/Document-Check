import os
import subprocess
import sys

from gunicorn.app.base import BaseApplication

from app import create_app
from app.network import access_urls
from app.observability import log_startup_self_check
from app.task_supervisor import wait_for_supervisor


SUPERVISOR_STOP_TIMEOUT_SECONDS = 20


class DocumentCheckServer(BaseApplication):
    def __init__(self, application, options: dict):
        self.application = application
        self.options = options
        super().__init__()

    def load_config(self):
        for key, value in self.options.items():
            if key in self.cfg.settings and value is not None:
                self.cfg.set(key, value)

    def load(self):
        return self.application


def main() -> None:
    app = create_app()
    host = (
        os.environ.get("HOST", app.config["LISTEN_HOST"])
        if app.config["PLATFORM"]
        else "127.0.0.1"
    )
    port = int(os.environ.get("PORT", app.config["LISTEN_PORT"]))
    stop_event, supervisor = _start_task_supervisor(app)
    owner_pid = os.getpid()

    try:
        log_startup_self_check(app)
        app.logger.info(
            "服务监听：http://%s:%s pid=%s url_prefix=%s proxy_fix=%s "
            "web_workers=%s web_threads=%s max_task_processes=%s",
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
            app.logger.info("可访问地址：%s", url)
        DocumentCheckServer(
            app,
            _server_options(app, host, port),
        ).run()
    finally:
        if os.getpid() == owner_pid:
            _stop_task_supervisor(app, stop_event, supervisor)


def _server_options(app, host: str, port: int) -> dict:
    return {
        "bind": f"{host}:{port}",
        "worker_class": "gthread",
        "workers": app.config["WEB_WORKERS"],
        "threads": app.config["WEB_THREADS"],
        "accesslog": None,
        "errorlog": "-",
        "capture_output": False,
    }


def _start_task_supervisor(app):
    supervisor = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "app.task_supervisor",
            "--parent-pid",
            str(os.getpid()),
        ],
        cwd=str(app.config["ROOT_DIR"]),
        stdin=subprocess.DEVNULL,
        start_new_session=False,
    )
    stop_event = None
    if wait_for_supervisor(app, supervisor):
        return stop_event, supervisor

    supervisor.terminate()
    supervisor.wait(timeout=3)
    raise RuntimeError("任务调度进程启动失败")


def _stop_task_supervisor(app, stop_event, supervisor) -> None:
    if supervisor.poll() is not None:
        return
    supervisor.terminate()
    try:
        supervisor.wait(timeout=SUPERVISOR_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        app.logger.warning("任务调度进程未在期限内退出，准备终止 pid=%s", supervisor.pid)
        supervisor.kill()
        supervisor.wait(timeout=3)


if __name__ == "__main__":
    main()
