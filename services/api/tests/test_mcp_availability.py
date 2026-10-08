"""回归真实发现名称与旧预设不一致；不连接网络、不调用真实模型。"""

import json

import pytest
from app.agent.factory import refresh_agent_tools
from app.agent.prompts import build_system_prompt
from app.core.config import MCPSettings
from app.mcp_client.catalog import ServerConfig, digest
from app.mcp_client.manager import MCPManager
from app.mcp_client.status import MCPStatusTool, StatusParams


@pytest.fixture
def connected(tmp_path, monkeypatch):
    import app.mcp_client.manager as module

    class Connection:
        def __init__(self, config, settings):
            self.config = config
            self.client = None
            self.error = self.selection_error = ""
            self.protocol = "test"
            self.definitions = {
                name: {"name": name, "description": name, "inputSchema": {"type": "object"}}
                for name in ["tavily_search", "tavily_extract", "tavily_crawl"]
            }

        async def start(self):
            self.client = object()

        async def close(self):
            self.client = None

        def fingerprint(self, name):
            return digest([self.config.connection_fingerprint(), self.definitions[name]])

    monkeypatch.setattr(module, "Connection", Connection)
    manager = MCPManager(
        MCPSettings(_env_file=None, enabled=True, config_path=tmp_path / "mcp.json")
    )
    config = ServerConfig(
        name="公开 Tavily",
        preset="tavily",
        enabled=True,
        url="https://mcp.tavily.com/mcp/",
        headers={"Authorization": "Bearer private-test-secret"},
        selected_tools=["tavily-search", "tavily-extract"],
        trusted_tools={"tavily-search": "old-trust"},
    )
    manager.servers[config.id] = config
    return manager, config


async def test_connect_repairs_selected_aliases_and_persists_without_transferring_trust(connected):
    manager, config = connected
    await manager.connect(config.id)
    repaired = manager.servers[config.id]
    assert repaired.selected_tools == ["tavily_search", "tavily_extract"]
    assert repaired.trusted_tools == {}
    assert repaired.headers == config.headers
    assert manager.catalog.load()[0].selected_tools == repaired.selected_tools
    assert {tool.remote_name for tool in manager.tools()} == set(repaired.selected_tools)
    assert all(not tool.trusted() and tool.serial for tool in manager.tools())
    server = manager.view()["servers"][0]
    assert server["status"] == "connected" and server["available_tool_count"] == 2
    assert server["missing_tools"] == []


@pytest.mark.parametrize("selected", [[], ["tavily-search"], ["unknown"]])
async def test_repair_preserves_explicit_selection_and_reports_missing(connected, selected):
    manager, config = connected
    config.selected_tools = selected
    await manager.connect(config.id)
    expected = ["tavily_search"] if selected == ["tavily-search"] else selected
    assert manager.servers[config.id].selected_tools == expected
    server = manager.view()["servers"][0]
    assert server["available_tool_count"] == (1 if selected == ["tavily-search"] else 0)
    assert server["missing_tools"] == (["unknown"] if selected == ["unknown"] else [])


async def test_custom_service_is_not_renamed_and_existing_hyphen_tool_wins(connected):
    manager, config = connected
    config.preset = "custom"
    config.url = "https://example.invalid/mcp"
    conn = await manager.connect(config.id)
    assert manager.servers[config.id].selected_tools == ["tavily-search", "tavily-extract"]
    assert manager.view()["servers"][0]["available_tool_count"] == 0
    config.preset = "tavily"
    conn.definitions["tavily-search"] = {
        "name": "tavily-search",
        "inputSchema": {"type": "object"},
    }
    manager._repair_tavily_selection(config.id, conn)
    assert manager.servers[config.id].selected_tools == ["tavily-search", "tavily_extract"]


async def test_selection_save_failure_keeps_old_catalog_and_gives_actionable_error(
    connected, monkeypatch
):
    manager, config = connected

    def fail(_values):
        raise PermissionError("private-path-must-not-leak")

    monkeypatch.setattr(manager.catalog, "save", fail)
    await manager.connect(config.id)
    server = manager.view()["servers"][0]
    assert server["available_tool_count"] == 0
    assert server["missing_tools"] == config.selected_tools
    assert "写权限" in server["error"] and "private-path" not in server["error"]
    assert manager.servers[config.id] is config


async def test_status_is_read_only_redacted_and_tracks_actual_tools(connected):
    manager, config = connected
    await manager.connect(config.id)
    result = await MCPStatusTool(manager).run(StatusParams())
    view = json.loads(result.content)
    assert view["servers"][0]["available_tool_count"] == 2
    assert {t["name"] for t in view["servers"][0]["tools"]} == {"tavily_search", "tavily_extract"}
    assert "private-test-secret" not in result.content
    assert "Authorization" not in result.content and "https://" not in result.content
    manager.settings.enabled = False
    view = json.loads((await MCPStatusTool(manager).run(StatusParams())).content)
    assert not view["enabled"] and view["servers"][0]["available_tool_count"] == 0
    assert view["servers"][0]["tools"] == []


async def test_refreshed_model_receives_remote_tools_and_status_result(connected):
    from app.llm.types import StreamDelta, Usage

    from tests.test_file_approvals import WriteLLM, application

    class InspectLLM(WriteLLM):
        def __init__(self):
            super().__init__(name="get_mcp_status", arguments={})
            self.inputs = []

        async def stream_chat(self, messages, **kwargs):
            self.inputs.append((list(messages), kwargs.get("tools", [])))
            if messages[-1].role.value == "tool":
                yield StreamDelta(content="已按实际状态回答。")
                yield StreamDelta(usage=Usage(total_tokens=5), finish_reason="stop")
            else:
                yield StreamDelta(
                    tool_call_deltas=[
                        {
                            "index": 0,
                            "id": "status",
                            "function": {"name": "get_mcp_status", "arguments": "{}"},
                        }
                    ]
                )
                yield StreamDelta(usage=Usage(total_tokens=5), finish_reason="tool_calls")

    manager, config = connected
    app = application(llm=InspectLLM())
    app.state.mcp = manager
    manager.on_change = lambda: refresh_agent_tools(app, app.state.settings)
    await manager.connect(config.id)
    registry = app.state.tools
    assert "get_mcp_status" in registry.names()
    events = [event async for event in app.state.agent.run_stream("查询 MCP 状态")]
    assert events
    definitions = app.state.llm.inputs[0][1]
    assert {tool.name for tool in manager.tools()}.issubset(
        {definition["function"]["name"] for definition in definitions}
    )
    messages = app.state.llm.inputs[-1][0]
    results = [m.content for m in messages if m.role.value == "tool"]
    assert results and '"available_tool_count": 2' in results[0]
    assert "tavily_search" in results[0] and "private-test-secret" not in results[0]
    manager.settings.enabled = False
    refresh_agent_tools(app, app.state.settings)
    assert "get_mcp_status" in registry.names()
    assert not any(name.startswith("mcp_") for name in registry.names())


@pytest.mark.parametrize("profile", ["general", "jobhunt"])
def test_external_terminal_prompt_does_not_deny_mcp_execution(profile):
    prompt = build_system_prompt(profile, {"mcp_commander_start", "get_mcp_status"})
    assert "不能执行 shell" not in prompt
    assert "get_mcp_status" in prompt and "实际" in prompt
    assert "仅在当前工具确有对应能力" in prompt or profile == "jobhunt"
    unavailable = build_system_prompt(profile, {"calculator"})
    assert "get_mcp_status" not in unavailable
