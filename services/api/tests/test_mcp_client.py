"""实际 MCP SDK 与真实 stdio/HTTP；无模型、密钥或私人数据。"""

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest
from app.agent.approvals import ApprovalBroker
from app.agent.runtime import RunContext, _current
from app.core.config import AgentSettings, MCPSettings
from app.llm.types import ToolCall
from app.mcp_client.catalog import MASK, Catalog, ServerConfig, merge_secrets
from app.mcp_client.manager import MCPManager
from app.mcp_client.tool import MCPTool, validate_definition
from app.tools.base import ToolRegistry
from app.tools.terminal import terminal_environment

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_server.py"


@pytest.fixture(params=["stdio", "http"])
async def manager(request, tmp_path):
    settings = MCPSettings(
        _env_file=None,
        enabled=True,
        config_path=tmp_path / "mcp.json",
        connect_timeout=20,
        tool_timeout=3,
    )
    process = None
    if request.param == "stdio":
        config = ServerConfig(
            name="公开测试",
            transport="stdio",
            command=sys.executable,
            args=[str(FIXTURE)],
            cwd=str(tmp_path),
            enabled=True,
            selected_tools=["echo", "write_public", "fail"],
        )
    else:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(  # noqa: ASYNC220 -- test process fixture
            [sys.executable, str(FIXTURE), str(port)],
            cwd=tmp_path,
            env=terminal_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        async with httpx2.AsyncClient(trust_env=False) as client:
            async with asyncio.timeout(20):
                while True:
                    try:
                        await client.get(f"http://127.0.0.1:{port}/mcp")
                        break
                    except httpx2.ConnectError:
                        await asyncio.sleep(0.05)
        config = ServerConfig(
            name="公开测试",
            url=f"http://127.0.0.1:{port}/mcp",
            enabled=True,
            selected_tools=["echo", "write_public", "fail"],
        )
    m = MCPManager(settings)
    m.servers[config.id] = config
    await m.start()
    try:
        conn = m.connections[config.id]
        assert conn.client is not None, conn.error
        yield m, config, conn
    finally:
        await m.close()
        if request.param == "stdio" and os.name == "nt":
            from tests.test_terminal_process import windows_pid_alive

            pids = [int(pid) for pid in (tmp_path / "mcp-pids.txt").read_text().split()]
            assert len(pids) == 2
            alive = [pid for pid in pids if windows_pid_alive(pid)]
            assert not alive, (alive, conn.error, conn.task.cancelled())
        if process:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)


async def execute(m, name, args, approve=True):
    registry = ToolRegistry()
    for tool in m.tools():
        registry.register(tool)
    tool = next(t for t in m.tools() if t.remote_name == name)
    context = RunContext.create(AgentSettings(_env_file=None))
    context.approvals = ApprovalBroker(2)
    token = _current.set(context)
    try:
        task = asyncio.create_task(
            registry.execute(ToolCall(id="c", name=tool.name, arguments=args))
        )
        if not tool.trusted() and approve is not None:
            event = await asyncio.wait_for(context.approvals.events.get(), 2)
            assert event.approval["kind"] == "mcp"
            assert context.execution_facts[0]["status"] == "not_executed"
            context.approvals.decide(event.approval["id"], "approve" if approve else "reject")
        return await task, context
    finally:
        _current.reset(token)


async def test_real_transport_approval_and_fact(manager, tmp_path):
    m, _config, conn = manager
    assert {"echo", "write_public"}.issubset(conn.definitions)
    result, context = await execute(m, "write_public", {"text": "public"}, False)
    assert not result.ok and not (tmp_path / "public.txt").exists()
    assert context.execution_facts[0]["status"] == "not_executed"
    result, context = await execute(m, "write_public", {"text": "public"})
    assert result.ok and (tmp_path / "public.txt").read_text() == "public"
    assert context.execution_facts[0]["status"] == "succeeded"


async def test_trusted_read_and_invalidated_definition(manager):
    m, config, conn = manager
    config.trusted_tools["echo"] = conn.fingerprint("echo")
    tool = MCPTool(m, config.id, "echo", conn)
    assert tool.trusted() and not tool.serial
    result, context = await execute(m, "echo", {"text": "public"})
    assert result.ok and "public" in result.content
    assert not context.approvals.items
    conn.definitions["echo"]["description"] = "changed"
    assert not m.valid(config.id, "echo", tool.fingerprint)
    assert not MCPTool(m, config.id, "echo", conn).trusted()


