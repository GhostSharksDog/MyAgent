"""Only public output markers: distinguish shell startup from protected execution.

No model calls, no user configuration writes, no environment values in the report.
The unprotected probes run fixed output-only commands, never supplied user commands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "api"))

from app.tools import terminal_process as runner
from app.tools.terminal import terminal_environment

MARKER = "legacy-terminal-probe"


def _plain(argv: list[str], directory: Path, env: dict[str, str]) -> dict:
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=directory,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if os.name == "nt":
            process._handle.Close()
    return {
        "ok": process.returncode == 0
        and MARKER in stdout.decode("utf-8", errors="replace"),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stderr": stderr.decode("utf-8", errors="replace")[:1024],
    }


async def probe() -> dict:
    env = terminal_environment()
    if os.name == "nt":
        env.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        command = f"[Console]::WriteLine('{MARKER}')"
        plain = [
            str(runner.powershell_path()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ]
    else:
        command = f"printf '{MARKER}\\n'"
        plain = ["/bin/sh", "-c", command]
    report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "shell": runner.shell_name(),
        "environment_keys": sorted(env),
        "stages": {},
    }
    with tempfile.TemporaryDirectory(prefix="legacy-terminal-probe-") as temporary:
        directory = Path(temporary)
        stages = report["stages"]
        for name, argv in (
            ("plain_shell", plain),
            ("wrapped_shell", runner._shell_argv(command)),
        ):
            try:
                stages[name] = await asyncio.to_thread(_plain, argv, directory, env)
            except (OSError, ValueError, RuntimeError) as exc:
                stages[name] = {"ok": False, "error": str(exc)[:1024]}
            print(name, json.dumps(stages[name], ensure_ascii=False), flush=True)
        try:
            result = await runner.run_command(command, directory, timeout=30, env=env)
            stages["protected_shell"] = {
                "ok": result.exit_code == 0
                and MARKER in result.stdout
                and not result.timed_out,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": round(result.duration_ms),
                "stdout_bytes": len(result.stdout.encode("utf-8")),
                "stderr_bytes": len(result.stderr.encode("utf-8")),
                "stderr": result.stderr[:1024],
            }
        except (OSError, ValueError, RuntimeError) as exc:
            stages["protected_shell"] = {"ok": False, "error": str(exc)[:1024]}
        print(
            "protected_shell",
            json.dumps(stages["protected_shell"], ensure_ascii=False),
            flush=True,
        )
    report["ok"] = all(stage["ok"] for stage in stages.values())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out", type=Path, default=Path("data/terminal-probe.json")
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger(runner.__name__).setLevel(logging.DEBUG)
    report = asyncio.run(probe())
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{'PASS' if report['ok'] else 'FAIL'}: {args.json_out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
