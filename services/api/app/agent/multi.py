"""多 Agent 协作：主管（Supervisor）+ 专家（Specialist）。

【三种多 Agent 协作模式的取舍】

| 模式 | 结构 | 适合 | 主要问题 |
|---|---|---|---|
| **主管-工人**（本模块） | 一个协调者按需派发给若干专家 | 任务能被清晰切分为不同专长领域 | 协调者成为单点；路由错了全错 |
| 辩论 | 多个平级 Agent 各自作答再互相质疑 | 需要降低单点偏差 | 成本高；可能出现无法收敛的争论 |
| 流水线 | 固定顺序传递（A→B→C） | 流程确定、每步职责单一 | 不灵活，本质是工作流而非 Agent |

本项目选**主管-工人**。理由很具体：求职场景的任务天然按专长切分
（简历诊断 / 岗位匹配 / 面试准备 / 知识检索），
而它们的输出**互不依赖** —— 这正是主管-工人最擅长的形态。

【与 Plan-and-Execute 的关键差别】
Plan-and-Execute 的步骤是**有依赖的**（第 2 步要用第 1 步的结论），所以必须串行。
职责不同的专家之间**没有依赖**，所以可以并发。
这个差别直接决定实现方式：

    有依赖 → 串行 + 传递结论
    无依赖 → 并发 + 各自独立作答，最后汇总

**搞反了的代价**：把无依赖的专家串行化，总耗时是所有专家之和；
把有依赖的步骤并发化，后面的步骤会拿到空的前置结果。

【必须处理的三件事】

1. **路由错了就全错了**。路由只给专家**名字与描述**，不给能力清单 ——
   描述写得含糊，主管就会派错人。所以 Specialist.description 必须写清
   "什么时候该找它"，而不是"它是什么"。

2. **并发要限量**。5 个专家同时发起 = 5 路并发 LLM 调用，
   很容易触发限流。用信号量限制并发数。

3. **汇总不能简单拼接**。多个专家可能给出矛盾的建议（比如一个说
   "突出 AI 项目"，另一个说"补齐分布式经验"）。主管必须**做取舍**，
   而不是把几段话粘在一起丢给用户 —— 那反而比单个专家更差。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Sequence

from pydantic import BaseModel

from app.agent.events import AgentEvent, AgentRunResult, EventType
from app.agent.loop import Agent
from app.agent.prompts import build_system_prompt
from app.agent.runtime import (
    RunBudgetExceeded,
    RunContext,
    current_run_context,
    guard_llm,
    guarded_stream,
)
from app.core.config import AgentSettings
from app.llm.client import LLMClient
from app.llm.types import ChatMessage, Usage
from app.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


class Specialist(BaseModel):
    """一个专家 Agent 的定义。

    `description` 是**路由依据**，不是自我介绍。要写"什么时候该找它"，
    而不是"它擅长什么" —— 前者能直接支撑判断，后者要靠主管自己脑补。
    """

    name: str
    description: str
    system_prompt: str


# ============================================================
# 默认专家团（求职场景）
# ============================================================
DEFAULT_SPECIALISTS: list[Specialist] = [
    Specialist(
        name="简历诊断师",
        description=(
            "当用户需要评价、诊断、改写简历，或想知道自己的简历有什么问题时找它。"
            "它只负责简历本身的质量，不做岗位匹配。"
        ),
        system_prompt=(
            "你是资深简历诊断师。你的唯一职责是评价与改进简历本身。\n"
            "根据实际可用的资料工具获取简历内容；工具或资料缺失时说明，绝不凭空评价。\n"
            "指出问题时必须给出可直接替换的改写示例，而不是「建议优化」这类空话。\n"
            "面试官视角：你会先看什么、什么让你皱眉、什么让你想约面。"
        ),
    ),
    Specialist(
        name="岗位分析师",
        description=(
            "当用户需要找岗位、了解某类岗位的要求、或判断某个岗位是否值得投时找它。"
            "它负责岗位侧的信息，不评价简历质量。"
        ),
        system_prompt=(
            "你是招聘市场分析师。你的职责是把岗位的真实要求讲清楚。\n"
            "根据实际可用的资料工具获取岗位原文；缺少来源时说明，不要凭印象描述。\n"
            "输出要区分「硬性门槛」（学历、年限、必备技能）与「加分项」——\n"
            "用户最需要知道的是自己会不会因为硬门槛被筛掉。"
        ),
    ),
    Specialist(
        name="匹配度顾问",
        description=(
            "当用户想知道自己和某个岗位/方向是否匹配、差距在哪、该怎么补时找它。"
            "它需要在简历与岗位之间做交叉分析。"
        ),
        system_prompt=(
            "你是求职匹配顾问。你的职责是把候选人与岗位逐条比对。\n"
            "根据实际可用的资料工具，同时获取简历内容与岗位要求，\n"
            "缺任何一侧都必须明确说明——基于单侧的判断是猜测。\n"
            "输出必须是逐条对照表：岗位要求 | 候选人证据 | 判定。\n"
            "不允许为了鼓励用户而夸大匹配度。"
        ),
    ),
    Specialist(
        name="面试教练",
        description=(
            "当用户需要准备面试、想被提问、或想复盘面试表现时找它。"
            "它负责出题、追问与点评，不做简历改写。"
        ),
        system_prompt=(
            "你是技术面试官。你的职责是通过提问检验候选人的真实水平。\n"
            "基于用户的真实经历（先读简历）设计由浅入深的问题。\n"
            "追问要具体到技术细节，不要停留在「你做过什么」这种层面。\n"
            "每次只问 2~3 个问题，等用户回答后再追问，不要一次性抛出一堆。"
        ),
    ),
]

GENERAL_SPECIALISTS: list[Specialist] = [
    Specialist(
        name="资料分析员",
        description="需要查阅文档、检索事实、提取证据时选择。",
        system_prompt="你是资料分析员。根据已提供的工具获取资料，只报告可核对的事实与出处；缺资料时明确说明。",
    ),
    Specialist(
        name="方案分析员",
        description="需要拆解问题、比较方案、提出具体执行步骤时选择。",
        system_prompt="你是方案分析员。基于可验证事实比较方案并给出具体步骤，标明假设和缺失信息。",
    ),
    Specialist(
        name="结果核验员",
        description="需要核对条件、计算、前提或结论的可靠性时选择。",
        system_prompt="你是结果核验员。独立核对用户任务中的条件与计算，使用可用工具验证，指出不成立的前提。",
    ),
]


class SupervisorAgent:
    """主管：路由 → 并发派发 → 汇总。"""

    ROUTER_PROMPT = """\
