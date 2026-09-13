"""Plan-and-Execute：显式规划型 Agent。

【它和 ReAct 的本质区别】

    ReAct（隐式规划）：想一步 → 做一步 → 看结果 → 再想下一步
    Plan-and-Execute（显式规划）：先出完整计划 → 逐步执行 → （必要时）修订计划

两者不是"谁更先进"，而是针对不同任务形态的取舍：

| 维度 | ReAct | Plan-and-Execute |
|---|---|---|
| 规划时机 | 每步现想 | 一次性先想全 |
| 模型调用 | 每步 1 次（还要带工具结果） | 规划 1 次 + 每步 1 次 |
| 适合任务 | 探索型：不知道下一步会看到什么 | 结构型：步骤事先大致可预知 |
| 长任务成本 | 上下文随步数**平方级**增长 | 每步只带计划摘要，**线性**增长 |
| 失败模式 | 容易原地打转（已用死循环护栏兜住） | 计划与事实脱节后仍硬着头皮执行 |
| 可解释性 | 差：事后才知道它想干什么 | **好**：计划是人能看懂的中间产物 |

本项目两者都保留：ReAct 是默认（`Agent`），Plan-and-Execute 是本模块。
选择依据是**任务是否可事先结构化**，而不是哪个听起来更高级。

【本实现最需要注意的两件事】

1. **必须有全局预算**。计划有 5 步、每步最多 4 次模型调用，
   那就是 20 次调用起步。没有总预算的规划型 Agent 是烧钱机器 ——
   而且它会"看起来正在认真工作"，用户不会意识到该打断它。

2. **上下文要"滚动摘要"而不是全部累积**。把所有步骤的完整结果都塞给
   后续步骤，上下文会线性膨胀到不可控。正确做法是每步只把**结论**
   （而非过程）带给下一步。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from enum import StrEnum

from pydantic import BaseModel, Field

from app.agent.events import AgentEvent, AgentRunResult, EventType
from app.agent.loop import Agent
from app.agent.prompts import SYSTEM_PROMPT
from app.core.config import AgentSettings
from app.llm.client import LLMClient
from app.llm.types import ChatMessage, Usage
from app.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


class StepExecutionError(RuntimeError):
    """单个步骤未正常完成。

    单独一个异常类型而不是复用 RuntimeError：调用方需要区分
    "步骤本身失败了"与"执行过程中出了别的岔子"，
    而它们的处理策略不同（前者触发重规划，后者只记日志）。
    """


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    id: int
    description: str
    # 这一步要达成什么，用于判断"做完了没有"
    expected: str = ""
    status: StepStatus = StepStatus.PENDING
    result: str = ""
    tools_used: list[str] = Field(default_factory=list)
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED)


class Plan(BaseModel):
    goal: str
    steps: list[PlanStep] = Field(default_factory=list)
    reasoning: str = ""

    def next_pending(self) -> PlanStep | None:
        return next((s for s in self.steps if s.status is StepStatus.PENDING), None)

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.steps if s.status is StepStatus.DONE)

    def conclusion_digest(self, max_chars: int = 200) -> str:
        """把已完成步骤的**结论**（不是过程）压缩成给下一步的上下文。

        这是控制上下文膨胀的关键：如果每步都把自己用过的工具结果、
        中间推理全部传下去，上下文会线性膨胀，第 5 步的输入可能
        是第 1 步的十几倍。只带结论则近似恒定。
        """
        lines = []
        for step in self.steps:
            if step.status is StepStatus.DONE and step.result:
                lines.append(f"步骤{step.id}（{step.description}）结论：{step.result[:max_chars]}")
            elif step.status is StepStatus.FAILED:
                lines.append(f"步骤{step.id}（{step.description}）**失败**：{step.error}")
        return "\n".join(lines)


# ============================================================
# 规划器
# ============================================================
class Planner:
    """把用户目标拆成可执行步骤。

    用**结构化输出**（JSON mode）而不是解析自然语言：计划需要被程序消费
    （逐步执行、更新状态、传给前端渲染），格式不稳定会让整条链路崩掉。
    同时要求模型输出 `reasoning` —— 计划背后的理由对排查"为什么拆得不对"
    至关重要，而它一旦不要求就永远不会出现。
    """

    PROMPT = """\
