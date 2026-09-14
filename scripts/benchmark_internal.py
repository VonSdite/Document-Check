"""使用临时数据库和独立服务进程测量多用户页面与轮询接口。"""

import argparse
import concurrent.futures
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil
import requests
import yaml
from flask import Flask

from app.infrastructure.config import DEFAULT_WEB_WORKERS
from app.persistence.connection import get_db
from app.persistence.schema import init_db
from app.reporting.service import _empty_report_suppression_version

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def prepare_database(root: Path, tasks: int, users: int) -> None:
    instance = root / "instance"
    instance.mkdir()
    app = Flask(__name__)
    app.config["DATABASE"] = str(instance / "document_check.sqlite3")
    with app.app_context():
        init_db()
        db = get_db()
        db.executemany(
            """
            INSERT INTO tasks(
                ip, owner_subject, owner_source, original_filename, stored_filename,
                file_type, file_size, checks_json, model_name, api_base,
                status, progress, document_text, result_json, created_at, updated_at
            ) VALUES ('127.0.0.1', ?, 'trusted_header', 'example.txt', 'example.txt',
                      'txt', 4096, '[]', 'benchmark', 'http://example.invalid',
                      'completed', 100, ?, '[]', '2026-09-01 12:00:00', '2026-09-01 12:00:00')
            """,
            (
                (f"trusted_header:bench-{i % users}", "文档性能测试。" * 512)
                for i in range(tasks)
            ),
        )
        db.execute(
            "INSERT INTO task_report_stats(task_id, source_updated_at, suppression_version, updated_at) "
            "SELECT id, updated_at, ?, updated_at FROM tasks",
            (_empty_report_suppression_version(),),
        )
        db.commit()


def benchmark(args) -> dict:
    if (
        min(args.tasks, args.users, args.requests, args.concurrency) < 1
        or args.tasks < args.users
    ):
        raise ValueError("任务、用户、请求和并发数为正整数，任务数应覆盖全部用户")
    with tempfile.TemporaryDirectory(prefix="documentcheck-benchmark-") as temporary:
        root = Path(temporary)
        shutil.copytree(
            args.source_root / "app",
            root / "app",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        shutil.copy2(args.source_root / "run.py", root / "run.py")
        prepare_database(root, args.tasks, args.users)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = {
            "platform": True,
            "secret_key": "isolated-benchmark-session",
            "admin_url": "/benchmark-console",
            "server": {
                "host": "127.0.0.1",
                "port": port,
                "web_workers": DEFAULT_WEB_WORKERS,
                "web_threads": 16,
            },
            "worker": {"max_task_processes": 4},
            "auth": {
                "mode": "trusted_header",
                "trusted_header": {"user_id": "X-Benchmark-User"},
            },
        }
        (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        base_url = f"http://127.0.0.1:{port}"
        sessions = []
        local = threading.local()
        with (root / "server-output.log").open("w", encoding="utf-8") as output:
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"HOST", "PORT"}
            }
            environment["DOCUMENTCHECK_ROOT_DIR"] = str(root)
            environment["PYTHONIOENCODING"] = "utf-8"
            process = subprocess.Popen(
                [sys.executable, str(root / "run.py")],
                cwd=root,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                with requests.Session() as probe:
                    probe.trust_env = False
                    deadline = time.monotonic() + 30
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(
                                (root / "server-output.log").read_text(
                                    encoding="utf-8"
                                )[-4000:]
                            )
                        try:
                            response = probe.get(base_url + "/health/ready", timeout=1)
                            if response.status_code == 200:
                                break
                        except requests.RequestException:
                            pass
                        if time.monotonic() >= deadline:
                            raise TimeoutError("测试服务未在 30 秒内就绪")
                        time.sleep(0.1)

                def request_one(index: int):
                    if not hasattr(local, "session"):
                        local.session = requests.Session()
                        local.session.trust_env = False
                        sessions.append(local.session)
                    user = index % args.users
                    first_id = user + 1
                    polling = index % 5 != 0
                    path = (
                        f"/task-statuses?task_type=document_check&ids={first_id}"
                        if polling
                        else "/"
                    )
                    start = time.perf_counter()
                    try:
                        with local.session.get(
                            base_url + path,
                            headers={"X-Benchmark-User": f"bench-{user}"},
                            timeout=30,
                        ) as response:
                            response.raise_for_status()
                            if polling:
                                payload = response.json()
                                if [task["id"] for task in payload["tasks"]] != [
                                    first_id
                                ]:
                                    raise AssertionError("轮询响应与用户所属任务不一致")
                            elif "example.txt" not in response.text:
                                raise AssertionError("任务列表缺少测试文档")
                        return time.perf_counter() - start, None
                    except Exception as error:
                        return time.perf_counter() - start, str(error)

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=args.concurrency
                ) as executor:
                    warmup_results = list(executor.map(request_one, range(args.users)))
                    if any(error for _, error in warmup_results):
                        raise RuntimeError("预热请求失败")
                    start = time.perf_counter()
                    results = list(executor.map(request_one, range(args.requests)))
                elapsed = time.perf_counter() - start
                latencies = sorted(duration * 1000 for duration, _ in results)
                errors = [error for _, error in results if error]

                def percentile(fraction):
                    return round(
                        latencies[
                            min(len(latencies) - 1, int(len(latencies) * fraction))
                        ],
                        2,
                    )

                return {
                    "tasks": args.tasks,
                    "users": args.users,
                    "requests": args.requests,
                    "concurrency": args.concurrency,
                    "web_server": "uvicorn",
                    "web_workers": DEFAULT_WEB_WORKERS,
                    "web_threads_per_worker": 16,
                    "status_requests_percent": 80,
                    "task_list_requests_percent": 20,
                    "errors": len(errors),
                    "error_samples": errors[:5],
                    "elapsed_seconds": round(elapsed, 3),
                    "requests_per_second": round(args.requests / elapsed, 2),
                    "latency_ms": {
                        "p50": percentile(0.5),
                        "p95": percentile(0.95),
                        "p99": percentile(0.99),
                    },
                }
            finally:
                for session in sessions:
                    session.close()
                try:
                    children = psutil.Process(process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    children = []
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                _, alive = psutil.wait_procs(children, timeout=20)
                for child in alive:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                psutil.wait_procs(alive, timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=10000)
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--source-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    result = benchmark(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
