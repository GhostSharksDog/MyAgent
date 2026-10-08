"""真实三内核与请求审批；命令执行替身不读取用户文件、不联网。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from app.agent import runtime
from app.agent.approvals import ApprovalBroker, ApprovalUnavailable, merge_approval_events
from app.agent.events import EventType
from app.agent.loop import Agent
from app.agent.prompts import build_system_prompt
from app.agent.runtime import RunBudgetExceeded, RunContext
from app.api import settings as settings_api
from app.api.routes import chat_stream
from app.api.schemas import ChatRequest
from app.core.config import AgentSettings, get_settings
from app.llm.types import ToolCall
from app.tools import terminal
from app.tools.base import FunctionTool, ToolRegistry, ToolResult
from app.tools.builtin import build_default_registry
from app.tools.files import WriteFileTool
from app.tools.terminal import ApprovedCommand, TerminalParams, TerminalTool
from app.tools.terminal_process import available as runner_available
from app.tools.terminal_process import shell_name as runner_shell_name
from starlette.requests import Request

from tests.test_api import _collect_from_stream
from tests.test_file_approvals import WriteLLM, application, pending


@pytest.fixture(autouse=True)
def isolated_sse_loop(monkeypatch):
    from sse_starlette.sse import AppStatus

    # SSE 3.x uses per-loop events; legacy 2.x needs explicit reset.
    if hasattr(AppStatus, "should_exit_event"):
        monkeypatch.setattr(AppStatus, "should_exit_event", None)


@pytest.fixture
def command_workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "child").mkdir()
    monkeypatch.setattr(
        get_settings(),
        "agent",
        AgentSettings(
            _env_file=None,
            workspace_root=str(root),
            terminal_enabled=True,
            terminal_timeout=2,
            terminal_approval_timeout=1,
            file_write_enabled=True,
            file_approval_required=True,
            file_approval_timeout=1,
        ),
    )
    monkeypatch.setattr(terminal, "available", lambda: True)
    monkeypatch.setattr(terminal, "shell_name", lambda: "synthetic-shell")
    return root


@pytest.fixture
def runner(monkeypatch):
    calls = []

    async def fake(command, cwd, **kwargs):
        calls.append((command, cwd, kwargs["timeout"], kwargs["env"]))
        return SimpleNamespace(
            exit_code=0,
            stdout="合成标准输出",
            stderr="合成诊断",
            duration_ms=4,
            truncated=False,
            timed_out=False,
        )

    monkeypatch.setattr(terminal, "run_command", fake)
    return calls


class CommandLLM(WriteLLM):
    def __init__(self, arguments=None):
        super().__init__(
            name="run_terminal",
            arguments=arguments or {"command": "Write-Output '公开样本'", "cwd": "child"},
        )

    async def stream_chat(self, messages, **kwargs):
        self.messages.append(list(messages))
        async for delta in super().stream_chat(messages, **kwargs):
            yield delta


def command_app(*, mode_timeout=0, llm=None):
    app = application(llm=llm or CommandLLM(), mode_timeout=mode_timeout)
    app.state.tools.register(TerminalTool())
    return app


@contextmanager
def bind(context):
    token = runtime._current.set(context)
    try:
        yield
    finally:
        runtime._current.reset(token)


def context_and_registry():
    context = RunContext.create(get_settings().agent)
    context.approvals = ApprovalBroker(1)
    registry = ToolRegistry()
    registry.register(TerminalTool())
    return context, registry


def command_call(*, command="Write-Output 'approved'", cwd="child"):
    return ToolCall(
        id="terminal-call", name="run_terminal", arguments={"command": command, "cwd": cwd}
    )


async def request_view(broker):
    async with asyncio.timeout(2):
        while True:
            event = await broker.events.get()
            if event.type is EventType.APPROVAL_REQUEST:
                return event.approval


async def approve_preparation(tool, call, context):
    with bind(context):
        operation = asyncio.create_task(tool.prepare_execution(call))
    view = await request_view(context.approvals)
    context.approvals.decide(view["id"], "approve")
    result = await asyncio.wait_for(operation, 2)
    assert isinstance(result, ApprovedCommand)
    return result


@pytest.mark.parametrize("mode,expected", [("react", 1), ("plan", 2), ("multi", 3)])
async def test_modes_pause_and_each_approved_command_executes_once(
    command_workspace, runner, mode, expected
):
    app = command_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        operation = asyncio.create_task(
            api.post("/api/chat/stream", json={"message": "执行公开核验", "mode": mode})
        )
        seen = []
        try:
            for index in range(expected):
                run_id, _broker, item = (await pending(app))[0]
                view = item.view
                seen.append(view["id"])
                # Supervisor的目录线程可同时准备多个批准，前一批准不必已经启动。
                assert len(runner) <= index
                assert view["kind"] == "command"
                assert view["command"] == "Write-Output '公开样本'"
                assert Path(view["cwd"]) == command_workspace / "child"
                assert view["shell"] == "synthetic-shell" and view["timeout_seconds"] == 2
                assert "尚未执行" in view["message"]
                if index == 0:
                    assert not app.state.tools._serial_lock.locked()
                path = f"/api/runs/{run_id}/approvals/{view['id']}"
                assert (await api.post(path, json={"decision": "approve"})).json() == {
                    "status": "approved"
                }
                assert (await api.post(path, json={"decision": "approve"})).status_code == 409
            response = await asyncio.wait_for(operation, 2)
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
        events = _collect_from_stream(response.text)
        assert len(runner) == expected and len(set(seen)) == expected
        assert sum(e["type"] == "done" for e in events) == 1
        assert events[-1]["stopped_reason"] == "finished"
        assert sum(e["type"] == "approval_request" for e in events) == expected
        assert sum(e.get("approval", {}).get("status") == "applied" for e in events) == expected
        record = app.state.run_history.get(response.headers["X-Run-Id"])
        assert record.tool_calls == record.tool_results == expected
        assert "Write-Output" not in record.model_dump_json()
        assert str(command_workspace) not in record.model_dump_json()
        assert "合成标准输出" not in record.model_dump_json()
        assert not app.state.file_approvals
        assert (await api.post(path, json={"decision": "approve"})).status_code == 409


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
@pytest.mark.parametrize("decision", ["reject", "expire"])
async def test_reject_or_expire_seals_further_commands_for_the_run(
    command_workspace, runner, mode, decision
):
    app = command_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        operation = asyncio.create_task(
            api.post("/api/chat/stream", json={"message": "核验", "mode": mode})
        )
        run_id, broker, item = (await pending(app))[0]
        path = f"/api/runs/{run_id}/approvals/{item.view['id']}"
        if decision == "expire":
            item.expires_at = asyncio.get_running_loop().time() - 1
            assert (await api.post(path, json={"decision": "approve"})).status_code == 409
        else:
            assert (await api.post(path, json={"decision": "reject"})).status_code == 200
        events = _collect_from_stream((await asyncio.wait_for(operation, 2)).text)
        assert not runner
        assert sum(e["type"] == "approval_request" for e in events) <= 3
        terminal_index = next(
            i
            for i, e in enumerate(events)
            if e.get("approval", {}).get("status") in {"rejected", "expired"}
        )
        assert not any(e["type"] == "approval_request" for e in events[terminal_index + 1 :])
        assert sum(e["type"] == "done" for e in events) == 1
        assert broker.blocked_reason is not None


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_http_and_cli_like_entry_without_approval_channel_fail_closed(
    command_workspace, runner, mode
):
    app = command_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        response = await api.post("/api/chat", json={"message": "执行核验", "mode": mode})
    assert response.status_code == 200
    assert not runner and not getattr(app.state, "file_approvals", {})
    assert app.state.run_history.list()[0].tool_failures > 0
    llm = CommandLLM()
    result = await Agent(llm, app.state.tools, get_settings().agent).run("CLI 等价入口")
    assert not runner
    assert any(
        "没有命令审批通道" in (message.content or "")
        for messages in llm.messages
        for message in messages
        if message.role.value == "tool"
    )
    assert result.tool_calls


async def test_direct_execute_run_and_unprepared_entry_cannot_execute(command_workspace, runner):
    tool = TerminalTool()
    params = TerminalParams(command="Write-Output x")
    results = [
        await tool.run(params),
        await tool.execute(command_call()),
        await tool.execute_prepared(command_call(), None),
    ]
    assert all(not result.ok for result in results)
    assert not runner


@pytest.mark.parametrize(
    "enabled,root,expected",
    [(False, "configured", False), (True, "", False), (True, "configured", True)],
)
def test_registration_requires_explicit_terminal_permission_and_workspace(
    command_workspace, monkeypatch, enabled, root, expected
):
    monkeypatch.setattr(
        get_settings(),
        "agent",
        get_settings().agent.model_copy(
            update={
                "terminal_enabled": enabled,
                "workspace_root": str(command_workspace) if root else "",
            }
        ),
    )
    names = build_default_registry(profile="general", terminal=enabled).names()
    assert ("run_terminal" in names) is expected
    assert ("write_file" in names) is False  # 终端授权不会顺带打开文件写工具
    assert TerminalTool().serial


def test_terminal_defaults_are_off_and_finite():
    cfg = AgentSettings(_env_file=None, terminal_enabled=False, workspace_root="")
    assert not cfg.terminal_enabled
    assert AgentSettings.model_fields["terminal_enabled"].default is False
    assert cfg.terminal_timeout == 30 and cfg.terminal_approval_timeout == 300


@pytest.mark.parametrize(
    "mutation", ["disable", "root", "cwd_identity", "root_identity", "timeout", "broker_close"]
)
async def test_approval_queued_for_shared_lock_is_invalidated_before_start(
    command_workspace, runner, monkeypatch, mutation
):
    context, registry = context_and_registry()
    await registry._serial_lock.acquire()
    with bind(context):
        operation = asyncio.create_task(registry.execute(command_call()))
    view = await request_view(context.approvals)
    context.approvals.decide(view["id"], "approve")
    await asyncio.sleep(0)
    assert not operation.done() and not runner
    cfg = get_settings()
    try:
        if mutation == "disable":
            monkeypatch.setattr(
                cfg, "agent", cfg.agent.model_copy(update={"terminal_enabled": False})
            )
        elif mutation == "root":
            other = command_workspace.parent / "other"
            other.mkdir()
            monkeypatch.setattr(
                cfg, "agent", cfg.agent.model_copy(update={"workspace_root": str(other)})
            )
        elif mutation in {"cwd_identity", "root_identity"}:
            target = (
                command_workspace / "child" if mutation == "cwd_identity" else command_workspace
            )
            displaced = target.with_name(target.name + "-displaced")
            target.rename(displaced)
            target.mkdir()
            if mutation == "root_identity":
                (target / "child").mkdir()
        elif mutation == "timeout":
            monkeypatch.setattr(cfg, "agent", cfg.agent.model_copy(update={"terminal_timeout": 3}))
        else:
            context.approvals.close()
    finally:
        registry._serial_lock.release()
    result = await asyncio.wait_for(operation, 2)
    assert not result.ok and not runner
    assert context.approvals.items[view["id"]].view["status"] == "conflict"


@pytest.mark.parametrize("mutation", ["command", "cwd", "name", "other_context"])
async def test_prepared_approval_binds_exact_parameters_and_request(
    command_workspace, runner, mutation
):
    tool = TerminalTool()
    context, _ = context_and_registry()
    call = command_call()
    prepared = await approve_preparation(tool, call, context)
    if mutation == "command":
        call = command_call(command="Write-Output 'changed'")
    elif mutation == "cwd":
        call = command_call(cwd=".")
    elif mutation == "name":
        call = call.model_copy(update={"name": "different_tool"})
    else:
        context, _ = context_and_registry()
    with bind(context):
        result = await tool.execute_prepared(call, prepared)
    assert not result.ok and not runner
    if mutation == "other_context":
        assert prepared.broker.items[prepared.approval_id].view["status"] == "approved"


async def test_one_approval_cannot_be_reused_after_execution(command_workspace, runner):
    tool = TerminalTool()
    context, _ = context_and_registry()
    call = command_call()
    prepared = await approve_preparation(tool, call, context)
    with bind(context):
        first = await tool.execute_prepared(call, prepared)
        second = await tool.execute_prepared(call, prepared)
    assert first.ok and not second.ok and len(runner) == 1
    assert context.approvals.items[prepared.approval_id].view["status"] == "applied"
    with pytest.raises(ApprovalUnavailable):
        context.approvals.decide(prepared.approval_id, "approve")


async def test_approval_cannot_be_reused_while_its_command_is_still_running(
    command_workspace, monkeypatch
):
    tool = TerminalTool()
    context, _ = context_and_registry()
    call = command_call()
    prepared = await approve_preparation(tool, call, context)
    started, release = asyncio.Event(), asyncio.Event()
    invocations = []

    async def waiting(command, cwd, **kwargs):
        invocations.append(command)
        started.set()
        await release.wait()
        return SimpleNamespace(
            exit_code=0, stdout="done", stderr="", duration_ms=1, truncated=False, timed_out=False
        )

    monkeypatch.setattr(terminal, "run_command", waiting)
    with bind(context):
        first = asyncio.create_task(tool.execute_prepared(call, prepared))
    await started.wait()
    with bind(context):
        second = await tool.execute_prepared(call, prepared)
    assert not second.ok and len(invocations) == 1
    assert context.approvals.items[prepared.approval_id].view["status"] == "approved"
    assert context.approvals.items[prepared.approval_id].view["started"] is True
    release.set()
    assert (await first).ok
    assert context.approvals.items[prepared.approval_id].view["status"] == "applied"


async def test_rejecting_another_command_does_not_relabel_an_already_started_command(
    command_workspace, monkeypatch
):
    context, registry = context_and_registry()
    started, release = asyncio.Event(), asyncio.Event()

    async def waiting(command, cwd, **kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(
            exit_code=0, stdout="done", stderr="", duration_ms=1, truncated=False, timed_out=False
        )

    monkeypatch.setattr(terminal, "run_command", waiting)
    with bind(context):
        first = asyncio.create_task(registry.execute(command_call()))
    approved = await request_view(context.approvals)
    context.approvals.decide(approved["id"], "approve")
    await started.wait()
    rejecting = asyncio.create_task(
        context.approvals.request({"kind": "command", "command": "later command"})
    )
    rejected = await request_view(context.approvals)
    context.approvals.decide(rejected["id"], "reject")
    assert (await rejecting)[1] is False
    assert context.approvals.items[approved["id"]].view["status"] == "approved"
    release.set()
    assert (await first).ok
    assert context.approvals.items[approved["id"]].view["status"] == "applied"
    with bind(context):
        next_result = await registry.execute(command_call())
    assert not next_result.ok and len(context.approvals.items) == 2


@pytest.mark.skipif(not runner_available(), reason="系统没有受支持的本机命令运行器")
@pytest.mark.parametrize("startup_delay", [0, 2.2])
async def test_real_approved_command_runs_through_registry_in_temporary_workspace(
    command_workspace,
    monkeypatch,
    startup_delay,
):
    from app.tools import terminal_process

    # 合成用例的两秒预算不代表系统 shell 的启动 SLA；真实集成使用默认30秒。
    monkeypatch.setattr(get_settings().agent, "terminal_timeout", 30)
    real_start = terminal_process._start

    def delayed_start(*args):
        time.sleep(startup_delay)
        return real_start(*args)

    monkeypatch.setattr(terminal_process, "_start", delayed_start)
    monkeypatch.setattr(terminal, "shell_name", runner_shell_name)
    monkeypatch.setattr(terminal, "available", runner_available)
    context, registry = context_and_registry()
    # fixture中的shell名仅用于审批展示，实际运行器选择自己的平台。
    command = (
        "Write-Output 'terminal-真实-utf8'; [Console]::Error.WriteLine('terminal-stderr')"
        if os.name == "nt"
        else "printf 'terminal-真实-utf8\\n'; printf 'terminal-stderr\\n' >&2"
    )
    with bind(context):
        operation = asyncio.create_task(registry.execute(command_call(command=command)))
    view = await request_view(context.approvals)
    assert view["shell"] == runner_shell_name()
    assert not operation.done()
    context.approvals.decide(view["id"], "approve")
    result = await asyncio.wait_for(operation, 35)
    assert result.ok
    assert isinstance(result.duration_ms, int) and result.duration_ms > 0
    output = json.loads(result.content.split("\n", 1)[1])
    assert output["exit_code"] == 0 and output["timed_out"] is False
    assert "terminal-真实-utf8" in output["stdout"] and "terminal-stderr" in output["stderr"]
    assert output["encoding_errors"] is False and not result.truncated
    assert context.approvals.items[view["id"]].view["status"] == "applied"
    assert list(command_workspace.iterdir()) == [command_workspace / "child"]


@pytest.mark.parametrize("rejected_kind", ["command", "file"])
async def test_cross_tool_rejection_seals_approved_command_and_direct_file_branch(
    command_workspace, runner, monkeypatch, rejected_kind
):
    context, registry = context_and_registry()
    registry.register(WriteFileTool())
    await registry._serial_lock.acquire()
    with bind(context):
        command = asyncio.create_task(registry.execute(command_call()))
    approved = await request_view(context.approvals)
    context.approvals.decide(approved["id"], "approve")
    await asyncio.sleep(0)
    rejected_view = (
        {"kind": "command", "command": "another command"}
        if rejected_kind == "command"
        else {"path": "other.txt", "diff": "+other"}
    )
    rejecting = asyncio.create_task(context.approvals.request(rejected_view))
    rejected = await request_view(context.approvals)
    context.approvals.decide(rejected["id"], "reject")
    assert (await rejecting)[1] is False
    registry._serial_lock.release()
    assert not (await asyncio.wait_for(command, 2)).ok
    monkeypatch.setattr(
        get_settings(),
        "agent",
        get_settings().agent.model_copy(update={"file_approval_required": False}),
    )
    with bind(context):
        write = await registry.execute(
            ToolCall(
                id="write",
                name="write_file",
                arguments={"path": "not-written.txt", "content": "sample"},
            )
        )
    assert not write.ok and not runner
    assert not (command_workspace / "not-written.txt").exists()
    assert len(context.approvals.items) == 2


async def test_command_and_file_side_effects_share_one_registry_lock(
    command_workspace, monkeypatch
):
    context, registry = context_and_registry()
    registry.register(WriteFileTool())
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(command, cwd, **kwargs):
        entered.set()
        await release.wait()
        assert not (command_workspace / "serialized.txt").exists()
        return SimpleNamespace(
            exit_code=0, stdout="done", stderr="", duration_ms=1, truncated=False, timed_out=False
        )

    monkeypatch.setattr(terminal, "run_command", delayed)
    with bind(context):
        command = asyncio.create_task(registry.execute(command_call()))
    first = await request_view(context.approvals)
    context.approvals.decide(first["id"], "approve")
    await asyncio.wait_for(entered.wait(), 2)
    with bind(context):
        write = asyncio.create_task(
            registry.execute(
                ToolCall(
                    id="write",
                    name="write_file",
                    arguments={"path": "serialized.txt", "content": "after command"},
                )
            )
        )
    second = await request_view(context.approvals)
    context.approvals.decide(second["id"], "approve")
    await asyncio.sleep(0.01)
    assert not write.done() and not (command_workspace / "serialized.txt").exists()
    release.set()
    assert (await command).ok and (await write).ok
    assert (command_workspace / "serialized.txt").read_text(encoding="utf-8") == "after command"


@pytest.mark.parametrize("exit_code,timed_out", [(7, False), (0, True), (1, True)])
async def test_failed_commands_retain_output_for_model_and_mark_failed(
    command_workspace, monkeypatch, exit_code, timed_out
):
    async def failed(command, cwd, **kwargs):
        return SimpleNamespace(
            exit_code=exit_code,
            stdout="stdout-marker" + "o" * 10000,
            stderr="stderr-marker" + "e" * 10000,
            duration_ms=21,
            truncated=True,
            timed_out=timed_out,
        )

    monkeypatch.setattr(terminal, "run_command", failed)
    app = command_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        operation = asyncio.create_task(api.post("/api/chat/stream", json={"message": "输出诊断"}))
        run_id, broker, item = (await pending(app))[0]
        await api.post(
            f"/api/runs/{run_id}/approvals/{item.view['id']}", json={"decision": "approve"}
        )
        events = _collect_from_stream((await operation).text)
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["tool_ok"] is False and result["truncated"] is True
    assert "stdout-marker" in result["content"] and "stderr-marker" in result["content"]
    assert broker.items[item.view["id"]].view["status"] == "failed"
    observations = [
        m.content for messages in app.state.llm.messages for m in messages if m.role.value == "tool"
    ]
    assert any(
        "stdout-marker" in message and "stderr-marker" in message for message in observations
    )


async def test_request_override_timeout_is_independent_and_blocks_followups():
    broker = ApprovalBroker(0)
    with pytest.raises(ApprovalUnavailable, match="超时"):
        await broker.request({"kind": "command", "command": "not executed"}, timeout=0.01)
    assert next(iter(broker.items.values())).view["status"] == "expired"
    with pytest.raises(ApprovalUnavailable, match="超时"):
        await broker.request({"path": "not-written.txt"})
    assert len(broker.items) == 1


@pytest.mark.parametrize("started", [False, True])
async def test_late_repeated_decision_does_not_expire_approved_or_other_pending_command(started):
    broker = ApprovalBroker(0)
    first = asyncio.create_task(broker.request({"kind": "command", "command": "first"}))
    approved = await request_view(broker)
    broker.decide(approved["id"], "approve")
    assert (await first)[1] is True
    if started:
        broker.update(approved["id"], "approved", "正在执行", started=True)
    second = asyncio.create_task(broker.request({"kind": "command", "command": "second"}))
    pending_view = await request_view(broker)
    broker.items[approved["id"]].expires_at = asyncio.get_running_loop().time() - 1
    with pytest.raises(ApprovalUnavailable, match="已处理"):
        broker.decide(approved["id"], "approve")
    assert broker.blocked_reason is None
    assert broker.items[pending_view["id"]].view["status"] == "pending"
    assert broker.items[approved["id"]].view["status"] == "approved"
    broker.decide(pending_view["id"], "approve")
    assert (await second)[1] is True


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_shared_deadline_includes_command_confirmation_and_does_not_resume(
    command_workspace, runner, mode
):
    app = command_app(mode_timeout=0.08)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        events = _collect_from_stream(
            (await api.post("/api/chat/stream", json={"message": "核验", "mode": mode})).text
        )
    assert not runner
    assert sum(e["type"] == "done" for e in events) == 1
    assert events[-1]["stopped_reason"] == "timeout"
    assert any(e["type"] == "approval_request" for e in events)
    assert "synthesis" not in app.state.llm.calls
    assert not app.state.file_approvals


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_shared_deadline_cancels_running_command_and_waits_for_cleanup(
    command_workspace, monkeypatch, mode
):
    started, closed = asyncio.Event(), asyncio.Event()

    async def waiting(command, cwd, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(terminal, "run_command", waiting)
    app = command_app(mode_timeout=0.12)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        operation = asyncio.create_task(
            api.post("/api/chat/stream", json={"message": "核验", "mode": mode})
        )
        run_id, _broker, item = (await pending(app))[0]
        assert (
            await api.post(
                f"/api/runs/{run_id}/approvals/{item.view['id']}", json={"decision": "approve"}
            )
        ).status_code == 200
        events = _collect_from_stream((await asyncio.wait_for(operation, 2)).text)
    assert started.is_set() and closed.is_set()
    assert not app.state.tools._serial_lock.locked() and not app.state.file_approvals
    assert sum(event["type"] == "done" for event in events) == 1
    assert events[-1]["stopped_reason"] == "timeout"
    assert "synthesis" not in app.state.llm.calls


@pytest.mark.parametrize("stage", ["snapshot", "recheck"])
async def test_slow_directory_thread_keeps_loop_responsive_and_deadline_stops_before_effects(
    command_workspace, runner, monkeypatch, stage
):
    active = threading.Event()
    finished = threading.Event()
    ticks = []
    context, registry = context_and_registry()
    if stage == "snapshot":
        original = terminal._directory_snapshot

        def delayed_snapshot(cwd):
            active.set()
            try:
                time.sleep(0.05)
                return original(cwd)
            finally:
                active.clear()
                finished.set()

        monkeypatch.setattr(terminal, "_directory_snapshot", delayed_snapshot)
    else:
        original = TerminalTool._recheck_directories

        def delayed_recheck(self, prepared):
            active.set()
            try:
                time.sleep(0.05)
                return original(self, prepared)
            finally:
                active.clear()
                finished.set()

        monkeypatch.setattr(TerminalTool, "_recheck_directories", delayed_recheck)

    async def heartbeat():
        while True:
            await asyncio.sleep(0.002)
            if active.is_set():
                ticks.append(True)
                if len(ticks) == 2:
                    # 在确切阶段注入预算耗尽，避免依赖机器冷启动速度。
                    context.timeout = 0.025
                    context.deadline = asyncio.get_running_loop().time() - 1

    async def collect():
        agent = Agent(CommandLLM(), registry, get_settings().agent)
        events = []
        source = merge_approval_events(
            agent.run_stream("目录核验", run_context=context), context.approvals
        )
        async for event in source:
            events.append(event)
            if event.type is EventType.APPROVAL_REQUEST:
                context.approvals.decide(event.approval["id"], "approve")
        return events

    pulse = asyncio.create_task(heartbeat())
    try:
        events = await asyncio.wait_for(collect(), 2)
        assert len(ticks) >= 2  # 同步堵住事件循环的对照实现无法产生任何活跃期间心跳。
        assert not runner and not registry._serial_lock.locked()
        assert sum(event.type is EventType.DONE for event in events) == 1
        assert events[-1].stopped_reason == "timeout"
        previews = sum(event.type is EventType.APPROVAL_REQUEST for event in events)
        assert previews == (1 if stage == "recheck" else 0)
        assert not any(event.approval and event.approval.get("started") is True for event in events)
        # 只读线程可能晚于取消返回；等它结束，让后续用例不与撤回的夹具并发。
        assert await asyncio.to_thread(finished.wait, 1)
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)


async def test_cleanup_failure_on_cancel_does_not_claim_processes_were_cleaned(
    command_workspace, monkeypatch
):
    async def cancelled_with_cleanup_failure(command, cwd, **kwargs):
        error = asyncio.CancelledError()
        error.add_note("终端清理失败；请检查服务日志和系统进程")
        raise error

    monkeypatch.setattr(terminal, "run_command", cancelled_with_cleanup_failure)
    tool = TerminalTool()
    context, _ = context_and_registry()
    call = command_call()
    prepared = await approve_preparation(tool, call, context)
    with bind(context), pytest.raises(asyncio.CancelledError):
        await tool.execute_prepared(call, prepared)
    view = context.approvals.items[prepared.approval_id].view
    assert view["status"] == "cancelled" and view["started"] is True
    assert "清理失败" in view["message"] and "可能仍在运行" in view["message"]
    assert "已清理普通子进程" not in view["message"]


async def test_token_budget_is_rechecked_after_approval_while_waiting_for_lock(
    command_workspace, runner
):
    context, registry = context_and_registry()
    context.token_limit = 1
    await registry._serial_lock.acquire()
    with bind(context):
        operation = asyncio.create_task(registry.execute(command_call()))
    view = await request_view(context.approvals)
    context.approvals.decide(view["id"], "approve")
    await asyncio.sleep(0)
    context.usage.total_tokens = 1
    registry._serial_lock.release()
    with pytest.raises(RunBudgetExceeded) as error:
        await operation
    assert error.value.reason == "token_budget" and not runner


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_disconnect_cancels_running_command_and_waits_for_cleanup(
    command_workspace, monkeypatch, mode
):
    started, closed = asyncio.Event(), asyncio.Event()

    async def waiting(command, cwd, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(terminal, "run_command", waiting)
    app = command_app()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/stream",
        "headers": [],
        "app": app,
        "client": ("127.0.0.1", 1234),
    }
    response = await chat_stream(ChatRequest(message="核验", mode=mode), Request(scope))
    brokers = []

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if b"event: approval_request" in message.get("body", b""):
            broker = next(iter(app.state.file_approvals.values()))
            brokers.append(broker)
            item = next(item for item in broker.items.values() if item.view["status"] == "pending")
            broker.decide(item.view["id"], "approve")

    await asyncio.wait_for(response(scope, receive, send), 2)
    assert started.is_set() and closed.is_set()
    assert not app.state.file_approvals
    assert not app.state.tools._serial_lock.locked()
    assert all(not broker.active for broker in brokers)
    assert app.state.run_history.list()[0].stopped_reason == "cancelled"
    assert "synthesis" not in app.state.llm.calls


async def test_rejected_approval_cannot_leak_into_next_independent_run(command_workspace, runner):
    context, registry = context_and_registry()
    with bind(context):
        first = asyncio.create_task(registry.execute(command_call()))
    view = await request_view(context.approvals)
    context.approvals.decide(view["id"], "reject")
    assert not (await first).ok
    fresh = RunContext.create(get_settings().agent)
    fresh.approvals = ApprovalBroker(1)
    with bind(fresh):
        second = asyncio.create_task(registry.execute(command_call()))
    view = await request_view(fresh.approvals)
    fresh.approvals.decide(view["id"], "approve")
    assert (await second).ok and len(runner) == 1
    assert fresh.approvals.blocked_reason is None and fresh.usage.total_tokens == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": " "},
        {"command": "x\x00y"},
        {"command": 1},
        {"command": "x", "cwd": "../outside"},
        {"command": "x", "cwd": "missing"},
        {"command": "x", "env": {"LLM_API_KEY": "secret"}},
        {"command": "x", "cwd": "child", "timeout": 0},
    ],
)
async def test_invalid_command_or_directory_never_prompts_or_starts(
    command_workspace, runner, arguments
):
    context, registry = context_and_registry()
    call = ToolCall(id="invalid", name="run_terminal", arguments=arguments)
    with bind(context):
        result = await registry.execute(call)
    assert not result.ok and not runner and not context.approvals.items


def test_environment_allowlist_never_inherits_application_keys_proxy_or_python_target(monkeypatch):
    for key in (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "SECURITY_API_KEY",
        "API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "PIP_TARGET",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "UNLISTED_SECRET",
    ):
        monkeypatch.setenv(key, "synthetic-secret")
    monkeypatch.setenv("TEMP", "synthetic-temp")
    monkeypatch.setenv("PATH", "synthetic-path")
    env = terminal.terminal_environment()
    assert env["TEMP"] == "synthetic-temp" and env["PATH"] == "synthetic-path"
    assert "synthetic-secret" not in env.values()


async def test_generic_tool_execute_preserves_preexisting_truncation_flag():
    async def already_cut(params):
        return ToolResult.success("已被执行器截短的短输出", truncated=True)

    tool = FunctionTool("already_cut", "测试执行器截断标记", TerminalParams, already_cut)
    result = await tool.execute(
        ToolCall(id="cut", name="already_cut", arguments={"command": "unused"})
    )
    assert result.ok and result.truncated and result.content == "已被执行器截短的短输出"


@pytest.mark.parametrize("profile", ["general", "jobhunt"])
def test_prompt_exposes_terminal_rules_only_when_registered(profile):
    unavailable = build_system_prompt(profile, {"calculator", "read_file"})
    enabled = build_system_prompt(profile, {"calculator", "read_file", "run_terminal"})
    assert "`run_terminal`" not in unavailable
    assert "`run_terminal`" in enabled
    assert "确认" in enabled and "沙箱" in enabled
    assert "不能执行 shell" not in enabled and "不能安装依赖" not in enabled
    if profile == "general":
        assert "不能执行 shell" in unavailable


async def test_settings_save_refreshes_terminal_preserves_lock_client_and_file_switch(
    command_workspace, monkeypatch, tmp_path
):
    cfg = get_settings()
    monkeypatch.setattr(
        cfg,
        "agent",
        cfg.agent.model_copy(update={"terminal_enabled": False, "file_write_enabled": False}),
    )
    for name in (
        "AGENT_TERMINAL_ENABLED",
        "AGENT_TERMINAL_TIMEOUT",
        "AGENT_TERMINAL_APPROVAL_TIMEOUT",
        "AGENT_FILE_WRITE_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# temporary test only\nAGENT_TERMINAL_ENABLED=false\nAGENT_FILE_WRITE_ENABLED=false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_api, "ENV_PATH", env_path)

    def apply_temporary_env():
        cfg.agent = AgentSettings(_env_file=env_path, workspace_root=str(command_workspace))

    monkeypatch.setattr(settings_api, "_apply", apply_temporary_env)
    app = command_app()
    app.state.tools = ToolRegistry()
    app.state.agent = Agent(app.state.llm, app.state.tools, cfg.agent)
    app.state.long_term = None
    app.state.memory = None
    app.state.tasks = SimpleNamespace(backend="memory")
    app.include_router(settings_api.router)
    original_settings, original_client, original_registry = (
        app.state.settings,
        app.state.llm,
        app.state.tools,
    )
    original_lock = original_registry._serial_lock
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        before = (await api.get("/api/settings")).json()["agent"]
        assert before["terminal_enabled"] is False
        response = await api.put(
            "/api/settings",
            json={"terminal_enabled": True, "terminal_timeout": 7, "terminal_approval_timeout": 11},
        )
        assert response.status_code == 200
        view = response.json()["agent"]
        assert (
            view["terminal_enabled"]
            and view["terminal_timeout"] == 7
            and view["terminal_approval_timeout"] == 11
        )
        names = (await api.get("/healthz")).json()["tools"]
        assert "run_terminal" in names and "write_file" not in names and "edit_file" not in names
        assert not view["file_write_enabled"] and view["file_approval_required"]
        assert (
            app.state.tools is original_registry and app.state.tools._serial_lock is original_lock
        )
        assert app.state.llm is original_client
        assert original_settings.agent.terminal_enabled is False
        assert "AGENT_TERMINAL_ENABLED=true" in env_path.read_text(encoding="utf-8")
        assert "AGENT_FILE_WRITE_ENABLED=false" in env_path.read_text(encoding="utf-8")
        for update in (
            {"terminal_timeout": 0},
            {"terminal_timeout": 601},
            {"terminal_timeout": "nan"},
            {"terminal_approval_timeout": 0},
            {"terminal_approval_timeout": 3601},
            {"terminal_approval_required": False},
        ):
            assert (await api.put("/api/settings", json=update)).status_code == 422
        assert (await api.put("/api/settings", json={"terminal_enabled": False})).status_code == 200
        assert "run_terminal" not in (await api.get("/healthz")).json()["tools"]
        assert app.state.llm is original_client and app.state.tools._serial_lock is original_lock
