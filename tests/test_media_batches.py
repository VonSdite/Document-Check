import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask

from app.persistence.connection import close_db, get_db
from app.tasks.runtime.media_batches import run_media_batches


class MediaBatchTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config["DATABASE"] = str(Path(self.root.name) / "media.sqlite3")
        self.app.teardown_appcontext(close_db)

    def tearDown(self):
        self.root.cleanup()

    def test_groups_share_gate_and_snapshot_callbacks_stay_on_owner_thread(self):
        gate = threading.BoundedSemaphore(3)
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active = peak = count = 0

        def group():
            owner = threading.get_ident()
            results = []

            def execute(number, batch, emit, canceled):
                nonlocal active, peak, count
                self.assertNotEqual(threading.get_ident(), owner)
                self.assertEqual(get_db().execute("SELECT 1").fetchone()[0], 1)
                with lock:
                    active += 1
                    count += 1
                    initial = count <= 3
                    peak = max(peak, active)
                try:
                    if initial:
                        barrier.wait(timeout=3)
                    emit(number, "部分结果")
                    time.sleep((4 - number) * 0.02)
                    return batch * 2
                finally:
                    with lock:
                        active -= 1

            def on_result(number, batch, result):
                self.assertEqual(threading.get_ident(), owner)
                results.append((number, result))

            def on_content(number, content):
                self.assertEqual(threading.get_ident(), owner)
                self.assertEqual(content, "部分结果")

            run_media_batches(
                self.app,
                [10, 20, 30],
                execute,
                on_content,
                on_result,
                lambda: None,
                threading.Event(),
                gate,
            )
            return sorted(results)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: group(), range(2)))
        self.assertEqual(peak, 3)
        self.assertEqual(active, 0)
        self.assertEqual(results, [[(1, 20), (2, 40), (3, 60)]] * 2)

    def test_snapshot_failure_cancels_running_requests_and_releases_slots(self):
        gate = threading.BoundedSemaphore(3)
        started = threading.Barrier(3)
        canceled_workers = []

        def execute(number, batch, emit, canceled):
            started.wait(timeout=3)
            emit(number, "正在输出")
            self.assertTrue(canceled.wait(3))
            canceled_workers.append(number)
            return "结果"

        def fail(*args):
            raise RuntimeError("快照保存失败")

        with self.assertRaisesRegex(RuntimeError, "快照保存失败"):
            run_media_batches(
                self.app,
                list(range(12)),
                execute,
                fail,
                fail,
                lambda: None,
                threading.Event(),
                gate,
            )
        self.assertEqual(sorted(canceled_workers), [1, 2, 3])
        for _ in range(3):
            self.assertTrue(gate.acquire(blocking=False))
        self.assertFalse(gate.acquire(blocking=False))

    def test_completed_stream_events_cannot_overwrite_final_result(self):
        results = []

        def execute(number, batch, emit, canceled):
            emit(number, "partial")
            return "done"

        def on_content(number, content):
            self.assertNotIn(number, results)

        run_media_batches(
            self.app,
            list(range(20)),
            execute,
            on_content,
            lambda number, *args: results.append(number),
            lambda: None,
            threading.Event(),
            threading.BoundedSemaphore(3),
        )
        self.assertEqual(sorted(results), list(range(1, 21)))


if __name__ == "__main__":
    unittest.main()