你是一个任务协调者。下面有一组专家，请判断为了完成用户的请求，
应该咨询哪几位专家（1~{max_n} 位，按重要性排序）。

判断原则：
- 只选**真正需要**的。多选一位就多一次完整的 Agent 运行，代价是真实的。
- 如果用户的问题只涉及一个领域，就只选一位。
- 如果问题跨越多个领域（比如"我的简历和这个岗位匹配吗"同时涉及简历与岗位），
  才选多位。

严格只输出 JSON：
{{"specialists": ["专家名1", "专家名2"], "reasoning": "为什么选这几位"}}

可选专家：
{menu}"""

    SYNTHESIS_PROMPT = """\
你是 Legacy 的协调者。下面几位专家分别给出了各自的分析。
请把它们综合成一个**统一、连贯**的回答给用户。

关键要求：
- **做取舍，不要拼接**。多位专家的建议可能互相矛盾（比如一个说"突出 AI 项目"，
  另一个说"补齐分布式经验"），你的职责是判断优先级并给出明确结论 ——
  把几段话粘在一起丢给用户，比只给一位专家的回答还差。
- 如果某位专家失败了，如实说明哪部分缺失，不要用其他专家的内容去补。
- 只使用专家结论中真实出现的信息，不要补充未经验证的内容。
- 结论先行，用 Markdown 组织。