async def test_timeout_is_unknown_and_never_retried(manager):
    m, config, conn = manager
    m.settings.tool_timeout = 0.05
    config.trusted_tools["echo"] = conn.fingerprint("echo")
    result, context = await execute(m, "echo", {"text": "public", "delay": 1})
    assert not result.ok
    assert context.execution_facts[0]["status"] == "unknown"
    assert len(context.execution_facts) == 1
    registry = ToolRegistry()
    tool = next(t for t in m.tools() if t.remote_name == "echo")
    registry.register(tool)
    token = _current.set(context)
    try:
        result = await registry.execute(
            ToolCall(id="again", name=tool.name, arguments={"text": "public"})
        )
        assert not result.ok and "不再重发" in result.content
        assert context.execution_facts[-1]["status"] == "not_executed"
    finally:
        _current.reset(token)


@pytest.mark.parametrize("mode,expected", [("react", 1), ("plan", 2), ("multi", 3)])
async def test_three_modes_real_transport_and_approval(manager, tmp_path, mode, expected):
    import httpx
    from app.agent.loop import Agent

    from tests.test_api import _collect_from_stream
    from tests.test_file_approvals import WriteLLM, application, pending

    m, _, _ = manager
    tools = ToolRegistry()
    for tool in m.tools():
        tools.register(tool)
    write = next(t for t in m.tools() if t.remote_name == "write_public")
    app = application(llm=WriteLLM(name=write.name, arguments={"text": "public"}))
    app.state.tools = tools
    app.state.agent = Agent(app.state.llm, tools, app.state.settings.agent)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        task = asyncio.create_task(
            api.post("/api/chat/stream", json={"message": "public", "mode": mode})
        )
        try:
            for index in range(expected):
                run_id, _, item = (await pending(app))[0]
                assert item.view["kind"] == "mcp"
                assert item.view["arguments"] == {"text": "public"}
                if index == 0:
                    assert not (tmp_path / "public.txt").exists()
                response = await api.post(
                    f"/api/runs/{run_id}/approvals/{item.view['id']}", json={"decision": "approve"}
                )
                assert response.status_code == 200
            response = await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        events = _collect_from_stream(response.text)
        assert sum(e["type"] == "done" for e in events) == 1
        assert events[-1]["stopped_reason"] == "finished"
        assert (
            len([e for e in events if e.get("approval", {}).get("status") == "applied"]) == expected
        )
        assert (tmp_path / "public.txt").read_text() == "public"


async def test_invalid_arguments_fail_before_approval_or_side_effect(manager, tmp_path):
    m, _, _ = manager
    result, context = await execute(m, "write_public", {"text": 42}, approve=None)
    assert not result.ok and "schema" in result.content
    assert not context.approvals.items
    assert context.execution_facts[0]["status"] == "not_executed"
    assert not (tmp_path / "public.txt").exists()


async def test_cancellation_stops_waiting_and_connection_remains_usable(manager):
    m, config, conn = manager
    config.trusted_tools["echo"] = conn.fingerprint("echo")
    tool = next(t for t in m.tools() if t.remote_name == "echo")
    registry = ToolRegistry()
    registry.register(tool)
    context = RunContext.create(AgentSettings(_env_file=None))
    token = _current.set(context)
    try:
        task = asyncio.create_task(
            registry.execute(
                ToolCall(id="c", name=tool.name, arguments={"text": "public", "delay": 2})
            )
        )
        async with asyncio.timeout(2):
            while not context.execution_facts or context.execution_facts[0]["status"] != "running":  # noqa: ASYNC110 -- observe real transport boundary
                await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert context.execution_facts[0]["status"] == "unknown"
    finally:
        _current.reset(token)
    result, new_context = await execute(m, "echo", {"text": "next public"})
    assert result.ok and not new_context.mcp_uncertain_tools


