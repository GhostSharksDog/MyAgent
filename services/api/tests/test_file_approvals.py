"""真正的三种内核 + HTTP 决定 + 合成模型；所有副作用仅发生在临时工作区。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from app.agent.approvals import ApprovalBroker, merge_approval_events
from app.agent.events import EventType
from app.agent.loop import Agent
from app.agent.runtime import RunContext
from app.api.auth import ApiKeyMiddleware
from app.api.routes import chat_stream, router
from app.api.schemas import ChatRequest
from app.core.config import AgentSettings, SecuritySettings, Settings, get_settings
from app.llm.types import StreamDelta, ToolCall, Usage
from app.runs.history import RunHistory
from app.session.store import InMemorySessionStore
from app.tools.base import ToolRegistry
from app.tools.file_changes import text_format, unified_diff
from app.tools.files import EditFileTool, WriteFileTool
from fastapi import FastAPI
from starlette.requests import Request

from tests.test_agent_runtime import RuntimeLLM
from tests.test_api import _collect_from_stream


@pytest.fixture(autouse=True)
def reset_sse_event_loop(monkeypatch):
    # 本文件的 ASGI 用例各自使用 pytest 的事件循环；SSE 全局退出事件不能跨循环复用。
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "should_exit_event", None)


class WriteLLM(RuntimeLLM):
    def __init__(self, *, name="write_file", arguments=None):
        super().__init__()
        self.tool_name = name
        self.arguments = arguments
        self.serial = 0

    async def stream_chat(self, messages, **kwargs):
        self.calls.append("expert")
        if messages[-1].role.value == "tool":
            yield StreamDelta(content="工具已返回；按实际结果结束。")
            yield StreamDelta(usage=Usage(total_tokens=5), finish_reason="stop")
            return
        self.serial += 1
        args = self.arguments or {"path": f"notes/result-{self.serial}.md", "content": "公开样本\n"}
        yield StreamDelta(
            tool_call_deltas=[
                {
                    "index": 0,
                    "id": "same-child-call-id",
                    "function": {"name": self.tool_name, "arguments": json.dumps(args)},
                }
            ]
        )
        yield StreamDelta(usage=Usage(total_tokens=5), finish_reason="tool_calls")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    cfg = get_settings()
    monkeypatch.setattr(
        cfg,
        "agent",
        AgentSettings(
            _env_file=None,
            workspace_root=str(root),
            file_write_enabled=True,
            file_approval_required=True,
            file_approval_timeout=1,
        ),
    )
    return root


def application(*, llm=None, mode_timeout=0, auth=False):
    app = FastAPI()
    agent_cfg = get_settings().agent.model_copy(update={"run_timeout": mode_timeout})
    app.state.settings = Settings(
        agent=agent_cfg,
        security=SecuritySettings(
            _env_file=None,
            api_key="synthetic-access-key" if auth else "",
        ),
    )
    app.state.tools = ToolRegistry()
    app.state.tools.register(WriteFileTool())
    app.state.tools.register(EditFileTool())
    app.state.llm = llm or WriteLLM()
    app.state.agent = Agent(app.state.llm, app.state.tools, agent_cfg)
    app.state.sessions = InMemorySessionStore()
    app.state.run_history = RunHistory(app.state.settings.run_history)
    app.state.replayer = None
    app.include_router(router)
    app.add_middleware(ApiKeyMiddleware, settings=app.state.settings)
    return app


async def pending(app, *, count=1):
    async with asyncio.timeout(2):
        while True:
            brokers = getattr(app.state, "file_approvals", {})
            items = [
                (run, broker, item)
                for run, broker in brokers.items()
                for item in broker.items.values()
                if item.view["status"] == "pending"
            ]
            if len(items) >= count:
                return items
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("mode,expected", [("react", 1), ("plan", 2), ("multi", 3)])
async def test_real_modes_pause_before_write_and_resume_once(workspace, mode, expected):
    app = application(auth=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "synthetic-access-key"},
    ) as api:
        task = asyncio.create_task(
            api.post("/api/chat/stream", json={"message": "生成公开样本", "mode": mode})
        )
        ids = []
        try:
            for _ in range(expected):
                run_id, broker, item = (await pending(app))[0]
                ids.append(item.view["id"])
                target = workspace / item.view["path"]
                assert not target.exists()
                if len(ids) == 1:
                    assert not (workspace / "notes").exists()
                assert "+公开样本" in item.view["diff"]
                assert len(broker.items) <= expected
                path = f"/api/runs/{run_id}/approvals/{item.view['id']}"
                assert (
                    await api.post(
                        path, json={"decision": "approve"}, headers={"X-API-Key": "wrong"}
                    )
                ).status_code == 401
                assert (
                    await api.post(path.replace(run_id, "wrong-run"), json={"decision": "approve"})
                ).status_code == 409
                result = await api.post(path, json={"decision": "approve"})
                assert result.json() == {"status": "approved"}
                assert (await api.post(path, json={"decision": "approve"})).status_code == 409
            response = await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        events = _collect_from_stream(response.text)
        assert sum(e["type"] == "done" for e in events) == 1
        assert events[-1]["stopped_reason"] == "finished"
        requests = [e for e in events if e["type"] == "approval_request"]
        applied = [e for e in events if e.get("approval", {}).get("status") == "applied"]
        assert len(requests) == len(applied) == expected
        assert len(set(ids)) == expected
        assert len(list(workspace.rglob("*.md"))) == expected
        for event in requests:
            assert event["run_id"] == response.headers["X-Run-Id"]
            index = events.index(event)
            commit = next(e for e in applied if e["approval"]["id"] == event["approval"]["id"])
            assert events.index(commit) > index
        record = app.state.run_history.get(response.headers["X-Run-Id"])
        assert record.tool_calls == record.tool_results == expected
        assert "公开样本" not in record.model_dump_json()
        assert "notes/" not in record.model_dump_json()
        assert not app.state.file_approvals
        assert (await api.post(path, json={"decision": "approve"})).status_code == 409


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_reject_never_writes_or_reprompts_in_same_run(workspace, mode):
    app = application()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        task = asyncio.create_task(
            api.post("/api/chat/stream", json={"message": "写样本", "mode": mode})
        )
        run_id, _, item = (await pending(app))[0]
        await api.post(
            f"/api/runs/{run_id}/approvals/{item.view['id']}", json={"decision": "reject"}
        )
        events = _collect_from_stream((await asyncio.wait_for(task, 2)).text)
        assert not list(workspace.iterdir())
        assert not any(e.get("approval", {}).get("status") == "applied" for e in events)
        first_rejection = next(
            i for i, e in enumerate(events) if e.get("approval", {}).get("status") == "rejected"
        )
        assert not any(e["type"] == "approval_request" for e in events[first_rejection + 1 :])
        assert sum(e["type"] == "done" for e in events) == 1
        record = app.state.run_history.list()[0]
        assert record.tool_failures > 0


@pytest.mark.parametrize(
    "name,args,before,after",
    [
        (
            "write_file",
            {"path": "a.txt", "content": "new\n", "overwrite": True},
            b"old\n",
            b"new\n",
        ),
        (
            "edit_file",
            {"path": "a.txt", "old_text": "old", "new_text": "new"},
            b"\xef\xbb\xbfold\r\n",
            b"\xef\xbb\xbfnew\r\n",
        ),
        ("edit_file", {"path": "a.txt", "old_text": "old", "new_text": "new"}, b"old", b"new"),
    ],
)
async def test_exact_approved_bytes_and_format_metadata(workspace, name, args, before, after):
    target = workspace / "a.txt"
    target.write_bytes(before)
    app = application(llm=WriteLLM(name=name, arguments=args))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        task = asyncio.create_task(api.post("/api/chat/stream", json={"message": "改样本"}))
        run_id, _, item = (await pending(app))[0]
        assert target.read_bytes() == before
        assert item.view["before_format"] == text_format(before)
        assert item.view["after_format"] == text_format(after)
        if not before.endswith(b"\n"):
            assert "No newline" in item.view["diff"]
        await api.post(
            f"/api/runs/{run_id}/approvals/{item.view['id']}", json={"decision": "approve"}
        )
        events = _collect_from_stream((await task).text)
        assert target.read_bytes() == after
        assert any(e.get("approval", {}).get("status") == "applied" for e in events)
        assert list(workspace.iterdir()) == [target]  # 没留下临时文件


@pytest.mark.parametrize("mutation", ["content", "delete", "root", "permission", "secret"])
async def test_old_approval_cannot_override_changed_file_or_authority(
    workspace, monkeypatch, mutation
):
    target = workspace / (".env" if mutation == "secret" else "a.txt")
    target.write_text("original", encoding="utf-8")
    cfg = get_settings()
    if mutation == "secret":
        monkeypatch.setattr(cfg, "agent", cfg.agent.model_copy(update={"file_allow_secrets": True}))
    app = application(
        llm=WriteLLM(arguments={"path": target.name, "content": "proposed", "overwrite": True})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        task = asyncio.create_task(api.post("/api/chat/stream", json={"message": "修改"}))
        run_id, _, item = (await pending(app))[0]
        if mutation == "content":
            target.write_text("external", encoding="utf-8")
        elif mutation == "delete":
            target.unlink()
        else:
            updates = {
                "root": {"workspace_root": ""},
                "permission": {"file_write_enabled": False},
                "secret": {"file_allow_secrets": False},
            }[mutation]
            monkeypatch.setattr(cfg, "agent", cfg.agent.model_copy(update=updates))
        await api.post(
            f"/api/runs/{run_id}/approvals/{item.view['id']}", json={"decision": "approve"}
        )
        events = _collect_from_stream((await task).text)
        assert any(e.get("approval", {}).get("status") == "conflict" for e in events)
        assert not any(e.get("approval", {}).get("status") == "applied" for e in events)
        assert (
            not target.exists()
            if mutation == "delete"
            else target.read_text(encoding="utf-8")
            == ("external" if mutation == "content" else "original")
        )


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_whole_deadline_expires_approval_and_no_later_model_call(workspace, mode):
    app = application(mode_timeout=0.08)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        response = await api.post("/api/chat/stream", json={"message": "写样本", "mode": mode})
        events = _collect_from_stream(response.text)
        assert sum(e["type"] == "done" for e in events) == 1
        assert events[-1]["stopped_reason"] == "timeout"
        assert any(e["type"] == "approval_request" for e in events)
        assert not list(workspace.iterdir())
        assert "synthesis" not in app.state.llm.calls
        assert not app.state.file_approvals


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_real_sse_disconnect_cleans_pending_children(workspace, monkeypatch, mode):
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "should_exit_event", None)
    app = application()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/stream",
        "headers": [],
        "app": app,
        "client": ("127.0.0.1", 2345),
    }
    response = await chat_stream(ChatRequest(message="写样本", mode=mode), Request(scope))
    saw_preview = asyncio.Event()
    broker_copy = []

    async def receive():
        await saw_preview.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if b"approval_request" in message.get("body", b""):
            broker_copy.extend(app.state.file_approvals.values())
            saw_preview.set()

    await asyncio.wait_for(response(scope, receive, send), 2)
    assert saw_preview.is_set()
    assert not app.state.file_approvals
    assert not list(workspace.iterdir())
    assert app.state.run_history.list()[0].stopped_reason == "cancelled"
    for broker in broker_copy:
        assert not broker.active
        assert all(i.future.done() for i in broker.items.values())
    assert "synthesis" not in app.state.llm.calls


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_non_interactive_http_fails_closed(workspace, mode):
    app = application()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        response = await api.post("/api/chat", json={"message": "写样本", "mode": mode})
        assert response.status_code == 200
        assert not list(workspace.iterdir())
        assert app.state.run_history.list()[0].tool_failures > 0
        assert not getattr(app.state, "file_approvals", {})


async def test_waiting_confirmation_does_not_hold_serial_mutex_or_tool_execution_timeout(workspace):
    cfg = get_settings().agent
    registry = ToolRegistry()
    tool = registry.register(WriteFileTool())
    tool.timeout = 0.001
    ctx = RunContext.create(cfg)
    ctx.approvals = ApprovalBroker(1)
    agent = Agent(WriteLLM(), registry, cfg)
    source = merge_approval_events(agent.run_stream("写样本", run_context=ctx), ctx.approvals)
    events = []
    async for event in source:
        events.append(event)
        if event.type is EventType.APPROVAL_REQUEST:
            assert not registry._serial_lock.locked()
            await asyncio.sleep(0.03)  # 人审超过执行超时仍可批准
            ctx.approvals.decide(event.approval["id"], "approve")
    assert any(e.approval and e.approval["status"] == "applied" for e in events)
    assert events[-1].stopped_reason == "finished"


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "../escape.txt", "content": "x"},
        {"path": ".env", "content": "x"},
        {"path": "a.txt", "content": "x" * 200001},
        {"path": "a.txt", "content": "\x00"},
    ],
)
async def test_invalid_or_unreviewable_proposal_never_creates_approval(workspace, arguments):
    app = application(llm=WriteLLM(arguments=arguments))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        events = _collect_from_stream(
            (await api.post("/api/chat/stream", json={"message": "核验"})).text
        )
        assert not any(e["type"] == "approval_request" for e in events)
        assert not list(workspace.iterdir())
        assert app.state.run_history.list()[0].tool_failures == 1


def test_diff_exposes_newline_change_and_formats_bom_crlf():
    diff = unified_diff("x\r\n", "x", "a.txt")
    assert "-x\r\n" in diff and "+x\n\\ No newline" in diff
    assert text_format(b"\xef\xbb\xbfx\r\n") == "UTF-8 BOM · CRLF · 有末尾换行"
    assert text_format(b"x\r\ny\n") == "UTF-8 · CRLF/LF · 有末尾换行"


async def test_explicit_direct_mode_remains_available(workspace, monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(
        cfg, "agent", cfg.agent.model_copy(update={"file_approval_required": False})
    )
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    result = await registry.execute(
        ToolCall(id="direct", name="write_file", arguments={"path": "a.txt", "content": "direct"})
    )
    assert result.ok
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "direct"


async def test_two_requests_preview_same_file_only_one_can_commit(workspace):
    (workspace / "a.txt").write_text("original", encoding="utf-8")
    app = application(
        llm=WriteLLM(arguments={"path": "a.txt", "content": "approved", "overwrite": True})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        tasks = [
            asyncio.create_task(api.post("/api/chat/stream", json={"message": "修改样本"}))
            for _ in range(2)
        ]
        try:
            items = await pending(app, count=2)
            assert len({run for run, _, _ in items}) == 2
            for run, _, item in items:
                await api.post(
                    f"/api/runs/{run}/approvals/{item.view['id']}", json={"decision": "approve"}
                )
            events = [_collect_from_stream(r.text) for r in await asyncio.gather(*tasks)]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        statuses = [e["approval"]["status"] for frames in events for e in frames if "approval" in e]
        assert statuses.count("applied") == statuses.count("conflict") == 1
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "approved"


async def test_token_budget_exhausted_while_waiting_prevents_approved_write(workspace):
    cfg = get_settings().agent
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    ctx = RunContext.create(cfg, token_limit=100)
    ctx.approvals = ApprovalBroker(1)
    agent = Agent(WriteLLM(), registry, cfg)
    source = merge_approval_events(agent.run_stream("写样本", run_context=ctx), ctx.approvals)
    events = []
    async for event in source:
        events.append(event)
        if event.type is EventType.APPROVAL_REQUEST:
            ctx.usage.total_tokens = 100  # 同一运行的其它专家已消耗预算
            ctx.approvals.decide(event.approval["id"], "approve")
    assert events[-1].stopped_reason == "token_budget"
    assert sum(e.type is EventType.DONE for e in events) == 1
    assert not list(workspace.iterdir())


async def test_source_constructor_error_still_expires_broker(workspace):
    class BrokenSource:
        def run_stream(self, *args, **kwargs):
            raise RuntimeError("synthetic construction failure")

    app = application()
    app.state.agent = BrokenSource()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        events = _collect_from_stream(
            (await api.post("/api/chat/stream", json={"message": "样本"})).text
        )
    assert sum(e["type"] == "done" for e in events) == 1
    assert events[-1]["stopped_reason"] == "error"
    assert not app.state.file_approvals
    assert not list(workspace.iterdir())


def test_approval_settings_save_into_temp_env_refreshes_tools_and_preserves_lock(
    workspace, tmp_path, monkeypatch
):
    from app.api import settings as settings_api
    from fastapi.testclient import TestClient

    original = get_settings()
    disabled = original.agent.model_copy(update={"file_write_enabled": False})
    temporary_env = tmp_path / "settings.env"
    monkeypatch.setattr(settings_api, "ENV_PATH", temporary_env)
    monkeypatch.setattr(settings_api, "_apply", lambda: None)

    def fresh_settings():
        agent = AgentSettings(_env_file=temporary_env, workspace_root=str(workspace))
        return original.model_copy(update={"agent": agent})

    monkeypatch.setattr(settings_api, "get_settings", fresh_settings)
    app = application()
    app.state.settings = original.model_copy(update={"agent": disabled})
    app.state.tools = ToolRegistry()
    old_registry = app.state.tools
    old_lock = old_registry._serial_lock
    old_llm = app.state.llm
    app.include_router(settings_api.router)
    with TestClient(app) as api:
        result = api.put(
            "/api/settings",
            json={
                "file_write_enabled": True,
                "file_approval_required": True,
                "file_approval_timeout": 12.5,
            },
        )
        assert result.status_code == 200
        text = temporary_env.read_text(encoding="utf-8")
        assert "AGENT_FILE_APPROVAL_REQUIRED=true" in text
        assert "AGENT_FILE_APPROVAL_TIMEOUT=12.5" in text
        assert app.state.tools is old_registry and old_registry._serial_lock is old_lock
        assert app.state.llm is old_llm  # 保存权限不关闭在途模型连接
        assert {"write_file", "edit_file"} <= set(old_registry.names())
        assert app.state.settings.agent.file_approval_timeout == 12.5
        assert disabled.file_write_enabled is False
        assert result.json()["agent"]["file_approval_required"] is True
        assert api.put("/api/settings", json={"file_approval_timeout": -1}).status_code == 422
        result = api.put("/api/settings", json={"file_write_enabled": False})
        assert result.status_code == 200
        assert "write_file" not in old_registry.names()


async def test_conflict_requires_fresh_preview_next_run_can_commit(workspace):
    target = workspace / "a.txt"
    target.write_text("original", encoding="utf-8")
    app = application(
        llm=WriteLLM(arguments={"path": "a.txt", "content": "proposed", "overwrite": True})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        ids = []
        for attempt in range(2):
            task = asyncio.create_task(
                api.post("/api/chat/stream", json={"message": "重新核验修改"})
            )
            run, _, item = (await pending(app))[0]
            ids.append(item.view["id"])
            if attempt == 0:
                target.write_text("external", encoding="utf-8")
            else:
                assert "-external" in item.view["diff"]
                assert target.read_text(encoding="utf-8") == "external"
            await api.post(
                f"/api/runs/{run}/approvals/{item.view['id']}", json={"decision": "approve"}
            )
            events = _collect_from_stream((await task).text)
            expected = "conflict" if attempt == 0 else "applied"
            assert any(e.get("approval", {}).get("status") == expected for e in events)
        assert ids[0] != ids[1]
        assert target.read_text(encoding="utf-8") == "proposed"


async def test_overwrite_detaches_hardlink_without_changing_external_alias(workspace):
    import os

    external = workspace.parent / "outside.txt"
    external.write_text("external original", encoding="utf-8")
    target = workspace / "a.txt"
    os.link(external, target)
    app = application(
        llm=WriteLLM(arguments={"path": "a.txt", "content": "proposed", "overwrite": True})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        task = asyncio.create_task(api.post("/api/chat/stream", json={"message": "修改样本"}))
        run, _, item = (await pending(app))[0]
        await api.post(f"/api/runs/{run}/approvals/{item.view['id']}", json={"decision": "approve"})
        events = _collect_from_stream((await task).text)
    assert any(e.get("approval", {}).get("status") == "applied" for e in events)
    assert target.read_text(encoding="utf-8") == "proposed"
    assert external.read_text(encoding="utf-8") == "external original"
    assert target.stat().st_ino != external.stat().st_ino
