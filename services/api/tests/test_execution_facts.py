"""工具实际执行与会话终态分别记录；全部使用合成模型与临时目录。"""

import asyncio
import json

import httpx
import pytest
from app.agent.memory import ConversationMemory, Turn
from app.agent.operations import merge_facts
from app.agent.runtime import RunContext, _current
from app.core.config import AgentSettings, get_settings
from app.llm.types import ChatMessage, ChatResponse, ToolCall, Usage
from app.session.gate import SessionGate
from app.session.sqlite_store import SqlSessionStore
from app.session.store import InMemorySessionStore, RedisSessionStore
from app.tools.base import ToolRegistry
from app.tools.files import WriteFileTool
from fakeredis.aioredis import FakeRedis

from tests.test_file_approvals import application
from tests.test_session_api import ScriptedLLM, text_turn, tool_turn


async def test_compression_receives_tool_summary():
    received = []

    class Summary:
        async def chat(self, messages, **kwargs):
            received.extend(messages)
            return ChatResponse(message=ChatMessage.assistant("摘要"), usage=Usage())

    memory = ConversationMemory.from_turns(
        [
            Turn(user="写文件", assistant="完成", tool_summary="write_file 已执行"),
            Turn(user="继续", assistant="好"),
        ],
        llm=Summary(),
        max_turns=1,
        keep_recent=1,
    )
    await memory.abuild_context()
    assert "write_file 已执行" in received[0].content
    assert "求职咨询" not in received[0].content


@pytest.fixture(params=["memory", "sql", "redis"])
async def store(request, tmp_path):
    redis = None
    if request.param == "sql":
        value = SqlSessionStore.from_url(
            f"sqlite+aiosqlite:///{(tmp_path / 'facts.db').as_posix()}"
        )
    elif request.param == "redis":
        redis = FakeRedis()
        value = RedisSessionStore(redis)
    else:
        value = InMemorySessionStore()
    yield value
    await value.aclose()
    if redis:
        await redis.aclose()


async def test_atomic_merge_keeps_turns_and_other_metadata(store):
    session = await store.create()
    session.meta = {"other": "preserved"}
    await store.save(session)
    await asyncio.gather(
        *[
            fn
            for i in range(20)
            for fn in (
                store.append_turn(session.id, str(i), "answer"),
                store.merge_execution_facts(
                    session.id, [{"id": str(i), "tool": "write_file", "status": "succeeded"}]
                ),
            )
        ]
    )
    loaded = await store.get(session.id)
    assert len(loaded.turns) == 20
    assert len(loaded.meta["execution_facts"]) == 20
    assert loaded.meta["other"] == "preserved"
    await store.delete(session.id)
    assert not await store.merge_execution_facts(session.id, [{"id": "late", "tool": "write_file"}])
    assert await store.get(session.id) is None


def test_fact_limits_dedup_and_no_raw_parameters():
    values = [
        {"id": str(i), "tool": "write_file", "content": "PRIVATE", "arguments": {"key": "SECRET"}}
        for i in range(130)
    ]
    merged = merge_facts({"other": True}, [*values, values[-1]])
    assert len(merged["execution_facts"]) == 100
    assert "PRIVATE" not in json.dumps(merged) and "SECRET" not in json.dumps(merged)
    assert merged["other"] is True


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        get_settings(),
        "agent",
        AgentSettings(
            _env_file=None,
            workspace_root=str(tmp_path),
            file_write_enabled=True,
            file_approval_required=False,
        ),
    )
    return tmp_path


async def test_write_records_fact_before_any_loop_result_event(workspace):
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    context = RunContext.create(get_settings().agent)
    token = _current.set(context)
    try:
        result = await registry.execute(
            ToolCall(
                id="c", name="write_file", arguments={"path": "public.txt", "content": "public"}
            )
        )
    finally:
        _current.reset(token)
    assert result.ok and (workspace / "public.txt").read_text() == "public"
    assert context.tool_trace == []  # 此时尚无 Agent 的 TOOL_RESULT。
    assert context.execution_facts[0]["target"] == "public.txt"
    assert context.execution_facts[0]["status"] == "succeeded"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fail_after_write", [False, True])
