"""记忆模块测试。

覆盖三个层次：
  1. `ConversationMemory`（短期）：窗口、摘要压缩、无 LLM 时的降级
  2. `LongTermMemory`（长期）：去重、容量、相关性召回、持久化
  3. 与 Agent 的集成：**只有成功的轮次才写入记忆**

第 3 条是最重要的不变量：被预算掐断或死循环中止的轮次不是有效上下文，
写进记忆会让后续对话基于半成品推理而模型自己毫无察觉 ——
这是那种"不报错、只是越来越不对劲"的 bug。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from app.agent.factory import build_memories
from app.agent.loop import Agent
from app.agent.memory import ConversationMemory, Fact, LongTermMemory
from app.core.config import AgentSettings, MemorySettings, Settings
from app.llm.types import ChatMessage, ChatResponse, Role, StreamDelta, Usage
from app.tools.base import ToolRegistry
from app.tools.builtin import build_default_registry
from app.tools.memory_tool import RememberFactParams, RememberFactTool


class _FakeLLM:
    """可控的假 LLM：既能当摘要器，也能当对话模型。"""

    def __init__(self, *, summary: str = "用户在北京找工作。", boom: bool = False) -> None:
        self._summary = summary
        self._boom = boom
        self.summary_calls = 0

    async def chat(self, messages: Sequence[ChatMessage], **kw: Any) -> ChatResponse:
        self.summary_calls += 1
        if self._boom:
            raise RuntimeError("摘要服务不可用")
        return ChatResponse(
            message=ChatMessage(role=Role.ASSISTANT, content=self._summary),
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


# ============================================================
# 短期记忆
# ============================================================
class TestConversationMemory:
    def test_starts_empty(self) -> None:
        m = ConversationMemory()
        assert m.turns == []
        assert m.summary == ""

    async def test_single_turn_round_trip(self) -> None:
        m = ConversationMemory()
        m.add_turn("我是谁", "你是张三")
        ctx = await m.abuild_context()
        assert [str(x.role) for x in ctx] == ["user", "assistant"]
        assert ctx[0].content == "我是谁"
        assert ctx[1].content == "你是张三"

    async def test_within_window_shows_all_turns(self) -> None:
        """窗口内必须显示**全部**轮次，不能提前按 keep_recent 裁剪。

        回归：初版在 abuild_context 里对 self._turns 做了 [-keep_recent:] 切片，
        导致 max_turns=5、keep_recent=3 时放进第 4 轮就会静默丢掉第 1 轮，
        而且没有摘要补偿 —— 信息凭空消失、不报错，只是模型偶尔"不记得"。
        """
        m = ConversationMemory(max_turns=5, keep_recent=3)
        for i in range(5):
            m.add_turn(f"问题{i}", f"回答{i}")
        ctx = await m.abuild_context()
        assert m.summary == ""
        assert len(ctx) == 10  # 5 轮 × 2 条，一条都不能少
        assert ctx[0].content == "问题0"

    async def test_just_below_window_still_loses_nothing(self) -> None:
        m = ConversationMemory(max_turns=5, keep_recent=2)
        for i in range(4):  # 未触发压缩
            m.add_turn(f"q{i}", f"a{i}")
        ctx = await m.abuild_context()
        assert len(ctx) == 8
        assert "q0" in (ctx[0].content or "")

    async def test_overflow_triggers_summary_and_keeps_recent(self) -> None:
        llm = _FakeLLM(summary="用户在找北京的 Agent 岗位。")
        m = ConversationMemory(llm=llm, max_turns=4, keep_recent=2)
        for i in range(6):
            m.add_turn(f"问题{i}", f"回答{i}")

        ctx = await m.abuild_context()

        assert llm.summary_calls == 1, "超出窗口应只压缩一次，而不是每轮都压缩"
        assert m.summary == "用户在找北京的 Agent 岗位。"
        # 摘要以 system 角色承载：它在语义上是背景设定，不是某一轮对话
        assert str(ctx[0].role) == "system"
        assert "前情摘要" in (ctx[0].content or "")
        # 只保留最近 keep_recent 轮
        assert len(ctx) == 1 + 2 * 2
        assert ctx[-2].content == "问题5"

    async def test_summary_is_incremental(self) -> None:
        """第二次压缩要把已有摘要一起交给模型，而不是丢掉重来。"""
        llm = _FakeLLM(summary="累积摘要")
        m = ConversationMemory(llm=llm, max_turns=2, keep_recent=1)
        for i in range(4):
            m.add_turn(f"q{i}", f"a{i}")
        await m.abuild_context()
        for i in range(4, 8):
            m.add_turn(f"q{i}", f"a{i}")
        await m.abuild_context()
        assert llm.summary_calls >= 2

    async def test_without_llm_degrades_to_truncation_with_warning(self) -> None:
        """没有摘要模型时退化为截断，但**必须留下痕迹**。

        静默丢弃上下文会让模型行为异常却无从解释 ——
        宁可给模型一句"更早的对话已被丢弃"的提示，也不要让它以为看到了全部历史。
        """
        m = ConversationMemory(llm=None, max_turns=2, keep_recent=1)
        for i in range(4):
            m.add_turn(f"q{i}", f"a{i}")
        ctx = await m.abuild_context()
        assert "已被丢弃" in m.summary
        assert "system" in [str(x.role) for x in ctx]

    async def test_summary_failure_does_not_break_conversation(self) -> None:
        """摘要失败绝不能中断对话 —— 记忆是增强项，不是关键路径。"""
        llm = _FakeLLM(boom=True)
        m = ConversationMemory(llm=llm, max_turns=2, keep_recent=1)
        for i in range(4):
            m.add_turn(f"q{i}", f"a{i}")
        ctx = await m.abuild_context()  # 不应抛异常
        assert ctx
        assert len(m.turns) == 1

    async def test_summary_truncated_to_budget(self) -> None:
        llm = _FakeLLM(summary="很长的摘要" * 500)
        m = ConversationMemory(llm=llm, max_turns=1, keep_recent=1, max_summary_chars=100)
        m.add_turn("a", "b")
        m.add_turn("c", "d")
        await m.abuild_context()
        assert len(m.summary) <= 100

    def test_clear(self) -> None:
        m = ConversationMemory()
        m.add_turn("a", "b")
        m.clear()
        assert m.turns == []
        assert m.summary == ""

    def test_stats(self) -> None:
        m = ConversationMemory(max_turns=5, keep_recent=3)
        m.add_turn("a", "b")
        stats = m.stats()
        assert stats["turns"] == 1
        assert stats["max_turns"] == 5
        assert stats["summary_enabled"] is False  # 未传 LLM


# ============================================================
# 长期记忆
# ============================================================
class TestLongTermMemory:
    def test_remember_and_count(self) -> None:
        m = LongTermMemory()
        assert m.remember("用户的意向城市是北京")
        assert len(m) == 1

    def test_duplicate_rejected(self) -> None:
        """去重是必需的：Agent 可能反复被告知同一件事，
        重复存储会挤占召回名额，让真正有用的记忆排不进来。"""
        m = LongTermMemory()
        m.remember("用户的意向城市是北京")
        assert not m.remember("用户的意向城市是北京")
        assert not m.remember("  用户的意向城市是北京  ")  # 首尾空白不算新内容
        assert len(m) == 1

    def test_empty_rejected(self) -> None:
        m = LongTermMemory()
        assert not m.remember("   ")
        assert len(m) == 0

    def test_capacity_evicts_oldest(self) -> None:
        m = LongTermMemory(max_facts=3)
        for i in range(5):
            m.remember(f"事实{i}")
        assert len(m) == 3
        assert m.facts[0].text == "事实2"
        assert m.facts[-1].text == "事实4"

    def test_recall_ranks_by_lexical_relevance(self) -> None:
        m = LongTermMemory()
        m.remember("用户的意向城市是北京")
        m.remember("用户擅长 Kafka 与 Flink")
        m.remember("用户希望薪资 30K 以上")

        facts = m.recall("城市", k=1)
        assert facts
        assert "北京" in facts[0].text

    def test_recall_finds_latin_terms(self) -> None:
        m = LongTermMemory()
        m.remember("用户的意向城市是北京")
        m.remember("用户擅长 Kafka 与 Flink")
        facts = m.recall("Kafka", k=1)
        assert facts
        assert "Kafka" in facts[0].text

    def test_recall_limitation_cross_lingual(self) -> None:
        """**已知局限**：TF-IDF 无法做同义/跨语言匹配。

        查询『消息队列』与事实『用户擅长 Kafka』没有任何字符重叠，
        所以相似度为 0、排序退化为任意顺序（不是"找不到"，而是"找得不准"）。

        验证方式是给一个极小的分数闸门：有字符重叠的查询能过闸门，
        无重叠的过不去。这比断言"返回空列表"准确 —— 因为没有闸门时
        排名式检索**总是**会返回点东西。

        这不是 bug，是纯词法检索的固有边界。要靠语义 embedding 解决，
        与 RAG 部分的结论一致：语义能力是当前瓶颈。
        """
        m = LongTermMemory()
        m.remember("用户擅长 Kafka 与 Flink")

        # 有字符重叠 → 分数显著大于 0
        assert m.recall("Kafka", k=1, min_score=0.05)
        # 无字符重叠 → 分数为 0，过不了闸门
        assert m.recall("消息队列经验", k=1, min_score=0.05) == []

    def test_recall_empty_memory(self) -> None:
        assert LongTermMemory().recall("任何查询") == []
        assert LongTermMemory().as_context("任何查询") == ""

    def test_as_context_renders_bullets(self) -> None:
        m = LongTermMemory()
        m.remember("用户的意向城市是北京")
        ctx = m.as_context("城市", k=1)
        assert ctx.startswith("- ")

    def test_tags_preserved(self) -> None:
        m = LongTermMemory()
        m.remember("用户的意向城市是北京", tags=["求职意向"])
        assert m.facts[0].tags == ["求职意向"]

    def test_persistence_round_trip(self, tmp_path: Any) -> None:
        path = tmp_path / "facts.json"
        m1 = LongTermMemory(path=path)
        m1.remember("用户的意向城市是北京", tags=["求职意向"])
        m1.remember("用户擅长 Kafka")
        m1.save()

        m2 = LongTermMemory(path=path)
        assert m2.load() == 2
        assert m2.facts[0].text == "用户的意向城市是北京"
        assert m2.facts[0].tags == ["求职意向"]

    def test_load_missing_file_is_empty(self, tmp_path: Any) -> None:
        assert LongTermMemory(path=tmp_path / "不存在.json").load() == 0

    def test_load_corrupted_file_does_not_crash(self, tmp_path: Any) -> None:
        """记忆文件损坏不该让服务起不来 —— 从空开始，但要记日志。"""
        path = tmp_path / "facts.json"
        path.write_text("{ 这不是合法 JSON", encoding="utf-8")
        m = LongTermMemory(path=path)
        assert m.load() == 0
        assert len(m) == 0

    def test_index_invalidated_after_remember(self) -> None:
        """新增事实后必须让向量索引失效，否则新记忆检索不到。"""
        m = LongTermMemory()
        m.remember("用户擅长 Kafka")
        assert m.recall("Kafka", k=1)  # 建索引
        m.remember("用户的意向城市是北京")
        assert any("北京" in f.text for f in m.recall("城市", k=3))

    def test_save_without_path_is_noop(self) -> None:
        m = LongTermMemory(path=None)
        m.remember("x")
        m.save()  # 不应抛异常


# ============================================================
# remember_fact 工具
# ============================================================
class TestRememberFactTool:
    def test_remembers_and_persists(self, tmp_path: Any) -> None:
        m = LongTermMemory(path=tmp_path / "f.json")
        tool = RememberFactTool(m)
        result = tool.run(RememberFactParams(fact="用户的意向城市是北京"))
        assert result.ok
        assert "已记住" in result.content
        assert tmp_path.joinpath("f.json").exists(), "记录后应立即落盘"

    def test_confirmation_requested(self) -> None:
        """工具必须要求 Agent 向用户确认，否则记错了也没人纠正。"""
        tool = RememberFactTool(LongTermMemory())
        result = tool.run(RememberFactParams(fact="用户在准备面试"))
        assert "确认" in result.content

    def test_duplicate_reports_clearly(self) -> None:
        tool = RememberFactTool(LongTermMemory())
        tool.run(RememberFactParams(fact="用户的意向城市是北京"))
        result = tool.run(RememberFactParams(fact="用户的意向城市是北京"))
        # 去重命中不是失败，但要如实告知，否则模型会以为记下了新东西
        assert result.ok
        assert "已经" in result.content

    def test_schema_requires_fact(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            RememberFactParams()  # type: ignore[call-arg]

    def test_registered_only_when_memory_provided(self) -> None:
        """不提供记忆实例就不注册该工具。

        "工具存在但永远失败"比"工具不存在"更糟：模型会反复尝试调用它。
        """
        assert "remember_fact" not in build_default_registry().names()
        assert "remember_fact" in build_default_registry(long_term_memory=LongTermMemory()).names()


# ============================================================
# 装配
# ============================================================
class TestFactory:
    def test_disabled_by_default(self) -> None:
        """默认关闭：记忆的价值应该被度量而不是被假设。"""
        short, long_term = build_memories()
        assert short is None and long_term is None

    def test_enabled_creates_both(self) -> None:
        settings = Settings(memory=MemorySettings(enabled=True))
        short, long_term = build_memories(settings)
        assert short is not None
        assert long_term is not None

    def test_enabled_loads_existing_facts(self, tmp_path: Any) -> None:
        path = tmp_path / "facts.json"
        path.write_text(
            json.dumps([Fact(text="用户的意向城市是北京").model_dump()], ensure_ascii=False),
            encoding="utf-8",
        )
        settings = Settings(memory=MemorySettings(enabled=True, facts_path=str(path)))
        _, long_term = build_memories(settings)
        assert long_term is not None
        assert len(long_term) == 1

    def test_summary_disabled_when_no_llm(self) -> None:
        settings = Settings(memory=MemorySettings(enabled=True))
        short, _ = build_memories(settings, llm=None)
        assert short is not None
        assert short.stats()["summary_enabled"] is False


# ============================================================
# 与 Agent 的集成
# ============================================================
def _text_turn(text: str) -> list[StreamDelta]:
    return [StreamDelta(content=text), StreamDelta(finish_reason="stop", usage=Usage())]


def _tool_turn(name: str, args: dict[str, Any]) -> list[StreamDelta]:
    import json as _json

    raw = _json.dumps(args, ensure_ascii=False)
    return [
        StreamDelta(
            tool_call_deltas=[
                {"index": 0, "id": "c1", "function": {"name": name, "arguments": raw}}
            ]
        ),
        StreamDelta(finish_reason="tool_calls", usage=Usage()),
    ]


class _ScriptedLLM:
    def __init__(self, turns: list[list[StreamDelta]]) -> None:
        self._turns = turns
        self._i = 0
        self.received: list[list[ChatMessage]] = []

    async def stream_chat(
        self, messages: Sequence[ChatMessage], tools: Any = None, **kw: Any
    ) -> AsyncIterator[StreamDelta]:
        self.received.append(list(messages))
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for d in turn:
            yield d


def _agent(turns: list[list[StreamDelta]], **kw: Any) -> tuple[Agent, _ScriptedLLM]:
    llm = _ScriptedLLM(turns)
    registry = kw.pop("tools", None) or ToolRegistry()
    return Agent(llm, registry, AgentSettings(max_steps=3), **kw), llm  # type: ignore[arg-type]


class TestAgentIntegration:
    async def test_memory_provides_context(self) -> None:
        memory = ConversationMemory()
        memory.add_turn("上一轮问题", "上一轮回答")
        agent, llm = _agent([_text_turn("本轮回答")], memory=memory)

        await agent.run("本轮问题")

        roles = [str(m.role) for m in llm.received[0]]
        assert roles == ["system", "user", "assistant", "user"]
        assert llm.received[0][-1].content == "本轮问题"

    async def test_memory_input_still_builds_prompt_without_extra_system(self) -> None:
        """**关键**：记忆装载的是"之前"的对话，模型每次都必须被问完整问题。

        这里断言"最近一条一定是本轮输入" —— 如果实现写错成
        "把本轮也加进记忆再装配"，模型会看到重复的本轮提问。
        """
        memory = ConversationMemory()
        agent, llm = _agent([_text_turn("答案")], memory=memory)
        await agent.run("只问一次的问题")
        contents = [m.content for m in llm.received[0]]
        assert contents.count("只问一次的问题") == 1

    async def test_successful_turn_recorded(self) -> None:
        memory = ConversationMemory()
        agent, _ = _agent([_text_turn("答案")], memory=memory)
        await agent.run("问题")
        assert len(memory.turns) == 1
        assert memory.turns[0].user == "问题"
        assert memory.turns[0].assistant == "答案"

    @pytest.mark.parametrize(
        ("turns", "reason"),
        [
            # 参数刻意各不相同：若用同一参数，死循环护栏会先生效，
            # 测到的就不是"步数耗尽"这条路径了
            (
                [_tool_turn("calculator", {"expression": f"{i}+1"}) for i in range(6)],
                "max_steps",
            ),
            ([_text_turn("ok")], "finished"),
        ],
    )
    async def test_only_successful_turns_recorded(
        self, turns: list[list[StreamDelta]], reason: str
    ) -> None:
        """被预算掐断的轮次**不写入记忆**。

        它们不是有效上下文，写进去会让后续对话基于半成品推理，
        而模型自己毫无察觉 —— 属于"不报错、只是越来越不对劲"的 bug。
        """
        memory = ConversationMemory()
        registry = build_default_registry()
        agent, _ = _agent(turns, memory=memory, tools=registry)
        result = await agent.run("问题")

        assert result.stopped_reason == reason
        if reason == "finished":
            assert len(memory.turns) == 1
        else:
            assert len(memory.turns) == 0

    async def test_loop_detected_turn_not_recorded(self) -> None:
        same = _tool_turn("calculator", {"expression": "1+1"})
        memory = ConversationMemory()
        agent, _ = _agent([same] * 5, memory=memory, tools=build_default_registry())
        result = await agent.run("问题")
        assert result.stopped_reason == "loop_detected"
        assert len(memory.turns) == 0

    async def test_error_turn_not_recorded(self) -> None:
        class _BoomLLM:
            async def stream_chat(self, *a: Any, **kw: Any) -> AsyncIterator[StreamDelta]:
                raise RuntimeError("模型不可用")
                yield  # pragma: no cover

        memory = ConversationMemory()
        agent = Agent(
            _BoomLLM(),  # type: ignore[arg-type]
            ToolRegistry(),
            AgentSettings(),
            memory=memory,
        )
        result = await agent.run("问题")
        assert result.stopped_reason == "error"
        assert len(memory.turns) == 0

    async def test_long_term_recalled_into_system_message(self) -> None:
        long_term = LongTermMemory()
        long_term.remember("用户的意向城市是北京")
        agent, llm = _agent([_text_turn("好的")], long_term=long_term)

        await agent.run("帮我看看北京的机会")

        messages = llm.received[0]
        # 第 0 条是角色设定提示词，长期记忆紧随其后（它是背景，不是对话内容）
        assert str(messages[0].role) == "system"
        assert str(messages[1].role) == "system"
        assert "长期记忆" in (messages[1].content or "")
        assert "北京" in (messages[1].content or "")

    async def test_no_recall_no_extra_message(self) -> None:
        """没有命中记忆时不该塞一条空消息 —— 那只会浪费 token。"""
        long_term = LongTermMemory()  # 空记忆
        agent, llm = _agent([_text_turn("好的")], long_term=long_term)
        await agent.run("问题")
        assert len(llm.received[0]) == 2  # system + user

    async def test_no_memory_keeps_stateless_behaviour(self) -> None:
        """不传记忆时必须保持 P1 的无状态行为（历史由调用方传入）。"""
        agent, llm = _agent([_text_turn("ok")])
        history = [ChatMessage.user("旧问题"), ChatMessage.assistant("旧回答")]
        await agent.run("新问题", history)

        roles = [str(m.role) for m in llm.received[0]]
        assert roles == ["system", "user", "assistant", "user"]

    async def test_history_ignored_when_memory_active(self) -> None:
        """两者同时提供时以记忆为准，避免历史被重复注入。"""
        memory = ConversationMemory()
        memory.add_turn("记忆中的问题", "记忆中的回答")
        agent, llm = _agent([_text_turn("ok")], memory=memory)

        await agent.run("新问题", [ChatMessage.user("外部历史")])

        contents = [m.content for m in llm.received[0]]
        assert "记忆中的问题" in contents
        assert "外部历史" not in contents
