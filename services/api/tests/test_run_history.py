"""运行摘要的反例：取消也可查、子任务不漏计、未知用量不假装为零、原文不能落盘。"""

from __future__ import annotations

import asyncio
import json

import pytest
from app.agent.events import AgentEvent, EventType
from app.agent.runtime import RunContext
from app.core.config import AgentSettings, RunHistorySettings, get_settings
from app.llm.types import StreamDelta, Usage
from app.main import app
from app.runs.history import RunHistory, RunRecord, RunRecorder
from app.tools.base import Tool, ToolResult
from fastapi.testclient import TestClient
from pydantic import BaseModel

from tests.test_agent_runtime import RuntimeLLM, _agent
from tests.test_api import FakeAgent, _collect_from_stream


@pytest.fixture(autouse=True)
def reset_sse_event_loop(monkeypatch):
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "should_exit_event", None)


def config(tmp_path, **updates):
    return RunHistorySettings(_env_file=None, path=str(tmp_path / "runs.db"), **updates)


@pytest.mark.parametrize("backend", ["memory", "sql"])
async def test_store_snapshot_retention_and_delete(tmp_path, backend):
    settings = config(tmp_path, backend=backend, max_records=2)
    store = RunHistory(settings)
    await store.ensure_ready()
    rows = [
        RunRecord(mode="react", session_id="session", started_at=f"2026-10-06T00:00:0{i}Z")
        for i in range(3)
    ]
    for r in rows:
        await store.save(r)
    assert store.get(rows[0].run_id) is None
    assert len(store.list()) == 2
    with pytest.raises(ValueError, match="先停止"):
        await store.delete(rows[2].run_id)
    rows[2].stopped_reason = "cancelled"
    await store.save(rows[2])
    copy = store.get(rows[2].run_id)
    copy.stopped_reason = "error"
    assert store.get(rows[2].run_id).stopped_reason == "cancelled"
    assert len(store.list(session_id="other")) == 0
    assert len(store.list(reason="cancelled")) == 1
    assert await store.delete(rows[2].run_id)
    assert not await store.delete(rows[2].run_id)
    if backend == "memory":
        assert not (tmp_path / "runs.db").exists()
        assert RunHistory(settings).list() == []
    else:
        restarted = RunHistory(settings)
        await restarted.ensure_ready()
        assert len(restarted.list()) == 1
        assert restarted.list()[0].stopped_reason == "interrupted"
        assert not restarted.list()[0].usage_complete


async def test_sql_terminal_survives_restart_and_original_text_never_reaches_disk(tmp_path):
    settings = config(tmp_path, backend="sql")
    store = RunHistory(settings)
    await store.ensure_ready()
    r = RunRecorder(RunRecord(mode="multi"), settings, ["calculator"])
    secret = "SECRET_INPUT_KEY_PATH_AND_PRIVATE_DOCUMENT_928671"
    for kind in [
        EventType.START,
        EventType.TOKEN,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.PLAN,
        EventType.DELEGATE,
        EventType.DELEGATE_RESULT,
        EventType.ERROR,
        EventType.FINAL,
    ]:
        r.observe(
            AgentEvent(
                type=kind,
                content=secret,
                specialist=secret,
                tool_name=secret,
                tool_args={"password": secret},
                plan={"goal": secret, "steps": [{"description": secret, "status": secret}]},
            )
        )
    r.observe(
        AgentEvent(type=EventType.DONE, stopped_reason="token_budget", usage=Usage(total_tokens=13))
    )
    await store.save(r.finish())
    assert secret not in json.dumps(store.get(r.record.run_id).model_dump())
    assert secret.encode() not in (tmp_path / "runs.db").read_bytes()
    restarted = RunHistory(settings)
    await restarted.ensure_ready()
    record = restarted.get(r.record.run_id)
    assert record.stopped_reason == "token_budget"
    assert record.usage.total_tokens == 13
    assert record.usage_complete
    assert record.events[1].tool_name == "unknown_tool"


