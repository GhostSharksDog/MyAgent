"""发行版 worker 不能重新走主程序入口；测试不打开真实系统窗口。"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.core import directory_picker, picker_worker
from app.desktop.entry import PICKER_ARGUMENT, main


def forbid_launcher(monkeypatch):
    def fail():
        raise AssertionError("目录选择错误地启动了 Legacy 主界面")

    monkeypatch.setitem(sys.modules, "app.desktop.launcher", SimpleNamespace(run=fail))


def test_frozen_command_dispatches_before_launcher_and_preserves_title(tmp_path, monkeypatch):
    forbid_launcher(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Legacy.exe"))
    titles = []
    monkeypatch.setattr(
        picker_worker, "pick_folder", lambda title: titles.append(title) or "D:/公开"
    )
    runner = directory_picker.ComDialogRunner(title="专用目录选择")
    out = tmp_path / "结果 空格.json"
    command = runner._command(out)
    assert command == [str(tmp_path / "Legacy.exe"), PICKER_ARGUMENT, str(out), "专用目录选择"]
    assert main(command) == 0
    assert titles == ["专用目录选择"]
    assert json.loads(out.read_text(encoding="utf-8"))["path"] == "D:/公开"


@pytest.mark.parametrize("outcome", [None, OSError("受控 COM 错误")])
def test_worker_dispatch_records_cancel_and_failure_without_service(tmp_path, monkeypatch, outcome):
    forbid_launcher(monkeypatch)

    def pick(_title):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(picker_worker, "pick_folder", pick)
    out = tmp_path / "result.json"
    code = main(["Legacy.exe", PICKER_ARGUMENT, str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["path"] is None
    if outcome is None:
        assert code == 0 and payload["cancelled"] and not payload["error"]
    else:
        assert code == 1 and not payload["cancelled"] and "COM" in payload["error"]


@pytest.mark.parametrize(
    "args",
    [
        ["Legacy.exe", PICKER_ARGUMENT],
        ["Legacy.exe", PICKER_ARGUMENT, "result", "title", "unexpected"],
        ["Legacy.exe", "_internal/app/core/picker_worker.py", "result"],
    ],
)
def test_invalid_worker_arguments_cannot_open_main_window(monkeypatch, args):
    forbid_launcher(monkeypatch)
    assert main(args) == 2


def test_ordinary_start_still_calls_launcher_once(monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules, "app.desktop.launcher", SimpleNamespace(run=lambda: calls.append(True))
    )
    assert main(["Legacy.exe"]) == 0 and calls == [True]


def test_frozen_runner_uses_result_channel_and_actionable_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Legacy.exe"))
    calls = []

    def launch(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(wait=lambda: 0)

    monkeypatch.setattr(directory_picker.subprocess, "Popen", launch)
    outcome = directory_picker.ComDialogRunner().run()
    assert calls[0][1] == PICKER_ARGUMENT
    assert "没有返回结果" in outcome.error and "手动填写" in outcome.error
    assert "python" not in outcome.error and "picker_worker.py" not in outcome.error


@pytest.mark.parametrize(
    "payload,code",
    [
        ([], 0),
        ({"path": 123, "cancelled": False}, 0),
        ({"path": None, "cancelled": False}, 0),
        ({"path": "D:/公开", "cancelled": True}, 0),
        ({"path": None, "cancelled": True}, 7),
    ],
)
def test_invalid_result_or_failure_cannot_be_reported_as_cancel(
    tmp_path, monkeypatch, payload, code
):
    def launch(command, **kwargs):
        Path(command[2]).write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(wait=lambda: code)

    monkeypatch.setattr(directory_picker.subprocess, "Popen", launch)
    outcome = directory_picker.ComDialogRunner(python=sys.executable).run()
    assert outcome.error and not outcome.cancelled and not outcome.ok


def test_windowed_worker_usage_error_does_not_require_stderr(monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    assert picker_worker.main(["Legacy.exe"]) == 2
