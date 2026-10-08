"""实际驱动发行版系统目录选择：只操作自己的唯一标题/PID窗口和公开临时目录。"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4

import httpx


def child_pids(parent_pid: int) -> set[int]:
    """只允许操作指定验收实例的直接子进程窗口。"""

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD),
            ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD),
            ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD),
            ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
            ("name", wintypes.WCHAR * 260),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(api, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        getattr(api, name).restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    found = set()
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        more = api.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.parent == parent_pid:
                found.add(entry.pid)
            more = api.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        api.CloseHandle(snapshot)
    return found


def verify(
    exe: Path, directory: Path, *, api_url: str = "", parent_pid: int | None = None
) -> dict:
    if sys.platform != "win32":
        raise RuntimeError("此验收需要 Windows 桌面会话")
    directory.mkdir(parents=True, exist_ok=True)
    selected = directory / "公开目录 中文 空格"
    selected.mkdir(exist_ok=True)
    environment = {
        **os.environ,
        "LEGACY_DATA_DIR": str(directory / "isolated-data"),
        "PATH": os.pathsep.join(
            [os.environ["SystemRoot"], str(Path(os.environ["SystemRoot"]) / "System32")]
        ),
    }
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    for name in ("GetWindowTextW", "GetClassNameW"):
        getattr(user32, name).argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
    user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetDlgItem.restype = wintypes.HWND
    user32.SendMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    results = []

    def check(condition, label):
        results.append({"check": label, "passed": bool(condition)})
        print(("PASS " if condition else "FAIL ") + label, flush=True)
        if not condition:
            raise AssertionError(label)

    def text(hwnd, api):
        buffer = ctypes.create_unicode_buffer(512)
        api(hwnd, buffer, len(buffer))
        return buffer.value

    def dialog(pids, title):
        found = []

        def visit(hwnd, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if (
                owner.value in pids
                and user32.IsWindowVisible(hwnd)
                and text(hwnd, user32.GetWindowTextW) == title
                and text(hwnd, user32.GetClassNameW) == "#32770"
            ):
                found.append(hwnd)
            return True

        user32.EnumWindows(callback_type(visit), 0)
        return found[0] if found else None

    def children(hwnd):
        found = []

        def visit(child, _):
            found.append(
                (child, text(child, user32.GetClassNameW), user32.GetDlgCtrlID(child))
            )
            return True

        user32.EnumChildWindows(hwnd, callback_type(visit), 0)
        return found

    for action in ("cancel", "select"):
        title = (
            "选择工作区文件夹" if api_url else "Legacy受控目录验收 " + uuid4().hex[:10]
        )
        out = directory / (action + ".json")
        process = None
        executor = None
        future = None
        if api_url:
            if parent_pid is None:
                raise ValueError("API 验收必须指定自己启动的父进程 PID")
            executor = ThreadPoolExecutor(max_workers=1)
            existing_children = child_pids(parent_pid)
            future = executor.submit(
                httpx.post,
                api_url + "api/files/pick",
                json={},
                trust_env=False,
                timeout=60,
            )
        else:
            process = subprocess.Popen(
                [
                    str(exe.resolve()),
                    "--legacy-pick-directory",
                    str(out.resolve()),
                    title,
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        hwnd = None

        def active(future=future, process=process):
            return not future.done() if future else process.poll() is None

        def owners(future=future, process=process):
            return child_pids(parent_pid) if future else {process.pid}

        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and active():
                hwnd = dialog(owners(), title)
                if hwnd:
                    break
                time.sleep(0.1)
            check(bool(hwnd), action + "：发行程序实际显示专用系统对话框")
            kinds = {kind for _, kind, _ in children(hwnd)}
            check(
                "DirectUIHWND" in kinds and "Breadcrumb Parent" in kinds,
                action + "：现代文件夹选择器含地址栏",
            )
            if action == "cancel":
                check(
                    user32.PostMessageW(hwnd, 0x0010, 0, 0),
                    "取消：向自己的对话框发送关闭",
                )
            else:
                edits = [
                    child
                    for child, kind, ident in children(hwnd)
                    if kind == "Edit" and ident in (1148, 1152)
                ]
                check(bool(edits), "选择：找到目录输入框")
                buffer = ctypes.create_unicode_buffer(str(selected.resolve()))
                user32.SendMessageW(edits[0], 0x000C, 0, ctypes.addressof(buffer))
                button = user32.GetDlgItem(hwnd, 1)
                check(
                    button and user32.PostMessageW(button, 0x00F5, 0, 0),
                    "选择：提交明确的公开临时目录",
                )
                # 文件夹输入首先导航到目标；再次点“选择文件夹”才确认当前目录。
                time.sleep(1)
                if active() and dialog(owners(), title):
                    user32.PostMessageW(button, 0x00F5, 0, 0)
            if future:
                response = future.result(timeout=20)
                check(response.status_code == 200, action + "：真实选择接口返回成功")
                payload = response.json()
            else:
                check(process.wait(timeout=15) == 0, action + "：worker正常退出")
                payload = json.loads(out.read_text(encoding="utf-8"))
            if action == "cancel":
                check(
                    payload["path"] is None
                    and payload["cancelled"]
                    and not payload.get("error"),
                    "取消：返回取消结果而非异常",
                )
            else:
                check(
                    not payload["cancelled"]
                    and not payload.get("error")
                    and Path(payload["path"]).resolve() == selected.resolve(),
                    "选择：返回完整中文及空格绝对目录",
                )
            if future:
                check(
                    not (child_pids(parent_pid) - existing_children),
                    action + "：目录选择子进程已回收",
                )
            else:
                check(
                    not (directory / "isolated-data" / "instance.json").exists()
                    and not (directory / "isolated-data" / "instance.lock").exists(),
                    action + "：worker没有启动服务或托盘实例",
                )
        finally:
            if active():
                if hwnd:
                    user32.PostMessageW(hwnd, 0x0010, 0, 0)
                if process:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            if executor:
                executor.shutdown(wait=True)
    return {
        "checks": results,
        "windows_gui_actual": True,
        "transport": "POST /api/files/pick" if api_url else "frozen worker IPC",
        "environment": "Windows desktop; isolated directory; Windows-only PATH",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument(
        "--report", type=Path, default=Path("data/frozen-picker-verification.json")
    )
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="legacy-frozen-picker-"))
    result = verify(args.exe, directory)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
