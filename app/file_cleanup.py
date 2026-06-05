import shutil
import time
from pathlib import Path


LOCKED_FILE_WINERRORS = {32, 33}


def remove_file(path: Path, *, retries: int = 2, delay_seconds: float = 0.2) -> tuple[bool, str]:
    target = Path(path)
    last_error = None
    for attempt in range(max(0, retries) + 1):
        try:
            if not target.exists():
                return True, ""
            if target.is_file() or target.is_symlink():
                target.unlink()
            return True, ""
        except FileNotFoundError:
            return True, ""
        except OSError as exc:
            last_error = exc
            if not is_transient_file_access_error(exc) or attempt >= retries:
                return False, file_error_text(exc)
            time.sleep(delay_seconds * (attempt + 1))
    return False, file_error_text(last_error)


def remove_directory_tree(path: Path, *, retries: int = 2, delay_seconds: float = 0.2) -> tuple[bool, str]:
    target = Path(path)
    last_error = None
    for attempt in range(max(0, retries) + 1):
        try:
            if not target.exists():
                return True, ""
            if target.is_dir():
                shutil.rmtree(target)
            return True, ""
        except FileNotFoundError:
            return True, ""
        except OSError as exc:
            last_error = exc
            if not is_transient_file_access_error(exc) or attempt >= retries:
                return False, file_error_text(exc)
            time.sleep(delay_seconds * (attempt + 1))
    return False, file_error_text(last_error)


def remove_empty_directory(path: Path) -> bool:
    try:
        Path(path).rmdir()
        return True
    except OSError:
        return False


def is_transient_file_access_error(exc: BaseException) -> bool:
    return getattr(exc, "winerror", None) in LOCKED_FILE_WINERRORS


def file_error_text(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    winerror = getattr(exc, "winerror", None)
    if winerror:
        return f"[WinError {winerror}] {exc}"
    return str(exc)


def describe_failures(failures: list[tuple[Path, str]], *, limit: int = 3) -> str:
    if not failures:
        return ""
    names = [Path(path).name or str(path) for path, _ in failures[:limit]]
    suffix = f" 等 {len(failures)} 个文件" if len(failures) > limit else ""
    return "、".join(names) + suffix
