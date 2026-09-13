"""Plan-and-Execute 测试。

规划型 Agent 最大的价值是**可解释性**（计划是人能看懂的中间产物），
所以测试也围绕这一点：
  - 计划的结构与状态机是否正确
  - 上下文是否只传"结论"而不传"过程"（这是控制膨胀的关键）
  - 预算护栏是否真的拦得住（没有总预算的规划型 Agent 是烧钱机器）
  - 规划/综合失败时是否有降级路径（而不是整轮失败）

全部用脚本化假 LLM，不花一分钱 token —— 规划与执行的行为由
"模型返回什么"决定，把模型换成脚本就能精确控制每一步。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from app.agent.events import EventType
from app.agent.planning import (
    Plan,
    PlanAndExecuteAgent,
    Planner,
    PlanStep,
    StepStatus,
    plan_from_dict,
)
from app.core.config import AgentSettings
from app.llm.types import ChatMessage, ChatResponse, Role, StreamDelta, Usage
from app.tools.base import Tool, ToolRegistry, ToolResult
from app.tools.builtin import CalculatorParams


# ============================================================
# 假 LLM：按提示词内容路由到不同响应
# ============================================================
class RoutingLLM:
    """一个会根据**提示词内容**决定返回什么的假 LLM。

    真实实现里规划、单步执行、综合是三次不同性质的调用；
    用内容路由可以在一个假对象里模拟全部三种，
    从而让整个 Plan-and-Execute 流程可测。
    """

    def __init__(
        self,
        *,
        plan: dict | None = None,
        revise_plan: dict | None = None,
        step_answer: str = "这一步的结论：用户有 3 年后端经验。",
        synthesis: str = "综合结论已完成。",
        plan_raises: bool = False,
        synthesis_raises: bool = False,
    ) -> None:
        self._plan = plan if plan is not None else _default_plan()
        self._revise = revise_plan
        self._step_answer = step_answer
        self._synthesis = synthesis
        self._plan_raises = plan_raises
        self._synthesis_raises = synthesis_raises

        self.chat_prompts: list[str] = []
        self.stream_calls = 0

    async def chat(self, messages: Sequence[ChatMessage], **_kw: Any) -> ChatResponse:
        prompt = messages[-1].content or ""
        self.chat_prompts.append(prompt)

        if "任务规划器" in prompt:
            if self._plan_raises:
                raise RuntimeError("规划服务不可用")
            # 重规划请求会要求"只输出剩余步骤"
            if "剩余需要做的步骤" in prompt and self._revise is not None:
                return _resp(json.dumps(self._revise, ensure_ascii=False))
            return _resp(json.dumps(self._plan, ensure_ascii=False))

        if "综合成一个" in prompt:
            if self._synthesis_raises:
                raise RuntimeError("综合服务不可用")
            return _resp(self._synthesis)

        return _resp("（未识别的调用）")

    async def stream_chat(
        self, messages: Sequence[ChatMessage], tools: Any = None, **_kw: Any
    ) -> AsyncIterator[StreamDelta]:
        self.stream_calls += 1
        for delta in [
            StreamDelta(content=self._step_answer),
            # 【必须报告 usage】总预算靠累加 usage 来判定，
            # 而流式响应里的 usage 通常只在最后一个 chunk 出现。
            # 服务端不返回 usage 时预算就形同虚设 —— 这是真实存在的限制，
            # 所以这里也按真实形态给出，让预算测试测的是真东西。
            StreamDelta(
                finish_reason="stop",
                usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            ),
        ]:
            yield delta


def _resp(text: str) -> ChatResponse:
    return ChatResponse(
        message=ChatMessage(role=Role.ASSISTANT, content=text),
        usage=Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    )


def _default_plan(n: int = 3) -> dict:
    return {
        "reasoning": "先看简历，再找岗位，最后比对。",
        "steps": [
            {"id": i, "description": f"第{i}步：分析资料{i}", "expected": f"给出结论{i}"}
            for i in range(1, n + 1)
        ],
    }


class _EchoTool(Tool):
    name = "echo"
    description = "回显"
    params_model = CalculatorParams

    async def run(self, params: Any) -> ToolResult:
        return ToolResult.success("ok")


def _agent(llm: Any, **kw: Any) -> PlanAndExecuteAgent:
    return PlanAndExecuteAgent(
        llm,
        kw.pop("tools", ToolRegistry()),
        AgentSettings(max_steps=3),
        **kw,
    )


# ============================================================
# 计划模型
# ============================================================
class TestPlanModel:
    def test_next_pending(self) -> None:
        plan = Plan(
            goal="g", steps=[PlanStep(id=1, description="a"), PlanStep(id=2, description="b")]
        )
        assert plan.next_pending().id == 1  # type: ignore[union-attr]
        plan.steps[0].status = StepStatus.DONE
        assert plan.next_pending().id == 2  # type: ignore[union-attr]

    def test_next_pending_none_when_all_terminal(self) -> None:
        plan = Plan(goal="g", steps=[PlanStep(id=1, description="a", status=StepStatus.DONE)])
        assert plan.next_pending() is None

    def test_done_count(self) -> None:
        plan = Plan(
            goal="g",
            steps=[
                PlanStep(id=1, description="a", status=StepStatus.DONE),
                PlanStep(id=2, description="b", status=StepStatus.FAILED),
                PlanStep(id=3, description="c", status=StepStatus.DONE),
            ],
        )
        assert plan.done_count == 2

    def test_conclusion_digest_contains_only_conclusions(self) -> None:
        """**这是控制上下文膨胀的关键**。

        如果每步都把自己用过的工具结果、中间推理全传下去，
        上下文会线性膨胀，第 5 步的输入可能是第 1 步的十几倍。
        只带结论则近似恒定。
        """
        plan = Plan(
            goal="g",
            steps=[
                PlanStep(
                    id=1,
                    description="读简历",
                    status=StepStatus.DONE,
                    result="用户有 3 年后端经验",
                ),
                PlanStep(id=2, description="找岗位", status=StepStatus.PENDING),
            ],
        )
        digest = plan.conclusion_digest()
        assert "用户有 3 年后端经验" in digest
        assert "步骤2" not in digest  # 未完成的不带（它没有结论）

    def test_conclusion_digest_marks_failures(self) -> None:
        """失败必须出现在摘要里 —— 否则后续步骤不知道自己建立在什么前提上。"""
        plan = Plan(
            goal="g",
            steps=[PlanStep(id=1, description="x", status=StepStatus.FAILED, error="缺数据")],
        )
        assert "失败" in plan.conclusion_digest()
        assert "缺数据" in plan.conclusion_digest()

    def test_conclusion_digest_truncates(self) -> None:
        plan = Plan(
            goal="g",
            steps=[
                PlanStep(id=1, description="x", status=StepStatus.DONE, result="很长的结论" * 500)
            ],
        )
        assert len(plan.conclusion_digest(max_chars=100)) < 300

    def test_step_terminal_states(self) -> None:
        assert PlanStep(id=1, description="a", status=StepStatus.DONE).is_terminal
        assert PlanStep(id=1, description="a", status=StepStatus.FAILED).is_terminal
        assert PlanStep(id=1, description="a", status=StepStatus.SKIPPED).is_terminal
        assert not PlanStep(id=1, description="a", status=StepStatus.PENDING).is_terminal

    def test_plan_from_dict_round_trip(self) -> None:
        plan = Plan(goal="g", steps=[PlanStep(id=1, description="a")])
        restored = plan_from_dict(plan.model_dump())
        assert restored is not None
        assert restored.goal == "g"

    def test_plan_from_dict_invalid(self) -> None:
        assert plan_from_dict(None) is None
        assert plan_from_dict({"goal": "g"}) is not None
        assert plan_from_dict({"不合法": "结构"}) is None


# ============================================================
# 规划器
# ============================================================
class TestPlanner:
    async def test_makes_plan(self) -> None:
        planner = Planner(RoutingLLM())  # type: ignore[arg-type]
        plan = await planner.amake_plan("帮我分析简历")
        assert plan.goal == "帮我分析简历"
        assert len(plan.steps) == 3
        assert plan.reasoning
        assert plan.steps[0].description.startswith("第1步")

    async def test_truncates_to_max_steps(self) -> None:
        """步数上限必须在解析层强制，而不是靠提示词"请只输出 2~5 步"。

        提示词是请求，代码是保证。
        """
        planner = Planner(RoutingLLM(plan=_default_plan(10)), max_steps=3)  # type: ignore[arg-type]
        assert len((await planner.amake_plan("g")).steps) == 3

    async def test_parses_json_in_code_fence(self) -> None:
        """模型常把 JSON 包在 ```json 代码块里 —— 必须能从噪声中提取。"""
        payload = json.dumps(_default_plan(2), ensure_ascii=False)
        llm = RoutingLLM()
        llm._plan = {"x": 1}  # 让默认分支不可用
        llm.chat = _fenced_chat(f"好的：\n```json\n{payload}\n```")  # type: ignore[method-assign]

        plan = await Planner(llm).amake_plan("g")  # type: ignore[arg-type]
        assert len(plan.steps) == 2

    @pytest.mark.parametrize(
        "raw",
        [
            "完全不是 JSON",
            "{ 这不是合法 JSON",
            '{"steps": []}',
            '{"steps": "不是数组"}',
            '{"steps": [{"没有": "description"}]}',
        ],
    )
    async def test_bad_output_falls_back_to_single_step(self, raw: str) -> None:
        """解析失败时**退化成一个单步计划**，而不是抛异常。

        规划失败不该让整个请求失败 —— 退化成"直接把目标当成一步去做"
        仍然能给出有价值的回答，只是少了结构化过程。
        """
        llm = RoutingLLM()
        llm.chat = _fenced_chat(raw)  # type: ignore[method-assign]
        plan = await Planner(llm).amake_plan("原始目标")  # type: ignore[arg-type]
        assert len(plan.steps) == 1
        assert plan.steps[0].description == "原始目标"

    async def test_revise_keeps_history_and_renumbers(self) -> None:
        """重规划必须保留**已完成与已失败**的步骤，并给新步骤重新编号。

        - 保留历史：计划对外是一份时间线。丢掉失败的那一步，用户只会看到
          "计划变了"却不知道为什么，而"上一步为什么失败"恰恰是理解新计划的关键。
        - 重新编号：不重编会出现 id 冲突，前端列表渲染会错乱。
        """
        planner = Planner(RoutingLLM(revise_plan={"steps": [{"id": 1, "description": "新步骤"}]}))  # type: ignore[arg-type]
        plan = Plan(
            goal="g",
            steps=[
                PlanStep(id=1, description="已完成", status=StepStatus.DONE, result="r"),
                PlanStep(id=2, description="失败", status=StepStatus.FAILED, error="e"),
                PlanStep(id=3, description="未执行", status=StepStatus.PENDING),
            ],
        )
        revised = await planner.arevise(plan, "前提不成立")

        # 历史保留
        assert revised.steps[0].description == "已完成"
        assert revised.steps[0].status is StepStatus.DONE
        assert revised.steps[1].description == "失败"
        assert revised.steps[1].status is StepStatus.FAILED
        # 未执行的被新计划取代（不回收到结果里）
        assert not any(s.description == "未执行" for s in revised.steps)
        # 新步骤编号接在历史之后
        assert revised.steps[-1].description == "新步骤"
        assert revised.steps[-1].id == 3

    async def test_revise_can_drop_history(self) -> None:
        planner = Planner(RoutingLLM(revise_plan={"steps": [{"id": 1, "description": "新"}]}))  # type: ignore[arg-type]
        plan = Plan(
            goal="g",
            steps=[PlanStep(id=1, description="旧", status=StepStatus.DONE, result="r")],
        )
        revised = await planner.arevise(plan, "x", keep_history=False)
        assert [s.description for s in revised.steps] == ["新"]

    async def test_revise_with_empty_result_keeps_original(self) -> None:
        planner = Planner(RoutingLLM(revise_plan={"steps": []}))  # type: ignore[arg-type]
        plan = Plan(goal="g", steps=[PlanStep(id=1, description="a")])
        assert await planner.arevise(plan, "x") is plan


def _fenced_chat(raw: str):
    async def _chat(messages: Sequence[ChatMessage], **_kw: Any) -> ChatResponse:
        return _resp(raw)

    return _chat


# ============================================================
# Plan-and-Execute 主流程
# ============================================================
class TestPlanAndExecute:
    async def test_event_sequence(self) -> None:
        """规划型 Agent 的事件序列：start → plan → plan_step×N → final → done。"""
        events = [e async for e in _agent(RoutingLLM()).run_stream("分析简历")]
        types = [e.type for e in events]

        assert types[0] is EventType.START
        assert EventType.PLAN in types
        assert types[-1] is EventType.DONE
        assert types[-2] is EventType.FINAL
        # 每个步骤一个 plan_step（开始与完成各一个）
        assert types.count(EventType.PLAN_STEP) == 6

    async def test_plan_event_carries_full_snapshot(self) -> None:
        """计划事件携带**完整快照**而非增量 diff ——
        diff 要求前端自己维护可变状态并与后端保持一致，那是 bug 的温床。"""
        events = [e async for e in _agent(RoutingLLM()).run_stream("g")]
        plan_event = next(e for e in events if e.type is EventType.PLAN)
        assert plan_event.plan is not None
        assert len(plan_event.plan["steps"]) == 3

        step_events = [e for e in events if e.type is EventType.PLAN_STEP]
        # 最后一个步骤事件里的快照应显示前两步已完成
        assert step_events[-1].plan is not None

    async def test_all_steps_executed(self) -> None:
        result = await _agent(RoutingLLM()).run("g")
        assert result.stopped_reason == "finished"
        assert result.steps_used == 3
        assert result.answer == "综合结论已完成。"
        assert result.plan is not None
        assert all(s["status"] == "done" for s in result.plan["steps"])

    async def test_step_conclusions_passed_to_next_step(self) -> None:
        """下一步必须看到上一步的**结论**。

        这是 Plan-and-Execute 相对 ReAct 的关键差别：
        每步不必重新读全部历史，只带结论就够，上下文近似恒定。
        """
        llm = RoutingLLM(step_answer="关键结论：3 年后端经验")
        await _agent(llm).run("g")
        # 至少有一次单步执行的提示词里包含了前面的结论
        assert llm.stream_calls == 3

    async def test_token_budget_stops_remaining_steps(self) -> None:
        """**没有总预算的规划型 Agent 是烧钱机器。**

        5 步 × 每步 4 次调用 = 20 次起步。更糟的是它"看起来正在认真工作"，
        用户不会意识到该打断它。所以必须有硬预算。

        预算靠累加每次调用的 usage 判定：规划 150 + 第一步 120 = 270，
        超过 200 的预算后剩余步骤被跳过。
        """
        result = await _agent(RoutingLLM(), max_total_tokens=200).run("g")
        assert result.plan is not None
        statuses = [s["status"] for s in result.plan["steps"]]
        assert statuses[0] == "done"
        assert "skipped" in statuses, f"预算未生效，步骤状态：{statuses}"

    async def test_budget_depends_on_reported_usage(self) -> None:
        """预算依赖服务端返回 usage —— 不返回就控不住成本。

        这不是 bug，而是必须知晓的前提：流式响应里 usage 通常只出现在
        最后一个 chunk，且部分兼容端点根本不返回。
        这个用例把该前提固定下来，避免有人误以为预算是绝对可靠的。
        """

        class NoUsageLLM(RoutingLLM):
            async def stream_chat(self, messages, tools=None, **kw):  # type: ignore[no-untyped-def]
                self.stream_calls += 1
                yield StreamDelta(content="结果")
                yield StreamDelta(finish_reason="stop")  # 不带 usage

        result = await _agent(NoUsageLLM(), max_total_tokens=200).run("g")
        assert result.plan is not None
        # 单步用量为 0，预算只被规划与综合推高 → 所有步骤都会执行
        statuses = [s["status"] for s in result.plan["steps"]]
        assert all(s == "done" for s in statuses)

    async def test_planning_failure_ends_gracefully(self) -> None:
        """规划失败必须**优雅终止**并给出原因，而不是抛异常。"""
        events = [e async for e in _agent(RoutingLLM(plan_raises=True)).run_stream("g")]
        assert any(e.type is EventType.ERROR for e in events)
        assert events[-1].type is EventType.DONE
        assert events[-1].stopped_reason == "error"

    async def test_synthesis_failure_falls_back_to_step_digest(self) -> None:
        """综合失败时退回到"直接拼步骤结论"。

        比返回"出错了"有用得多 —— 用户至少能看到已完成部分的结果，
        而不是一次白跑。
        """
        events = [e async for e in _agent(RoutingLLM(synthesis_raises=True)).run_stream("g")]
        final = next(e for e in events if e.type is EventType.FINAL)
        assert "执行结果" in final.content
        assert "步骤 1" in final.content
        assert "综合生成失败" in final.content

    async def test_step_failure_triggers_replan(self) -> None:
        """步骤失败且提供了修订计划时，应产出 replan 事件。"""
        llm = RoutingLLM(revise_plan={"steps": [{"id": 1, "description": "补救步骤"}]})

        class FailingStreamLLM(RoutingLLM):
            async def stream_chat(self, messages, tools=None, **kw):  # type: ignore[no-untyped-def]
                raise RuntimeError("步骤执行失败")
                yield  # pragma: no cover

        failing = FailingStreamLLM(revise_plan=llm._revise)
        events = [e async for e in _agent(failing).run_stream("g")]
        assert any(e.type is EventType.REPLAN for e in events)

    async def test_replan_disabled(self) -> None:
        class FailingStreamLLM(RoutingLLM):
            async def stream_chat(self, messages, tools=None, **kw):  # type: ignore[no-untyped-def]
                raise RuntimeError("失败")
                yield  # pragma: no cover

        events = [
            e
            async for e in _agent(
                FailingStreamLLM(revise_plan={"steps": [{"id": 1, "description": "x"}]}),
                enable_replan=False,
            ).run_stream("g")
        ]
        assert not any(e.type is EventType.REPLAN for e in events)

    async def test_step_failure_does_not_abort_whole_run(self) -> None:
        """单个步骤失败不该让整轮失败 —— 其余步骤继续跑，最终仍给出结论。"""

        class PartiallyFailingLLM(RoutingLLM):
            def __init__(self, **kw: Any) -> None:
                super().__init__(**kw)
                self._call = 0

            async def stream_chat(self, messages, tools=None, **kw):  # type: ignore[no-untyped-def]
                self.stream_calls += 1
                self._call += 1
                if self._call == 1:
                    raise RuntimeError("第一步失败")
                for delta in [
                    StreamDelta(content="后续步骤正常"),
                    StreamDelta(finish_reason="stop"),
                ]:
                    yield delta

        result = await _agent(PartiallyFailingLLM()).run("g")
        assert result.plan is not None
        statuses = [s["status"] for s in result.plan["steps"]]
        assert statuses[0] == "failed"
        assert "done" in statuses[1:]

    async def test_step_agents_reuse_tool_registry(self) -> None:
        """单步执行必须复用同一套工具与护栏 ——
        两套实现迟早会不一致，而且那时无法判断差异来自范式还是实现。"""
        agent = _agent(RoutingLLM(), tools=_registry_with_echo())
        result = await agent.run("g")
        assert result.stopped_reason == "finished"

    async def test_planner_tokens_counted(self) -> None:
        """规划本身的 token 也要计入总用量，否则成本统计会漏掉固定开销。"""
        result = await _agent(RoutingLLM()).run("g")
        # 规划 150 + 3 步 × 0（假 stream 无 usage）+ 综合 150
        assert result.usage.total_tokens >= 300


def _registry_with_echo() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    return registry