def test_event_cap_does_not_lose_terminal_or_totals(tmp_path):
    recorder = RunRecorder(RunRecord(mode="plan"), config(tmp_path, max_events=1), ["calculator"])
    for _ in range(4):
        recorder.observe(AgentEvent(type=EventType.TOOL_CALL, tool_name="calculator"))
        recorder.observe(
            AgentEvent(type=EventType.TOOL_RESULT, tool_name="calculator", tool_ok=False)
        )
    recorder.observe(
        AgentEvent(
            type=EventType.DONE,
            stopped_reason="timeout",
            usage=Usage(total_tokens=42),
            steps_used=5,
        )
    )
    record = recorder.finish()
    assert len(record.events) == 1
    assert record.events_dropped == 8
    assert record.tool_calls == record.tool_results == record.tool_failures == 4
    assert record.stopped_reason == "timeout"
    assert record.usage.total_tokens == 42
    assert record.steps_used == 5


def test_optional_plan_metadata_cannot_break_recording(tmp_path):
    recorder = RunRecorder(RunRecord(mode="plan"), config(tmp_path), [])
    recorder.observe(AgentEvent(type=EventType.PLAN, plan={"steps": None, "goal": "私密文本"}))
    recorder.observe(
        AgentEvent(type=EventType.DONE, stopped_reason="finished", usage=Usage(total_tokens=4))
    )
    record = recorder.finish()
    assert record.stopped_reason == "finished"
    assert record.events[0].counts is None
    assert "私密文本" not in record.model_dump_json()


def test_children_usage_is_not_added_twice_and_cancel_keeps_known_usage(tmp_path):
    recorder = RunRecorder(RunRecord(mode="multi"), config(tmp_path), [])
    ctx = RunContext.create(AgentSettings(_env_file=None))
    ctx.usage = Usage(total_tokens=60)
    ctx.observer = recorder.observe_runtime
    ctx.decorate(AgentEvent(type=EventType.DONE, usage=Usage(total_tokens=99)), root=False)
    assert recorder.record.stopped_reason == "running"
    record = recorder.finish("cancelled")
    assert record.usage.total_tokens == 60
    assert not record.usage_complete


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_runtime_records_child_tools_without_duplicates(tmp_path, mode):
    class Empty(BaseModel):
        pass

    class SampleTool(Tool):
        name = "sample"
        description = "公开测试"
        params_model = Empty

        async def run(self, args):
            return ToolResult.failure("PRIVATE_FILE_TEXT_974639")

    class ToolsLLM(RuntimeLLM):
        async def stream_chat(self, messages, **kwargs):
            if messages[-1].role.value != "tool":
                yield StreamDelta(
                    tool_call_deltas=[
                        {
                            "index": 0,
                            "id": "test",
                            "function": {"name": "sample", "arguments": "{}"},
                        }
                    ]
                )
                yield StreamDelta(usage=Usage(total_tokens=5), finish_reason="tool_calls")
            else:
                yield StreamDelta(content="公开结论")
                yield StreamDelta(usage=Usage(total_tokens=5), finish_reason="stop")

    llm = ToolsLLM()
    agent = _agent(mode, llm)
    agent._tools.register(SampleTool())
    r = RunRecorder(RunRecord(mode=mode), config(tmp_path), ["sample"])
    ctx = RunContext.create(agent._s)
    ctx.observer = r.observe_runtime
    async for _ in agent.run_stream("公开任务", run_context=ctx):
        pass
    record = r.finish()
    expected = {"react": 1, "plan": 2, "multi": 3}[mode]
    assert record.tool_calls == record.tool_results == record.tool_failures == expected
    assert record.usage.total_tokens == ctx.usage.total_tokens
    assert "PRIVATE_FILE_TEXT" not in record.model_dump_json()
    if mode != "react":
        assert any(e.scope == "child" and e.kind == "tool_result" for e in record.events)


