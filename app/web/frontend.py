"""前端构建资源与服务端组件的展示协议。"""

import json
from pathlib import Path, PurePosixPath

from flask import current_app, url_for

from app.contracts.task_types import CONSISTENCY_TASK_TYPE
from app.tasks.submission import _consistency_task_title


def frontend_assets() -> dict:
    path = Path(current_app.static_folder) / "frontend" / "manifest.json"
    signature = path.stat().st_mtime_ns if path.is_file() else None
    cached = current_app.extensions.get("frontend_manifest")
    if cached is None or cached[0] != signature:
        if signature is None:
            raise RuntimeError(
                "前端资源未构建，请执行 npm --prefix frontend run build。"
            )
        manifest = json.loads(path.read_text(encoding="utf-8"))
        current_app.extensions["frontend_manifest"] = (signature, manifest)
    else:
        manifest = cached[1]
    entry = manifest["src/main.ts"]
    styles = []
    visited = set()

    def collect(chunk):
        for css in chunk.get("css", []):
            if css not in styles:
                styles.append(css)
        for name in chunk.get("imports", []):
            if name not in visited:
                visited.add(name)
                collect(manifest[name])

    collect(entry)

    def asset(filename):
        parts = PurePosixPath(filename)
        if parts.is_absolute() or ".." in parts.parts or "\\" in filename:
            raise ValueError("前端资源路径必须位于构建目录。")
        return url_for("static", filename=f"frontend/{filename}")

    return {"script": asset(entry["file"]), "styles": [asset(css) for css in styles]}


def task_display_title(task, full=False):
    if task["task_type"] == CONSISTENCY_TASK_TYPE:
        return _consistency_task_title(task, full)
    return task["original_filename"]
