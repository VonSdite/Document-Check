from collections import OrderedDict
from pathlib import Path
from threading import RLock
from typing import Callable, TypeVar

T = TypeVar("T")
_MAX_CACHE_ENTRIES = 16
_cache_lock = RLock()
_cache: OrderedDict[
    tuple[str, str], tuple[tuple[int, int, int], tuple[object, ...]]
] = OrderedDict()


def load_cached_terms(
    namespace: str, path: Path, loader: Callable[[Path], list[T]]
) -> list[T]:
    resolved_path = Path(path).resolve()
    cache_key = (str(namespace), str(resolved_path))
    signature = _file_signature(resolved_path)
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            _cache.move_to_end(cache_key)
            return list(cached[1])

        values = tuple(loader(resolved_path))
        final_signature = _file_signature(resolved_path)
        if final_signature != signature:
            values = tuple(loader(resolved_path))
            final_signature = _file_signature(resolved_path)
        _cache[cache_key] = (final_signature, values)
        _cache.move_to_end(cache_key)
        while len(_cache) > _MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
        return list(values)


def clear_term_file_cache():
    with _cache_lock:
        _cache.clear()


def _file_signature(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
