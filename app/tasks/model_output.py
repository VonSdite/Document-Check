"""按任务追加模型响应，按字节游标读取；输出文件随任务产物一起清理。"""

import json
import logging
import threading
from pathlib import Path

from app.documents.images import default_image_folder

logger = logging.getLogger(__name__)
READ_BYTES = 64 * 1024
TEXT_CHARS = 4096
FLUSH_INTERVAL_SECONDS = 0.25


def model_output_path(app, task_id):
    root = app.config.get("IMAGE_FOLDER") or default_image_folder(
        app.config["UPLOAD_FOLDER"]
    )
    return Path(root) / f"model-output-{int(task_id)}.jsonl"


class ModelOutputRecorder:
    """同一任务共享写锁和缓冲；合并请求只保存一份响应。"""

    def __init__(self, app, task_id):
        self.path = model_output_path(app, task_id)
        self.lock = threading.Lock()
        self.pending = []
        self.chars = 0
        self.timer = None
        self.disabled = False

    def for_checks(self, codes, label=""):
        codes = list(codes)

        def record(event):
            with self.lock:
                if self.disabled:
                    return
                event = dict(
                    event,
                    codes=event.get("codes", codes),
                    label=" · ".join(filter(None, [label, event.get("label", "")])),
                )
                text = event.pop("text", "")
                for offset in range(0, max(1, len(text)), TEXT_CHARS):
                    part = text[offset : offset + TEXT_CHARS]
                    previous = self.pending[-1] if self.pending else None
                    if (
                        part
                        and previous
                        and previous["stream"] == event["stream"]
                        and previous["kind"] == event["kind"]
                        and len(previous.get("text", "")) + len(part) <= TEXT_CHARS
                    ):
                        previous["text"] += part
                    else:
                        self.pending.append(dict(event, text=part))
                    self.chars += len(part)
                if event["kind"] in {"start", "end"} or self.chars >= TEXT_CHARS:
                    self._flush()
                elif self.timer is None:
                    self.timer = threading.Timer(
                        FLUSH_INTERVAL_SECONDS, self._flush_pending
                    )
                    self.timer.daemon = True
                    self.timer.start()

        return record

    def _flush_pending(self):
        with self.lock:
            if self.pending:
                self._flush()

    def _flush(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = "".join(
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                for event in self.pending
            ).encode("utf-8")
            with self.path.open("ab") as output:
                output.write(data)
        except OSError:
            self.disabled = True
            logger.warning("保存模型输出失败 path=%s", self.path, exc_info=True)
        finally:
            self.pending.clear()
            self.chars = 0


def read_model_output(app, task_id, cursor=0):
    """只读取新增完整记录；截断的末行等待写入完成后重读。"""
    path = model_output_path(app, task_id)
    try:
        with path.open("rb") as source:
            size = source.seek(0, 2)
            reset = cursor > size
            if reset:
                cursor = 0
            source.seek(cursor)
            data = source.read(READ_BYTES)
    except FileNotFoundError:
        return {"events": [], "cursor": 0, "more": False, "reset": cursor != 0}
    complete = data.rfind(b"\n") + 1
    events = []
    for line in data[:complete].splitlines():
        try:
            events.append(json.loads(line))
        except (ValueError, UnicodeError):
            continue
    return {
        "events": events,
        "cursor": cursor + complete,
        "more": complete > 0 and cursor + complete < size,
        "reset": reset,
    }