async def test_followup_receives_execution_even_if_answer_failed(
    workspace, stream, fail_after_write, monkeypatch
):
    from sse_starlette.sse import AppStatus

    # SSE 3.x uses per-loop events; legacy 2.x needs explicit reset.
    if hasattr(AppStatus, "should_exit_event"):
        monkeypatch.setattr(AppStatus, "should_exit_event", None)

    class LLM(ScriptedLLM):
        async def stream_chat(self, messages, **kwargs):
            if fail_after_write and self._i == 1:
                self._i += 1
                raise RuntimeError("synthetic model failure")
            async for delta in super().stream_chat(messages, **kwargs):
                yield delta

    llm = LLM(
        [
            tool_turn("write_file", {"path": "saved.txt", "content": "public"}),
            text_turn("已完成"),
            text_turn("看到了记录"),
        ]
    )
    app = application(llm=llm)
    app.state.long_term = None
    session = await app.state.sessions.create()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        endpoint = "/api/chat/stream" if stream else "/api/chat"
        response = await client.post(
            endpoint, json={"message": "保存文件", "session_id": session.id}
        )
        assert response.status_code == 200
        saved = await app.state.sessions.get(session.id)
        assert saved.meta["execution_facts"][0]["status"] == "succeeded"
        assert len(saved.turns) == (0 if fail_after_write else 1)
        await client.post(endpoint, json={"message": "之前执行了吗", "session_id": session.id})
    received = "\n".join(m.content or "" for m in llm.received[-1])
    assert "saved.txt" in received and "succeeded" in received


async def test_gate_waits_for_cleanup_but_not_other_sessions():
    gate = SessionGate()
    first = await gate.acquire("same")
    waiting = asyncio.create_task(gate.acquire("same"))
    other = await gate.acquire("other")
    await asyncio.sleep(0)
    assert not waiting.done()
    first.release()
    second = await waiting
    second.release()
    first.release()  # 收尾与 background 可重复释放同一 lease。
    other.release()


async def test_stop_then_immediate_followup_waits_for_fact_save(workspace):
    from app.api.routes import chat_stream
    from app.api.schemas import ChatRequest
    from starlette.requests import Request

    executing, saving, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class LLM(ScriptedLLM):
        async def stream_chat(self, messages, **kwargs):
            if self._i == 1:
                self._i += 1
                executing.set()
                await asyncio.Event().wait()
            async for delta in super().stream_chat(messages, **kwargs):
                yield delta

    class SlowStore(InMemorySessionStore):
        async def merge_execution_facts(self, session_id, facts):
            saving.set()
            await release.wait()
            return await super().merge_execution_facts(session_id, facts)

    llm = LLM(
        [
            tool_turn("write_file", {"path": "saved.txt", "content": "public"}),
            text_turn("never"),
            text_turn("已看到执行记录"),
        ]
    )
    app = application(llm=llm)
    app.state.long_term = None
    app.state.sessions = SlowStore()
    session = await app.state.sessions.create()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/stream",
        "headers": [],
        "app": app,
        "client": ("127.0.0.1", 1234),
    }
    response = await chat_stream(
        ChatRequest(message="写公开样本", session_id=session.id), Request(scope)
    )

    async def receive():
        await executing.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    first = asyncio.create_task(response(scope, receive, send))
    await asyncio.wait_for(saving.wait(), 3)
    assert (workspace / "saved.txt").read_text() == "public"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        followup = asyncio.create_task(
            api.post("/api/chat", json={"message": "刚才执行了吗", "session_id": session.id})
        )
        await asyncio.sleep(0.03)
        assert not followup.done() and len(llm.received) == 1
        release.set()
        await asyncio.wait_for(first, 3)
        assert (await asyncio.wait_for(followup, 3)).status_code == 200
    assert "saved.txt" in "\n".join(m.content or "" for m in llm.received[-1])
    saved = await app.state.sessions.get(session.id)
    assert len(saved.turns) == 1
    assert saved.meta["execution_facts"][0]["status"] == "succeeded"
