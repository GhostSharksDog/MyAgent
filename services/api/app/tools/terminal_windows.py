"""Windows Job Objects for ordinary child-process lifetime management.

This is a lifetime boundary, not a security sandbox. Windows permits some other
launch mechanisms (for example WMI) to create processes outside the caller's job.
No application code runs before a new shell has been assigned to its job.
"""

from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path


class _IOCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


def _kernel():
    if os.name != "nt":
        raise OSError("Windows Job Objects are available only on Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    word = ctypes.c_uint32
    declarations = {
        "CreateJobObjectW": ([handle, ctypes.c_wchar_p], handle),
        "SetInformationJobObject": ([handle, ctypes.c_int, handle, word], ctypes.c_int),
        "AssignProcessToJobObject": ([handle, handle], ctypes.c_int),
        "TerminateJobObject": ([handle, word], ctypes.c_int),
        "QueryInformationJobObject": ([handle, ctypes.c_int, handle, word, handle], ctypes.c_int),
        "CloseHandle": ([handle], ctypes.c_int),
        "CreateToolhelp32Snapshot": ([word, word], handle),
        "Thread32First": ([handle, ctypes.POINTER(_ThreadEntry)], ctypes.c_int),
        "Thread32Next": ([handle, ctypes.POINTER(_ThreadEntry)], ctypes.c_int),
        "OpenThread": ([word, ctypes.c_int, word], handle),
        "ResumeThread": ([handle], word),
        "GetWindowsDirectoryW": ([ctypes.c_wchar_p, word], word),
    }
    for name, (args, result) in declarations.items():
        fn = getattr(api, name)
        fn.argtypes = args
        fn.restype = result
    return api


def _failed(operation: str) -> OSError:
    error = ctypes.get_last_error()
    return OSError(error, f"{operation}: {ctypes.FormatError(error)}")


def powershell_path() -> Path:
    api = _kernel()
    buffer = ctypes.create_unicode_buffer(32768)
    length = api.GetWindowsDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise _failed("GetWindowsDirectoryW")
    result = Path(buffer.value) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not result.is_file():
        raise OSError("系统 Windows PowerShell 不存在；请安装系统 PowerShell 后再开启终端")
    return result


class WindowsJob:
    """Assign a suspended Popen shell, then resume its sole initial thread."""

    def __init__(self):
        self.api = _kernel()
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise _failed("CreateJobObjectW")
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = _failed("SetInformationJobObject")
            self.close()
            raise error

    def attach_and_resume(self, process_handle: int, pid: int) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, process_handle):
            raise _failed("AssignProcessToJobObject")
        # CPython Popen closes CreateProcess's thread handle. The shell is still
        # suspended and has never run, so it must have exactly one initial thread.
        snapshot = self.api.CreateToolhelp32Snapshot(4, 0)  # SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise _failed("CreateToolhelp32Snapshot")
        ids = []
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            more = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    ids.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                more = self.api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        if len(ids) != 1:
            raise OSError("无法唯一确认挂起 shell 的主线程；终端启动已拒绝，请检查进程注入软件")
        thread = self.api.OpenThread(2, False, ids[0])  # THREAD_SUSPEND_RESUME
        if not thread:
            raise _failed("OpenThread")
        try:
            previous = self.api.ResumeThread(thread)
            if previous == 0xFFFFFFFF:
                raise _failed("ResumeThread")
            if previous != 1:
                raise OSError("shell 主线程挂起状态异常；终端启动已拒绝")
        finally:
            self.api.CloseHandle(thread)

    def terminate(self, *, wait_seconds: float = 5.0) -> None:
        if not self.handle:
            return
        if not self.api.TerminateJobObject(self.handle, 1):
            raise _failed("TerminateJobObject")
        deadline = time.monotonic() + wait_seconds
        while True:
            accounting = _Accounting()
            if not self.api.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            ):
                raise _failed("QueryInformationJobObject")
            if accounting.ActiveProcesses == 0:
                return
            if time.monotonic() >= deadline:
                raise OSError("终端进程树未在清理期限内退出；请检查系统进程状态")
            time.sleep(0.01)

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