用户请求：{request}
"""

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        settings: AgentSettings,
        *,
        specialists: list[Specialist] | None = None,
        max_delegates: int = 3,
        max_concurrency: int = 3,
        max_total_tokens: int | None = None,
    ) -> None:
        self._llm = guard_llm(llm)
        self._tools = tools
        self._s = settings
        self._specialists = (
            specialists
            if specialists is not None
            else (DEFAULT_SPECIALISTS if settings.profile == "jobhunt" else GENERAL_SPECIALISTS)
        )
        self._by_name = {s.name: s for s in self._specialists}
        self._max_delegates = max_delegates
        # 并发上限：5 个专家同时发起 = 5 路并发调用，很容易触发限流
        self._max_concurrency = max_concurrency
        self._max_total_tokens = (
            settings.multi_max_total_tokens if max_total_tokens is None else max_total_tokens
        )
        self._base_prompt = build_system_prompt(settings.profile, set(tools.names()))

    # ---------- 主入口 ----------

    async def run_stream(
        self,
        user_input: str,
        history: Sequence[ChatMessage] | None = None,
        *,
        run_context: RunContext | None = None,
    ) -> AsyncIterator[AgentEvent]:
        context = (
            run_context
            or current_run_context()
            or RunContext.create(
                self._s,
                token_limit=self._max_total_tokens,
            )
        )
        source = guarded_stream(self._run_stream(user_input, history), context)
        try:
            async for event in source:
                yield event
        finally:
            await source.aclose()

    async def _run_stream(
        self,
        user_input: str,
        history: Sequence[ChatMessage] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行一轮。

        `history` 为接口一致性而存在（理由同 planning.py）。
        **当前不使用会话历史**：每位专家都拿完整请求**独立**作答 ——
        这正是"多专家"成立的前提。若各专家带一份不同的历史，
        它们就不再是"针对同一个问题的不同视角"了。
        """
        if history:
            logger.info("多 Agent 收到 %d 条会话历史但当前不使用（各专家独立作答）", len(history))
        total_usage = Usage()
        yield AgentEvent(type=EventType.START, content=user_input)

        # ---------- 1. 路由 ----------
        yield AgentEvent(type=EventType.STEP, step=1, content="正在判断需要哪些专家…")
        try:
            selected, route_usage = await self._route(user_input)
        except RunBudgetExceeded:
            raise
        except Exception as exc:
            logger.exception("路由失败")
            yield AgentEvent(type=EventType.ERROR, content=f"路由失败：{exc}")
            yield AgentEvent(type=EventType.DONE, stopped_reason="error", usage=total_usage)
            return
        total_usage = total_usage + route_usage
        if context := current_run_context():
            context.check()

        if not selected:
            # 路由没选出人时不要空转：退回单专家（简历诊断师），
            # 而不是回一句"我不知道该找谁"
            logger.warning("路由未选出专家，退回默认专家")
            selected = self._specialists[:1]

        for s in selected:
            yield AgentEvent(
                type=EventType.DELEGATE,
                specialist=s.name,
                content=f"派发给「{s.name}」：{s.description[:60]}",
            )

        # ---------- 2. 并发派发 ----------
        #
        # 专家之间**没有依赖**，所以并发。总耗时 ≈ 最慢的那位，
        # 而不是所有专家之和。这是与 Plan-and-Execute（步骤有依赖、必须串行）
        # 的关键实现差别。
        semaphore = asyncio.Semaphore(self._max_concurrency)
        results: dict[str, str] = {}
        failures: dict[str, str] = {}
        usages: dict[str, Usage] = {}

        async def run_one(specialist: Specialist) -> tuple[str, str | None, Usage, str | None]:
            """执行一位专家。

            **返回值里必须带上专家名**：`asyncio.as_completed` 只产出协程本身，
            不带任何身份信息。若在这里把名字丢掉，调用方就无法知道
            刚完成的是哪一位 —— 初版就踩了这个坑，只好在循环里
            反复扫描 results 并做去重，写得很绕且容易出错。
            """
            async with semaphore:
                try:
                    answer, usage = await self._delegate(specialist, user_input)
                    return specialist.name, answer, usage, None
                except RunBudgetExceeded:
                    raise
                except Exception as exc:
                    logger.exception("专家 %s 执行失败", specialist.name)
                    return specialist.name, None, Usage(), f"{type(exc).__name__}: {exc}"

        # as_completed：先完成的先出结果 —— 用户不必等最慢的那位才开始看到内容
        tasks = [asyncio.create_task(run_one(s)) for s in selected]
        try:
            for future in asyncio.as_completed(tasks):
                name, answer, usage, err = await future

                if err is not None:
                    failures[name] = err
                    yield AgentEvent(
                        type=EventType.DELEGATE_RESULT,
                        specialist=name,
                        tool_ok=False,
                        content=err,
                    )
                    continue

                results[name] = answer or ""
                usages[name] = usage
                yield AgentEvent(
                    type=EventType.DELEGATE_RESULT,
                    specialist=name,
                    tool_ok=True,
                    content=(answer or "")[:400],
                )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        total_usage = total_usage + Usage(
            prompt_tokens=sum(u.prompt_tokens for u in usages.values()),
            completion_tokens=sum(u.completion_tokens for u in usages.values()),
            total_tokens=sum(u.total_tokens for u in usages.values()),
        )

        # ---------- 3. 汇总 ----------
        yield AgentEvent(type=EventType.STEP, step=2, content="正在汇总专家意见…")
        try:
            answer, usage = await self._synthesize(user_input, results, failures)
            total_usage = total_usage + usage
        except RunBudgetExceeded:
            raise
        except Exception as exc:
            logger.exception("汇总失败")
            answer = self._fallback_answer(results, failures)
            yield AgentEvent(type=EventType.ERROR, content=f"汇总失败，已退回专家原文：{exc}")

        yield AgentEvent(type=EventType.FINAL, content=answer)
        yield AgentEvent(
            type=EventType.DONE,
            steps_used=len(results),
            usage=total_usage,
            stopped_reason="finished" if results else "error",
        )

    async def run(
        self,
        user_input: str,
        history: Sequence[ChatMessage] | None = None,
        *,
        run_context: RunContext | None = None,
    ) -> AgentRunResult:
        answer = ""
        steps_used = 0
        usage = Usage()
        error: str | None = None
        stopped = "finished"
        done = AgentEvent(type=EventType.DONE)
        delegated: list[dict[str, object]] = []

        async for event in self.run_stream(user_input, history, run_context=run_context):
            if event.type is EventType.FINAL:
                answer = event.content
            elif event.type is EventType.DONE:
                done = event
                steps_used = event.steps_used
                usage = event.usage or Usage()
                stopped = event.stopped_reason
            elif event.type is EventType.ERROR:
                error = event.content
            elif event.type is EventType.DELEGATE_RESULT:
                delegated.append(
                    {"name": event.specialist, "ok": event.tool_ok, "content": event.content}
                )

        return AgentRunResult(
            answer=answer,
            steps_used=steps_used,
            usage=usage,
            usage_complete=done.usage_complete,
            tool_summary=done.tool_summary,
            context_trimmed=done.context_trimmed,
            context_tokens=done.context_tokens,
            tool_calls=delegated,
            stopped_reason=stopped,
            error=error,
        )

    # ---------- 内部 ----------

    async def _route(self, request: str) -> tuple[list[Specialist], Usage]:
        menu = "\n".join(f"- {s.name}：{s.description}" for s in self._specialists)
        prompt = self.ROUTER_PROMPT.format(max_n=self._max_delegates, menu=menu)

        response = await self._llm.chat(
            [ChatMessage.user(f"{prompt}\n\n用户请求：{request}")],
            temperature=0.1,
            response_format={"type": "json_object"},
        )

        names = self._parse_names(response.message.content or "")

        # 必须去重：模型完全可能返回 ["岗位分析师", "岗位分析师"]。
        # 不去重就会把同一个请求派发给同一位专家两次 —— 白白多跑一整轮 Agent
        # （真实成本），而且 UI 上会出现两张一模一样的结果卡片。
        # 保留首次出现的顺序，因为它承载了"谁更重要"的信息。
        seen: set[str] = set()
        selected: list[Specialist] = []
        for name in names:
            if name in self._by_name and name not in seen:
                seen.add(name)
                selected.append(self._by_name[name])
            if len(selected) >= self._max_delegates:
                break

        return selected, response.usage

    @staticmethod
    def _parse_names(raw: str) -> list[str]:
        """解析路由结果。

        对未知名字**静默忽略**而不是报错：主管偶尔会拼错专家名或编一个
        不存在的名字。忽略它比让整个请求失败合理得多 ——
        剩下的专家通常仍然能完成任务。
        """
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return []
        items = data.get("specialists")
        if not isinstance(items, list):
            return []
        return [str(x) for x in items if isinstance(x, str)]

    async def _delegate(self, specialist: Specialist, request: str) -> tuple[str, Usage]:
        """把请求交给某位专家。

        每位专家是一个**完整的 Agent**（有自己的工具、循环与护栏），
        而不是一次普通的 LLM 调用 —— 这正是"多 Agent"与"多轮 prompt"的区别：
        专家能自己决定调哪些工具、需不需要多步。
        """
        agent = Agent(
            self._llm,
            self._tools,
            self._s,
            system_prompt=f"{self._base_prompt}\n\n{specialist.system_prompt}",
        )
        result = await agent.run(request)

        # 【必须把 result.error 带出去】
        # `Agent` 会把模型调用失败、步数耗尽、死循环中止都**转成事件**而不是
        # 抛异常（刻意的：Agent 不该把故障变成调用方的异常）。此时
        # `result.answer` 往往是空的，而**真正的失败原因在 `result.error` 里**。
        #
        # 初版只拼了 `stopped_reason`，于是失败信息退化成
        # "未正常完成（error）：" —— 冒号后面什么都没有，排查时完全无从下手。
        # 把底层原因原样带出来，是"失败可诊断"的最低要求。
        if result.stopped_reason != "finished":
            detail = result.error or result.answer or "（无更多信息）"
            raise RuntimeError(
                f"专家「{specialist.name}」未正常完成（{result.stopped_reason}）：{detail[:300]}"
            )
        return result.answer, result.usage

    async def _synthesize(
        self, request: str, results: dict[str, str], failures: dict[str, str]
    ) -> tuple[str, Usage]:
        parts = []
        for name, text in results.items():
            parts.append(f"### 专家「{name}」的结论\n{text[:3000]}")
        for name, err in failures.items():
            parts.append(f"### 专家「{name}」**执行失败**\n{err}")

        response = await self._llm.chat(
            [
                ChatMessage.system(self._base_prompt),
                *(ChatMessage.assistant(part) for part in parts),
                ChatMessage.user(self.SYNTHESIS_PROMPT.format(request=request)),
            ],
            temperature=0.3,
        )
        return (response.message.content or "").strip(), response.usage

    @staticmethod
    def _fallback_answer(results: dict[str, str], failures: dict[str, str]) -> str:
        """汇总失败时的兜底：把专家原文按顺序拼出来。

        比返回"出错了"有用得多 —— 用户至少能看到几位专家的完整分析。
        """
        parts = ["# 专家分析结果", ""]
        for name, text in results.items():
            parts += [f"## ✅ {name}", text, ""]
        for name, err in failures.items():
            parts += [f"## ❌ {name}", f"执行失败：{err}", ""]
        parts.append("> 汇总生成失败，以上为各位专家的原始结论。")
        return "\n".join(parts)


__all__ = ["DEFAULT_SPECIALISTS", "Specialist", "SupervisorAgent"]
