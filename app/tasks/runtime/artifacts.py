import logging
import os
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from app.contracts.task_types import document_groups_from_meta
from app.documents.images import (
    default_image_folder,
    image_items_from_meta,
    image_path_from_item,
)
from app.infrastructure.files import describe_failures, remove_file
from app.infrastructure.files import (
    remove_empty_directory as cleanup_remove_empty_directory,
)
from app.persistence.connection import get_db, now_text
from app.persistence.settings import get_setting

logger = logging.getLogger(__name__)


DEFAULT_TASK_FILE_RETENTION_DAYS = 0
TASK_FILE_CLEANUP_BATCH_SIZE = 100
TASK_FILE_CACHE_SNAPSHOT_TTL_SECONDS = 15
_TASK_FILE_CACHE_STATE_INIT_LOCK = threading.Lock()


class TaskArtifactCleanupError(RuntimeError):
    pass


def _task_value(task, key: str):
    if hasattr(task, "keys") and key in task.keys():
        return task[key]
    if isinstance(task, dict):
        return task.get(key)
    return None


def _int_setting(key: str, default: int) -> int:
    value = get_setting(key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _task_image_folder(app) -> Path:
    configured = app.config.get("IMAGE_FOLDER")
    if configured:
        return Path(configured)
    return default_image_folder(app.config["UPLOAD_FOLDER"])


def cleanup_expired_task_files(app) -> int:
    retention_days = _task_file_retention_days()
    if retention_days <= 0:
        return 0

    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    db = get_db()
    tasks = db.execute(
        """
        SELECT id, stored_filename, document_meta_json
        FROM tasks
        WHERE status IN ('completed', 'partial', 'failed', 'canceled')
          AND source_files_cleaned_at IS NULL
          AND COALESCE(finished_at, updated_at, created_at) < ?
        ORDER BY COALESCE(finished_at, updated_at, created_at) ASC, id ASC
        LIMIT ?
        """,
        (cutoff, TASK_FILE_CLEANUP_BATCH_SIZE),
    ).fetchall()
    updates = []
    for task in tasks:
        try:
            _remove_task_artifacts(app, task)
            updates.append((now_text(), task["id"]))
        except TaskArtifactCleanupError as exc:
            logger.warning("定期清理任务文件跳过 task_id=%s error=%s", task["id"], exc)
        except Exception:
            logger.exception("定期清理任务文件失败 task_id=%s", task["id"])
    cleaned = len(updates)
    if updates:
        db.executemany(
            "UPDATE tasks SET document_text = NULL, source_files_cleaned_at = ? WHERE id = ?",
            updates,
        )
        db.commit()
        _invalidate_task_file_cache_snapshot(app)
        logger.info(
            "定期清理任务文件完成 cleaned=%s cutoff=%s retention_days=%s",
            cleaned,
            cutoff,
            retention_days,
        )
    return cleaned


def task_file_cache_snapshot(app, *, force: bool = False) -> dict:
    """返回任务文件缓存快照，并在短时间内复用已计算结果。

    该页面每分钟会刷新一次。快照计算涉及大量文件 stat 和元数据解析，
    因此使用进程内短缓存，避免同一进程内的重复扫描。
    """

    state = _task_file_cache_state(app)
    now = time.monotonic()
    with state["lock"]:
        if (
            not force
            and state["snapshot"] is not None
            and now - state["cached_at"] < TASK_FILE_CACHE_SNAPSHOT_TTL_SECONDS
        ):
            return deepcopy(state["snapshot"])
        snapshot = _build_task_file_cache_snapshot(app)
        state["snapshot"] = snapshot
        state["cached_at"] = time.monotonic()
        return deepcopy(snapshot)


def task_file_cache_snapshot_async(app) -> tuple[dict, bool]:
    """非阻塞地获取快照；首次计算由后台线程完成。"""

    state = _task_file_cache_state(app)
    now = time.monotonic()
    with state["lock"]:
        if (
            state["snapshot"] is not None
            and now - state["cached_at"] < TASK_FILE_CACHE_SNAPSHOT_TTL_SECONDS
        ):
            return deepcopy(state["snapshot"]), True
        if not state["building"]:
            state["building"] = True
            generation = state["generation"]
            threading.Thread(
                target=_build_task_file_cache_snapshot_background,
                args=(app, generation),
                daemon=True,
                name="task-file-cache-snapshot",
            ).start()
        return _empty_task_file_cache_snapshot(), False


def _task_file_cache_state(app) -> dict:
    state = app.extensions.get("task_file_cache_snapshot")
    if state is not None:
        return state
    with _TASK_FILE_CACHE_STATE_INIT_LOCK:
        state = app.extensions.get("task_file_cache_snapshot")
        if state is None:
            state = {
                "lock": threading.RLock(),
                "snapshot": None,
                "cached_at": 0.0,
                "building": False,
                "generation": 0,
            }
            app.extensions["task_file_cache_snapshot"] = state
    return state


def _invalidate_task_file_cache_snapshot(app) -> None:
    state = _task_file_cache_state(app)
    with state["lock"]:
        state["snapshot"] = None
        state["cached_at"] = 0.0
        state["generation"] += 1


def _build_task_file_cache_snapshot_background(app, generation: int) -> None:
    state = _task_file_cache_state(app)
    try:
        with app.app_context():
            snapshot = _build_task_file_cache_snapshot(app)
        with state["lock"]:
            if state["generation"] == generation:
                state["snapshot"] = snapshot
                state["cached_at"] = time.monotonic()
    except Exception:
        logger.exception("后台生成任务文件缓存快照失败")
    finally:
        with state["lock"]:
            state["building"] = False


def _empty_task_file_cache_snapshot() -> dict:
    return {
        "generated_at": now_text(),
        "total_size_bytes": 0,
        "upload_size_bytes": 0,
        "generated_size_bytes": 0,
        "cleanable_size_bytes": 0,
        "cleanable_count": 0,
        "items": [],
    }


def _build_task_file_cache_snapshot(app) -> dict:
    db = get_db()
    file_index, upload_size_bytes, generated_size_bytes = _task_file_cache_file_index(
        app
    )
    tasks = db.execute(
        """
        SELECT id, task_type, original_filename, stored_filename, document_meta_json,
               created_at, updated_at, finished_at
        FROM tasks
        WHERE status IN ('completed', 'partial', 'failed', 'canceled')
          AND source_files_cleaned_at IS NULL
        """
    )
    items = []
    for task in tasks:
        size_bytes, file_count = _task_artifact_usage(app, task, file_index=file_index)
        if file_count <= 0:
            continue
        finished_at = task["finished_at"] or task["updated_at"] or task["created_at"]
        items.append(
            {
                "id": int(task["id"]),
                "task_type": task["task_type"],
                "original_filename": task["original_filename"],
                "document_meta_json": task["document_meta_json"],
                "finished_at": finished_at,
                "size_bytes": size_bytes,
                "file_count": file_count,
            }
        )
    items.sort(key=lambda item: (item["finished_at"], item["size_bytes"], item["id"]))

    return {
        "generated_at": now_text(),
        "total_size_bytes": upload_size_bytes + generated_size_bytes,
        "upload_size_bytes": upload_size_bytes,
        "generated_size_bytes": generated_size_bytes,
        "cleanable_size_bytes": sum(item["size_bytes"] for item in items),
        "cleanable_count": len(items),
        "items": items,
    }


def _task_file_cache_file_index(app) -> tuple[dict[str, int], int, int]:
    index: dict[str, int] = {}
    totals = []
    for root in (Path(app.config["UPLOAD_FOLDER"]), _task_image_folder(app)):
        total = 0
        for path, size_bytes in _iter_regular_files(root):
            index[_artifact_path_key(path)] = size_bytes
            total += size_bytes
        totals.append(total)
    return index, totals[0], totals[1]


def _iter_regular_files(root: Path):
    """递归遍历常规文件，避免 Path.rglob 对大量文件反复创建对象。"""

    if not root.is_dir():
        return
    pending = [os.fspath(root)]
    while pending:
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        yield (
                            Path(entry.path),
                            int(entry.stat(follow_symlinks=False).st_size),
                        )
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                except OSError:
                    continue


def _artifact_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def cleanup_task_file_cache(app, task_ids: list[int]) -> dict:
    normalized_ids = []
    for value in task_ids:
        try:
            task_id = int(value)
        except (TypeError, ValueError):
            continue
        if task_id > 0 and task_id not in normalized_ids:
            normalized_ids.append(task_id)

    result = {"cleaned_ids": [], "freed_size_bytes": 0, "failed": [], "skipped_ids": []}
    if not normalized_ids:
        return result

    db = get_db()
    placeholders = ",".join("?" for _ in normalized_ids)
    tasks = {
        int(task["id"]): task
        for task in db.execute(
            f"""
            SELECT id, stored_filename, document_meta_json
            FROM tasks
            WHERE id IN ({placeholders})
              AND status IN ('completed', 'partial', 'failed', 'canceled')
              AND source_files_cleaned_at IS NULL
            """,
            tuple(normalized_ids),
        ).fetchall()
    }
    for task_id in normalized_ids:
        task = tasks.get(task_id)
        if task is None:
            result["skipped_ids"].append(task_id)
            continue
        size_bytes, _ = _task_artifact_usage(app, task)
        try:
            _remove_task_artifacts(app, task)
        except TaskArtifactCleanupError as exc:
            result["failed"].append({"id": task_id, "error": str(exc)})
            continue
        except Exception:
            logger.exception("手动清理任务文件失败 task_id=%s", task_id)
            result["failed"].append({"id": task_id, "error": "清理失败，请稍后重试。"})
            continue
        result["cleaned_ids"].append(task_id)
        result["freed_size_bytes"] += size_bytes

    if result["cleaned_ids"]:
        db.executemany(
            "UPDATE tasks SET document_text = NULL, source_files_cleaned_at = ? WHERE id = ?",
            [(now_text(), task_id) for task_id in result["cleaned_ids"]],
        )
        db.commit()
        _invalidate_task_file_cache_snapshot(app)
        logger.info(
            "手动清理任务文件完成 cleaned=%s freed_size_bytes=%s",
            len(result["cleaned_ids"]),
            result["freed_size_bytes"],
        )
    return result


def _task_file_retention_days() -> int:
    return max(
        0, _int_setting("task_file_retention_days", DEFAULT_TASK_FILE_RETENTION_DAYS)
    )


def _remove_task_artifacts(app, task):
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    image_root = _task_image_folder(app)
    paths = _task_artifact_paths(app, task)
    image_dirs = {
        path.parent
        for path in paths
        if path.parent.resolve() != image_root.resolve()
        and _path_is_relative_to(path.parent, image_root)
    }
    failures = []
    for path in paths:
        if not (
            _path_is_relative_to(path, upload_root)
            or _path_is_relative_to(path, image_root)
        ):
            logger.warning(
                "跳过不在运行目录内的任务文件 task_id=%s path=%s", task["id"], path
            )
            continue
        if path.exists() and path.is_file():
            ok, error = remove_file(path)
            if not ok:
                failures.append((path, error))
    for image_dir in sorted(
        image_dirs, key=lambda value: len(value.parts), reverse=True
    ):
        _remove_empty_directory(image_dir)
    if failures:
        raise TaskArtifactCleanupError(
            f"任务文件正被其他程序使用，暂时无法清理：{describe_failures(failures)}。"
            "请关闭正在下载、预览或扫描该文件的程序后稍后重试。"
        )


def _task_artifact_paths(app, task) -> list[Path]:
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    image_root = _task_image_folder(app)
    raw_meta = _task_value(task, "document_meta_json")
    paths = []
    groups = document_groups_from_meta(raw_meta)
    if groups:
        for group in groups:
            for file_info in group["files"]:
                stored_filename = Path(str(file_info.get("stored_filename") or "")).name
                if stored_filename:
                    paths.append(upload_root / stored_filename)
    else:
        stored_filename = Path(str(_task_value(task, "stored_filename") or "")).name
        if stored_filename:
            paths.append(upload_root / stored_filename)

    for image in image_items_from_meta(raw_meta):
        image_path = image_path_from_item(image_root, image)
        if image_path is not None:
            paths.append(image_path)
    for image in image_items_from_meta(raw_meta, "page_images"):
        image_path = image_path_from_item(image_root, image)
        if image_path is not None:
            paths.append(image_path)
    for image in image_items_from_meta(raw_meta, "frames"):
        image_path = image_path_from_item(image_root, image)
        if image_path is not None:
            paths.append(image_path)
    return _dedupe_paths(paths)


def _task_artifact_usage(
    app, task, *, file_index: dict[str, int] | None = None
) -> tuple[int, int]:
    upload_root = Path(app.config["UPLOAD_FOLDER"])
    image_root = _task_image_folder(app)
    size_bytes = 0
    file_count = 0
    for path in _task_artifact_paths(app, task):
        if not (
            _path_is_relative_to(path, upload_root)
            or _path_is_relative_to(path, image_root)
        ):
            continue
        if file_index is not None:
            artifact_size = file_index.get(_artifact_path_key(path))
            if artifact_size is None:
                continue
            size_bytes += artifact_size
            file_count += 1
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size_bytes += path.stat().st_size
            file_count += 1
        except OSError:
            continue
    return size_bytes, file_count


def _directory_file_size(root: Path) -> int:
    return sum(size_bytes for _, size_bytes in _iter_regular_files(Path(root)))


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _remove_empty_directory(path: Path):
    return cleanup_remove_empty_directory(path)
