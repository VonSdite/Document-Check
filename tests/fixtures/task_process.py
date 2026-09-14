"""在独立解释器中模拟任务解析阻塞与启动异常。"""

import os
import sys
from pathlib import Path


def blocked_task_command(module, *, root_dir=None):
    from app.infrastructure.subprocesses import python_module_command

    command, environment = python_module_command(module, root_dir=root_dir)
    command[-1] = "tests.fixtures.task_process"
    return [*command, "blocked"], environment


def blocked_preprocessing(_app, _db, task, *_args, **_kwargs):
    import ctypes

    root = Path(os.environ["DOCUMENTCHECK_ROOT_DIR"])
    (root / f"started-{task['id']}").write_text("started", encoding="utf-8")
    # PyDLL 保留 GIL，模拟长时间阻塞 Python 线程的底层解析调用。
    if os.name == "nt":
        ctypes.PyDLL("kernel32").Sleep(10000)
    else:
        ctypes.PyDLL(None).sleep(10)
    return "测试文本", None


def main():
    mode = sys.argv.pop(1)
    if mode == "bootstrap-hang":
        import time

        time.sleep(30)
        return
    if mode == "initialization-hang":
        import time

        print("DOCUMENTCHECK_BOOTSTRAP", flush=True)
        sys.stdin.readline()
        print("DOCUMENTCHECK_INITIALIZING", flush=True)
        time.sleep(30)
        return
    if mode == "exit":
        raise SystemExit(7)

    from unittest.mock import patch

    if mode == "blocked":
        from app.bootstrap.task import main as run_task

        with patch(
            "app.tasks.runner._prepare_task_inputs", side_effect=blocked_preprocessing
        ):
            run_task()
    elif mode == "supervisor":
        import threading

        from app.infrastructure.runtime import create_task_app
        from app.tasks.supervisor import TaskSupervisor

        with patch(
            "app.tasks.processes.python_module_command",
            side_effect=blocked_task_command,
        ):
            TaskSupervisor(create_task_app()).run(threading.Event())
    else:
        raise ValueError(mode)


if __name__ == "__main__":
    main()
