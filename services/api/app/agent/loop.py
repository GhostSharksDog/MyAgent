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
from app.agent.prompts import SYSTEM_PROMPT
from app.core.config import AgentSettings
from app.llm.client import LLMClient, StreamAccumulator
from app.llm.types import ChatMessage, ToolCall, Usage
from app.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


def _call_signature(call: ToolCall) -> str:
    """工具调用的指纹，用于死循环检测。"""
    payload = json.dumps([call.name, call.arguments], sort_keys=True, ensure_ascii=False)
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
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._s = settings
        self._system_prompt = system_prompt

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
        # 组装上下文：系统提示 + 历史 + 本轮输入
        messages: list[ChatMessage] = [ChatMessage.system(self._system_prompt)]
        if history:
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
                yield AgentEvent(type=EventType.DONE, step=step, steps_used=step, usage=total_usage)
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
                yield AgentEvent(type=EventType.FINAL, step=step, content=answer)
                yield AgentEvent(type=EventType.DONE, step=step, steps_used=step, usage=total_usage)
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
                        type=EventType.DONE, step=step, steps_used=step, usage=total_usage
                    )
                    return

            # ---------- 4. 执行工具 ----------
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
        yield AgentEvent(type=EventType.DONE, steps_used=self._s.max_steps, usage=total_usage)

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
                    stopped = "error"
                case EventType.DONE:
                    steps_used = event.steps_used
                    usage = event.usage or Usage()

        return AgentRunResult(
            answer="".join(answer_parts),
            steps_used=steps_used,
            usage=usage,
            tool_calls=tool_calls,
            stopped_reason=stopped,
            error=error,
        )
