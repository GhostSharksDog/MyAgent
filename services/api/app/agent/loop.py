"""Agent 内核：手写 ReAct 循环。

【ReAct 是什么】
ReAct = Reasoning + Acting。核心思想是让模型交替进行「推理」和「行动」：

    Thought（想） → Action（调工具） → Observation（看结果） → Thought → ... → Answer

和"直接问模型要答案"的本质区别：
  - 直接问：模型只能用它参数里的知识，可能过时、可能算错、可能编造。
  - ReAct：模型可以**主动获取信息**，用真实数据支撑答案。
    Agent 的智能程度，很大程度上等于"它能获得多少真实信息"。

【循环的终止条件】
1. 模型返回了不带 tool_calls 的消息  => 它认为可以直接回答了（正常结束）
2. 达到 max_steps                  => 防止无限循环烧钱（预算保护）
3. 检测到工具调用死循环             => 模型卡住了（如反复调同一个工具）
4. 出错                            => 如实暴露

【为什么不用 while True】
没有步数上限的 Agent 在生产环境是定时炸弹：一个模糊的问题就可能让它
调用工具几十次，单次请求成本从 0.01 元变成 3 元。max_steps 就是保险丝。

【并发说明】
同一个模型回合里的多个工具调用**并发**执行（延迟从 3×T 降到 1×T），
上限见 `AgentSettings.tool_concurrency`。三条边界保证它不会改变行为语义：
结果仍按模型给出的顺序回灌、单个工具失败只影响自己、
声明了 `Tool.serial` 的工具会让整个回合退回串行。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence

from app.agent.context import ContextBudget, TrimReport, summarize_tools
from app.agent.events import AgentEvent, AgentRunResult, EventType
from app.agent.memory import ConversationMemory, LongTermMemory
from app.agent.prompts import build_system_prompt
from app.core.config import AgentSettings
from app.llm.client import LLMClient, StreamAccumulator
from app.llm.tokens import record_prompt_estimate, tokenizer_name
from app.llm.types import ChatMessage, ToolCall, Usage
from app.tools.base import ToolRegistry, ToolResult

logger = logging.getLogger(__name__)


def _call_signature(call: ToolCall) -> str:
    """工具调用的指纹，用于死循环检测。

    【踩坑修正】最初只对已解析的 `call.arguments` 做哈希。问题是模型吐出
    非法 JSON 时 `arguments` 会退化成空 dict，于是**参数完全不同的非法调用
    会得到同一个指纹**，被误判成"反复调同一个工具"而提前中止——
    本该触发模型自我修正的场景，反而变成了硬失败。

    修正：解析成功时用规范化 JSON（键顺序无关），失败时退回原始字符串。
    """
    if call.arguments:
        payload = json.dumps([call.name, call.arguments], sort_keys=True, ensure_ascii=False)
    else:
        payload = f"{call.name}|{call.raw_arguments.strip()}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class Agent:
    """一个可复用的 Agent 实例。

    设计上是**无状态的**：对话历史由调用方传入、由调用方保存。
    这样同一个 Agent 实例可以并安全地服务多个会话，
    也方便后面把状态挪到 Redis（P3）而不用改内核。
    """

    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        settings: AgentSettings,
        *,
        system_prompt: str | None = None,
        memory: ConversationMemory | None = None,
        long_term: LongTermMemory | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._s = settings
        # 【为什么默认值是 None 而不是 SYSTEM_PROMPT】
        # 写 `system_prompt: str = SYSTEM_PROMPT` 会让默认值在**模块导入时**
        # 就固定下来 —— 那时还没有配置、也不知道注册了哪些工具。
        # 于是"按 profile 选提示词"和"按实际工具裁剪提示词"两件事都做不了。
        #
        # 改成 None 之后，解析发生在 `__init__`（运行时），
        # 一个地方决定，所有调用点自动拿到正确的提示词 ——
        # **不需要每个构造 Agent 的地方都记得传 profile**。
        # 后者正是那种"加了新调用点就忘了传"的典型漏洞。
        # 【为什么用 settings.profile 而不是 get_settings().agent.profile】
        #
        # 我第一版写的是后者（读全局单例），后果是：构造函数里的
        # `settings: AgentSettings` 参数**在 profile 上完全失效** ——
        # `AgentSettings(profile="jobhunt")` 会被静默忽略，拿到的还是全局配置。
        #
        # 这是个很危险的设计：参数看起来能控制行为，实际不能，而且不报错。
        # 任何"想给某个 Agent 单独指定形态"的写法（评测里对比两种 profile、
        # 测试里跑特定形态）都会得到错误结果却毫无提示。
        #
        # **参数既然存在，就必须真的起作用** —— 否则它是陷阱而不是接口。
        self._system_prompt = system_prompt or build_system_prompt(
            self._s.profile, set(tools.names())
        )

        # ---------- 记忆（可选） ----------
        # 不传就保持 P1 的无状态行为（历史由调用方传入）。
        # 这样既向后兼容，也让"有记忆/无记忆"成为可对比的实验条件 ——
        # 记忆的价值同样应该被度量，而不是默认它有用。
        self._memory = memory
        self._long_term = long_term

        # ---------- 上下文预算（技术债 T09） ----------
        # `protect_prefix=1` 保护系统提示；长期记忆紧随其后，也一并保护 ——
        # 裁掉"关于用户的稳定事实"比裁掉一轮旧对话损失更大（前者跨会话复用）。
        self._context_budget = ContextBudget(
            settings.context_token_budget,
            protect_prefix=2 if long_term is not None else 1,
        )
        if self._context_budget.enabled:
            logger.info(
                "上下文预算：%d token（估算器 %s），超出时从最早的对话轮次开始丢弃",
                settings.context_token_budget,
                tokenizer_name(),
            )

    # ============================================================
    # 主入口 A：流式（给 UI 用）
    # ============================================================
    async def run_stream(
        self,
        user_input: str,
        history: Sequence[ChatMessage] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """执行一轮对话，边执行边产出事件。

        `history` 是**之前轮次**的消息（不含本轮）。本轮内部的
        工具调用消息不会写回 history —— 它们是"思考过程"，
        对后续轮次没有价值，留着只会持续消耗 token。
        """
        # 组装上下文：系统提示 + 长期记忆 + 短期记忆 + 本轮输入
        #
        # 【顺序为什么是这个顺序】
        # 1. 系统提示必须在最前（角色设定优先于一切）
        # 2. 长期记忆（跨会话的事实/偏好）紧随其后：它是稳定的背景，不是对话内容
        # 3. 短期记忆（近期对话或摘要）：越接近当前的问题，模型越应该参考
        # 4. 本轮输入在最后
        messages: list[ChatMessage] = [ChatMessage.system(self._system_prompt)]

        if self._long_term is not None:
            if recalled := self._long_term.as_context(user_input, k=3):
                messages.append(
                    ChatMessage.system(f"【关于该用户的已知信息（长期记忆）】\n{recalled}")
                )

        if self._memory is not None:
            # 有短期记忆时，历史由记忆模块统一提供（它内部做了窗口与摘要）
            messages.extend(await self._memory.abuild_context())
        elif history:
            messages.extend(history)

        messages.append(ChatMessage.user(user_input))

        total_usage = Usage()
        tool_trace: list[dict[str, object]] = []
        recent_signatures: list[str] = []

        # ---------- 单轮总时长预算（技术债 T15） ----------
        # 用 loop.time() 而不是 time.time()：前者是**单调时钟**，
        # 不受系统时间调整（NTP 校时、夏令时、用户改表）影响。
        # 用挂钟时间算 deadline 的话，一次系统校时就可能让预算瞬间"到期"，
        # 或者干脆永远不到期 —— 这类 bug 只在特定时刻出现，几乎无法复现。
        loop = asyncio.get_running_loop()
        budget = self._s.run_timeout
        deadline = (loop.time() + budget) if budget > 0 else None

        yield AgentEvent(type=EventType.START, content=user_input)

        for step in range(1, self._s.max_steps + 1):
            # 每步开始先看一眼预算。**这是一个防御性护栏，不是主机制**：
            # 每一跳都被"剩余预算"包着（见下面两处 asyncio.timeout），
            # 所以正常路径下这里不会触发 —— 它防的是"将来有人加了一条
            # 没被包住的 await"。有它，那种改动最坏是"多花一步"，
            # 而不是"彻底失去时间上限"。
            if deadline is not None and loop.time() >= deadline:
                # 这是**本步开始之前**的护栏：此刻还没算过本步的上下文裁剪，
                # 所以 trim 传 None。传上一轮遗留的值会报出一个与本次终止
                # 无关的裁剪结论 —— 那比不报更糟。
                for event in self._budget_exhausted(
                    step, total_usage, budget, tool_trace=tool_trace, trim=None
                ):
                    yield event
                return

            yield AgentEvent(type=EventType.STEP, step=step)

            # ---------- 0. 上下文预算（技术债 T09） ----------
            # 放在**每一步的模型调用之前**，而不是只在开头做一次：
            # 上下文是在循环里长大的（每一步都可能追加几千字的工具观察），
            # 只在开头检查等于没检查 —— 增长全发生在后面。
            #
            # 未启用（budget=0）时 `fit()` 会立刻返回，只多一次 token 计数。
            messages, trim = self._context_budget.fit(messages)

            # ---------- 1. 调用模型（流式） ----------
            accumulator = StreamAccumulator()
            try:
                # `asyncio.timeout(None)` 是合法的空操作，所以这里不需要分支：
                # 有预算就用剩余时间包住这一步，没有就原样跑。
                # 【为什么用 asyncio.timeout 而不是 wait_for】
                # 这一步消费的是一个异步迭代器，wait_for 只能包住单个 await，
                # 要包住 "async for" 得手写一层协程；timeout 是上下文管理器，
                # 直接套住整段循环，而且它的取消会正确传播进 stream_chat。
                async with asyncio.timeout(self._remaining(deadline, loop.time())):
                    async for delta in self._llm.stream_chat(
                        messages, tools=self._tools.schemas() or None
                    ):
                        accumulator.feed(delta)
                        # 文本增量实时吐给前端 —— 这就是打字机效果的来源
                        if delta.content:
                            yield AgentEvent(type=EventType.TOKEN, step=step, content=delta.content)
            except TimeoutError:
                # 【必须在 `except Exception` 之前】
                # TimeoutError 也是 Exception 的子类，放在后面就会被当成
                # "模型调用失败"，于是终止原因变成 error —— 而"预算用完"
                # 是可预期的运行结果，不是故障。把它算进错误率会让监控失真。
                logger.warning("第 %d 步超出单轮总时长预算（%.1fs）", step, budget)
                for event in self._budget_exhausted(
                    step, total_usage, budget, tool_trace=tool_trace, trim=trim
                ):
                    yield event
                return
            except Exception as exc:
                logger.exception("第 %d 步模型调用失败", step)
                for event in self._finish(
                    stopped_reason="error",
                    steps_used=step,
                    usage=total_usage,
                    step=step,
                    error=str(exc),
                    tool_trace=tool_trace,
                    trim=trim,
                ):
                    yield event
                return

            total_usage = total_usage + accumulator.usage
            # 【估算器的精度：测量而不是声称】
            # 模型回来的 `prompt_tokens` 是**权威值**，而我们知道自己发了多少
            # 估算 token（本步开头的 fit() 算过，存在 trim.after_tokens 里）。
            # 每次调用都记一对数字，/api/metrics 里两个累加计数器一除，
            # 就是估算器在**真实流量**上的整体偏差倍数。
            #
            # 为什么不写死"误差 < 10%"这种话：那种数字没有依据，换一个模型族
            # 或换一种输入分布就不成立。让它成为可观测的指标，偏了能看见。
            if accumulator.usage.prompt_tokens:
                ratio = record_prompt_estimate(
                    trim.after_tokens or 0, accumulator.usage.prompt_tokens
                )
                if ratio is not None:
                    logger.debug(
                        "第 %d 步：上下文估算 %d token，实际 %d token（%.2f×）",
                        step,
                        trim.after_tokens,
                        accumulator.usage.prompt_tokens,
                        ratio,
                    )
            assistant_msg = accumulator.build_message()
            messages.append(
                assistant_msg
            )  # 关键：assistant 消息必须入列，否则 tool 消息没有配对父节点

            # ---------- 2. 分支：结束还是继续 ----------
            tool_calls = assistant_msg.tool_calls

            if not tool_calls:
                # 模型认为可以回答了 —— 正常终止
                answer = accumulator.content
                if not answer.strip():
                    answer = "（模型返回了空回复，请重试或换一种问法）"

                # 只把**成功的最终回答**写入短期记忆。
                # 被预算掐断或死循环中止的轮次不写：它们不是有效上下文，
                # 写进去只会让后续对话基于半成品推理。
                #
                # 工具摘要跟着一起写（技术债 T07）：下一轮的历史里会出现
                # 一行"我查过什么"，这样模型不会把同一个工具再查一遍 ——
                # 这是"禁止 tool 消息入历史"（ADR-006）留下的缺口的补法。
                if self._memory is not None:
                    self._memory.add_turn(
                        user_input, answer, tool_summary=summarize_tools(tool_trace)
                    )

                yield AgentEvent(type=EventType.FINAL, step=step, content=answer)
                for event in self._finish(
                    stopped_reason="finished",
                    steps_used=step,
                    usage=total_usage,
                    step=step,
                    tool_trace=tool_trace,
                    trim=trim,
                ):
                    yield event
                return

            # ---------- 3. 死循环检测 ----------
            for call in tool_calls:
                recent_signatures.append(_call_signature(call))
            if len(recent_signatures) >= self._s.loop_guard:
                window = recent_signatures[-self._s.loop_guard :]
                if len(set(window)) == 1:
                    msg = (
                        f"检测到重复调用同一个工具（{tool_calls[0].name}）"
                        f"{self._s.loop_guard} 次且参数完全相同，已中止以避免浪费额度。"
                        f"建议换一种问法，或补充更多信息。"
                    )
                    logger.warning(msg)
                    for event in self._finish(
                        stopped_reason="loop_detected",
                        steps_used=step,
                        usage=total_usage,
                        step=step,
                        error=msg,
                        tool_trace=tool_trace,
                        trim=trim,
                    ):
                        yield event
                    return

            # ---------- 4. 预算检查：最后一步的工具调用没有意义 ----------
            # 工具的执行结果只能通过"回灌给模型"产生价值。如果这一步已经是最后一步，
            # 观察结果永远不会被消费，执行它纯属浪费（读大文件、调外部 API 都可能很贵）。
            # 提前 break 到预算耗尽分支，既省钱又能给出更准确的终止原因。
            if step >= self._s.max_steps:
                break

            # ---------- 5. 执行工具 ----------
            #
            # 【为什么并发，以及并发的三条边界】
            # 一个模型回合里可能有多个互不依赖的调用（"看看 A，再看看 B"）。
            # 串行执行让延迟线性叠加：3 个各 1 秒的工具就是 3 秒，而它们之间
            # 没有任何数据依赖 —— 这是本项目最容易拿到的性能收益。
            #
            # 并发要成立，必须同时守住三件事：
            #   1. **结果顺序不变**：回灌给模型的 tool 消息仍按 tool_calls 的
            #      原始顺序排列。协议只要求 tool_call_id 能对上，但顺序决定了
            #      上下文里"先做了什么"的叙事，也让同样的输入产生同样的上下文 ——
            #      并发不该让行为变得不可复现。
            #   2. **失败隔离**：一个工具出问题只影响它自己。`gather` 在这里用
            #      真值而不是 return_exceptions=True，因为每条调用都已经被
            #      `_execute_one` 包住并转成了 ToolResult —— 隔离发生在那里，
            #      而 CancelledError 是 BaseException，不会被吞掉，
            #      所以"用户断开连接"仍然能立刻取消整批工具。
            #   3. **有上限**：见 config.py 的 tool_concurrency。
            #
            # 【有副作用的工具会让整个回合退回串行】
            # 只要本回合里有任何一个调用命中声明了 `serial` 的工具，就不并发。
            # 混合策略（只读并发、写入串行）需要调度保证，而这个循环给不出 ——
            # 与其写一个"大部分情况下正确"的聪明策略，不如写一个显然正确的笨策略。
            for call in tool_calls:
                yield AgentEvent(
                    type=EventType.TOOL_CALL,
                    step=step,
                    tool_name=call.name,
                    tool_args=call.arguments,
                )

            if any(self._tools.is_serial(call.name) for call in tool_calls):
                logger.debug("第 %d 步含不可并发的工具，整段串行执行", step)
                pending = self._execute_serially(tool_calls)
            else:
                pending = self._execute_batch(tool_calls)

            # 工具执行同样受总预算约束：某一步的工具卡住时，
            # 光有单工具超时是不够的（3 个各 30s 的工具就是 90s）
            results = await self._run_with_budget(pending, deadline, loop, step)
            if results is None:
                for event in self._budget_exhausted(
                    step, total_usage, budget, tool_trace=tool_trace, trim=trim
                ):
                    yield event
                return

            # 按**原始顺序**回灌（不是完成顺序）—— 见上面第 1 条
            for call, result in zip(tool_calls, results, strict=True):
                tool_trace.append(
                    {
                        "step": step,
                        "name": call.name,
                        "args": call.arguments,
                        "ok": result.ok,
                        "duration_ms": result.duration_ms,
                        # 观察结果的**字符数**：工具摘要里的"约 1.2k 字"靠它。
                        # 传字符而不是 token：摘要是给模型看的一句话，
                        # 字符规模已经足够表达"这次查回来多少东西"。
                        "chars": len(result.content or ""),
                    }
                )

                yield AgentEvent(
                    type=EventType.TOOL_RESULT,
                    step=step,
                    tool_name=call.name,
                    tool_ok=result.ok,
                    content=result.content,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                )

                # 关键：把观察结果作为 role=tool 的消息回灌，并用 tool_call_id 配对
                messages.append(
                    ChatMessage.tool_result(
                        tool_call_id=call.id,
                        content=result.as_observation(),
                        name=call.name,
                    )
                )

        # ---------- 预算耗尽 ----------
        msg = (
            f"已达到单轮最大步数限制（{self._s.max_steps} 步）仍未得到最终答案。"
            f"这通常意味着任务被拆得太碎或工具没提供有效信息。"
            f"已调用工具：{'、'.join(str(t['name']) for t in tool_trace) or '无'}。"
        )
        logger.warning(msg)
        for event in self._finish(
            stopped_reason="max_steps",
            steps_used=self._s.max_steps,
            usage=total_usage,
            error=msg,
            tool_trace=tool_trace,
            trim=trim,
        ):
            yield event
        return

    # ============================================================
    # 只读视图
    # ============================================================
    @property
    def tool_briefs(self) -> list[dict[str, str]]:
        """已注册工具的简表（名字 + 描述），给 CLI 的 `/tools` 用。

        【为什么要专门开一个只读入口，而不是让调用方读 `agent._tools`】
        原来的 CLI 直接读 `agent._tools`（技术债 T12）。代价不是"不优雅"，
        而是**Agent 一旦调整内部结构就会连带改坏 CLI，而这种破坏不会出现在
        Agent 自己的测试里** —— 它会在别人运行 CLI 时以 AttributeError 的形式出现。

        暴露一个只读入口之后，内部怎么存（注册表？字典？懒加载？）
        就成了 Agent 自己的事。这也是"接口比实现小"的一个具体例子。
        """
        out: list[dict[str, str]] = []
        for schema in self._tools.schemas():
            fn = schema["function"]
            out.append({"name": str(fn["name"]), "description": str(fn["description"])})
        return out

    # ============================================================
    # 工具执行
    # ============================================================
    async def _execute_one(self, call: ToolCall) -> ToolResult:
        """执行一次工具调用，并把**任何**异常都转成失败的观察结果。

        【为什么这里还要兜一层】
        `ToolRegistry.execute` → `Tool.execute` 已经把参数校验失败、超时、
        工具内部异常都转成了 ToolResult。但"工具层之上"仍然可能出错
        （注册表被替换、工具对象本身有 bug）。串行时这种异常会直接抛出，
        与"工具失败"的区分还算清楚；**并发之后不一样了**：
        gather 遇到异常会取消同批的其他调用，于是一个工具的小毛病
        会让同一批里本来能成功的调用一起失败 —— 失败的传播范围被放大了。

        所以并发路径上必须有这一层，把异常就地收敛成"这一条失败了"。
        `CancelledError` 不在捕获范围内（它继承 BaseException）：用户断开
        连接时必须能立刻取消整批工具，而不是把它们一个个跑完。
        """
        try:
            return await self._tools.execute(call)
        except Exception as exc:
            logger.exception("工具 %s 执行时抛出未处理异常", call.name)
            return ToolResult.failure(f"工具 {call.name} 执行时发生未预期错误：{exc}")

    async def _execute_batch(self, calls: Sequence[ToolCall]) -> list[ToolResult]:
        """并发执行一批工具调用，返回**与入参同序**的结果。"""
        limit = max(1, min(self._s.tool_concurrency, len(calls)))
        gate = asyncio.Semaphore(limit)

        async def guarded(call: ToolCall) -> ToolResult:
            async with gate:
                return await self._execute_one(call)

        return list(await asyncio.gather(*(guarded(call) for call in calls)))

    async def _execute_serially(self, calls: Sequence[ToolCall]) -> list[ToolResult]:
        """串行执行（本回合里有 `serial` 工具时走这条路）。"""
        return [await self._execute_one(call) for call in calls]

    # ============================================================
    # 单轮总时长预算（T15）
    # ============================================================
    @staticmethod
    def _remaining(deadline: float | None, now: float) -> float | None:
        """还剩多少预算。`None` 表示不限制（`asyncio.timeout(None)` 是合法的）。"""
        if deadline is None:
            return None
        # 不允许负数：asyncio.timeout 收到负数会立刻超时，语义上正确，
        # 但显式夹到 0 更清楚地表达"已经用完了"
        return max(0.0, deadline - now)

    async def _run_with_budget(
        self,
        coro: Awaitable[list[ToolResult]],
        deadline: float | None,
        loop: asyncio.AbstractEventLoop,
        step: int,
    ) -> list[ToolResult] | None:
        """在剩余预算内等一批工具执行完；超预算返回 None（由调用方收尾）。

        【为什么返回值是 None 而不是抛异常】
        超时在这里是一个**正常的终止原因**，不是一个需要向上冒泡的错误。
        用返回值表达它，调用方就没法"忘记处理" —— 类型上就必须想一下
        `None` 是什么意思（漏掉的话，下一步会拿 None 当结果列表用，
        那会是一句毫无线索的 TypeError）。
        """
        try:
            async with asyncio.timeout(self._remaining(deadline, loop.time())):
                return await coro
        except TimeoutError:
            logger.warning("第 %d 步的工具执行超出单轮总时长预算", step)
            return None

    def _budget_exhausted(
        self,
        step: int,
        usage: Usage,
        budget: float,
        *,
        tool_trace: Sequence[Mapping[str, object]] | None = None,
        trim: TrimReport | None = None,
    ) -> list[AgentEvent]:
        """预算用尽时的终结事件：说明"用完了多少、做到第几步、怎么放宽"。

        也带上工具摘要与裁剪报告：超时的轮次里，"它当时在查什么"恰恰是最有用
        的信息（否则用户只看到"超时了"，不知道卡在哪一步）。
        """
        msg = (
            f"已达单轮总时长预算（{budget:.0f} 秒），在第 {step} 步中止。"
            f"这通常意味着某一步的外部调用（模型或工具）耗时远超预期，"
            f"或这个任务本身就需要更长时间。"
            f"如确有必要，请在配置里调大 AGENT_RUN_TIMEOUT（0 表示不限制）。"
        )
        return self._finish(
            stopped_reason="timeout",
            steps_used=step,
            usage=usage,
            step=step,
            error=msg,
            tool_trace=tool_trace,
            trim=trim,
        )

    # ============================================================
    # 终结事件
    # ============================================================
    def _finish(
        self,
        *,
        stopped_reason: str,
        steps_used: int,
        usage: Usage,
        step: int = 0,
        error: str | None = None,
        tool_trace: Sequence[Mapping[str, object]] | None = None,
        trim: TrimReport | None = None,
    ) -> list[AgentEvent]:
        """一个轮次的**终结事件序列**：可选的 error + 必定有的 done。

        【为什么必须收敛到一处】
        原来是四个 return 分支各自手写"先 error 再 done"。这个约定只存在于
        写代码的人的脑子里，而它有两个后果：
          · 新增一个终止分支时漏发 done → 前端一直转圈等一个永远不来的终态，
            而且**不会有任何报错**（它只是在等）；
          · 反过来多发一个 done → 调用方可能把中间状态当成结束。

        现在所有出口都必须经过这里，`done` 的存在成了结构性事实而不是纪律。
        对应的回归测试见 test_agent_loop.py 的"终态不变量"。

        【为什么工具摘要与裁剪报告也从这里带上】
        它们是"这一轮实际发生了什么"的一部分，而每个出口都该说清楚 ——
        尤其是被预算掐断或出错的那几个出口：那几轮的摘要恰恰最有用
        （比如"reindex 调了三次都失败"）。
        """
        events: list[AgentEvent] = []
        if error:
            events.append(AgentEvent(type=EventType.ERROR, step=step, content=error))
        events.append(
            AgentEvent(
                type=EventType.DONE,
                step=step,
                steps_used=steps_used,
                usage=usage,
                stopped_reason=stopped_reason,
                tool_summary=summarize_tools(tool_trace or []),
                context_trimmed=bool(trim and trim.trimmed),
                context_tokens=trim.after_tokens if trim else 0,
            )
        )
        return events

    # ============================================================
    # 主入口 B：非流式（给程序调用 / 测试用）
    # ============================================================
    async def run(
        self,
        user_input: str,
        history: Sequence[ChatMessage] | None = None,
    ) -> AgentRunResult:
        """把流式事件收集成一个完整结果。

        **复用同一份循环逻辑**，不重复实现 —— 这是"单一事实来源"原则。
        如果流式和非流式各写一遍，两边行为迟早不一致，这是很难查的 bug。
        """
        answer_parts: list[str] = []
        steps_used = 0
        usage = Usage()
        tool_calls: list[dict[str, object]] = []
        error: str | None = None
        stopped = "finished"

        async for event in self.run_stream(user_input, history):
            match event.type:
                case EventType.TOKEN:
                    answer_parts.append(event.content)
                case EventType.FINAL:
                    answer_parts = [event.content]  # final 是权威答案，覆盖增量拼接结果
                case EventType.TOOL_CALL:
                    tool_calls.append({"name": event.tool_name, "args": event.tool_args})
                case EventType.ERROR:
                    error = event.content
                    # 注意：这里**不**设置 stopped_reason。
                    # 步数耗尽与死循环也会发 ERROR 事件，但它们不是故障。
                    # 以 DONE 事件上的 stopped_reason 为权威来源，
                    # 否则指标统计会把"正常预算终止"算成"错误"。
                case EventType.DONE:
                    steps_used = event.steps_used
                    usage = event.usage or Usage()
                    stopped = event.stopped_reason

        return AgentRunResult(
            answer="".join(answer_parts),
            steps_used=steps_used,
            usage=usage,
            tool_calls=tool_calls,
            stopped_reason=stopped,
            error=error,
        )
