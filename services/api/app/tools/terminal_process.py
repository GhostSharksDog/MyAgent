"""Bounded, noninteractive shell execution without asyncio subprocess support.

Callers supply an explicit environment and already-authorized absolute directory.
Windows jobs and POSIX groups stop ordinary descendants; neither is a security
sandbox. Commands can access everything the API OS identity can access.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.tools.terminal_windows import WindowsJob, powershell_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    encoding_errors: bool = False

    @property
    def truncated(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated


def shell_name() -> str:
    return "powershell" if os.name == "nt" else "sh"


def available() -> bool:
    try:
        if os.name == "nt":
            powershell_path()
            job = WindowsJob()
            job.close()
            return True
        return os.name == "posix" and Path("/bin/sh").is_file()
    except (AttributeError, OSError):
        return False


def _shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        # -EncodedCommand protects multiline text and Chinese from PS 5.1's
        # locale and shell quoting. It encodes the exact approved command body.
        wrapper = (
            "$ProgressPreference = 'SilentlyContinue'; "
            "$ErrorActionPreference = 'Stop'; "
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "$OutputEncoding = [Console]::OutputEncoding; "
            "try { & {\n" + command + "\n}; "
            "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE } "
            "} catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }"
        )
        encoded = base64.b64encode(wrapper.encode("utf-16-le")).decode("ascii")
        argv = [
            str(powershell_path()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ]
        if len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 > 32766:
            raise ValueError("命令超出 Windows 启动长度，请拆分任务后重新确认")
        return argv
    if os.name == "posix" and Path("/bin/sh").is_file():
        return ["/bin/sh", "-c", command]
    raise OSError("当前系统没有支持的终端；请使用 Windows PowerShell 或 POSIX /bin/sh")


class _Capture:
    def __init__(self, stream: BinaryIO, limit: int):
        self.stream = stream
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self.error: OSError | None = None
        self.thread = threading.Thread(target=self._read, daemon=True, name="terminal-pipe")
        self.thread.start()

    def _read(self) -> None:
        try:
            while block := self.stream.read(8192):
                remaining = self.limit - len(self.data)
                self.data.extend(block[:remaining])
                if len(block) > remaining:
                    self.truncated = True
                # Continue draining after the limit; stopping here deadlocks a
                # child that is writing more than the pipe capacity.
        except OSError as exc:
            self.error = exc
        finally:
            self.stream.close()

    def finish(self) -> None:
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise OSError("终端输出管道未关闭；后台进程可能逃出清理范围，请检查系统进程")
        if self.error:
            raise self.error

    def decoded(self) -> tuple[str, bool]:
        raw = bytes(self.data)
        try:
            return raw.decode("utf-8"), False
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace"), True


@dataclass
class _Running:
    process: subprocess.Popen
    stdout: _Capture
    stderr: _Capture
    job: WindowsJob | None

    def cleanup(self) -> None:
        try:
            if self.job:
                self.job.terminate()
            else:
                # Also kill descendants when the original shell exited normally.
                # A deliberately detached setsid child can escape this group.
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.process.wait(timeout=5)
        finally:
            if self.job:
                self.job.close()
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            if os.name == "nt":
                # Popen otherwise waits for garbage collection to close this
                # handle. Completed asyncio tasks can retain its result longer.
                self.process._handle.Close()
            # Drain both streams even if the first reports an error.
            first_error = None
            for capture in (self.stdout, self.stderr):
                try:
                    capture.finish()
                except OSError as exc:
                    first_error = first_error or exc
            if first_error:
                raise first_error


def _start(command: str, cwd: Path, env: Mapping[str, str], output_limit: int) -> _Running:
    argv = _shell_argv(command)
    process = None
    child_env = dict(env)
    if os.name == "nt":
        # PS 5.1 can silently skip even an absolute .exe path without PATHEXT.
        # Supply fixed OS extensions, rather than implicitly inheriting all env.
        child_env.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    job = WindowsJob() if os.name == "nt" else None
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            close_fds=True,
            creationflags=(0x00000004 | 0x08000000) if job else 0,  # SUSPENDED | NO_WINDOW
            start_new_session=not bool(job),
        )
        logger.debug("终端进程已创建：pid=%s，等待绑定清理范围", process.pid)
        if job:
            job.attach_and_resume(int(process._handle), process.pid)
        logger.debug("终端进程已恢复：pid=%s", process.pid)
        return _Running(
            process,
            _Capture(process.stdout, output_limit),
            _Capture(process.stderr, output_limit),
            job,
        )
    except BaseException:
        # Assign/Resume failure must not leave a suspended orphan or run the
        # command without job protection. Closing a job kills assigned shells.
        if job:
            job.close()
        if process:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if os.name == "nt":
                process._handle.Close()
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
        raise


async def _settle(task: asyncio.Task):
    """Finish a spawn/cleanup task despite repeated cancellation requests."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