async def test_config_change_while_awaiting_approval_cannot_dispatch(manager, tmp_path):
    m, config, _ = manager
    registry = ToolRegistry()
    tool = next(t for t in m.tools() if t.remote_name == "write_public")
    registry.register(tool)
    context = RunContext.create(AgentSettings(_env_file=None))
    context.approvals = ApprovalBroker(2)
    token = _current.set(context)
    try:
        task = asyncio.create_task(
            registry.execute(ToolCall(id="c", name=tool.name, arguments={"text": "public"}))
        )
        event = await asyncio.wait_for(context.approvals.events.get(), 2)
        config.headers["X-Changed"] = "public"
        context.approvals.decide(event.approval["id"], "approve")
        result = await task
        assert not result.ok
        assert context.approvals.items[event.approval["id"]].view["status"] == "conflict"
        assert not (tmp_path / "public.txt").exists()
    finally:
        _current.reset(token)


async def test_unknown_failure_namespaces_and_definition_budget(manager):
    from app.llm.types import ChatMessage
    from app.mcp_client.tool import trust_allowed

    m, config, conn = manager
    result, context = await execute(m, "fail", {})
    assert not result.ok and context.execution_facts[0]["status"] == "failed"
    duplicate = config.model_copy(update={"id": "another"})
    m.servers[duplicate.id] = duplicate
    first = MCPTool(m, config.id, "echo", conn)
    second = MCPTool(m, duplicate.id, "echo", conn)
    assert first.name != second.name and len(first.name) <= 64
    for name in ["write_file", "writeFile", "run-command", "shell"]:
        assert not trust_allowed(name, {"annotations": {"readOnlyHint": True}})
    context = RunContext.create(AgentSettings(_env_file=None, context_token_budget=20))
    with pytest.raises(ValueError, match="工具定义"):
        context.fit([ChatMessage.user("public")], [first.json_schema()])


async def test_refresh_preserves_connections_and_shared_lock(manager):
    from app.agent.factory import build_agent_stack, mount_agent_stack, refresh_agent_tools

    from tests.test_file_approvals import application

    m, config, conn = manager
    app = application()
    app.state.mcp = m
    registry, llm = app.state.tools, app.state.llm
    lock = registry._serial_lock
    refresh_agent_tools(app, app.state.settings)
    assert app.state.tools is registry and registry._serial_lock is lock
    assert app.state.llm is llm
    assert any(t.source == "mcp" for t in m.tools())
    stack = build_agent_stack(app.state.settings)
    try:
        mount_agent_stack(app, stack)
        assert registry._serial_lock is lock and app.state.tools is registry
        assert m.connections[config.id] is conn and conn.client
        assert {t.name for t in m.tools()}.issubset(registry.names())
    finally:
        await stack.llm.aclose()


async def test_unsupported_contents_are_visible_and_resource_links_never_followed(
    manager, monkeypatch
):
    from types import SimpleNamespace

    m, config, conn = manager
    calls = []

    async def result(name, arguments):
        calls.append((name, arguments))
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="public " * 2000),
                SimpleNamespace(type="resource_link", uri="https://example.invalid/private"),
                SimpleNamespace(type="image"),
            ],
            structured_content={"public": True},
            is_error=False,
        )

    monkeypatch.setattr(conn.client, "call_tool", result)
    config.trusted_tools["echo"] = conn.fingerprint("echo")
    response, context = await execute(m, "echo", {"text": "public"})
    assert response.ok and response.truncated
    assert "未处理" in response.content and "未自动读取" in response.content
    assert '"public": true' in response.content
    assert len(calls) == 1 and context.execution_facts[0]["status"] == "succeeded"


@pytest.mark.parametrize("failure", [None, "duplicate", "cursor"])
async def test_paginated_discovery_and_cycles(tmp_path, monkeypatch, failure):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import app.mcp_client.manager as module

    @asynccontextmanager
    async def transport(config):
        yield None

    class Client:
        protocol_version = "public-test"

        def __init__(self, *args, **kwargs):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self, cursor=None):
            self.calls.append(cursor)
            name = "one" if cursor is None or failure == "duplicate" else "two"
            tool = SimpleNamespace(
                name=name, model_dump=lambda **kw: {"name": name, "inputSchema": {"type": "object"}}
            )
            return SimpleNamespace(
                tools=[tool], next_cursor="next" if cursor is None or failure == "cursor" else None
            )

    monkeypatch.setattr(module, "Client", Client)
    monkeypatch.setattr(module, "stdio_transport", transport)
    conn = module.Connection(
        ServerConfig(name="public", transport="stdio", command="unused", cwd=str(tmp_path)),
        MCPSettings(_env_file=None),
    )
    try:
        await conn.start()
        if failure:
            assert conn.client is None and conn.error
        else:
            assert set(conn.definitions) == {"one", "two"}
            assert conn.client.calls == [None, "next"]
    finally:
        await conn.close()


