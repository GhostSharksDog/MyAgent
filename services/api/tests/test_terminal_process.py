"""Real local shells/process trees, using only synthetic temporary scripts."""

from __future__ import annotations

import asyncio
import ctypes
import os
import shlex
import sys
import threading
from pathlib import Path

import pytest
from app.tools import terminal_process as runner
from app.tools.terminal_windows import WindowsJob


def child_env() -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def quoted(value: str) -> str:
    if os.name == "nt":
        return "'" + value.replace("'", "''") + "'"
    return shlex.quote(value)


def python_command(script: Path) -> str:
    prefix = "& " if os.name == "nt" else ""
    return prefix + quoted(sys.executable) + " " + quoted(str(script))


def script(tmp_path: Path, body: str, name: str = "script.py") -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8", newline="\n")
    return python_command(path)


async def wait_file(path: Path) -> None:
    async with asyncio.timeout(8):
        while not path.exists():  # noqa: ASYNC110, ASYNC240 — observe a different OS process.
            await asyncio.sleep(0.01)


def windows_pid_alive(pid: int) -> bool:
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return api.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
    finally:
        api.CloseHandle(handle)


async def assert_tree_stopped(tmp_path: Path) -> None:
    pulse = tmp_path / "pulse.txt"
    before = pulse.read_bytes()
    await asyncio.sleep(0.25)
    assert pulse.read_bytes() == before
    if os.name == "nt":
        pids = [int(value) for value in (tmp_path / "pids.txt").read_text().split()]
        assert pids
        assert not any(windows_pid_alive(pid) for pid in pids)


def tree_command(tmp_path: Path, *, parent_exits: bool = False) -> str:
    (tmp_path / "grandchild.py").write_text(
        "import os,time\n"
        "from pathlib import Path\n"
        "with open('pids.txt','a') as f: f.write(str(os.getpid())+'\\n')\n"
        "Path('ready.txt').write_text('ready')\n"
        "while True:\n"
        " with open('pulse.txt','a') as f: f.write('pulse\\n')\n"
        " time.sleep(.03)\n",
        encoding="utf-8",
    )
    (tmp_path / "child.py").write_text(
        "import os,subprocess,sys,time\n"
        "with open('pids.txt','a') as f: f.write(str(os.getpid())+'\\n')\n"
        "subprocess.Popen([sys.executable,'grandchild.py'])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return script(
        tmp_path,
        "import os,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "with open('pids.txt','a') as f: f.write(str(os.getpid())+'\\n')\n"
        "subprocess.Popen([sys.executable,'child.py'])\n"
        "while not Path('pulse.txt').exists(): time.sleep(.01)\n"
        "print('tree ready', flush=True)\n" + ("" if parent_exits else "time.sleep(60)\n"),
    )


def test_platform_support_and_shell_identity():
    assert runner.available()
    assert runner.shell_name() == ("powershell" if os.name == "nt" else "sh")


async def test_utf8_stdout_stderr_exit_code_and_elapsed(tmp_path):
    command = script(
        tmp_path,
        "import sys\nprint('公开样本 · 鼠尾草')\nprint('synthetic stderr',file=sys.stderr)\n"
        "raise SystemExit(7)\n",
    )
    result = await runner.run_command(command, tmp_path, timeout=8, env=child_env())
    assert result.exit_code == 7
    assert result.stdout.strip() == "公开样本 · 鼠尾草"
    assert result.stderr.strip() == "synthetic stderr"
    assert result.duration_ms > 0
    assert not result.timed_out
    assert not result.truncated
    assert not result.encoding_errors


