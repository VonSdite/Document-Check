"""独立任务入口；标准输入提供启动许可，标准输出报告启动阶段。"""

import argparse
import json
import sys

from app.bootstrap.diagnostics import run_entrypoint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", type=int)
    task_id = parser.parse_args().task_id
    print("DOCUMENTCHECK_BOOTSTRAP", flush=True)
    permission = sys.stdin.buffer.readline(4096)
    if not permission:
        return
    payload = json.loads(permission)
    claim_token = payload.get("claim_token")
    if (
        payload.get("start") is not True
        or not isinstance(claim_token, str)
        or not claim_token
    ):
        raise ValueError("任务启动许可无效")

    print("DOCUMENTCHECK_INITIALIZING", flush=True)
    from app.infrastructure.runtime import create_task_app
    from app.tasks.runner import TaskRunner

    app = create_task_app()
    print("DOCUMENTCHECK_READY", flush=True)
    TaskRunner(app).run(task_id, claim_token)


if __name__ == "__main__":
    run_entrypoint(main, logger_name="app.tasks.process")
