"""上下文预算与工具摘要的**接线**测试（技术债 T09 / T07）。

【与 test_context_budget.py 的分工】

那个文件测的是纯逻辑：估算器准不准、裁剪顺序对不对、摘要怎么生成。
这个文件测的是**接线**：这些东西在 Agent 循环里真的被调用了吗？

两者必须都测。本项目已经有过教训 —— 单元逻辑全绿而功能没接上
（"注释里的意图不会自动变成实现"）。所以这里用假模型（FakeLLM）
真跑一遍循环，断言：

  · 超预算时模型**收到**的消息确实变短了，而且丢的是最早的轮次；
  · 工具摘要真的出现在**下一轮**的历史里（T07 的全部意义在此）；
  · 摘要随 DONE 事件下发，HTTP 层才有机会把它持久化。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.agent.context import summarize_tools
from app.agent.events import AgentEvent, EventType
from app.agent.loop import Agent
from app.agent.memory import ConversationMemory
from app.core.config import AgentSettings
from app.llm.tokens import count_messages_tokens
from app.llm.types import ChatMessage, StreamDelta, Usage
from app.tools.base import ToolRegistry
from app.tools.builtin import build_default_registry


def text_turn(text: str, *, prompt_tokens: int = 100) -> list[StreamDelta]:
    return [
        StreamDelta(content=text),
        StreamDelta(
            finish_reason="stop",
            usage=Usage(prompt_tokens=prompt_tokens, completion_tokens=10, total_tokens=110),
        ),
    ]


def tool_turn(name: str, arguments: str, call_id: str = "call_1") -> list[StreamDelta]:
    raw = arguments
    return [
        StreamDelta(tool_call_deltas=[{"index": 0, "id": call_id, "function": {"name": name}}]),
        StreamDelta(
            tool_call_deltas=[{"index": 0, "function": {"arguments": raw}}],
        ),
        StreamDelta(
            finish_reason="tool_calls",
            usage=Usage(prompt_tokens=80, completion_tokens=20, total_tokens=100),
        ),
    ]


class RecordingLLM:
    """记录每次调用收到的消息 —— 断言"模型实际看到了什么"的唯一办法。"""

    def __init__(self, turns: list[list[StreamDelta]]) -> None:
        self._turns = turns
        self._idx = 0
        self.received: list[list[ChatMessage]] = []

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[dict[str, Any]] | None = None,
        **_kw: Any,
    ) -> AsyncIterator[StreamDelta]:
        self.received.append(list(messages))
        turn = self._turns[self._idx] if self._idx < len(self._turns) else text_turn("（兜底）")
        self._idx += 1
        for delta in turn:
            yield delta


async def drain(agent: Agent, user_input: str, **kwargs: Any) -> list[AgentEvent]:
    return [event async for event in agent.run_stream(user_input, **kwargs)]


def make_agent(
    turns: list[list[StreamDelta]],
    settings: AgentSettings,
    *,
    system_prompt: str | None = None,
) -> tuple[Agent, RecordingLLM]:
    """造一个假模型驱动的 Agent。

    `system_prompt` 可以显式传一个**很小的**：预算测试要算的是"历史被裁了多少"，
    而真实的系统提示有近千 token（带工具说明），把它算进来会让断言依赖
    提示词的当前长度 —— 那种测试会在某次改提示词之后莫名其妙地红。
    """
    llm = RecordingLLM(turns)
    agent = Agent(
        llm,  # type: ignore[arg-type]
        build_default_registry(),
        settings,
        system_prompt=system_prompt,
    )
    return agent, llm


class TestBudgetIsWiredIntoTheLoop:
    async def test_long_history_is_trimmed_before_the_model_sees_it(self) -> None:
        """超预算时，模型收到的消息必须真的变短 —— 而且是丢最早的轮次。

        这条测试的价值在于"接线"：`ContextBudget` 自己测得再全，
        如果循环里没调用它，功能等于不存在（而且不会有任何报错）。
        """
        agent, llm = make_agent(
            [text_turn("答")],
            AgentSettings(context_token_budget=2000, max_steps=2),
            system_prompt="你是助手。",
        )
        history = [ChatMessage.user(f"很早以前的问题{i}" + "细节" * 200) for i in range(4)]

        await drain(agent, "当前的问题", history=history)

        seen = llm.received[0]
        assert count_messages_tokens(seen) <= 2000, f"发出去的消息没有被裁到预算内：{seen}"
        assert seen[-1].content == "当前的问题", "当前的问题必须在最后"
        joined = "".join(m.content or "" for m in seen)
        assert "很早以前的问题0" not in joined, "最早的轮次没有被丢掉"
        assert "当前的问题" in joined

    async def test_within_budget_is_untouched(self) -> None:
        """没超预算时一个字节都不该动 —— 裁剪必须只在前者触发。"""
        agent, llm = make_agent(
            [text_turn("答")],
            AgentSettings(context_token_budget=100_000, max_steps=2),
        )
        history = [ChatMessage.user("问题"), ChatMessage.assistant("回答")]
        await drain(agent, "当前的问题", history=history)
        seen = llm.received[0]
        assert [m.content for m in seen][-3:] == ["问题", "回答", "当前的问题"]

    async def test_disabled_budget_keeps_everything(self) -> None:
        """预算为 0（不限制）时，超长历史也必须原样发出。"""
        agent, llm = make_agent([text_turn("答")], AgentSettings(context_token_budget=0))
        history = [ChatMessage.user("很长" * 2000)]
        await drain(agent, "问题", history=history)
        assert any((m.content or "").startswith("很长") for m in llm.received[0])


class TestToolSummaryIsWired:
    async def test_done_event_carries_the_summary(self) -> None:
        """摘要随 DONE 下发 —— HTTP 层就是从这里取的（否则无从持久化）。"""
        agent, _ = make_agent(
            [
                tool_turn("calculator", '{"expression": "2+2"}'),
                text_turn("等于 4"),
            ],
            AgentSettings(max_steps=3),
        )
        events = await drain(agent, "算一下 2+2")
        done = next(e for e in events if e.type is EventType.DONE)
        assert "calculator" in done.tool_summary
        assert done.stopped_reason == "finished"

    async def test_summary_reaches_the_next_turn_history(self) -> None:
        """**T07 的全部意义**：下一轮的历史里要有"我查过什么"。

        没有它，模型每轮都会把同一个工具再查一遍 —— 慢、费 token，
        而用户看到的是"它怎么又问了一遍同样的事"。
        """
        memory = ConversationMemory(llm=None)  # 只要窗口，不要摘要压缩
        memory.add_turn(
            "上一轮的问题",
            "上一轮的回答",
            tool_summary=summarize_tools([{"name": "search_knowledge", "ok": True, "chars": 1200}]),
        )
        llm = RecordingLLM([text_turn("答")])
        agent = Agent(  # type: ignore[arg-type]
            llm, build_default_registry(), AgentSettings(max_steps=2), memory=memory
        )

        await drain(agent, "这一轮的问题")

        seen = llm.received[0]
        assistant_messages = [m for m in seen if m.role.value == "assistant"]
        assert assistant_messages, "历史里应当有上一轮的助手消息"
        last_assistant = assistant_messages[-1].content or ""
        assert "上一轮的回答" in last_assistant
        assert "search_knowledge" in last_assistant, "工具摘要没有进入历史"

    async def test_turns_without_tools_have_no_noise(self) -> None:
        """没调用工具的轮次不该多出一行空摘要 —— 历史里不该有噪音。"""
        memory = ConversationMemory(llm=None)
        memory.add_turn("问题", "回答")
        llm = RecordingLLM([text_turn("答")])
        agent = Agent(  # type: ignore[arg-type]
            llm, build_default_registry(), AgentSettings(max_steps=2), memory=memory
        )
        await drain(agent, "下一个问题")
        assistant = [m for m in llm.received[0] if m.role.value == "assistant"][-1]
        assert assistant.content == "回答", "空摘要不该改动历史消息"


class TestEstimateCalibration:
    async def test_estimate_and_actual_are_both_recorded(self) -> None:
        """估算与实际都要进指标 —— 偏差倍数靠这两个累加计数器相除得到。

        断言的是"计数器的值真的涨了"：只调用不记录等于没测。
        """
        from app.core.telemetry import METRICS

        before_est = METRICS.counter("legacy_prompt_tokens_estimated_total")
        before_act = METRICS.counter("legacy_prompt_tokens_actual_total")

        agent, _ = make_agent([text_turn("答", prompt_tokens=137)], AgentSettings(max_steps=2))
        await drain(agent, "问题")

        assert METRICS.counter("legacy_prompt_tokens_estimated_total") > before_est
        assert METRICS.counter("legacy_prompt_tokens_actual_total") == before_act + 137


class TestRegistryIsNotBroken:
    def test_tools_registry_still_works(self) -> None:
        """顺带确认默认工具表没被这轮改动影响（它是上面所有测试的依赖）。"""
        registry: ToolRegistry = build_default_registry()
        assert "calculator" in registry.names()