async def test_no_stdin_and_no_implicit_environment_inheritance(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_API_KEY", "do-not-inherit")
    env = child_env()
    env["SYNTHETIC_VISIBLE"] = "explicit"
    command = script(
        tmp_path,
        "import os,sys\n"
        "print(repr(sys.stdin.read()))\n"
        "print(os.environ.get('SYNTHETIC_API_KEY','absent'))\n"
        "print(os.environ['SYNTHETIC_VISIBLE'])\n",
    )
    result = await runner.run_command(command, tmp_path, timeout=8, env=env)
    assert result.stdout.splitlines() == ["''", "absent", "explicit"]
    assert result.exit_code == 0
    assert "PYTHONUTF8" not in env  # Runner copies rather than mutating caller environment.


async def test_output_limits_continue_draining_both_pipes(tmp_path):
    command = script(
        tmp_path,
        "import os\nfor i in range(1024):\n os.write(1,b'x'*4096)\n os.write(2,b'y'*4096)\n",
    )
    result = await runner.run_command(
        command, tmp_path, timeout=10, env=child_env(), output_limit=4096
    )
    assert result.exit_code == 0 and not result.timed_out
    assert result.stdout == "x" * 4096
    assert result.stderr == "y" * 4096
    assert result.stdout_truncated and result.stderr_truncated and result.truncated


async def test_invalid_utf8_is_explicit_and_not_locale_decoded(tmp_path):
    command = script(tmp_path, "import os\nos.write(1,b'\\xff')\n")
    result = await runner.run_command(command, tmp_path, timeout=8, env=child_env())
    assert result.stdout == "\ufffd"
    assert result.encoding_errors


async def test_timeout_stops_child_and_grandchild_before_return(tmp_path):
    result = await runner.run_command(tree_command(tmp_path), tmp_path, timeout=3, env=child_env())
    assert result.timed_out
    assert result.exit_code != 0
    assert "tree ready" in result.stdout
    await assert_tree_stopped(tmp_path)


async def test_cancel_stops_tree_and_propagates_cancelled_error(tmp_path):
    task = asyncio.create_task(
        runner.run_command(tree_command(tmp_path), tmp_path, timeout=30, env=child_env())
    )
    try:
        await wait_file(tmp_path / "pulse.txt")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await assert_tree_stopped(tmp_path)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_normal_parent_exit_also_stops_background_descendants(tmp_path):
    result = await runner.run_command(
        tree_command(tmp_path, parent_exits=True), tmp_path, timeout=8, env=child_env()
    )
    assert result.exit_code == 0
    assert not result.timed_out
    assert "tree ready" in result.stdout
    await assert_tree_stopped(tmp_path)


async def test_repeated_cancel_while_spawn_thread_is_pending_still_cleans(tmp_path, monkeypatch):
    real_start = runner._start
    entered = threading.Event()
    release = threading.Event()

    def delayed_start(*args):
        entered.set()
        assert release.wait(5)
        return real_start(*args)

    monkeypatch.setattr(runner, "_start", delayed_start)
    command = script(tmp_path, "import time\nprint('started',flush=True)\ntime.sleep(60)\n")
    task = asyncio.create_task(runner.run_command(command, tmp_path, timeout=30, env=child_env()))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(t.name == "terminal-pipe" for t in threading.enumerate())


async def test_already_cancelled_task_never_spawns(tmp_path, monkeypatch):
    starts = []
    monkeypatch.setattr(runner, "_start", lambda *args: starts.append(args))
    task = asyncio.create_task(runner.run_command("echo sample", tmp_path, timeout=1, env={}))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not starts


async def test_cancelled_spawn_failure_preserves_cancellation(tmp_path, monkeypatch, caplog):
    entered = threading.Event()
    release = threading.Event()

    def fail_start(*args):
        entered.set()
        assert release.wait(5)
        raise OSError("synthetic startup error")

    monkeypatch.setattr(runner, "_start", fail_start)
    task = asyncio.create_task(runner.run_command("echo sample", tmp_path, timeout=8, env={}))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.01)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "OSError" in caplog.text