@pytest.mark.parametrize(
    "reason", ["finished", "timeout", "token_budget", "error", "max_steps", "loop_detected"]
)
def test_sse_terminal_is_queryable_once_and_ids_are_isolated(tmp_path, reason):
    with TestClient(app) as client:
        app.state.run_history = RunHistory(config(tmp_path))
        app.state.agent = FakeAgent(
            events=[
                AgentEvent(type=EventType.START),
                AgentEvent(
                    type=EventType.DONE, stopped_reason=reason, usage=Usage(total_tokens=12)
                ),
            ]
        )
        ids = []
        for _ in range(2):
            response = client.post("/api/chat/stream", json={"message": "不应存储的问题"})
            frames = _collect_from_stream(response.text)
            ids.append(response.headers["X-Run-Id"])
            assert {e["run_id"] for e in frames} == {ids[-1]}
            assert sum(e["type"] == "done" for e in frames) == 1
            assert frames[-1]["record_saved"]
            record = client.get("/api/runs/" + ids[-1]).json()
            assert record["stopped_reason"] == reason
            assert record["usage"]["total_tokens"] == 12
            assert "不应存储的问题" not in json.dumps(record, ensure_ascii=False)
        assert ids[0] != ids[1]
        result = client.get("/api/runs?limit=1&offset=1&stopped_reason=" + reason).json()
        assert result["total"] == 2 and len(result["runs"]) == 1
        assert "events" not in result["runs"][0]
        assert client.get("/api/runs?stopped_reason=bogus").status_code == 422
        assert client.delete("/api/runs/" + ids[0]).json() == {"deleted": True}
        assert client.get("/api/runs/" + ids[0]).status_code == 404


def test_non_stream_run_gets_id_and_missing_usage_stays_unknown(tmp_path):
    with TestClient(app) as client:
        app.state.run_history = RunHistory(config(tmp_path))
        app.state.agent = FakeAgent()
        response = client.post("/api/chat", json={"message": "公开计算"}).json()
        record = client.get("/api/runs/" + response["run_id"]).json()
        assert record["stopped_reason"] == "finished"
        assert record["tool_calls"] == 1
        # 非流式替身返回 Usage 对象；真实缺失由内核的 usage_complete 标志传递。
        app.state.agent = FakeAgent(events=[AgentEvent(type=EventType.DONE)])
        response = client.post("/api/chat/stream", json={"message": "公开任务"})
        record = client.get("/api/runs/" + response.headers["X-Run-Id"]).json()
        assert record["usage"]["total_tokens"] == 0
        assert not record["usage_complete"]


@pytest.mark.parametrize("boom", [False, True])
def test_early_stream_exit_and_exception_leave_error_record(tmp_path, boom):
    with TestClient(app) as client:
        app.state.run_history = RunHistory(config(tmp_path))
        app.state.agent = FakeAgent(events=[AgentEvent(type=EventType.START)], boom=boom)
        response = client.post("/api/chat/stream", json={"message": "公开任务"})
        frames = _collect_from_stream(response.text)
        assert sum(e["type"] == "done" for e in frames) == 1
        record = client.get("/api/runs/" + response.headers["X-Run-Id"]).json()
        assert record["stopped_reason"] == "error"
        assert not record["usage_complete"]


async def test_concurrent_store_save_remains_bounded_and_durable(tmp_path):
    settings = config(tmp_path, backend="sql", max_records=3)
    store = RunHistory(settings)
    await store.ensure_ready()
    rows = [RunRecord(mode="react", stopped_reason="finished") for _ in range(10)]
    await asyncio.gather(*(store.save(row) for row in rows))
    assert len(store.list()) == 3
    other = RunHistory(settings)
    await other.ensure_ready()
    assert {r.run_id for r in other.list()} == {r.run_id for r in store.list()}


