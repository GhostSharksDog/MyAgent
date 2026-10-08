"""跨模式护栏的反例：预算不能重领，后台任务不能遗留，副作用不能重叠。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from app.agent.events import EventType
from app.agent.loop import Agent
from app.agent.multi import DEFAULT_SPECIALISTS, GENERAL_SPECIALISTS, SupervisorAgent
from app.agent.planning import PlanAndExecuteAgent
from app.agent.runtime import current_run_context
from app.core.config import AgentSettings
from app.llm.client import LLMClient
from app.llm.types import ChatMessage, ChatResponse, StreamDelta, ToolCall, Usage
from app.tools.base import Tool, ToolRegistry, ToolResult
from pydantic import BaseModel


class RuntimeLLM:
    def __init__(self, **delays: float) -> None:
        self.delays = delays
        self.calls: list[str] = []
        self.messages: list[list[ChatMessage]] = []
        self.active = 0
        self.started = asyncio.Event()
        self.closed = 0
        self.missing_usage = False
        self.fail_expert = ""
        self.answer = "可核对的结论"

    async def chat(self, messages: Sequence[ChatMessage], **kw: Any) -> ChatResponse:
        prompt = messages[-1].content or ""
        stage = (
            "replan"
            if "执行中遇到的问题" in prompt
            else "plan"
            if "任务规划器" in prompt
            else ("route" if "任务协调者" in prompt else "synthesis")
        )
        self.calls.append(stage)
        self.messages.append(list(messages))
        await asyncio.sleep(self.delays.get(stage, 0))
        if stage in {"plan", "replan"}:
            content = json.dumps(
                {"steps": [{"description": "获取事实"}, {"description": "核验事实"}]}
            )
        elif stage == "route":
            content = json.dumps({"specialists": [s.name for s in GENERAL_SPECIALISTS]})
        else:
            content = "综合结论"
        return ChatResponse(message=ChatMessage.assistant(content), usage=_usage())

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Any = None,
        **kw: Any,
    ) -> AsyncIterator[StreamDelta]:
        self.calls.append("expert")
        self.messages.append(list(messages))
        self.active += 1
        if self.active == 3:
            self.started.set()
        try:
            await asyncio.sleep(self.delays.get("expert", 0))
            yield StreamDelta(content=self.answer)
            if not self.missing_usage:
                yield StreamDelta(usage=_usage(), finish_reason="stop")
            if self.fail_expert and self.fail_expert in (messages[0].content or ""):
                raise RuntimeError("专家在有已知用量后失败")
        finally:
            self.active -= 1
            self.closed += 1


def _usage() -> Usage:
    return Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10)


@pytest.mark.parametrize("history_backend", ["memory", "sql"])
async def test_sse_disconnect_cancels_and_waits_for_all_experts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    history_backend: str,
) -> None:
    from app.api.routes import chat_stream
    from app.api.schemas import ChatRequest
    from app.core.config import Settings
    from app.session.store import InMemorySessionStore
    from sse_starlette.sse import AppStatus
    from starlette.applications import Starlette
    from starlette.requests import Request

    # SSE 包把退出事件缓存为进程全局，TestClient 与本用例使用不同 event loop。
    # SSE 3.x uses per-loop events; legacy 2.x needs explicit reset.
    if hasattr(AppStatus, "should_exit_event"):
        monkeypatch.setattr(AppStatus, "should_exit_event", None)

    app = Starlette()
    llm = RuntimeLLM(expert=10)
    app.state.llm = llm
    app.state.tools = ToolRegistry()
    app.state.sessions = InMemorySessionStore()
    app.state.settings = Settings(agent=AgentSettings(_env_file=None))
    from app.runs.history import RunHistory

    history_settings = app.state.settings.run_history.model_copy(
        update={"backend": history_backend, "path": str(tmp_path / "runs.db")}
    )
    app.state.run_history = RunHistory(history_settings)
    await app.state.run_history.ensure_ready()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat/stream",
        "headers": [],
        "app": app,
        "client": ("127.0.0.1", 1234),
    }
    response = await chat_stream(ChatRequest(message="核验", mode="multi"), Request(scope))

    async def receive() -> dict[str, str]:
        await llm.started.wait()
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        pass

    await asyncio.wait_for(response(scope, receive, send), timeout=2)
    assert llm.active == 0
    assert llm.closed == 3
    records = app.state.run_history.list()
    assert len(records) == 1
    assert records[0].stopped_reason == "cancelled"
    assert not records[0].usage_complete
    assert records[0].usage.total_tokens > 0  # 路由用量在专家取消后仍保留。
    if history_backend == "sql":
        restarted = RunHistory(history_settings)
        await restarted.ensure_ready()
        assert restarted.list()[0].stopped_reason == "cancelled"
        assert restarted.list()[0].usage.total_tokens == records[0].usage.total_tokens


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_tool_wait_is_in_whole_run_deadline(mode: str) -> None:
    class ToolLLM(RuntimeLLM):
        async def stream_chat(self, messages: Any, **kw: Any) -> AsyncIterator[StreamDelta]:
            self.calls.append("expert")
            yield StreamDelta(
                tool_call_deltas=[
                    {"index": 0, "id": "slow-call", "function": {"name": "slow", "arguments": "{}"}}
                ]
            )
            yield StreamDelta(usage=_usage(), finish_reason="tool_calls")

    class Slow(Tool):
        name = "slow"
        description = "延迟工具"
        params_model = EmptyParams

        async def run(self, args: EmptyParams) -> ToolResult:
            await asyncio.sleep(1)
            return ToolResult.success("结果")

    llm = ToolLLM()
    agent = _agent(mode, llm, run_timeout=0.3)
    agent._tools.register(Slow())
    events = [e async for e in agent.run_stream("核验")]
    assert events[-1].stopped_reason == "timeout"
    assert sum(e.type is EventType.DONE for e in events) == 1
    assert "synthesis" not in llm.calls
    # 工具超时前已经收到完整 Usage，不把已知用量丢掉。
    assert events[-1].usage.total_tokens >= 10


def test_partial_stream_usage_is_not_complete() -> None:
    delta = LLMClient._parse_chunk({"usage": {"prompt_tokens": 7}})
    assert delta.usage and delta.usage.prompt_tokens == 7
    assert delta.usage_complete is False


async def test_token_budget_stops_new_side_effect_after_model_returns() -> None:
    class CallLLM(RuntimeLLM):
        async def stream_chat(self, messages: Any, **kw: Any) -> AsyncIterator[StreamDelta]:
            self.calls.append("expert")
            yield StreamDelta(
                tool_call_deltas=[
                    {"index": 0, "id": "write", "function": {"name": "write", "arguments": "{}"}}
                ]
            )
            yield StreamDelta(usage=_usage(), finish_reason="tool_calls")

    invoked = []

    class Write(Tool):
        name = "write"
        description = "测试副作用"
        params_model = EmptyParams
        serial = True

        async def run(self, params: BaseModel) -> ToolResult:
            invoked.append(True)
            return ToolResult.success("written")

    llm = CallLLM()
    agent = _agent("plan", llm, plan_max_total_tokens=15)
    agent._tools.register(Write())
    result = await agent.run("核验")
    assert result.stopped_reason == "token_budget"
    assert result.usage.total_tokens == 20
    assert not invoked
    assert llm.calls == ["plan", "expert"]


async def test_close_after_token_closes_model_stream_immediately() -> None:
    llm = RuntimeLLM()
    source = _agent("react", llm).run_stream("核验")
    async for event in source:
        if event.type is EventType.TOKEN:
            break
    assert llm.active == 1
    await source.aclose()
    assert llm.active == 0
    assert llm.closed == 1


async def test_budget_exception_in_tool_batch_waits_for_sibling_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.runtime import RunBudgetExceeded

    started = asyncio.Event()
    closed = asyncio.Event()

    async def execute(call: ToolCall) -> ToolResult:
        if call.name == "budget":
            await started.wait()
            raise RunBudgetExceeded("token_budget", "test budget")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
        return ToolResult.success("unused")

    agent = _agent("react", RuntimeLLM(), tool_concurrency=2)
    monkeypatch.setattr(agent, "_execute_one", execute)
    with pytest.raises(RunBudgetExceeded):
        await agent._execute_batch(
            [
                ToolCall(id="a", name="slow", arguments={}),
                ToolCall(id="b", name="budget", arguments={}),
            ]
        )
    assert closed.is_set()


def _agent(mode: str, llm: Any, **settings: Any) -> Any:
    cfg = AgentSettings(_env_file=None, profile="general", **settings)
    cls = {"react": Agent, "plan": PlanAndExecuteAgent, "multi": SupervisorAgent}[mode]
    return cls(llm, ToolRegistry(), cfg)


def test_plan_inherits_all_settings_except_step_limit() -> None:
    settings = AgentSettings(
        _env_file=None,
        profile="jobhunt",
        run_timeout=0.2,
        tool_concurrency=1,
        context_token_budget=777,
        file_write_enabled=True,
    )
    agent = PlanAndExecuteAgent(RuntimeLLM(), ToolRegistry(), settings)  # type: ignore[arg-type]
    assert agent._step_settings.model_dump() == {
        **settings.model_dump(),
        "max_steps": 4,
    }


@pytest.mark.parametrize(
    "mode,stage",
    [
        ("react", "expert"),
        ("plan", "plan"),
        ("plan", "expert"),
        ("plan", "synthesis"),
        ("multi", "route"),
        ("multi", "expert"),
        ("multi", "synthesis"),
    ],
)
async def test_every_phase_shares_whole_run_timeout(mode: str, stage: str) -> None:
    llm = RuntimeLLM(**{stage: 0.8})
    events = [e async for e in _agent(mode, llm, run_timeout=0.3).run_stream("核验资料")]
    assert events[-1].stopped_reason == "timeout"
    assert sum(e.type is EventType.DONE for e in events) == 1
    assert llm.active == 0
    if stage != "synthesis":
        assert "synthesis" not in llm.calls
    if stage in {"plan", "route", "expert", "synthesis"}:
        assert events[-1].usage_complete is False
    assert current_run_context() is None


async def test_plan_does_not_restart_deadline_for_each_step() -> None:
    llm = RuntimeLLM(plan=0.03, expert=0.03)
    result = await _agent("plan", llm, run_timeout=0.075).run("核验")
    assert result.stopped_reason == "timeout"
    assert "synthesis" not in llm.calls
    assert result.usage.total_tokens >= 10


async def test_replanning_consumes_original_deadline_and_preserves_known_usage() -> None:
    llm = RuntimeLLM(replan=0.8)
    llm.fail_expert = "你是"
    result = await _agent("plan", llm, run_timeout=0.3).run("核验")
    assert result.stopped_reason == "timeout"
    assert llm.calls == ["plan", "expert", "replan"]
    assert result.usage.total_tokens == 20
    assert not result.usage_complete


async def test_replanning_usage_is_in_token_budget_before_next_step() -> None:
    llm = RuntimeLLM()
    llm.fail_expert = "你是"
    result = await _agent("plan", llm, plan_max_total_tokens=25).run("核验")
    assert result.stopped_reason == "token_budget"
    assert llm.calls == ["plan", "expert", "replan"]
    assert result.usage.total_tokens == 30


@pytest.mark.parametrize("mode", ["plan", "multi"])
async def test_routing_or_planning_budget_blocks_experts(mode: str) -> None:
    llm = RuntimeLLM()
    cfg = {f"{mode}_max_total_tokens": 1}
    result = await _agent(mode, llm, **cfg).run("核验")
    assert result.stopped_reason == "token_budget"
    assert result.usage.total_tokens == 10
    assert "expert" not in llm.calls
    assert "synthesis" not in llm.calls
    assert result.usage_complete is True


async def test_plan_budget_preserves_results_without_paid_synthesis() -> None:
    llm = RuntimeLLM()
    result = await _agent("plan", llm, plan_max_total_tokens=25).run("核验")
    assert result.stopped_reason == "token_budget"
    assert result.usage.total_tokens == 30
    assert result.plan and all(s["status"] == "done" for s in result.plan["steps"])
    assert "可核对的结论" in result.answer
    assert "synthesis" not in llm.calls


async def test_multi_budget_counts_inflight_calls_and_blocks_synthesis() -> None:
    llm = RuntimeLLM(expert=0.01)
    result = await _agent("multi", llm, multi_max_total_tokens=25).run("核验")
    assert result.stopped_reason == "token_budget"
    assert result.usage.total_tokens >= 30  # 已在途调用允许超额，但不能启动汇总
    assert "synthesis" not in llm.calls
    assert llm.active == 0


async def test_failed_specialist_usage_is_not_lost() -> None:
    llm = RuntimeLLM()
    llm.fail_expert = "结果核验员"
    result = await _agent("multi", llm).run("核验")
    assert result.usage.total_tokens == 50  # 路由 + 三位专家（含失败）+ 汇总
    assert any(not t["ok"] and "失败" in t["content"] for t in result.tool_calls)


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_missing_usage_is_explicit(mode: str) -> None:
    llm = RuntimeLLM()
    llm.missing_usage = True
    result = await _agent(mode, llm).run("核验")
    assert result.stopped_reason == "finished"
    assert result.usage_complete is False


async def test_cancelled_supervisor_waits_for_all_delegates() -> None:
    llm = RuntimeLLM(expert=10)
    agent = _agent("multi", llm)

    async def consume() -> None:
        async for _ in agent.run_stream("核验"):
            pass

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(llm.started.wait(), 1)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert llm.active == 0
    assert llm.closed == 3
    assert current_run_context() is None


async def test_closing_supervisor_stream_cleans_up_waiting_delegates() -> None:
    llm = RuntimeLLM()
    original = llm.stream_chat

    async def staggered(messages: Any, **kw: Any) -> AsyncIterator[StreamDelta]:
        if "资料分析员" not in (messages[0].content or ""):
            await asyncio.sleep(10)
        async for delta in original(messages, **kw):
            yield delta

    llm.stream_chat = staggered  # type: ignore[method-assign]
    source = _agent("multi", llm).run_stream("核验")
    async for event in source:
        if event.type is EventType.DELEGATE_RESULT:
            break
    await source.aclose()
    assert not [t for t in asyncio.all_tasks() if "run_one" in str(t.get_coro())]


@pytest.mark.parametrize("mode", ["react", "plan", "multi"])
async def test_reused_agent_gets_fresh_budget(mode: str) -> None:
    llm = RuntimeLLM(expert=0.02)
    agent = _agent(mode, llm, run_timeout=0.2)
    first = await agent.run("第一轮")
    await asyncio.sleep(0.21)
    second = await agent.run("第二轮")
    assert first.stopped_reason == second.stopped_reason == "finished"
    assert first.usage == second.usage
    assert current_run_context() is None


async def test_concurrent_requests_have_independent_usage() -> None:
    llm = RuntimeLLM(expert=0.01)
    agent = _agent("multi", llm)
    results = await asyncio.gather(agent.run("任务甲"), agent.run("任务乙"))
    assert [r.usage.total_tokens for r in results] == [50, 50]


@pytest.mark.parametrize("mode", ["plan", "multi"])
async def test_synthesis_context_is_trimmed_and_reported(mode: str) -> None:
    llm = RuntimeLLM()
    llm.answer = "经过核验的资料" * 2000
    result = await _agent(mode, llm, context_token_budget=1600).run("核验资料")
    assert result.context_trimmed
    assert result.context_tokens > 0
    assert result.stopped_reason == "finished"


def test_default_specialists_follow_profile() -> None:
    general = _agent("multi", RuntimeLLM())
    jobhunt = SupervisorAgent(RuntimeLLM(), ToolRegistry(), AgentSettings(profile="jobhunt"))  # type: ignore[arg-type]
    assert [s.name for s in general._specialists] == ["资料分析员", "方案分析员", "结果核验员"]
    assert jobhunt._specialists == DEFAULT_SPECIALISTS
    assert "read_resume" not in general._base_prompt


def test_nonstream_response_without_usage_is_not_reported_complete() -> None:
    response = LLMClient._parse_response({"choices": [{"message": {"content": "答"}}]})
    assert not response.usage_complete


class EmptyParams(BaseModel):
    pass


@pytest.mark.parametrize("serial,peak", [(True, 1), (False, 2)])
async def test_shared_registry_serializes_effects_but_keeps_reads_parallel(
    serial: bool, peak: int
) -> None:
    active, maximum = 0, 0

    class Probe(Tool):
        description = "探针"
        params_model = EmptyParams

        async def run(self, params: BaseModel) -> ToolResult:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.02)
                return ToolResult.success("ok")
            finally:
                active -= 1

    registry = ToolRegistry()
    for name in ("write", "edit"):
        tool = Probe()
        tool.name, tool.serial = name, serial
        registry.register(tool)
    await asyncio.gather(*(registry.execute(ToolCall(id=n, name=n)) for n in ("write", "edit")))
    assert maximum == peak


async def test_cancelled_sync_effect_keeps_lock_until_thread_finishes() -> None:
    started = threading.Event()
    active, maximum = 0, 0

    def effect(params: BaseModel) -> ToolResult:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        started.set()
        time.sleep(0.08)
        active -= 1
        return ToolResult.success("ok")

    registry = ToolRegistry()
    tool = registry.register_fn("effect", "同步副作用", EmptyParams, effect)
    tool.serial = True
    first = asyncio.create_task(registry.execute(ToolCall(id="1", name="effect")))
    await asyncio.to_thread(started.wait, 1)
    first.cancel()
    second = asyncio.create_task(registry.execute(ToolCall(id="2", name="effect")))
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second).ok
    assert maximum == 1


async def test_retry_success_still_marks_prior_consumption_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.llm.client as client_module
    import httpx
    from app.core.config import LLMSettings

    async def no_wait(delay: float) -> None:
        pass

    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)
    attempts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://example.invalid/v1", transport=httpx.MockTransport(handler)
    ) as http:
        client = client_module.LLMClient(
            LLMSettings(_env_file=None, api_key="public", max_retries=1), client=http
        )
        result = await client.chat([ChatMessage.user("public")])
    assert len(attempts) == 2
    assert result.usage.total_tokens == 10
    assert not result.usage_complete
