"""每轮共享的 deadline、对话模型用量与上下文记录。

子 Agent 继承当前运行上下文，独立请求创建新实例。ContextVar 只在驱动
生成器的一次 await 内绑定，yield 给调用方前还原，避免污染调用方或跨任务 reset。
token 上限按已经返回的 Usage 检查；在途调用可能超额，不是费用硬上限。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from app.agent.context import ContextBudget, summarize_tools
from app.agent.events import AgentEvent, EventType
from app.core.config import AgentSettings
from app.llm.types import ChatMessage, ChatResponse, Role, StreamDelta, Usage

logger = logging.getLogger(__name__)
_current: ContextVar[RunContext | None] = ContextVar("agent_run_context", default=None)


class RunBudgetExceeded(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class RunContext:
    timeout: float
    context_budget: int
    token_limit: int = 0
    deadline: float | None = None
    usage: Usage = field(default_factory=Usage)
    usage_complete: bool = True
    context_trimmed: bool = False
    context_tokens: int = 0
    tool_trace: list[dict[str, object]] = field(default_factory=list)
    observer: Callable[[AgentEvent, bool, RunContext], None] | None = field(
        default=None, repr=False, compare=False
    )

    @classmethod
    def create(cls, settings: AgentSettings, *, token_limit: int = 0) -> RunContext:
        deadline = (
            asyncio.get_running_loop().time() + settings.run_timeout
            if settings.run_timeout > 0
            else None
        )
        return cls(settings.run_timeout, settings.context_token_budget, token_limit, deadline)

    def check(self) -> None:
        self.check_time()
        if self.token_limit > 0 and self.usage.total_tokens >= self.token_limit:
            raise RunBudgetExceeded(
                "token_budget",
                f"已达整轮累计 token 预算（{self.token_limit}，已知用量 {self.usage.total_tokens}）。"
                "已停止后续模型调用；可调大 AGENT_PLAN_MAX_TOTAL_TOKENS / "
                "AGENT_MULTI_MAX_TOTAL_TOKENS。已在途调用可能使最终用量超过阈值。",
            )

    def check_time(self) -> None:
        if self.deadline is not None and asyncio.get_running_loop().time() >= self.deadline:
            raise self.timeout_error()

    def timeout_error(self) -> RunBudgetExceeded:
        return RunBudgetExceeded(
            "timeout",
            f"已达整轮总时长预算（{self.timeout:g} 秒），已停止后续步骤。"
            "如需更长时间，请调大 AGENT_RUN_TIMEOUT（0 表示不限制）。",
        )

    def fit(self, messages: Sequence[ChatMessage]) -> list[ChatMessage]:
        prefix = 0
        for message in messages:
            if message.role is not Role.SYSTEM:
                break
            prefix += 1
        fitted, report = ContextBudget(self.context_budget, protect_prefix=prefix).fit(messages)
        self.context_trimmed |= report.trimmed
        self.context_tokens = max(self.context_tokens, report.after_tokens)
        return fitted

    def observe(self, event: AgentEvent) -> None:
        self.context_trimmed |= event.context_trimmed
        self.context_tokens = max(self.context_tokens, event.context_tokens)
        if event.type is EventType.TOOL_RESULT:
            self.tool_trace.append(
                {
                    "name": event.tool_name or "未知工具",
                    "ok": event.tool_ok,
                    "chars": len(event.content),
                }
            )

    def decorate(self, event: AgentEvent, *, root: bool) -> AgentEvent:
        decorated = self._decorate(event, root=root)
        if self.observer is not None:
            self.observer(decorated, root, self)
        return decorated

    def _decorate(self, event: AgentEvent, *, root: bool) -> AgentEvent:
        if event.type is not EventType.DONE:
            return event
        return event.model_copy(
            update={
                "usage": self.usage.model_copy() if root else event.usage,
                "usage_complete": self.usage_complete,
                "context_trimmed": self.context_trimmed,
                "context_tokens": self.context_tokens,
                "tool_summary": summarize_tools(self.tool_trace) if root else event.tool_summary,
            }
        )


def current_run_context() -> RunContext | None:
    return _current.get()


class RunLLM:
    """不持有运行状态的客户端适配器，账本来自当前驱动该调用的 RunContext。"""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        context = current_run_context()
        if context is None:
            return await self.client.chat(messages, **kwargs)
        context.check()
        fitted = context.fit(messages)
        context.check()
        try:
            response = await self.client.chat(fitted, **kwargs)
        except BaseException:
            context.usage_complete = False
            raise
        context.usage = context.usage + response.usage
        context.usage_complete &= response.usage_complete
        return response

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamDelta]:
        context = current_run_context()
        if context is not None:
            context.check()
            messages = context.fit(messages)
            context.check()
        latest = Usage()
        saw_usage = False
        completed = False
        source = self.client.stream_chat(messages, tools=tools, **kwargs)
        try:
            async with aclosing(source):
                async for delta in source:
                    if context is not None and delta.usage is not None:
                        # 兼容端点可能重复发送累计 usage，只记增量。
                        usage = delta.usage
                        context.usage = context.usage + Usage(
                            prompt_tokens=max(0, usage.prompt_tokens - latest.prompt_tokens),
                            completion_tokens=max(
                                0, usage.completion_tokens - latest.completion_tokens
                            ),
                            total_tokens=max(0, usage.total_tokens - latest.total_tokens),
                        )
                        latest = usage
                        saw_usage = True
                        context.usage_complete &= delta.usage_complete
                    yield delta
                completed = True
        finally:
            if context is not None and not (saw_usage and completed):
                context.usage_complete = False


def guard_llm(client: Any) -> RunLLM:
    return client if isinstance(client, RunLLM) else RunLLM(client)


async def guarded_stream(
    source: AsyncIterator[AgentEvent],
    context: RunContext,
) -> AsyncIterator[AgentEvent]:
    """统一预算出口。清理不再受已耗尽的 deadline 限制，取消继续透传。"""
    root = current_run_context() is None
    parts: list[str] = []
    conclusions: list[str] = []
    plan: dict[str, Any] | None = None
    step = 0
    saw_final = False
    try:
        while True:
            token = _current.set(context)
            try:
                context.check_time()
                timeout = asyncio.timeout_at(context.deadline)
                try:
                    async with timeout:
                        event = await anext(source)
                except TimeoutError:
                    if timeout.expired():
                        raise context.timeout_error() from None
                    raise
            finally:
                _current.reset(token)
            context.observe(event)
            step = max(step, event.step, event.steps_used)
            if event.plan is not None:
                plan = event.plan
            if event.type is EventType.TOKEN:
                parts.append(event.content)
            if event.type is EventType.DELEGATE_RESULT and event.tool_ok:
                conclusions.append(f"### {event.specialist}\n{event.content}")
            if event.type is EventType.FINAL:
                saw_final = True
            yield context.decorate(event, root=root)
            if event.type is EventType.DONE:
                break
    except StopAsyncIteration:
        # 无终态的生成器不是成功，调用方必须能结束等待并知道结果不完整。
        yield context.decorate(
            AgentEvent(type=EventType.ERROR, content="执行流提前结束；请检查服务日志后重试。"),
            root=root,
        )
        yield context.decorate(AgentEvent(type=EventType.DONE, stopped_reason="error"), root=root)
    except RunBudgetExceeded as exc:
        token = _current.set(context)
        try:
            await source.aclose()  # type: ignore[attr-defined]
        finally:
            _current.reset(token)
        logger.warning("运行预算终止：%s", exc)
        if plan:
            for item in plan.get("steps", []):
                if item.get("status") == "done" and item.get("result"):
                    conclusions.append(f"步骤 {item['id']}：{item['result']}")
                elif item.get("status") in {"pending", "running"}:
                    item.update(status="skipped", error=str(exc))
            yield context.decorate(
                AgentEvent(type=EventType.PLAN_STEP, plan=plan, content=str(exc), step=step),
                root=root,
            )
        partial = "\n\n".join(conclusions) or "".join(parts)
        if not saw_final:
            answer = f"任务未完成：{exc}"
            if partial:
                answer += f"\n\n已获得的部分结果：\n{partial}"
            yield AgentEvent(type=EventType.FINAL, content=answer, step=step)
        yield context.decorate(
            AgentEvent(type=EventType.ERROR, content=str(exc), step=step), root=root
        )
        yield context.decorate(
            AgentEvent(
                type=EventType.DONE,
                stopped_reason=exc.reason,
                steps_used=step,
                plan=plan,
                usage=context.usage.model_copy(),
            ),
            root=root,
        )
    except Exception as exc:
        logger.exception("执行流异常")
        token = _current.set(context)
        try:
            await source.aclose()  # type: ignore[attr-defined]
        finally:
            _current.reset(token)
        yield context.decorate(
            AgentEvent(type=EventType.ERROR, content=f"执行失败：{exc}；请检查日志后重试。"),
            root=root,
        )
        yield context.decorate(AgentEvent(type=EventType.DONE, stopped_reason="error"), root=root)
        return
    finally:
        token = _current.set(context)
        try:
            await source.aclose()  # type: ignore[attr-defined]
        finally:
            _current.reset(token)
