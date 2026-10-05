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
本版本工具**串行**执行。模型一次返回多个 tool_calls 时，串行最简单也最好懂。
但如果多个工具互不依赖（如同时查三个城市的岗位），并发执行能把延迟
从 3×T 降到 1×T —— 这是 P3 的优化项，届时会用 asyncio.gather 改造。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Sequence

from app.agent.events import AgentEvent, AgentRunResult, EventType
from app.agent.memory import ConversationMemory, LongTermMemory
from app.agent.prompts import build_system_prompt
from app.core.config import AgentSettings, get_settings
from app.llm.client import LLMClient, StreamAccumulator
from app.llm.types import ChatMessage, ToolCall, Usage
from app.tools.base import ToolRegistry

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
        self._system_prompt = system_prompt or build_system_prompt(
            get_settings().agent.profile, set(tools.names())
        )

        # ---------- 记忆（可选） ----------
        # 不传就保持 P1 的无状态行为（历史由调用方传入）。
        # 这样既向后兼容，也让"有记忆/无记忆"成为可对比的实验条件 ——
        # 记忆的价值同样应该被度量，而不是默认它有用。
        self._memory = memory
        self._long_term = long_term

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

        yield AgentEvent(type=EventType.START, content=user_input)

        for step in range(1, self._s.max_steps + 1):
            yield AgentEvent(type=EventType.STEP, step=step)

            # ---------- 1. 调用模型（流式） ----------
            accumulator = StreamAccumulator()
            try:
                async for delta in self._llm.stream_chat(
                    messages, tools=self._tools.schemas() or None
                ):
                    accumulator.feed(delta)
                    # 文本增量实时吐给前端 —— 这就是打字机效果的来源
                    if delta.content:
                        yield AgentEvent(type=EventType.TOKEN, step=step, content=delta.content)
            except Exception as exc:
                logger.exception("第 %d 步模型调用失败", step)
                yield AgentEvent(type=EventType.ERROR, step=step, content=str(exc))
                yield AgentEvent(
                    type=EventType.DONE,
                    step=step,
                    steps_used=step,
                    usage=total_usage,
                    stopped_reason="error",
                )
                return

            total_usage = total_usage + accumulator.usage
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
                if self._memory is not None:
                    self._memory.add_turn(user_input, answer)

                yield AgentEvent(type=EventType.FINAL, step=step, content=answer)
                yield AgentEvent(
                    type=EventType.DONE,
                    step=step,
                    steps_used=step,
                    usage=total_usage,
                    stopped_reason="finished",
                )
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
                    yield AgentEvent(type=EventType.ERROR, step=step, content=msg)
                    yield AgentEvent(
                        type=EventType.DONE,
                        step=step,
                        steps_used=step,
                        usage=total_usage,
                        stopped_reason="loop_detected",
                    )
                    return

            # ---------- 4. 预算检查：最后一步的工具调用没有意义 ----------
            # 工具的执行结果只能通过"回灌给模型"产生价值。如果这一步已经是最后一步，
            # 观察结果永远不会被消费，执行它纯属浪费（读大文件、调外部 API 都可能很贵）。
            # 提前 break 到预算耗尽分支，既省钱又能给出更准确的终止原因。
            if step >= self._s.max_steps:
                break

            # ---------- 5. 执行工具 ----------
            for call in tool_calls:
                yield AgentEvent(
                    type=EventType.TOOL_CALL,
                    step=step,
                    tool_name=call.name,
                    tool_args=call.arguments,
                )

                result = await self._tools.execute(call)

                tool_trace.append(
                    {
                        "step": step,
                        "name": call.name,
                        "args": call.arguments,
                        "ok": result.ok,
                        "duration_ms": result.duration_ms,
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
        yield AgentEvent(type=EventType.ERROR, content=msg, steps_used=self._s.max_steps)
        yield AgentEvent(
            type=EventType.DONE,
            steps_used=self._s.max_steps,
            usage=total_usage,
            stopped_reason="max_steps",
        )

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