你是一个任务规划器。把用户的目标拆成 2~5 个**可独立执行**的步骤。

每个步骤必须：
- 是一个明确的动作，而不是泛泛的方向（❌"分析简历" ✅"读取并列出简历中的工作经历与量化成果"）
- 可以由一个具备工具的助手独立完成，不依赖后续步骤的未知结果
- 有明确的完成标准

严格只输出 JSON：
{
  "reasoning": "为什么这样拆分（一两句话）",
  "steps": [
    {"id": 1, "description": "步骤描述", "expected": "完成标准"}
  ]
}
不要输出任何其他文字。"""

    def __init__(
        self,
        llm: LLMClient,
        *,
        max_steps: int = 5,
        temperature: float = 0.2,
    ) -> None:
        self._llm = llm
        self.max_steps = max_steps
        self.temperature = temperature
        # 累计用量。**必须记录完整 Usage 而不只是一个 total_tokens 计数**：
        # 调用方需要把规划的开销并入整轮成本，而 prompt/completion 的拆分
        # 是分析"规划是不是太贵了"的必要信息（规划通常 prompt 占大头）。
        self.usage = Usage()

    async def amake_plan(self, goal: str, context: str = "") -> Plan:
        prompt = f"{self.PROMPT}\n\n用户目标：{goal}"
        if context:
            prompt += f"\n\n可用背景信息：\n{context[:2000]}"

        response = await self._llm.chat(
            [ChatMessage.user(prompt)],
            temperature=self.temperature,
            response_format={"type": "json_object"},
        )
        self.usage = self.usage + response.usage
        return self._parse(goal, response.message.content or "")

    async def arevise(self, plan: Plan, feedback: str, *, keep_history: bool = True) -> Plan:
        """根据执行情况修订计划。

        【什么时候该修订 —— 本模块最容易被做错的一点】
        不是"失败就重规划"。频繁重规划会让 Agent 陷入"计划-失败-重规划"
        的循环，成本远超收益。真正的触发条件只有两个：
          1. 某一步失败，且失败原因是**前提不成立**（不是临时故障）
          2. 执行结果**推翻了计划里的假设**（比如发现用户根本没有某段经历）

        临时故障（网络抖动、工具超时）应该重试那一步，而不是重写整个计划。

        【为什么保留失败步骤】
        计划对外是一份**时间线**。如果重规划时把失败的那一步丢掉，
        用户只会看到"计划变了"却不知道为什么 —— 而"上一步为什么失败"
        恰恰是理解新计划的关键。所以已完成的与已失败的都保留，
        只丢弃尚未执行的（它们的使命已经被新计划取代）。
        """
        kept = (
            [s for s in plan.steps if s.status in (StepStatus.DONE, StepStatus.FAILED)]
            if keep_history
            else []
        )

        prompt = (
            f"{self.PROMPT}\n\n"
            f"原始目标：{plan.goal}\n\n"
            f"已完成的步骤及结论：\n{plan.conclusion_digest() or '（无）'}\n\n"
            f"执行中遇到的问题：{feedback}\n\n"
            f"请只输出**剩余需要做的步骤**（不要重复已完成的），"
            f"id 从 {max((s.id for s in plan.steps), default=0) + 1} 开始编号。"
        )

        response = await self._llm.chat(
            [ChatMessage.user(prompt)],
            temperature=self.temperature,
            response_format={"type": "json_object"},
        )
        self.usage = self.usage + response.usage

        revised = self._parse(plan.goal, response.message.content or "", allow_fallback=False)
        if not revised.steps:
            logger.warning("重规划没有产出可用步骤，保留原计划")
            return plan

        next_id = max((s.id for s in kept), default=0)
        for offset, step in enumerate(revised.steps, start=1):
            step.id = next_id + offset

        return Plan(
            goal=plan.goal,
            steps=[*kept, *revised.steps],
            reasoning=revised.reasoning,
        )

    def _parse(self, goal: str, raw: str, *, allow_fallback: bool = True) -> Plan:
        """解析规划结果。

        Args:
            allow_fallback: 解析失败时是否退化成一个"把目标当成一步"的计划。
                - `amake_plan` 用 True：规划失败不该让整个请求失败，
                  退化成单步仍然能给出有价值的回答。
                - `arevise` 用 **False**：那里需要区分"没有可用步骤"与
                  "模型给了一个合理的新计划"。若在这里退化，会生成一个
                  「把原始目标重做一遍」的步骤 —— 那是重复劳动，
                  而且看起来完全合理，很难发现。
        """
        import re

        def fallback() -> Plan:
            if not allow_fallback:
                return Plan(goal=goal, steps=[])
            return Plan(goal=goal, steps=[PlanStep(id=1, description=goal)])

        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            logger.warning(
                "规划结果不是 JSON，%s", "退化为单步计划" if allow_fallback else "视为无修订"
            )
            return fallback()

        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            logger.warning(
                "规划结果 JSON 解析失败，%s", "退化为单步计划" if allow_fallback else "视为无修订"
            )
            return fallback()

        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return fallback()

        steps: list[PlanStep] = []
        for i, item in enumerate(raw_steps[: self.max_steps], start=1):
            if isinstance(item, dict) and item.get("description"):
                steps.append(
                    PlanStep(
                        id=i,
                        description=str(item["description"])[:500],
                        expected=str(item.get("expected", ""))[:300],
                    )
                )

        if not steps:
            return fallback()

        return Plan(goal=goal, steps=steps, reasoning=str(data.get("reasoning", ""))[:500])


# ============================================================
# Plan-and-Execute Agent
# ============================================================
class PlanAndExecuteAgent:
    """先规划、再逐步执行、必要时修订。

    与 `Agent`（ReAct）**共享同一套工具、事件与护栏** ——
    这不是巧合，而是刻意的：两套范式如果各自实现循环、各自实现工具调用，
    它们的行为差异就会和范式差异混在一起，无法归因。
    """

    STEP_PROMPT = """\