async def test_management_selection_binds_real_definition(manager):
    import httpx
    from app.api.mcp import router
    from fastapi import FastAPI

    m, config, conn = manager
    app = FastAPI()
    app.state.mcp = m
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        url = f"/api/mcp/servers/{config.id}/tools"
        denied = await api.put(
            url, json={"selected": ["write_public"], "trusted": ["write_public"]}
        )
        assert denied.status_code == 422
        assert not m.servers[config.id].trusted_tools
        accepted = await api.put(url, json={"selected": ["echo"], "trusted": ["echo"]})
        assert accepted.status_code == 200
        assert m.servers[config.id].trusted_tools["echo"] == conn.fingerprint("echo")
        assert len(m.tools()) == 1 and not m.tools()[0].serial
        assert m.catalog.load()[0].trusted_tools == m.servers[config.id].trusted_tools


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_original_deadline_cancels_mcp_approval_before_dispatch(manager, tmp_path, mode):
    import httpx
    from app.agent.loop import Agent

    from tests.test_api import _collect_from_stream
    from tests.test_file_approvals import WriteLLM, application

    m, _, _ = manager
    tools = ToolRegistry()
    for tool in m.tools():
        tools.register(tool)
    write = next(t for t in m.tools() if t.remote_name == "write_public")
    app = application(llm=WriteLLM(name=write.name, arguments={"text": "public"}), mode_timeout=0.1)
    app.state.tools = tools
    app.state.agent = Agent(app.state.llm, tools, app.state.settings.agent)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        response = await asyncio.wait_for(
            api.post("/api/chat/stream", json={"message": "public", "mode": mode}), 3
        )
    events = _collect_from_stream(response.text)
    assert any(e["type"] == "approval_request" for e in events)
    assert sum(e["type"] == "done" for e in events) == 1
    assert events[-1]["stopped_reason"] == "timeout"
    assert not (tmp_path / "public.txt").exists()
    assert not app.state.file_approvals
    assert not tools._serial_lock.locked()


async def test_rate_limit_is_actionable_and_never_leaks_sdk_error(manager, monkeypatch):
    m, config, conn = manager
    config.trusted_tools["echo"] = conn.fingerprint("echo")

    async def limited(name, arguments):
        error = httpx2.HTTPStatusError(
            "private SDK text",
            request=httpx2.Request("POST", "https://example.com"),
            response=httpx2.Response(429),
        )
        raise ExceptionGroup("private transport text", [error])

    monkeypatch.setattr(conn.client, "call_tool", limited)
    response, context = await execute(m, "echo", {"text": "public"})
    assert not response.ok and "限流" in response.content and "频率限制" in response.content
    assert "private" not in response.content
    assert context.execution_facts[0]["status"] == "unknown"


def test_catalog_masks_preserves_secrets_and_defaults_off(tmp_path):
    config = ServerConfig(
        name="test", url="https://example.com/mcp", headers={"Authorization": "Bearer public-test"}
    )
    assert not config.enabled
    assert config.public()["headers"]["Authorization"] == MASK
    restored = merge_secrets(ServerConfig.model_validate(config.public()), config)
    assert restored.headers == config.headers
    catalog = Catalog(tmp_path / "catalog.json")
    catalog.save([config])
    assert catalog.load() == [config]
    catalog.path.write_text("invalid", encoding="utf-8")
    with pytest.raises(ValueError):
        catalog.load()


@pytest.mark.parametrize("reference", ["https://example.com/schema", "file:///private", "#loop"])
def test_external_schema_references_never_resolve(reference):
    with pytest.raises(ValueError):
        validate_definition(
            {"inputSchema": {"type": "object", "properties": {"x": {"$ref": reference}}}}
        )