async def test_sql_startup_failure_is_actionable_and_never_silently_uses_memory(tmp_path):
    settings = config(tmp_path, backend="sql").model_copy(update={"path": str(tmp_path)})
    store = RunHistory(settings)
    with pytest.raises(RuntimeError, match=r"RUN_HISTORY_PATH.*RUN_HISTORY_BACKEND=memory"):
        await store.ensure_ready()
    assert store.backend == "sql" and store.list() == []


def test_persistence_setting_reports_saved_and_active_without_mutating_user_env(
    tmp_path, monkeypatch
):
    from app.api import settings as settings_api

    temp_env = tmp_path / ".env"
    monkeypatch.setattr(settings_api, "ENV_PATH", temp_env)
    monkeypatch.setenv("RUN_HISTORY_BACKEND", "memory")
    get_settings.cache_clear()
    with TestClient(app) as client:
        response = client.put("/api/settings", json={"run_history_backend": "sql"})
        assert response.status_code == 200
        assert "RUN_HISTORY_BACKEND=sql" in temp_env.read_text(encoding="utf-8")
        # 设置文件读回受环境变量覆盖；模拟下次读取已保存配置，不修改真正 .env。
        monkeypatch.setenv("RUN_HISTORY_BACKEND", "sql")
        get_settings.cache_clear()
        view = client.get("/api/settings").json()["run_history"]
        assert view["backend"] == "sql" and view["active_backend"] == "memory"
        assert view["restart_required"]
        assert not (tmp_path / "runs.db").exists()
    get_settings.cache_clear()


@pytest.mark.parametrize("failure_at", [1, 2])
@pytest.mark.parametrize("stream", [False, True])
def test_storage_failure_is_visible_without_repeating_model(tmp_path, failure_at, stream):
    class BrokenHistory(RunHistory):
        saves = 0

        async def save(self, record):
            self.saves += 1
            if self.saves == failure_at:
                raise OSError("private-path-and-secret")
            await super().save(record)

    with TestClient(app) as client:
        app.state.run_history = BrokenHistory(config(tmp_path))
        fake = FakeAgent()
        app.state.agent = fake
        response = client.post(
            "/api/chat/stream" if stream else "/api/chat", json={"message": "公开任务"}
        )
        if failure_at == 1:
            assert response.status_code == 503
            assert "RUN_HISTORY_PATH" in response.json()["detail"]
            assert "private-path" not in response.text
            assert fake.received == []
        else:
            assert response.status_code == 200
            terminal = _collect_from_stream(response.text)[-1] if stream else response.json()
            assert terminal["stopped_reason"] == "finished"
            assert terminal["record_saved"] is False
            assert len(fake.received) == 1


async def test_disconnect_before_generator_starts_still_finishes_record(tmp_path):
    from app.api.routes import chat_stream
    from app.api.schemas import ChatRequest
    from app.core.config import Settings
    from app.session.store import InMemorySessionStore
    from app.tools.base import ToolRegistry
    from starlette.applications import Starlette
    from starlette.requests import Request

    isolated = Starlette()
    isolated.state.settings = Settings(agent=AgentSettings(_env_file=None))
    isolated.state.agent = FakeAgent()
    isolated.state.tools = ToolRegistry()
    isolated.state.sessions = InMemorySessionStore()
    isolated.state.run_history = RunHistory(config(tmp_path))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/stream",
        "headers": [],
        "app": isolated,
        "client": ("127.0.0.1", 1234),
    }
    response = await chat_stream(ChatRequest(message="公开任务"), Request(scope))

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        # 明确卡在响应头发送，生成器尚未启动；对照旧的 finally-only 实现。
        await asyncio.sleep(0.1)

    await asyncio.wait_for(response(scope, receive, send), timeout=1)
    assert isolated.state.agent.received == []
    rows = isolated.state.run_history.list()
    assert len(rows) == 1 and rows[0].stopped_reason == "cancelled"
    assert rows[0].finished_at is not None and not rows[0].usage_complete