你是 JobPilot 的任务执行器。当前你在执行一个更大计划中的**某一步**。

严格执行原则：
1. **只做当前这一步**，不要越界去完成其他步骤 —— 后续步骤会由别的执行负责。
2. 需要事实依据时必须调用工具（简历内容用 read_resume / search_knowledge，
   岗位信息用 search_jobs，计算用 calculator），不要凭空推断。
3. 完成后输出**这一步的结论**（简洁、可直接给下一步使用），而不是过程描述。
4. 如果这一步无法完成（缺数据、前提不成立），**明确说出来**并说明原因 ——
   这比编一个看起来完成的结果有价值得多。
"""

    SYNTHESIS_PROMPT = """\
你是 JobPilot。下面是围绕用户目标制定的计划与各步骤的执行结论。
请把它们综合成一个**完整、可直接给用户看**的回答。

要求：
- 结论先行，然后给依据与行动建议
- 只使用各步骤结论中真实出现的信息，**不要补充任何未经验证的内容**
- 如果某些步骤失败或信息缺失，如实说明哪部分无法完成
- 用 Markdown 组织，善用表格与列表
"""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        settings: AgentSettings,
        *,
        planner: Planner | None = None,
        max_steps_per_step: int = 4,
        max_total_tokens: int = 60_000,
        max_replans: int = 1,
        enable_replan: bool = True,
        base_system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._s = settings
        self._planner = planner or Planner(llm)
        # 单步内的 ReAct 预算必须比全局预算小得多：
        # 5 步 × 12 次调用 = 60 次，那是烧钱机器
        self._step_settings = AgentSettings(
            max_steps=max_steps_per_step, loop_guard=settings.loop_guard
        )
        self._max_total_tokens = max_total_tokens
        # 重规划次数上限。默认 1 次：重规划应当处理"前提被推翻"这种重大变化，
        # 而不是当作失败重试机制 —— 后者应该重试那一步，而不是重写整个计划。
        self._max_replans = max_replans
        self._enable_replan = enable_replan
        self._base_prompt = base_system_prompt

    # ---------- 主入口 ----------

    async def run_stream(self, user_input: str) -> AsyncIterator[AgentEvent]:
        total_usage = Usage()
        yield AgentEvent(type=EventType.START, content=user_input)

        # ---------- 1. 规划 ----------
        yield AgentEvent(type=EventType.STEP, step=1, content="正在制定计划…")
        planner_before = self._planner.usage
        try:
            plan = await self._planner.amake_plan(user_input)
        except Exception as exc:
            logger.exception("规划失败")
            yield AgentEvent(type=EventType.ERROR, content=f"规划失败：{exc}")
            yield AgentEvent(type=EventType.DONE, stopped_reason="error", usage=total_usage)
            return
        # 规划本身的 token 必须计入总用量，否则成本统计会漏掉这笔固定开销 ——
        # 而它恰恰是规划型 Agent 相比 ReAct 多出来的那部分成本
        total_usage = total_usage + self._planner_delta(planner_before)

        yield AgentEvent(type=EventType.PLAN, content=plan.goal, plan=plan.model_dump())

        # ---------- 2. 逐步执行 ----------
        #
        # 用 while + next_pending() 而不是 for 遍历列表，是为了正确处理重规划：
        # 重规划意味着**放弃旧计划的剩余步骤、改走新计划**。
        #
        # 【初版在这里有一个真实缺陷】初版用 `for step in plan.steps:`，
        # 于是在步骤 1 失败并触发重规划后：`plan` 被替换成新计划，
        # 但 for 循环仍然拿着**旧列表**继续执行旧步骤 2、3 ——
        # 最终报告的计划（新的）与实际执行的内容（旧的）完全对不上。
        # 时间线是错的，而且看不出错在哪。
        #
        # 另一个必须加的护栏是**重规划次数上限**：否则会出现
        # "计划 → 失败 → 重规划 → 又失败 → 又重规划"的无限循环，
        # 而且每一轮都在真实花钱。`max_total_steps` 同时兜住这两件事。
        steps_executed = 0
        max_total_steps = self._s.max_steps + self._max_replans
        replans_used = 0

        while (step := plan.next_pending()) is not None:
            steps_executed += 1
            if steps_executed > max_total_steps:
                step.status = StepStatus.SKIPPED
                step.error = f"达到总步骤上限（{max_total_steps}），后续步骤被跳过"
                yield AgentEvent(
                    type=EventType.PLAN_STEP,
                    content=step.error,
                    plan=plan.model_dump(),
                )
                break

            if total_usage.total_tokens >= self._max_total_tokens:
                step.status = StepStatus.SKIPPED
                step.error = "已达到总 token 预算，剩余步骤被跳过"
                yield AgentEvent(
                    type=EventType.PLAN_STEP,
                    content=f"步骤 {step.id} 因预算耗尽被跳过",
                    plan=plan.model_dump(),
                )
                # 把剩余待办一并标记，避免计划面板里留着一堆永远 PENDING 的步骤
                for rest in plan.steps:
                    if rest.status is StepStatus.PENDING:
                        rest.status = StepStatus.SKIPPED
                        rest.error = "预算耗尽"
                break

            step.status = StepStatus.RUNNING
            yield AgentEvent(
                type=EventType.PLAN_STEP,
                step=step.id,
                content=f"开始执行：{step.description}",
                plan=plan.model_dump(),
            )

            try:
                result, usage, stopped_reason = await self._execute_step(step, plan)
                total_usage = total_usage + usage

                # 【关键】单步执行"没抛异常"不等于"成功了"。
                #
                # `Agent.run_stream` 会把模型调用失败、步数耗尽、死循环中止
                # 都**转成事件**而不是抛异常（这是刻意的：Agent 不该把故障
                # 变成调用方的异常）。所以这里必须检查 stopped_reason ——
                # 否则失败的步骤会被标记成 DONE，计划面板显示"全部成功"，
                # 而实际结果里混着"（模型返回了空回复）"这类占位文本。
                # 这种"看起来成功"的假象比直接报错更危险。
                if stopped_reason != "finished":
                    raise StepExecutionError(f"步骤未正常完成（{stopped_reason}）：{result[:200]}")

                step.status = StepStatus.DONE
                step.result = result
                yield AgentEvent(
                    type=EventType.PLAN_STEP,
                    step=step.id,
                    content=f"完成：{result[:200]}",
                    plan=plan.model_dump(),
                )
            except Exception as exc:
                logger.exception("步骤 %d 执行失败", step.id)
                step.status = StepStatus.FAILED
                step.error = (
                    f"{type(exc).__name__}: {exc}"
                    if not isinstance(exc, StepExecutionError)
                    else str(exc)
                )
                yield AgentEvent(
                    type=EventType.PLAN_STEP,
                    step=step.id,
                    content=f"失败：{step.error}",
                    plan=plan.model_dump(),
                )

                # 只在**前提不成立**时才重规划；临时故障应该重试那一步。
                # 这里无法自动判断，所以保守处理：允许重规划，但**计数封顶** ——
                # 没有上限的"失败就重规划"会变成无限循环，且每一轮都在真实花钱。
                if self._enable_replan and replans_used < self._max_replans:
                    revise_before = self._planner.usage
                    try:
                        revised = await self._planner.arevise(plan, step.error or "未知错误")
                        total_usage = total_usage + self._planner_delta(revise_before)
                        if revised.steps:
                            replans_used += 1
                            plan = revised  # 旧计划的剩余步骤就此作废
                            yield AgentEvent(
                                type=EventType.REPLAN,
                                content=(
                                    f"计划已修订（第 {replans_used}/{self._max_replans} 次），"
                                    f"剩余 {len(revised.steps)} 步"
                                ),
                                plan=plan.model_dump(),
                            )
                    except Exception:
                        logger.warning("重规划失败，继续按原计划执行")
                elif self._enable_replan:
                    logger.warning("重规划次数已达上限 %d，不再修订", self._max_replans)

        # ---------- 3. 综合结论 ----------
        yield AgentEvent(type=EventType.STEP, step=len(plan.steps) + 1, content="正在综合结论…")

        try:
            answer, usage = await self._synthesize(plan)
            total_usage = total_usage + usage
        except Exception as exc:
            logger.exception("综合失败")
            answer = self._fallback_answer(plan)
            yield AgentEvent(type=EventType.ERROR, content=f"综合结论失败，已退回步骤摘要：{exc}")

        yield AgentEvent(type=EventType.FINAL, content=answer)
        yield AgentEvent(
            type=EventType.DONE,
            steps_used=plan.done_count,
            usage=total_usage,
            stopped_reason="finished" if plan.done_count else "error",
        )

    async def run(self, user_input: str) -> AgentRunResult:
        answer = ""
        steps_used = 0
        usage = Usage()
        error: str | None = None
        stopped = "finished"
        plan_payload: dict | None = None

        async for event in self.run_stream(user_input):
            if event.type is EventType.FINAL:
                answer = event.content
            elif event.type is EventType.DONE:
                steps_used = event.steps_used
                usage = event.usage or Usage()
                stopped = event.stopped_reason
            elif event.type is EventType.ERROR:
                error = event.content
            elif event.type in (EventType.PLAN, EventType.PLAN_STEP, EventType.REPLAN):
                plan_payload = event.plan

        return AgentRunResult(
            answer=answer,
            steps_used=steps_used,
            usage=usage,
            tool_calls=[],
            stopped_reason=stopped,
            error=error,
            plan=plan_payload,
        )

    # ---------- 内部 ----------

    async def _execute_step(self, step: PlanStep, plan: Plan) -> tuple[str, Usage, str]:
        """用一次完整的 ReAct 循环执行单个步骤。

        复用 `Agent` 而不是自己写一遍循环：工具调用、参数校验、死循环护栏、
        消息配对这些细节只应该有一份实现。两套实现迟早会不一致，
        而且那时你无法判断差异来自"范式不同"还是"实现不同"。

        Returns:
            (回答文本, 用量, 终止原因)。**终止原因必须一起返回** ——
            `Agent` 把失败转成事件而不抛异常，调用方只能靠它判断成败。
        """
        context = plan.conclusion_digest()
        prompt = f"{self.STEP_PROMPT}\n\n整体目标：{plan.goal}"
        if context:
            prompt += f"\n\n已完成步骤的结论：\n{context}"

        agent = Agent(
            self._llm,
            self._tools,
            self._step_settings,
            system_prompt=prompt,
        )
        result = await agent.run(
            f"当前步骤（{step.id}/{len(plan.steps)}）：{step.description}\n"
            f"完成标准：{step.expected or '给出这一步的结论'}"
        )
        return result.answer, result.usage, result.stopped_reason

    def _planner_delta(self, before: Usage) -> Usage:
        """算出本轮规划新增的用量。

        规划器是长期存活的对象（可能服务多次运行），所以不能直接用它的
        累计值 —— 那会把上一轮的用量算到这一轮头上，成本统计会越跑越偏。
        """
        after = self._planner.usage
        return Usage(
            prompt_tokens=max(0, after.prompt_tokens - before.prompt_tokens),
            completion_tokens=max(0, after.completion_tokens - before.completion_tokens),
            total_tokens=max(0, after.total_tokens - before.total_tokens),
        )

    async def _synthesize(self, plan: Plan) -> tuple[str, Usage]:
        response = await self._llm.chat(
            [
                ChatMessage.system(self._base_prompt),
                ChatMessage.user(
                    f"{self.SYNTHESIS_PROMPT}\n\n"
                    f"用户目标：{plan.goal}\n\n"
                    f"计划与执行结论：\n{plan.conclusion_digest(max_chars=600)}\n\n"
                    f"失败的步骤：\n"
                    + (
                        "\n".join(
                            f"- 步骤{s.id}（{s.description}）：{s.error}"
                            for s in plan.steps
                            if s.status is StepStatus.FAILED
                        )
                        or "（无）"
                    )
                ),
            ],
            temperature=0.3,
        )
        return (response.message.content or "").strip(), response.usage

    @staticmethod
    def _fallback_answer(plan: Plan) -> str:
        """综合失败时的兜底：直接把已完成的步骤结论拼出来。

        比返回"出错了"有用得多 —— 用户至少能看到已完成部分的结果，
        而不是一次白跑。
        """
        parts = ["# 执行结果", ""]
        for step in plan.steps:
            icon = {
                StepStatus.DONE: "✅",
                StepStatus.FAILED: "❌",
                StepStatus.SKIPPED: "⏭️",
                StepStatus.PENDING: "⏳",
                StepStatus.RUNNING: "🔄",
            }[step.status]
            parts.append(f"## {icon} 步骤 {step.id}：{step.description}")
            parts.append(step.result or step.error or "（无结果）")
            parts.append("")
        parts.append("> 综合生成失败，以上为各步骤的原始结论。")
        return "\n".join(parts)


def plan_from_dict(payload: dict | None) -> Plan | None:
    """从事件载荷还原计划对象（前端或测试用）。"""
    if not payload:
        return None
    try:
        return Plan.model_validate(payload)
    except ValueError:
        return None


__all__ = [
    "Plan",
    "PlanAndExecuteAgent",
    "PlanStep",
    "Planner",
    "StepStatus",
    "plan_from_dict",
]