async def test_repeated_cancellation_waits_for_cleanup_before_return(tmp_path, monkeypatch):
    real_cleanup = runner._Running.cleanup
    cleanup_entered = threading.Event()
    release = threading.Event()

    def delayed_cleanup(self):
        cleanup_entered.set()
        assert release.wait(5)
        return real_cleanup(self)

    monkeypatch.setattr(runner._Running, "cleanup", delayed_cleanup)
    task = asyncio.create_task(
        runner.run_command(tree_command(tmp_path), tmp_path, timeout=30, env=child_env())
    )
    try:
        await wait_file(tmp_path / "pulse.txt")
        task.cancel()
        assert await asyncio.to_thread(cleanup_entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await assert_tree_stopped(tmp_path)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "command,directory,execution_timeout,limit",
    [
        ("", None, 1, 1),
        (" \n", None, 1, 1),
        ("echo\x00sample", None, 1, 1),
        ("echo sample", "relative", 1, 1),
        ("echo sample", "missing", 1, 1),
        ("echo sample", None, 0, 1),
        ("echo sample", None, -1, 1),
        ("echo sample", None, float("nan"), 1),
        ("echo sample", None, float("inf"), 1),
        ("echo sample", None, 1, 0),
        ("echo sample", None, 1, 0.5),
        ("echo sample", None, 1, True),
    ],
)
async def test_invalid_input_rejected_before_process_creation(
    tmp_path, monkeypatch, command, directory, execution_timeout, limit
):
    starts = []
    monkeypatch.setattr(runner, "_start", lambda *args: starts.append(args))
    cwd = (
        tmp_path
        if directory is None
        else Path("relative")
        if directory == "relative"
        else tmp_path / "missing"
    )
    with pytest.raises(ValueError):
        await runner.run_command(
            command, cwd, timeout=execution_timeout, env={}, output_limit=limit
        )
    assert not starts


@pytest.mark.skipif(os.name != "nt", reason="Windows Job startup failure regression")
async def test_job_assignment_failure_never_runs_suspended_command(tmp_path, monkeypatch):
    def fail(self, handle, pid):
        raise OSError("synthetic assignment failure")

    monkeypatch.setattr(WindowsJob, "attach_and_resume", fail)
    command = script(tmp_path, "from pathlib import Path\nPath('must-not-run').touch()\n")
    with pytest.raises(OSError, match="synthetic assignment failure"):
        await runner.run_command(command, tmp_path, timeout=8, env=child_env())
    assert not (tmp_path / "must-not-run").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell semantics")
@pytest.mark.parametrize(
    "command", ["throw 'synthetic failure'", "Write-Error 'synthetic failure'"]
)
async def test_powershell_errors_return_nonzero(tmp_path, command):
    result = await runner.run_command(command, tmp_path, timeout=8, env=child_env())
    assert result.exit_code != 0
    assert "synthetic failure" in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Real Windows Selector event loop regression")
def test_windows_selector_loop_can_run_process(tmp_path):
    loop = asyncio.SelectorEventLoop()
    try:
        result = loop.run_until_complete(
            runner.run_command(
                "Write-Output 'selector works'", tmp_path, timeout=8, env=child_env()
            )
        )
        assert result.exit_code == 0
        assert result.stdout.strip() == "selector works"
    finally:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle closure")
async def test_repeated_runs_close_owned_resources_without_gc(tmp_path, monkeypatch):
    owners = []
    real_start = runner._start

    def remember_start(*args):
        running = real_start(*args)
        owners.append(running)
        return running

    monkeypatch.setattr(runner, "_start", remember_start)
    env = child_env()
    for _ in range(5):
        result = await runner.run_command("Write-Output 'sample'", tmp_path, timeout=8, env=env)
        assert result.stdout.strip() == "sample"
    assert len(owners) == 5
    # Keep strong references, so a missing explicit Close cannot pass because
    # CPython happened to collect the completed Popen or job between checks.
    for running in owners:
        assert running.process._handle.closed
        assert running.job.handle is None
        for capture in (running.stdout, running.stderr):
            assert capture.stream.closed
            assert not capture.thread.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="Windows shell path spoof regression")
async def test_path_cannot_replace_fixed_system_powershell(tmp_path):
    (tmp_path / "powershell.exe").write_bytes(b"synthetic invalid executable")
    env = child_env()
    env["PATH"] = str(tmp_path)
    result = await runner.run_command("Write-Output 'trusted shell'", tmp_path, timeout=8, env=env)
    assert result.exit_code == 0
    assert result.stdout.strip() == "trusted shell"


@pytest.mark.skipif(os.name != "nt", reason="Windows CreateProcess command-line length")
async def test_oversized_encoded_command_is_rejected_before_start(tmp_path, monkeypatch):
    starts = []
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: starts.append(args))
    with pytest.raises(ValueError, match="启动长度"):
        await runner.run_command(
            "Write-Output '" + "样本" * 8000 + "'", tmp_path, timeout=8, env={}
        )
    assert not starts
