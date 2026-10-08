"""冻结程序启动系统程序时短暂恢复 Windows 默认 DLL 搜索目录。"""

import ctypes
import os
import subprocess
import sys
import threading
from contextlib import contextmanager

_spawn_lock = threading.RLock()


def external_popen(*args, **kwargs):
    if getattr(sys, "frozen", False) and "env" in kwargs:
        environment = dict(kwargs["env"])
        root = os.path.normcase(sys._MEIPASS)
        environment["PATH"] = os.pathsep.join(
            part
            for part in environment.get("PATH", "").split(os.pathsep)
            if not os.path.normcase(part).startswith(root)
        )
        kwargs["env"] = environment
    with external_process_environment():
        return subprocess.Popen(*args, **kwargs)


@contextmanager
def external_process_environment():
    if os.name != "nt" or not getattr(sys, "frozen", False):
        yield
        return
    with _spawn_lock:
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        api.SetDllDirectoryW.restype = ctypes.c_int
        if not api.SetDllDirectoryW(None):
            raise OSError("无法恢复系统 DLL 搜索目录，外部程序未启动。")
        try:
            yield
        finally:
            if not api.SetDllDirectoryW(sys._MEIPASS):
                raise OSError("无法恢复 Legacy 的 DLL 搜索目录。")