async def run_command(
    command: str,
    cwd: Path,
    *,
    timeout: float,  # noqa: ASYNC109 — process lifetime includes cancellation-safe cleanup.
    env: Mapping[str, str],
    output_limit: int = 32768,
) -> CommandResult:
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ValueError("终端命令不能为空，也不能包含 NUL 字符")
    cwd = Path(cwd)
    if not cwd.is_absolute() or not await asyncio.to_thread(cwd.is_dir):
        raise ValueError("终端工作目录必须是已批准的现存绝对目录")
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or not isinstance(output_limit, int)
        or isinstance(output_limit, bool)
        or output_limit <= 0
    ):
        raise ValueError("终端超时与输出上限必须大于零")
    # A cancellation already requested before launch must not start a process.
    await asyncio.sleep(0)
    started = time.monotonic()
    spawn = asyncio.create_task(asyncio.to_thread(_start, command, cwd, env, output_limit))
    running = None
    timed_out = False
    try:
        running = await asyncio.shield(spawn)
        while running.process.poll() is None:
            if time.monotonic() - started >= timeout:
                timed_out = True
                logger.warning(
                    "终端进程达到时限：pid=%s，stdout_bytes=%s，stderr_bytes=%s",
                    running.process.pid,
                    len(running.stdout.data),
                    len(running.stderr.data),
                )
                break
            await asyncio.sleep(min(0.02, max(0, timeout - (time.monotonic() - started))))
    except asyncio.CancelledError as cancelled:
        # A Popen in a worker thread cannot be cancelled. Obtain its owned
        # process first, then clean it before releasing registry serialization.
        try:
            if running is None:
                running = await _settle(spawn)
            await _settle(asyncio.create_task(asyncio.to_thread(running.cleanup)))
        except Exception as exc:
            # Startup failure does not turn a cancelled request into an error
            # response. A cleanup failure remains observable in server logs.
            logger.error("已取消的终端任务在启动或清理阶段失败：%s", type(exc).__name__)
            cancelled.add_note("终端启动或清理失败；请检查服务日志和系统进程")
        raise
    except BaseException:
        if running:
            await _settle(asyncio.create_task(asyncio.to_thread(running.cleanup)))
        raise
    cleanup = asyncio.create_task(asyncio.to_thread(running.cleanup))
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError as cancelled:
        try:
            await _settle(cleanup)
        except Exception as exc:
            logger.error("已取消的终端任务清理失败：%s", type(exc).__name__)
            cancelled.add_note("终端清理失败；请检查服务日志和系统进程")
        raise
    stdout, stdout_error = running.stdout.decoded()
    stderr, stderr_error = running.stderr.decoded()
    return CommandResult(
        exit_code=running.process.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=(time.monotonic() - started) * 1000,
        stdout_truncated=running.stdout.truncated,
        stderr_truncated=running.stderr.truncated,
        timed_out=timed_out,
        encoding_errors=stdout_error or stderr_error,
    )
