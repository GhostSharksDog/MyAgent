"""桌面持久化的反例：重启、取消、并发、删除与重复摘要。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.agent.approvals import ApprovalBroker
from app.agent.memory import ConversationMemory, Turn
from app.agent.runtime import RunContext, _current
from app.agent.sqlite_memory import SqliteLongTermMemory
from app.core.config import AgentSettings, SessionSettings
from app.llm.types import ChatMessage, ChatResponse, ToolCall, Usage
from app.session.sqlite_store import SqlSessionStore
from app.session.store import InMemorySessionStore
from app.tools.base import ToolRegistry
from app.tools.memory_tool import RememberFactTool


async def test_cancelled_summary_keeps_processed_position_without_retry():
    class Delayed:
        calls = 0

        async def chat(self, *_args, **_kwargs):
            self.calls += 1
            started.set()
            await asyncio.Event().wait()

    started = asyncio.Event()
    llm = Delayed()
    turns = [Turn(user=f"问题{i}", assistant="公开答复") for i in range(4)]
    memory = ConversationMemory.from_turns(turns, llm=llm, max_turns=3, keep_recent=2)
    task = asyncio.create_task(memory.abuild_context())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    restored = ConversationMemory.from_turns(
        turns, llm=llm, max_turns=3, keep_recent=2, summary_state=memory.summary_state
    )
    messages = await restored.abuild_context()
    assert llm.calls == 1
    assert restored.summary_state["processed"] == 2
    assert "尚未完成" in messages[0].content


async def test_memory_edits_delete_and_disable_change_actual_model_messages(tmp_path):
    from app.agent.loop import Agent
    from app.llm.types import StreamDelta

    class Capture:
        def __init__(self):
            self.received = []

        async def stream_chat(self, messages, **_kwargs):
            self.received.append([m.model_copy(deep=True) for m in messages])
            yield StreamDelta(content="公开回复", finish_reason="stop", usage=Usage(total_tokens=1))

    memory = SqliteLongTermMemory(tmp_path / "facts.db")
    memory.remember("用户喜欢简短中文", ["语言"])
    fact = memory.facts[0]
    llm = Capture()
    agent = Agent(llm, ToolRegistry(), AgentSettings(_env_file=None), long_term=memory)
    await agent.run("中文回复")
    assert "用户喜欢简短中文" in str(llm.received[-1])
    memory.update_fact(fact.id, "用户喜欢详细中文", ["语言"])
    await agent.run("中文回复")
    assert "用户喜欢详细中文" in str(llm.received[-1])
    assert "用户喜欢简短中文" not in str(llm.received[-1])
    memory.enabled = False
    await agent.run("中文回复")
    assert "用户喜欢详细中文" not in str(llm.received[-1])
    memory.enabled = True
    memory.delete_fact(fact.id)
    await agent.run("中文回复")
    assert "用户喜欢详细中文" not in str(llm.received[-1])


def test_failed_database_write_keeps_committed_cache(tmp_path, monkeypatch):
    import sqlite3

    memory = SqliteLongTermMemory(tmp_path / "facts.db")
    memory.remember("已确认的事实")

    def denied():
        raise sqlite3.OperationalError("合成磁盘不可写")

    monkeypatch.setattr(memory, "_connect", denied)
    with pytest.raises(RuntimeError, match="检查目录权限"):
        memory.remember("不能保存的事实")
    assert [f.text for f in memory.facts] == ["已确认的事实"]
    assert [f.text for f in SqliteLongTermMemory(memory.path).facts] == ["已确认的事实"]


async def test_model_rebuild_preserves_memory_resource(tmp_path):
    from app.agent.factory import build_agent_stack
    from app.core.config import MemorySettings, Settings

    memory = SqliteLongTermMemory(tmp_path / "facts.db")
    memory.remember("用户偏好中文")
    settings = Settings(
        _env_file=None,
        memory=MemorySettings(
            _env_file=None, enabled=True, backend="sql", facts_path=str(memory.path)
        ),
    )
    stack = build_agent_stack(settings, long_term=memory)
    try:
        assert stack.long_term is memory
        assert stack.tools.get("remember_fact")._memory is memory
        memory.delete_fact(memory.facts[0].id)
        # 清理旧模型不得重新保存、复活已删除的缓存。
        memory.save()
        assert SqliteLongTermMemory(memory.path).facts == []
    finally:
        await stack.llm.aclose()


def test_sqlite_fact_commits_and_disabled_retains(tmp_path):
    path = tmp_path / "记忆 文件.db"
    memory = SqliteLongTermMemory(path, max_facts=2)
    memory.remember("用户偏好中文", ["语言"])
    restored = SqliteLongTermMemory(path)
    fact = restored.facts[0]
    assert fact.text == "用户偏好中文"
    assert restored.update_fact(fact.id, "用户偏好简短中文", ["语言"])
    memory.load()
    assert memory.facts[0].text == "用户偏好简短中文"
    memory.enabled = False
    assert memory.as_context("中文") == ""
    with pytest.raises(ValueError, match="关闭"):
        memory.remember("不应保存")
    assert SqliteLongTermMemory(path).facts
    assert restored.delete_fact(fact.id)
    restored.save()
    assert SqliteLongTermMemory(path).facts == []


def test_parallel_sqlite_writers_and_capacity(tmp_path):
    path = tmp_path / "facts.db"
    memories = [SqliteLongTermMemory(path, max_facts=200) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda i: memories[i % 4].remember(f"确认的偏好 {i}"), range(40)))
    memory = SqliteLongTermMemory(path, max_facts=3)
    assert len(memory) == 40
    memory.remember("新偏好")
    assert len(SqliteLongTermMemory(path)) == 3


def test_corrupt_database_never_becomes_empty_memory(tmp_path):
    path = tmp_path / "broken.db"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(RuntimeError, match="不会退回内存"):
        SqliteLongTermMemory(path)
    assert path.read_bytes() == b"not a sqlite database"


def test_explicit_json_migration_backs_up_and_keeps_original(tmp_path):
    import importlib.util
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "scripts" / "migrate_memory.py"
    spec = importlib.util.spec_from_file_location("desktop_memory_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    source, destination = tmp_path / "old.json", tmp_path / "facts.db"
    original = json.dumps([{"text": "旧偏好", "tags": ["偏好"], "ts": 1}], ensure_ascii=False)
    source.write_text(original, encoding="utf-8")
    existing = SqliteLongTermMemory(destination)
    existing.remember("新偏好")
    report = migration.migrate(source, destination)
    assert source.read_text(encoding="utf-8") == original
    assert Path(report["source_backup"]).read_text(encoding="utf-8") == original
    assert len(list(tmp_path.glob("facts.db.backup-*"))) == 1
    assert report["imported"] == 1 and report["retained"] == 2
    assert [f.text for f in SqliteLongTermMemory(destination).facts] == ["旧偏好", "新偏好"]
    assert migration.migrate(source, destination)["imported"] == 0


@pytest.mark.parametrize("decision", ["approve", "reject", "cancel"])
async def test_memory_requires_actual_approval_and_persists_immediately(tmp_path, decision):
    memory = SqliteLongTermMemory(tmp_path / "facts.db")
    registry = ToolRegistry()
    registry.register(RememberFactTool(memory))
    context = RunContext.create(AgentSettings(_env_file=None))
    context.approvals = ApprovalBroker(3)
    token = _current.set(context)
    try:
        task = asyncio.create_task(
            registry.execute(
                ToolCall(id="m1", name="remember_fact", arguments={"fact": "用户偏好中文"})
            )
        )
        event = await asyncio.wait_for(context.approvals.events.get(), 1)
        assert event.approval["kind"] == "memory"
        assert SqliteLongTermMemory(memory.path).facts == []
        if decision == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            context.approvals.decide(event.approval["id"], decision)
            result = await task
            assert result.ok == (decision == "approve")
        assert bool(SqliteLongTermMemory(memory.path).facts) == (decision == "approve")
    finally:
        context.approvals.close()
        _current.reset(token)


class SummaryLLM:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("合成摘要失败")
        return ChatResponse(
            message=ChatMessage.assistant("较早的事实摘要"), usage=Usage(total_tokens=7)
        )


@pytest.mark.parametrize("fail", [False, True])
async def test_summary_restores_only_new_turns_and_accounts_usage(fail):
    turns = [Turn(user=f"公开问题{i}", assistant=f"答案{i}") for i in range(4)]
    llm = SummaryLLM(fail)
    context = RunContext.create(AgentSettings(_env_file=None))
    token = _current.set(context)
    try:
        memory = ConversationMemory.from_turns(turns, llm=llm, max_turns=3, keep_recent=2)
        await memory.abuild_context()
        state = memory.summary_state
        assert state["processed"] == 2
        assert context.context_trimmed
        assert context.usage.total_tokens == (0 if fail else 7)
        restored = ConversationMemory.from_turns(
            turns, llm=llm, max_turns=3, keep_recent=2, summary_state=state
        )
        messages = await restored.abuild_context()
        assert len(llm.calls) == 1
        assert "公开问题0" not in str(messages)
        assert "公开问题3" in str(messages)
        assert "摘要" in messages[0].content
        # 新增历史超过剩余窗口才再次摘要，不会重新发送已处理前缀。
        newer = [
            *turns,
            Turn(user="新增1", assistant="回复1"),
            Turn(user="新增2", assistant="回复2"),
        ]
        restored = ConversationMemory.from_turns(
            newer, llm=llm, max_turns=3, keep_recent=2, summary_state=state
        )
        await restored.abuild_context()
        assert len(llm.calls) == 2
        assert "公开问题0" not in str(llm.calls[-1])
        assert "公开问题2" in str(llm.calls[-1])
    finally:
        _current.reset(token)


@pytest.mark.parametrize("sql", [False, True])
async def test_zero_ttl_summary_atomic_and_deleted_session_not_revived(tmp_path, sql):
    store = (
        SqlSessionStore.from_url(f"sqlite+aiosqlite:///{tmp_path / 'session.db'}", ttl_seconds=0)
        if sql
        else InMemorySessionStore(ttl_seconds=0)
    )
    try:
        session = await store.create()
        await store.append_turn(session.id, "问题", "回答")
        assert await store.merge_summary(session.id, {"summary": "摘要", "processed": 0})
        assert await store.merge_execution_facts(session.id, [])
        current = await store.get(session.id)
        assert current.meta["conversation_summary"]["summary"] == "摘要"
        assert current.turns[0].user == "问题"
        await store.delete(session.id)
        assert not await store.merge_summary(session.id, {"summary": "不能复活"})
        assert await store.get(session.id) is None
    finally:
        await store.aclose()
    assert SessionSettings(ttl_seconds=0, _env_file=None).ttl_seconds == 0
