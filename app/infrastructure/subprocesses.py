"""独立 Python 进程的解释器与运行目录。"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def python_module_command(module: str, *, root_dir=None) -> tuple[list[str], dict]:
    executable = sys.executable
    environment = dict(os.environ)
    if root_dir is not None:
        environment["DOCUMENTCHECK_ROOT_DIR"] = str(root_dir)
    environment["PYTHONIOENCODING"] = "utf-8"
    base = getattr(sys, "_base_executable", None) or executable
    if sys.platform == "win32" and os.path.normcase(executable) != os.path.normcase(
        base
    ):
        # 基础解释器直接拥有进程 PID，虚拟环境负责依赖与 sys.executable。
        environment["__PYVENV_LAUNCHER__"] = executable
        executable = base
    return [executable, "-u", "-m", module], environment
