import signal
import sys
import threading

from app.tasks.supervisor import TaskSupervisor


def run_task_supervisor(stop_event, parent_pid: int | None = None) -> None:
    from app.infrastructure.runtime import create_task_app

    try:
        TaskSupervisor(create_task_app()).run(stop_event, parent_pid=parent_pid)
    except KeyboardInterrupt:
        # Gunicorn 将终端信号转发到进程组，监督器在此结束运行。
        return


def _run_supervisor_cli() -> None:
    parent_pid = None
    if "--parent-pid" in sys.argv:
        index = sys.argv.index("--parent-pid")
        try:
            parent_pid = int(sys.argv[index + 1])
        except (IndexError, TypeError, ValueError):
            raise SystemExit("--parent-pid 必须是整数")

    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run_task_supervisor(stop_event, parent_pid=parent_pid)


if __name__ == "__main__":
    _run_supervisor_cli()
