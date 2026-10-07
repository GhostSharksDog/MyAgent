"""The CI probe must fail on a protected-shell error, without dumping env values."""

from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace

import pytest
from app.core.config import PROJECT_ROOT


@pytest.mark.parametrize("protected_ok", [True, False])
async def test_probe_preserves_protected_failures_and_hides_environment_values(
    monkeypatch, protected_ok
):
    spec = importlib.util.spec_from_file_location(
        "terminal_probe_test", PROJECT_ROOT / "scripts" / "probe_terminal.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "terminal_environment", lambda: {"PATH": "synthetic-private-value"})
    monkeypatch.setattr(module, "_plain", lambda *args: {"ok": True, "duration_ms": 1})
    monkeypatch.setattr(module.runner, "_shell_argv", lambda command: ["synthetic-shell"])

    async def protected(command, directory, **kwargs):
        assert kwargs["timeout"] == 30
        assert kwargs["env"]["PATH"] == "synthetic-private-value"
        return SimpleNamespace(
            exit_code=0 if protected_ok else 1,
            stdout=module.MARKER if protected_ok else "",
            stderr="",
            duration_ms=3,
            timed_out=not protected_ok,
        )

    monkeypatch.setattr(module.runner, "run_command", protected)
    report = await module.probe()
    assert report["ok"] is protected_ok
    assert report["stages"]["protected_shell"]["ok"] is protected_ok
    assert "synthetic-private-value" not in json.dumps(report)
    assert "PATH" in report["environment_keys"]
