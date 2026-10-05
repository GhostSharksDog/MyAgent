"""Agent 循环测试 —— 用**假 LLM** 驱动，不花一分钱 token。

【这是本项目最值得学的一个测试技巧】
Agent 的行为由"模型在每一步返回什么"决定。把模型换成脚本化的假实现，
就能精确控制每一步的输入，从而覆盖真实模型极难复现的路径：

  - 模型卡住反复调同一个工具（死循环保护）
  - 模型一直要工具不给答案（步数预算耗尽）
  - 工具返回失败后模型如何应对
  - 多步工具调用的完整事件序列

真实 API 测试（标了 @pytest.mark.live）只用来验证"协议对接是否正确"，
不承担逻辑覆盖的责任 —— 那样既慢又不可靠（模型有随机性）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from app.agent.events import AgentRunResult, EventType
from app.agent.loop import Agent
from app.agent.prompts import SYSTEM_PROMPT, build_system_prompt
from app.core.config import AgentSettings, get_settings
from app.llm.types import ChatMessage, StreamDelta, ToolCall, Usage
from app.tools.base import Tool, ToolRegistry, ToolResult
from app.tools.builtin import CalculatorParams, build_default_registry
from app.tools.errors import ToolError
from pydantic import BaseModel


# ============================================================
# 假 LLM：按剧本逐次返回预设响应
# ============================================================
def text_turn(text: str, tokens: int = 10) -> list[StreamDelta]:
    """一次"直接回答"的流式响应。"""
    return [
        StreamDelta(content=text),
        StreamDelta(
            finish_reason="stop",
            usage=Usage(prompt_tokens=tokens, completion_tokens=tokens, total_tokens=tokens * 2),
        ),
    ]


def tool_turn(name: str, args: dict[str, Any], call_id: str = "call_1") -> list[StreamDelta]:
    """一次"请求调用工具"的流式响应，且参数故意拆成多个分片以贴近真实。"""
    import json

    raw = json.dumps(args, ensure_ascii=False)
    mid = max(1, len(raw) // 2)
    return [
        StreamDelta(
            tool_call_deltas=[
                {
                    "index": 0,
                    "id": call_id,
                    "function": {"name": name, "arguments": raw[:mid]},
                }
            ]
        ),
        StreamDelta(tool_call_deltas=[{"index": 0, "function": {"arguments": raw[mid:]}}]),
        StreamDelta(
            finish_reason="tool_calls",
            usage=Usage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
        ),
    ]


class FakeLLM:
    """按剧本返回的假客户端。记录收到的消息，便于断言上下文是否正确拼装。"""

    def __init__(self, turns: list[list[StreamDelta]]) -> None:
        self._turns = turns
        self._idx = 0
        self.received: list[list[ChatMessage]] = []
        self.received_tools: list[Any] = []

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[dict[str, Any]] | None = None,
        **_kw: Any,
    ) -> AsyncIterator[StreamDelta]:
        self.received.append(list(messages))
        self.received_tools.append(tools)
        if self._idx >= len(self._turns):
            # 剧本用完了就给个兜底回答，避免测试因为 IndexError 假失败
            for d in text_turn("（剧本已耗尽）"):
                yield d
            return
        turn = self._turns[self._idx]
        self._idx += 1
        for delta in turn:
            yield delta


def make_agent(
    turns: list[list[StreamDelta]],
    *,
    tools: ToolRegistry | None = None,
    max_steps: int = 12,
    loop_guard: int = 3,
    agent_settings: AgentSettings | None = None,
) -> tuple[Agent, FakeLLM]:
    fake = FakeLLM(turns)
    agent = Agent(
        fake,  # type: ignore[arg-type] - duck typing，正是依赖注入的好处
        tools or build_default_registry(),
        # 显式传入的 settings 会**真的生效**（包括 profile）。
        # 这一点值得单独写明：我第一版让 Agent 读全局单例，
        # 于是这个参数在 profile 上静默失效 —— 参数看起来能控制行为、
        # 实际不能，而且不报错。**参数既然存在就必须真的起作用。**
        agent_settings or AgentSettings(max_steps=max_steps, loop_guard=loop_guard),
    )
    return agent, fake


async def collect(
    agent: Agent, question: str, history: list[ChatMessage] | None = None
) -> list[Any]:
    return [e async for e in agent.run_stream(question, history)]


# ============================================================
# 场景 1：直接回答（不调工具）
# ============================================================
class TestDirectAnswer:
    async def test_single_step_finishes(self) -> None:
        agent, _ = make_agent([text_turn("你好，我是 Legacy。")])
        events = await collect(agent, "你好")

        types = [e.type for e in events]
        assert types[0] == EventType.START
        assert EventType.STEP in types
        assert types[-1] == EventType.DONE
        assert EventType.TOOL_CALL not in types

        final = next(e for e in events if e.type == EventType.FINAL)
        assert final.content == "你好，我是 Legacy。"

    async def test_usage_aggregated(self) -> None:
        agent, _ = make_agent([text_turn("答案", tokens=30)])
        events = await collect(agent, "问")
        done = events[-1]
        assert done.usage is not None
        assert done.usage.total_tokens == 60
        assert done.steps_used == 1

    async def test_system_prompt_is_first_message(self) -> None:
        """第一条消息必须是系统提示词，且是**当前 profile 解析出来的那一份**。

        【为什么期望值要现算而不是抄一段文案】
        提示词有两个动态来源：profile（决定通用/求职口径）与**实际注册的工具**
        （`build_system_prompt` 会把引用了未注册工具的规则裁掉）。
        把某一份文案复制进测试，等于把断言换成"这段文字没被改过"——
        改提示词本身是正常迭代，而"提示词没被送进去"才是 bug。

        前置条件：`Agent` 按 `get_settings().agent.profile` 解析，而这里的用例
        跑在默认配置上，所以期望的是**通用**那一份。
        """
        tools = build_default_registry()
        agent, fake = make_agent([text_turn("ok")], tools=tools)

        await collect(agent, "hi")
        first = fake.received[0][0]
        assert first.role == "system"

        assert get_settings().agent.profile == "general"
        assert first.content == build_system_prompt("general", set(tools.names()))
        # 默认形态不再是那份"求职顾问"提示词 —— 这正是本次改动的要害，
        # 所以除了"等于通用提示词"，还要显式排除"又变回求职提示词"。
        assert first.content != SYSTEM_PROMPT

    async def test_jobhunt_profile_keeps_job_prompt(self) -> None:
        """切回 jobhunt 时仍然拿得到求职提示词（只是不再默认加载）。

        与上一条配对：一条守住"默认不是求职形态"，一条守住
        "选了求职形态就真的回到求职形态"。只有前者会退化成功能被删掉。

        【为什么这里是**传参**而不是 monkeypatch 全局单例】
        我最初（经子代理）写的是 monkeypatch `get_settings().agent` ——
        那能通过，是因为当时 `Agent.__init__` 读的是**全局单例**而不是
        它自己收到的 `settings` 参数。也就是说：那个 `settings` 参数
        在 profile 上是**静默失效**的，而这个测试恰好掩盖了它。

        改成显式传 `AgentSettings(profile=...)` 之后，这条测试真正验证的是
        "profile 可注入" —— 顺带把上面那个陷阱也钉住了：
        如果有人把 `__init__` 改回读全局单例，这条会立刻红。
        评测里要对比两种 profile 时，也正是靠这个可注入性。
        """
        tools = build_default_registry(profile="jobhunt")
        agent, fake = make_agent(
            [text_turn("ok")],
            tools=tools,
            agent_settings=AgentSettings(profile="jobhunt"),
        )
        await collect(agent, "hi")

        assert fake.received[0][0].content == build_system_prompt("jobhunt", set(tools.names()))

    async def test_history_is_included_before_current_input(self) -> None:
        """多轮上下文：历史必须在当轮用户输入之前。"""
        agent, fake = make_agent([text_turn("ok")])
        history = [ChatMessage.user("上一轮问题"), ChatMessage.assistant("上一轮回答")]
        await collect(agent, "本轮问题", history)

        roles = [str(m.role) for m in fake.received[0]]
        assert roles == ["system", "user", "assistant", "user"]
        assert fake.received[0][-1].content == "本轮问题"

    async def test_tools_are_passed_to_model(self) -> None:
        """传给模型的 tools 必须**就是注册表里的那些**。

        【为什么不再写死五个工具名】
        断言"模型收到的工具集合 == 注册表里的工具集合"才是这条用例要守的契约：
        它同时能抓住"注册了却忘了传"和"传了没注册的"两类接线错误。
        写死名单会把这条接缝测试变成"默认能力集的第二个副本" ——
        能力集一变就要改两处，而其中一处改了另一处漏改时**没人会发现**。

        默认能力集本身由 test_tools.py / test_api.py 守住，这里只补一句
        核心三件套必须在场，避免"注册表恰好是空的"也让上面那条通过。
        """
        tools = build_default_registry()
        agent, fake = make_agent([text_turn("ok")], tools=tools)

        await collect(agent, "hi")
        assert fake.received_tools[0] is not None
        names = {s["function"]["name"] for s in fake.received_tools[0]}
        assert names == set(tools.names())
        assert {"calculator", "get_current_time", "search_knowledge"} <= names

    async def test_globally_empty_tools_sends_none(self) -> None:
        """没有工具时不应传 tools 字段（部分服务端对空数组报 400）。"""
        agent, fake = make_agent([text_turn("ok")], tools=ToolRegistry())
        await collect(agent, "hi")
        assert fake.received_tools[0] is None


# ============================================================
# 场景 2：单步工具调用
# ============================================================
class TestSingleToolCall:
    async def test_full_event_sequence(self) -> None:
        """验证 UI 依赖的完整事件序列。

        第一步模型只发起工具调用（无文本），所以没有 TOKEN；
        第二步模型给出答案，产生 TOKEN + FINAL。
        """
        agent, _ = make_agent(
            [
                tool_turn("calculator", {"expression": "1234*5678"}, call_id="c1"),
                text_turn("计算结果是 7006652。"),
            ]
        )
        events = await collect(agent, "1234 乘以 5678 是多少")

        assert [e.type for e in events] == [
            EventType.START,
            EventType.STEP,
            EventType.TOOL_CALL,
            EventType.TOOL_RESULT,
            EventType.STEP,
            EventType.TOKEN,
            EventType.FINAL,
            EventType.DONE,
        ]

    async def test_tool_call_and_result_payload(self) -> None:
        agent, _ = make_agent(
            [
                tool_turn("calculator", {"expression": "1234*5678"}, call_id="c1"),
                text_turn("答案是 7006652"),
            ]
        )
        events = await collect(agent, "算一下")

        call = next(e for e in events if e.type == EventType.TOOL_CALL)
        assert call.tool_name == "calculator"
        assert call.tool_args == {"expression": "1234*5678"}
        assert call.step == 1

        result = next(e for e in events if e.type == EventType.TOOL_RESULT)
        assert result.tool_ok is True
        assert "7006652" in result.content
        assert result.duration_ms is not None

    async def test_tool_result_fed_back_as_tool_message(self) -> None:
        """最关键的一步：工具结果必须以 role=tool + tool_call_id 回灌。"""
        agent, fake = make_agent(
            [
                tool_turn("calculator", {"expression": "2+2"}, call_id="c42"),
                text_turn("4"),
            ]
        )
        await collect(agent, "算")

        second_call_messages = fake.received[1]
        roles = [str(m.role) for m in second_call_messages]
        # system, user, assistant(带 tool_calls), tool(结果)
        assert roles == ["system", "user", "assistant", "tool"]

        assistant_msg = second_call_messages[2]
        assert assistant_msg.tool_calls is not None
        assert assistant_msg.tool_calls[0].id == "c42"

        tool_msg = second_call_messages[3]
        assert tool_msg.tool_call_id == "c42"  # 必须与请求配对，否则服务端 400
        assert "4" in (tool_msg.content or "")

    async def test_steps_counted_correctly(self) -> None:
        agent, _ = make_agent([tool_turn("calculator", {"expression": "1+1"}), text_turn("2")])
        events = await collect(agent, "算")
        assert events[-1].steps_used == 2


# ============================================================
# 场景 3：多步工具调用
# ============================================================
class TestMultiStep:
    async def test_three_tools_in_sequence(self) -> None:
        agent, _ = make_agent(
            [
                tool_turn("read_resume", {}, call_id="c1"),
                tool_turn("search_jobs", {"keyword": "Agent"}, call_id="c2"),
                text_turn("综合分析如下……"),
            ]
        )
        events = await collect(agent, "帮我看看简历和 Agent 岗位的匹配度")

        called = [e.tool_name for e in events if e.type == EventType.TOOL_CALL]
        assert called == ["read_resume", "search_jobs"]
        assert events[-1].steps_used == 3
        assert next(e for e in events if e.type == EventType.FINAL).content.startswith("综合分析")

    async def test_run_wrapper_matches_stream(self) -> None:
        """run() 是流式的封装，两者结果必须一致（单一事实来源）。"""
        agent, _ = make_agent([tool_turn("calculator", {"expression": "10/4"}), text_turn("2.5")])
        result: AgentRunResult = await agent.run("算")
        assert result.answer == "2.5"
        assert result.steps_used == 2
        assert result.stopped_reason == "finished"
        assert result.error is None
        assert result.usage.total_tokens == 90  # tool_turn 70 + text_turn 20
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "calculator"


# ============================================================
# 场景 4：护栏 —— 步数预算与死循环
# ============================================================
class TestGuardrails:
    async def test_max_steps_stops_runaway_agent(self) -> None:
        """模型无限要工具 => 必须在 max_steps 处停住并报错。"""
        agent, fake = make_agent(
            [tool_turn("calculator", {"expression": f"{i}+1"}, call_id=f"c{i}") for i in range(20)],
            max_steps=3,
        )
        events = await collect(agent, "无限循环")

        assert events[-1].type == EventType.DONE
        assert events[-1].steps_used == 3
        error = next(e for e in events if e.type == EventType.ERROR)
        assert "最大步数" in error.content
        assert len(fake.received) == 3  # 只调了 3 次模型，没有多烧一次

    async def test_loop_detection_aborts_identical_calls(self) -> None:
        """模型反复调同一个工具且参数完全相同 => 判定卡死，提前中止。"""
        same = tool_turn("calculator", {"expression": "1+1"})
        agent, fake = make_agent([same, same, same, same, same], loop_guard=3)
        events = await collect(agent, "卡住")

        error = next((e for e in events if e.type == EventType.ERROR), None)
        assert error is not None
        assert "重复调用" in error.content
        # 第 3 次时就该发现，不应该跑满 max_steps
        assert events[-1].steps_used == 3
        assert len(fake.received) == 3

    async def test_different_args_not_flagged_as_loop(self) -> None:
        """参数不同就是正常的多步推理，不能误杀。"""
        agent, _ = make_agent(
            [
                tool_turn("calculator", {"expression": "1+1"}, call_id="a"),
                tool_turn("calculator", {"expression": "2+2"}, call_id="b"),
                tool_turn("calculator", {"expression": "3+3"}, call_id="c"),
                text_turn("做完了"),
            ],
            loop_guard=3,
        )
        events = await collect(agent, "逐步算")
        assert not any(e.type == EventType.ERROR for e in events)
        assert next(e for e in events if e.type == EventType.FINAL).content == "做完了"


# ============================================================
# 场景 5：工具失败时 Agent 的行为
# ============================================================
class FailingTool(Tool):
    """一个总是失败的工具，用来验证错误回灌。"""

    name = "always_fails"
    description = "永远失败"
    params_model = CalculatorParams  # 复用即可

    async def run(self, params: BaseModel) -> ToolResult:
        raise ToolError("数据源不可用")


class TestToolFailure:
    async def test_failure_is_fed_back_not_raised(self) -> None:
        registry = ToolRegistry()
        registry.register(FailingTool())  # type: ignore[arg-type]

        agent, fake = make_agent(
            [
                tool_turn("always_fails", {"expression": "1"}, call_id="c1"),
                text_turn("抱歉，数据源暂时不可用，我无法完成这个查询。"),
            ],
            tools=registry,
        )
        events = await collect(agent, "查一下")

        result = next(e for e in events if e.type == EventType.TOOL_RESULT)
        assert result.tool_ok is False
        assert "数据源不可用" in result.content

        # 错误必须作为 observation 回灌，模型才能换策略
        tool_msg = fake.received[1][-1]
        assert str(tool_msg.role) == "tool"
        assert "数据源不可用" in (tool_msg.content or "")
        assert "工具执行失败" in (tool_msg.content or "")

    async def test_failure_does_not_break_the_run(self) -> None:
        registry = ToolRegistry()
        registry.register(FailingTool())  # type: ignore[arg-type]
        agent, _ = make_agent(
            [
                tool_turn("always_fails", {"expression": "1"}),
                text_turn("换个方式回答你。"),
            ],
            tools=registry,
        )
        result = await agent.run("查")
        assert result.stopped_reason == "finished"
        assert result.answer == "换个方式回答你。"


# ============================================================
# 场景 6：模型调用本身失败
# ============================================================
class ExplodingLLM:
    async def stream_chat(self, *_a: Any, **_kw: Any) -> AsyncIterator[StreamDelta]:
        raise RuntimeError("连接被重置")
        yield  # pragma: no cover - 让它是 async generator


class TestLLMFailure:
    async def test_llm_error_yields_error_event(self) -> None:
        agent = Agent(
            ExplodingLLM(),  # type: ignore[arg-type]
            build_default_registry(),
            AgentSettings(),
        )
        events = await collect(agent, "hi")
        assert any(e.type == EventType.ERROR for e in events)
        assert events[-1].type == EventType.DONE
        error = next(e for e in events if e.type == EventType.ERROR)
        assert "连接被重置" in error.content

    async def test_run_reports_error_reason(self) -> None:
        agent = Agent(
            ExplodingLLM(),  # type: ignore[arg-type]
            build_default_registry(),
            AgentSettings(),
        )
        result = await agent.run("hi")
        assert result.stopped_reason == "error"
        assert result.error is not None


# ============================================================
# 场景 7：空回复兜底
# ============================================================
class TestEmptyResponse:
    async def test_empty_model_output_gives_placeholder(self) -> None:
        """模型返回空内容既不报错也没答案 —— 必须给用户可理解的信息。"""
        agent, _ = make_agent([[StreamDelta(finish_reason="stop")]])
        events = await collect(agent, "hi")
        final = next(e for e in events if e.type == EventType.FINAL)
        assert "空回复" in final.content


# ============================================================
# 场景 8：终止原因必须是准确的（回归测试）
# ============================================================
class TestStoppedReason:
    """四个终止原因都必须可区分。

    曾经的 bug：`AgentRunResult` 声明了 finished/max_steps/loop_detected/error
    四个取值，但实现里 max_steps 与 loop_detected 都只发 ERROR 事件，
    `run()` 一律归为 "error"，导致**后两个取值永远不可达**。
    后果是按 stopped_reason 做指标统计时，"正常的预算终止"被算成"故障"。
    """

    async def test_finished(self) -> None:
        agent, _ = make_agent([text_turn("答案")])
        assert (await agent.run("问")).stopped_reason == "finished"

    async def test_max_steps(self) -> None:
        turns = [
            tool_turn("calculator", {"expression": f"{i}+1"}, call_id=f"c{i}") for i in range(10)
        ]
        agent, _ = make_agent(turns, max_steps=3)
        result = await agent.run("无限循环")
        assert result.stopped_reason == "max_steps"
        assert result.error is not None  # 仍然要给出可读的失败说明

    async def test_loop_detected(self) -> None:
        same = tool_turn("calculator", {"expression": "1+1"})
        agent, _ = make_agent([same, same, same, same], loop_guard=3)
        result = await agent.run("卡住")
        assert result.stopped_reason == "loop_detected"

    async def test_error(self) -> None:
        agent = Agent(
            ExplodingLLM(),  # type: ignore[arg-type]
            build_default_registry(),
            AgentSettings(),
        )
        assert (await agent.run("hi")).stopped_reason == "error"

    async def test_done_event_carries_reason(self) -> None:
        """事件层也要带原因，前端才能区分"做完了"和"被预算掐断"。"""
        turns = [
            tool_turn("calculator", {"expression": f"{i}+1"}, call_id=f"c{i}") for i in range(10)
        ]
        agent, _ = make_agent(turns, max_steps=2)
        events = await collect(agent, "x")
        assert events[-1].type == EventType.DONE
        assert events[-1].stopped_reason == "max_steps"


# ============================================================
# 场景 9：死循环指纹的正确性（回归测试）
# ============================================================
class TestCallSignature:
    """指纹必须"参数相同才相同"。

    曾经的 bug：只哈希已解析的 `arguments`。模型吐出非法 JSON 时它会退化成
    空 dict，于是**参数完全不同的非法调用拿到同一个指纹**，被误判成死循环而
    提前中止 —— 本该让模型自我修正的场景，反而变成硬失败。
    """

    def test_malformed_args_get_distinct_signatures(self) -> None:
        from app.agent.loop import _call_signature

        c1 = ToolCall.from_wire(
            {"id": "1", "function": {"name": "calculator", "arguments": "{'expr': broken1"}}
        )
        c2 = ToolCall.from_wire(
            {"id": "2", "function": {"name": "calculator", "arguments": "{'expr': broken2"}}
        )
        # 两者的 arguments 都解析失败、都是空 dict
        assert c1.arguments == {} and c2.arguments == {}
        # 但指纹必须不同，否则会被误判成重复调用
        assert _call_signature(c1) != _call_signature(c2)

    def test_key_order_does_not_change_signature(self) -> None:
        from app.agent.loop import _call_signature

        a = ToolCall.from_wire(
            {"id": "1", "function": {"name": "f", "arguments": '{"a": 1, "b": 2}'}}
        )
        b = ToolCall.from_wire(
            {"id": "2", "function": {"name": "f", "arguments": '{"b": 2, "a": 1}'}}
        )
        # 键顺序不同但语义相同，必须视为同一次调用
        assert _call_signature(a) == _call_signature(b)

    def test_different_values_differ(self) -> None:
        from app.agent.loop import _call_signature

        a = ToolCall.from_wire({"id": "1", "function": {"name": "f", "arguments": '{"a": 1}'}})
        b = ToolCall.from_wire({"id": "2", "function": {"name": "f", "arguments": '{"a": 2}'}})
        assert _call_signature(a) != _call_signature(b)

    async def test_malformed_args_do_not_falsely_trigger_loop_guard(self) -> None:
        """三次参数不同的非法调用不应被当成死循环，模型应有机会自我修正。"""
        bad_turns = [
            [
                StreamDelta(
                    tool_call_deltas=[
                        {
                            "index": 0,
                            "id": f"c{i}",
                            "function": {"name": "calculator", "arguments": f"{{'expr': broken{i}"},
                        }
                    ]
                ),
                StreamDelta(finish_reason="tool_calls"),
            ]
            for i in range(3)
        ]
        agent, _ = make_agent([*bad_turns, text_turn("我换个写法。")], loop_guard=3)
        result = await agent.run("算")
        assert result.stopped_reason == "finished"
        assert result.answer == "我换个写法。"


# ============================================================
# 场景 10：预算耗尽的最后一步不应再执行工具（回归测试）
# ============================================================
class TestLastStepEconomy:
    async def test_tools_not_executed_on_final_step(self) -> None:
        """第 max_steps 步的工具调用结果永远无法被模型消费，执行它纯属浪费。

        读大文件、调外部 API 的工具可能有真实成本，所以这一步必须省掉。
        """
        counted = {"n": 0}

        class CountingTool(Tool):
            name = "counter"
            description = "计数"
            params_model = CalculatorParams

            async def run(self, params: BaseModel) -> ToolResult:
                counted["n"] += 1
                return ToolResult.success("ok")

        registry = ToolRegistry()
        registry.register(CountingTool())

        turns = [tool_turn("counter", {"expression": f"{i}+1"}, call_id=f"c{i}") for i in range(10)]
        agent, _ = make_agent(turns, tools=registry, max_steps=3)
        result = await agent.run("x")

        assert result.stopped_reason == "max_steps"
        # 前两步的工具被执行（结果能被消费），第三步不执行
        assert counted["n"] == 2

    async def test_final_step_still_calls_model(self) -> None:
        """省钱不能省到"少调一次模型"——模型必须有机会给出最终答案。"""
        agent, fake = make_agent(
            [tool_turn("calculator", {"expression": "1+1"}), text_turn("2")],
            max_steps=2,
        )
        result = await agent.run("x")
        assert result.stopped_reason == "finished"
        assert len(fake.received) == 2


# ============================================================
# 场景 11：同一回合里的多个工具调用（并发执行）
# ============================================================
class _NoParams(BaseModel):
    """空参数模型 —— 这几个工具只关心"什么时候开始、睡多久"。"""


def multi_tool_turn(calls: list[tuple[str, dict[str, Any]]]) -> list[StreamDelta]:
    """一次请求**多个**工具调用的响应（一个 delta 里带多个 index）。

    这也顺带覆盖了 T13 修过的那条路径：一个 chunk 携带多个分片时不能只取 [0]。
    """
    import json

    deltas = [
        {
            "index": i,
            "id": f"call_{i}",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        for i, (name, args) in enumerate(calls)
    ]
    return [
        StreamDelta(tool_call_deltas=deltas),
        StreamDelta(
            finish_reason="tool_calls",
            usage=Usage(prompt_tokens=40, completion_tokens=10, total_tokens=50),
        ),
    ]


class _SleepTool(Tool):
    """睡一会儿的工具：用来把"并发/串行"从时序上量出来。

    它是这个测试文件里唯一带真实等待的工具 —— 因为并发与否**只能从耗时观测**，
    断言"结果对"是查不出串行的（串行也会得到正确结果，只是慢）。
    """

    params_model = _NoParams

    def __init__(
        self, name: str, delay: float, *, serial: bool = False, boom: bool = False
    ) -> None:
        self.name = name
        self.description = f"sleep {delay}s"
        self.timeout = 5.0
        self.serial = serial
        self.delay = delay
        self.boom = boom
        self.started: list[float] = []

    async def run(self, params: BaseModel) -> ToolResult:
        self.started.append(asyncio.get_running_loop().time())
        await asyncio.sleep(self.delay)
        if self.boom:
            raise ToolError("这个工具故意炸了")
        return ToolResult.success(f"{self.name} 完成")


def _sleep_registry(
    delay: float = 0.25, *, serial: bool = False, boom: str | None = None
) -> tuple[ToolRegistry, dict[str, _SleepTool]]:
    registry = ToolRegistry()
    made: dict[str, _SleepTool] = {}
    for name in ("alpha", "beta", "gamma"):
        tool = _SleepTool(name, delay, serial=serial, boom=(name == boom))
        made[name] = tool
        registry.register(tool)
    return registry, made


async def _timed_run(agent: Agent, question: str) -> tuple[list[Any], float]:
    started = asyncio.get_running_loop().time()
    events = await collect(agent, question)
    return events, asyncio.get_running_loop().time() - started


class TestConcurrentTools:
    async def test_independent_tools_overlap(self) -> None:
        """三个互不依赖的工具应当**重叠**执行，而不是排队。

        串行是 3×0.25s；并发是 ~0.25s。阈值取中间的 0.6s：
        既不会因为机器慢而误报，也不会把"真的没并发"放过去。
        """
        registry, _ = _sleep_registry(delay=0.25)
        agent, _ = make_agent(
            [
                multi_tool_turn([("alpha", {}), ("beta", {}), ("gamma", {})]),
                text_turn("都好了"),
            ],
            tools=registry,
        )
        events, elapsed = await _timed_run(agent, "一起做三件事")

        assert elapsed < 0.6, f"三个 0.25s 的工具花了 {elapsed:.2f}s —— 看起来还是串行"
        results = [e for e in events if e.type == EventType.TOOL_RESULT]
        assert len(results) == 3

    async def test_results_keep_the_model_order(self) -> None:
        """结果必须按**模型给出的顺序**回灌，而不是完成顺序。

        让第一个工具最慢：如果实现按完成顺序返回，alpha 就会跑到最后。
        顺序不是装饰 —— 它决定回灌给模型的上下文长什么样，
        而并发不该让"同样的输入产生不同的上下文"。
        """
        registry = ToolRegistry()
        registry.register(_SleepTool("alpha", 0.30))
        registry.register(_SleepTool("beta", 0.05))
        registry.register(_SleepTool("gamma", 0.05))
        agent, fake = make_agent(
            [
                multi_tool_turn([("alpha", {}), ("beta", {}), ("gamma", {})]),
                text_turn("好了"),
            ],
            tools=registry,
        )
        events = await collect(agent, "x")
        names = [e.tool_name for e in events if e.type == EventType.TOOL_RESULT]
        assert names == ["alpha", "beta", "gamma"]

        # 回灌给模型的 tool 消息同样按这个顺序（每一次工具结果都是一条 role=tool）
        tool_messages = [m for m in fake.received[1] if m.role == "tool"]
        assert [m.name for m in tool_messages] == ["alpha", "beta", "gamma"]

    async def test_serial_tool_makes_the_whole_turn_serial(self) -> None:
        """只要有一个调用命中 `serial` 工具，整批就退回串行。

        混合策略（只读并发、写入串行）需要调度保证，这个循环给不出；
        与其写一个"大部分情况下正确"的聪明策略，不如写一个显然正确的笨策略。
        这里三个都是 serial，所以应当量到 3×0.25s。
        """
        registry, _ = _sleep_registry(delay=0.2, serial=True)
        agent, _ = make_agent(
            [
                multi_tool_turn([("alpha", {}), ("beta", {}), ("gamma", {})]),
                text_turn("好了"),
            ],
            tools=registry,
        )
        _, elapsed = await _timed_run(agent, "x")
        assert elapsed >= 0.55, f"声明了 serial 却只有 {elapsed:.2f}s —— 没有被串行化"

    async def test_concurrency_limit_is_respected(self) -> None:
        """`tool_concurrency=1` 应当把并发完全关掉（这是那个配置项的意义）。"""
        registry, _ = _sleep_registry(delay=0.2)
        agent, _ = make_agent(
            [
                multi_tool_turn([("alpha", {}), ("beta", {}), ("gamma", {})]),
                text_turn("好了"),
            ],
            tools=registry,
            agent_settings=AgentSettings(max_steps=4, tool_concurrency=1),
        )
        _, elapsed = await _timed_run(agent, "x")
        assert elapsed >= 0.55, f"上限设为 1 却只花了 {elapsed:.2f}s"

    async def test_one_failing_tool_does_not_cancel_the_others(self) -> None:
        """一个工具炸了，同批的其他工具必须照常完成。

        【为什么这条在并发下才真的需要】
        串行时异常是顺序传播的；并发之后 gather 遇到异常会**取消同批的其他调用**，
        于是一个工具的小毛病会让本来能成功的调用一起失败 —— 失败的传播范围被放大了。
        """
        registry, _ = _sleep_registry(delay=0.05, boom="beta")
        agent, _ = make_agent(
            [
                multi_tool_turn([("alpha", {}), ("beta", {}), ("gamma", {})]),
                text_turn("好了"),
            ],
            tools=registry,
        )
        events = await collect(agent, "x")
        by_name = {e.tool_name: e for e in events if e.type == EventType.TOOL_RESULT}

        assert by_name["beta"].tool_ok is False
        assert "故意炸了" in by_name["beta"].content
        assert by_name["alpha"].tool_ok is True
        assert by_name["gamma"].tool_ok is True
        # 失败被如实回灌给模型之后，本轮照常走到最终答案
        assert events[-1].type == EventType.DONE
        assert events[-1].stopped_reason == "finished"

    async def test_unexpected_exception_in_execute_is_contained(self) -> None:
        """连"工具层之上"的异常（注册表/工具对象本身的 bug）也只能影响自己。

        `Tool.execute` 已经处理了校验失败、超时、工具内部异常；
        这一条测的是它之外的那一层 —— 并发路径上必须有这层兜底，
        否则一个 bug 会连带取消整批调用。
        """
        registry, _ = _sleep_registry(delay=0.05)
        original = registry.execute

        async def flaky(call: ToolCall) -> ToolResult:
            if call.name == "beta":
                raise RuntimeError("注册表层面的意外错误")
            return await original(call)

        registry.execute = flaky  # type: ignore[method-assign]
        agent, _ = make_agent(
            [
                multi_tool_turn([("alpha", {}), ("beta", {}), ("gamma", {})]),
                text_turn("好了"),
            ],
            tools=registry,
        )
        events = await collect(agent, "x")
        by_name = {e.tool_name: e for e in events if e.type == EventType.TOOL_RESULT}

        assert by_name["beta"].tool_ok is False
        assert "未预期错误" in by_name["beta"].content
        assert by_name["alpha"].tool_ok is True and by_name["gamma"].tool_ok is True


# ============================================================
# 场景 12：终态不变量（每个出口都必须发且只发一个 done）
# ============================================================
class TestTerminalInvariant:
    """不管从哪条路结束，事件流都要以**恰好一个** `done` 收尾。

    【为什么它值得单独立一组测试】
    原来四个终止分支各自手写"先 error 再 done"，这个约定只存在于写代码的人
    的脑子里。漏发 done 的后果是前端**一直转圈等一个永远不会来的终态**，
    而且不会有任何报错 —— 它只是在等。这类"没有错误信息的错误"最难发现，
    所以要用不变量把它钉住，而不是靠每个分支各写一遍。
    """

    async def test_direct_answer(self) -> None:
        agent, _ = make_agent([text_turn("直接回答")])
        events = await collect(agent, "你好")
        self._assert_single_terminal(events, "finished")

    async def test_max_steps(self) -> None:
        agent, _ = make_agent(
            [tool_turn("calculator", {"expression": "1+1"}, call_id=f"c{i}") for i in range(6)],
            max_steps=2,
        )
        events = await collect(agent, "算")
        self._assert_single_terminal(events, "max_steps", expect_error=True)

    async def test_loop_detected(self) -> None:
        turns = [tool_turn("calculator", {"expression": "1+1"}, call_id=f"c{i}") for i in range(5)]
        agent, _ = make_agent(turns, max_steps=6, loop_guard=3)
        events = await collect(agent, "算")
        self._assert_single_terminal(events, "loop_detected", expect_error=True)

    async def test_model_failure(self) -> None:
        class ExplodingLLM(FakeLLM):
            async def stream_chat(self, *a: Any, **kw: Any) -> AsyncIterator[StreamDelta]:
                raise RuntimeError("模型炸了")
                yield StreamDelta()  # pragma: no cover - 让它是异步生成器

        agent = Agent(ExplodingLLM([]), build_default_registry(), AgentSettings())  # type: ignore[arg-type]
        events = await collect(agent, "你好")
        self._assert_single_terminal(events, "error", expect_error=True)

    @staticmethod
    def _assert_single_terminal(
        events: list[Any], reason: str, *, expect_error: bool = False
    ) -> None:
        types = [e.type for e in events]
        dones = [e for e in events if e.type == EventType.DONE]
        assert len(dones) == 1, f"done 事件应当恰好一个，实际 {len(dones)} 个"
        assert types[-1] == EventType.DONE, "事件流必须以 done 收尾"
        assert dones[0].stopped_reason == reason

        errors = [e for e in events if e.type == EventType.ERROR]
        if expect_error:
            assert errors, f"终止原因 {reason} 必须附带一条 error 说明"
            assert types.index(EventType.ERROR) < types.index(EventType.DONE), "error 要先于 done"
        else:
            assert not errors


@pytest.mark.live
class TestRealAPI:
    """真实 API 冒烟测试：只验证协议对接，不验证业务逻辑。

    需要 .env 中配置真实密钥，用 `pytest -m live` 显式运行。
    """

    async def test_one_real_call(self) -> None:
        from app.core.config import get_settings
        from app.llm.client import LLMClient

        settings = get_settings()
        if not settings.llm.is_configured:
            pytest.skip("未配置 LLM_API_KEY")

        async with LLMClient(settings.llm) as client:
            resp = await client.chat([ChatMessage.user("只回复两个字：收到")])
            assert resp.message.content
            assert resp.usage.total_tokens > 0
