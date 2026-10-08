"""HTTP 路由。

三个端点对应三种消费方式：

    POST /api/chat         —— 一次性拿完整答案（简单集成、脚本调用）
    POST /api/chat/stream  —— SSE 流式，含工具调用过程（前端体验）
    GET  /api/tools        —— 查看当前注册的工具（调试 & 前端渲染工具卡片）

两个 chat 端点都支持**两种模式**：

    ┌─ 带 session_id ─→ 服务端从会话恢复历史与记忆，并把结果写回会话
    └─ 不带 session_id ─→ 无状态模式，历史完全由客户端提供（P1 的原始行为）

保留无状态模式不是偷懒：脚本、CI、以及"一次性的独立提问"都不需要会话，
强制它们先建会话只会增加无谓的往返。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any, Literal

import anyio
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from app import __version__
from app.agent.approvals import ApprovalBroker, ApprovalUnavailable, merge_approval_events
from app.agent.events import AgentEvent, EventType
from app.agent.loop import Agent
from app.agent.memory import ConversationMemory
from app.agent.multi import SupervisorAgent
from app.agent.operations import render_facts
from app.agent.planning import PlanAndExecuteAgent
from app.agent.runtime import RunContext
from app.api.schemas import (
    ChatRequest,
    ChatResponse,
    HistoryMessage,
    MetaResponse,
    ToolInfo,
)
from app.core.config import get_settings
from app.core.resilience import TokenBucket
from app.core.telemetry import METRICS, record_agent_event
from app.llm.tokens import tokenizer_name
from app.llm.types import ChatMessage
from app.rag.backend import describe_knowledge_backend
from app.runs.history import RunHistory, RunRecord, RunRecorder
from app.session.gate import SessionGate
from app.session.models import Session
from app.session.store import SessionStore

logger = logging.getLogger(__name__)

router = APIRouter()


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]


@router.post("/api/runs/{run_id}/approvals/{approval_id}", summary="批准或拒绝当前文件差异或命令")
async def decide_file_change(
    run_id: str, approval_id: str, payload: ApprovalDecision, request: Request
) -> dict[str, str]:
    # 与其余 API 使用相同的鉴权中间件；决定只能投递到绑定的活跃请求。
    brokers = getattr(request.app.state, "file_approvals", {})
    broker = brokers.get(run_id)
    if broker is None:
        raise HTTPException(409, "运行已结束或没有待批准修改；请重新生成差异。")
    try:
        return {"status": broker.decide(approval_id, payload.decision)}
    except ApprovalUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc


def _get_agent(request: Request) -> Agent:
    agent: Agent = request.app.state.agent
    return agent


def _get_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.sessions
    return store


def _to_history(items: list[HistoryMessage]) -> list[ChatMessage]:
    return [ChatMessage(role=m.role, content=m.content) for m in items]


# ============================================================
# 会话模式的解析
# ============================================================
async def _resolve(request: Request, payload: ChatRequest) -> tuple[Any, Session | None]:
    """决定这次请求用哪个 Agent。

    两个维度是**正交**的：**形态**（react / plan / multi）× **是否带会话**。

    【形态】三种形态共用同一套工具与护栏，只是循环结构不同。
    刻意不让它们各自实现一遍工具调用 —— 两套实现迟早不一致，
    而那时你无法判断差异来自"范式不同"还是"实现不同"。

    【会话】带 `session_id` 时新建一个绑定该会话记忆的 Agent，
    而不是复用 `app.state.agent`。理由：Agent 自身是无状态的，状态在它持有的
    memory 上；复用共享实例会让不同会话串上下文 ——
    表现为"用户 A 看到了用户 B 的历史"，属于最严重的一类问题。
    新建 Agent 的成本可以忽略：它只持有几个引用，
    真正的重活（HTTP 连接池、向量索引、工具表）都是共享的。
    """
    settings = request.app.state.settings

    # ---------- 形态 ----------
    if payload.mode == "plan":
        agent: Any = PlanAndExecuteAgent(
            request.app.state.llm,
            request.app.state.tools,
            settings.agent,
        )
    elif payload.mode == "multi":
        agent = SupervisorAgent(
            request.app.state.llm,
            request.app.state.tools,
            settings.agent,
        )
    else:
        agent = _get_agent(request)
        if isinstance(agent, Agent) and agent._memory is not None:
            agent = Agent(
                request.app.state.llm,
                request.app.state.tools,
                settings.agent,
                long_term=getattr(request.app.state, "long_term", None),
            )

    # ---------- 会话 ----------
    if not payload.session_id:
        return agent, None

    store = _get_store(request)
    session = await store.get(payload.session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"会话 {payload.session_id} 不存在或已过期。"
                f"请重新创建会话，或改用无状态模式（不传 session_id）。"
            ),
        )

    # 只有 react 形态需要把会话历史装进记忆；plan/multi 不使用历史，
    # 它们的 run_stream 会打日志说明这一点（见各自 docstring）。
    # 这里不传历史也不会静默失效 —— 后端会记录日志。
    if payload.mode != "react" and session.turns:
        logger.info("%s 本轮按独立任务处理，不使用会话 %s 的历史", payload.mode, session.id)
    if payload.mode == "react":
        memory = ConversationMemory.from_turns(
            session.turns,
            llm=request.app.state.llm,  # 摘要压缩需要 LLM
            max_turns=settings.memory.max_turns,
            keep_recent=settings.memory.keep_recent,
            max_summary_chars=settings.memory.max_summary_chars,
            # 会话摘要独立于长期记忆开关；关闭摘要时仍显式标记裁剪。
            enable_summary=settings.memory.enable_summary,
            summary_state=session.meta.get("conversation_summary"),
        )
        memory.execution_context = render_facts(session.meta)
        agent = Agent(
            request.app.state.llm,
            request.app.state.tools,
            settings.agent,
            memory=memory,
            long_term=request.app.state.long_term,
        )
    return agent, session


async def _persist(
    store: SessionStore,
    session: Session | None,
    user: str,
    assistant: str,
    tokens: int,
    tool_summary: str = "",
) -> bool | None:
    """把一轮成功对话写回会话。

    只在**成功产出最终答案**时调用 —— 被预算掐断、死循环中止或报错的轮次
    不是有效上下文，写进会话会让后续对话基于半成品推理。
    这与 Agent 内部写短期记忆的判据保持一致（同一条规则只在一处定义，
    但两处都要遵守；此处若漏掉，会话历史里就会出现半成品答案）。

    `tool_summary` 是同一条判据下的新成员（技术债 T07）：它只在成功轮次里
    才有意义 —— 半成品轮次的"查过什么"不足以让下一轮省掉一次调用。
    """
    if session is None:
        return None
    updated = await store.append_turn(
        session.id, user, assistant, tokens=tokens, tool_summary=tool_summary
    )
    if updated is None:
        # 会话在流式过程中被删除或过期。不该影响已经返回给用户的结果，
        # 但必须记日志 —— 否则这就是一次静默的数据丢失。
        logger.warning("会话 %s 在对话过程中消失，本轮结果未能持久化", session.id)
    return updated is not None


async def _save_session(store, session, context, *, user="", answer="", tokens=0, summary=""):
    if session is None:
        return None
    with anyio.CancelScope(shield=True):
        try:
            saved = True
            if context.session_memory is not None:
                saved = await store.merge_summary(session.id, context.session_memory.summary_state)
            if context.execution_facts:
                saved = (
                    await store.merge_execution_facts(session.id, context.execution_facts) and saved
                )
            if answer:
                saved = (
                    bool(await _persist(store, session, user, answer, tokens, summary)) and saved
                )
            return saved
        except Exception:
            logger.exception("会话或执行事实保存失败")
            return False


async def _lease(request, payload):
    request.state.run_started_at = asyncio.get_running_loop().time()
    if not hasattr(request.app.state, "session_gate"):
        request.app.state.session_gate = SessionGate()
    return await request.app.state.session_gate.acquire(
        payload.session_id, request.app.state.settings.agent.run_timeout
    )


# ============================================================
# 元信息
# ============================================================
@router.get("/healthz", summary="健康检查")
async def healthz(request: Request) -> dict[str, object]:
    """健康检查。

    【为什么这里只报配置，不探活下游】
    见 `rag/backend.py::describe_knowledge_backend`：让健康检查依赖下游
    会导致级联故障 —— 检索服务变慢会把所有 agent 副本一起拖下水。
    这里只回答"本进程的装配是否正确"，因此永远是常数时间。
    """
    settings = get_settings()
    rag_info = describe_knowledge_backend(settings)
    rc = settings.resilience
    return {
        "status": "ok",
        "env": str(settings.app_env),
        "llm_configured": settings.llm.is_configured,
        "model": settings.llm.model,
        # 【为什么把"要不要密钥"放在一个**公开**端点里】
        # 界面上要是没有这个信息，用户在启用鉴权之后只会看到一堆 401，
        # 而不知道"这是要求密钥"还是"服务坏了"。健康检查本来就是
        # "本进程装配成了什么样"的答案，访问控制是它的一部分。
        # 这里只说明"要不要"，**不会透露密钥本身**，也不透露长度。
        "auth_required": settings.security.enabled,
        # 形态（general / jobhunt）决定提示词、工具集与知识库默认数据源，
        # 而它配错了不会报错 —— 与下面三个 backend 属于同一类"静默差异"。
        "profile": settings.agent.profile,
        "tools": request.app.state.tools.names(),
        # 三个 backend 必须同时可见：它们的共同点是**配错了不报错**，
        # 只会默默以另一种拓扑运行。健康检查是唯一能一眼看出来的地方。
        "session_backend": request.app.state.sessions.backend,
        "task_backend": request.app.state.tasks.backend,
        "task_workers_in_api": settings.tasks.run_workers_in_api,
        "rag_backend": rag_info["backend"],
        "rag_service_url": rag_info.get("url", ""),
        # 韧性状态。熔断器打开时，表现是"检索功能莫名其妙没结果" ——
        # 如果没有地方能看到"它现在是 open、还有 12 秒恢复"，
        # 排查会从"检索为什么没结果"这个完全错误的方向开始。
        "circuit_enabled": rc.circuit_enabled,
        "rate_limit_enabled": rc.rate_limit_enabled,
        # 上下文预算与**估算器**必须可见（技术债 T09）。
        # 估算器是"精确分词"还是"启发式"，两者的精度差一个量级 ——
        # 只看到一个 token 数的话，没人知道该不该信它。
        # 0 表示不限制（与配置项同义）。
        "context_token_budget": settings.agent.context_token_budget,
        "tokenizer": tokenizer_name(),
        # 回放状态必须可见：演示前最怕"以为在放录制内容，其实在真调模型" ——
        # 那会在现场变成一个无法解释的等待（或者直接失败）。
        "demo_replay": (
            request.app.state.replayer.describe()
            if getattr(request.app.state, "replayer", None) is not None
            else "off"
        ),
    }


@router.get("/api/meta", response_model=MetaResponse, summary="服务元信息")
async def meta(request: Request) -> MetaResponse:
    settings = get_settings()
    return MetaResponse(
        service="legacy-api",
        version=__version__,
        env=str(settings.app_env),
        model=settings.llm.model,
        max_steps=settings.agent.max_steps,
        tool_count=len(request.app.state.tools.names()),
        profile=settings.agent.profile,
        session_backend=request.app.state.sessions.backend,
        rag_backend=describe_knowledge_backend(settings)["backend"],
        task_backend=request.app.state.tasks.backend,
        task_workers_in_api=settings.tasks.run_workers_in_api,
    )


@router.get("/api/tools", response_model=list[ToolInfo], summary="列出已注册工具")
async def list_tools(request: Request) -> list[ToolInfo]:
    out: list[ToolInfo] = []
    for schema in request.app.state.tools.schemas():
        fn = schema["function"]
        tool = request.app.state.tools.get(fn["name"])
        out.append(
            ToolInfo(
                name=fn["name"],
                description=fn["description"],
                parameters=fn["parameters"],
                source=getattr(tool, "source", "builtin"),
                server_name=(
                    tool.manager.servers[tool.server_id].name
                    if getattr(tool, "source", "") == "mcp"
                    else None
                ),
                remote_name=getattr(tool, "remote_name", None),
            )
        )
    return out


# ============================================================
# 限流
# ============================================================
def _limiter_state(request: Request) -> dict[str, Any]:
    """惰性挂载限流器。

    【为什么限流器必须在 app.state 上而不是模块级全局变量】
    模块级全局变量会跨测试用例、跨 TestClient 实例共享 ——
    上一个用例耗尽令牌，下一个用例就会莫名其妙地被 429。
    挂在 app.state 上，生命周期就跟着 app 走，这也是本文件里
    其它共享资源（store / agent / tasks）统一的做法。
    """
    state = request.app.state
    if not hasattr(state, "rate_buckets"):
        state.rate_buckets = {}  # type: dict[str, TokenBucket]
        state.rate_global = None
    return {"buckets": state.rate_buckets, "global": state.rate_global}


async def _enforce_rate_limit(request: Request, payload: ChatRequest) -> None:
    """按会话限流。

    【为什么限流的 key 是会话而不是 IP】
    这里的成本几乎全部来自 LLM 调用，而 LLM 成本是**按会话产生**的。
    按 IP 限流有两个问题：
      · 一个 IP 后面可能是很多人（公司出口、移动网络），
        限制一个人会误伤所有人
      · 一个人可以换 IP 绕过限制，但换会话的成本高得多（丢历史）

    所以按会话限流，配合一个全局兜底（防止开大量会话绕过）。

    【为什么用 429 而不是 403 或 503】
    429 Too Many Requests 是**唯一一个语义明确表示"你太快了，等会儿再来"**
    的状态码。用 403 会让调用方以为是自己权限有问题（然后去查鉴权），
    用 503 会让它以为服务挂了（然后去查服务状态）——
    两个都是错误的排查方向。

    配合 `Retry-After` 头，调用方就知道该等多久，
    而不是自己猜一个重试节奏 —— **猜出来的节奏往往比原来更糟**。
    """
    settings = request.app.state.settings
    rc = settings.resilience
    if not rc.rate_limit_enabled:
        return

    state = _limiter_state(request)

    # 全局兜底
    if rc.rate_limit_global_rps > 0:
        if state["global"] is None:
            state["global"] = TokenBucket(
                rate=rc.rate_limit_global_rps,
                burst=max(1, int(rc.rate_limit_global_rps * rc.rate_limit_burst)),
            )
        if not await state["global"].acquire():
            _raise_429(state["global"], "服务整体繁忙")

    key = payload.session_id or _client_key(request)
    bucket = state["buckets"].get(key)
    if bucket is None:
        bucket = TokenBucket(rate=rc.rate_limit_rps, burst=rc.rate_limit_burst)
        state["buckets"][key] = bucket

    if not await bucket.acquire():
        _raise_429(bucket, f"会话 {key[:12]} 请求过于频繁")


def _client_key(request: Request) -> str:
    """匿名请求的限流 key（无会话时）。

    【为什么不直接信任 X-Forwarded-For】
    那个头是**客户端可以随便伪造**的。信任它等于把限流 key 交给攻击者：
    换一个头值就是一个全新的桶，限流形同虚设。

    只有确定自己部署在可信反向代理后面（且代理会覆写这个头）时，
    才应该读它。默认只取 TCP 层的直连地址 —— 宁可把代理后的所有
    匿名请求算成一个，也不要提供一个能一键绕过的限流。
    """
    return f"anon:{request.client.host if request.client else 'unknown'}"


def _raise_429(bucket: TokenBucket, reason: str) -> None:
    wait = bucket.retry_after()
    METRICS.inc("legacy_rate_limited_total", scope="chat")
    raise HTTPException(
        status_code=429,
        detail=f"{reason}，请 {wait:.1f} 秒后重试。",
        headers={"Retry-After": f"{max(1, int(wait + 0.5))}"},
    )


# ============================================================
# 对话（非流式）
# ============================================================
async def _start_run(
    request: Request, payload: ChatRequest, session: Session | None, *, replay: bool = False
) -> tuple[RunHistory, RunRecorder, RunContext]:
    settings = request.app.state.settings
    ledger = request.app.state.run_history
    recorder = RunRecorder(
        RunRecord(
            mode=payload.mode,
            session_id=session.id if session else None,
            source="demo_replay" if replay else "agent",
        ),
        ledger.settings,
        request.app.state.tools.names(),
    )
    limit = {
        "react": 0,
        "plan": settings.agent.plan_max_total_tokens,
        "multi": settings.agent.multi_max_total_tokens,
    }[payload.mode]
    context = RunContext.create(settings.agent, token_limit=limit)
    if payload.mode in {"plan", "multi"}:
        context.preference_memory = getattr(request.app.state, "long_term", None)
        context.preference_query = payload.message
    if context.deadline is not None:
        context.deadline = min(
            context.deadline,
            getattr(request.state, "run_started_at", asyncio.get_running_loop().time())
            + settings.agent.run_timeout,
        )
    context.run_id = recorder.record.run_id
    context.observer = recorder.observe_runtime
    try:
        await ledger.save(recorder.record)
    except Exception as exc:
        logger.exception("运行记录初始化失败")
        raise HTTPException(
            503, "无法创建运行摘要。请检查 RUN_HISTORY_PATH 的写权限或切回 memory 后重启"
        ) from exc
    logger.info("开始运行 run_id=%s mode=%s", recorder.record.run_id, payload.mode)
    return ledger, recorder, context


async def _finish_run(ledger: RunHistory, recorder: RunRecorder, reason: str | None = None) -> bool:
    # SSE 断开由 AnyIO 取消作用域驱动；shield 保证摘要收尾仍能完成。
    with anyio.CancelScope(shield=True):
        try:
            await ledger.save(recorder.finish(reason))
            logger.info(
                "运行结束 run_id=%s reason=%s",
                recorder.record.run_id,
                recorder.record.stopped_reason,
            )
            return True
        except Exception:
            logger.exception("运行摘要保存失败，run_id=%s", recorder.record.run_id)
            return False


async def _finish_unstarted_run(ledger: RunHistory, recorder: RunRecorder) -> None:
    if recorder.record.finished_at is None:
        await _finish_run(ledger, recorder, "cancelled")


def _run_kwargs(agent: Any, context: RunContext) -> dict[str, Any]:
    # 保持注入式事件源与离线回放兼容；真实三种内核共享同一个上下文。
    return (
        {"run_context": context}
        if isinstance(agent, (Agent, PlanAndExecuteAgent, SupervisorAgent))
        else {}
    )


@router.post("/api/chat", response_model=ChatResponse, summary="对话（非流式）")
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    lease = await _lease(request, payload)
    try:
        return await _chat(payload, request)
    finally:
        lease.release()


async def _chat(payload: ChatRequest, request: Request) -> ChatResponse:
    await _enforce_rate_limit(request, payload)
    agent, session = await _resolve(request, payload)

    # 会话模式以服务端历史为准，忽略客户端传来的 history。
    # 这是刻意的：两套历史同时生效必然导致重复或错序。
    history = [] if session is not None else _to_history(payload.history)
    ledger, recorder, context = await _start_run(request, payload, session)
    context.session_memory = getattr(agent, "_memory", None) if session else None
    try:
        result = await agent.run(payload.message, history, **_run_kwargs(agent, context))
        if not recorder.runtime_observed:
            for call in result.tool_calls:
                recorder.observe(AgentEvent(type=EventType.TOOL_CALL, tool_name=call.get("name")))
            recorder.observe(
                AgentEvent(
                    type=EventType.DONE,
                    steps_used=result.steps_used,
                    stopped_reason=result.stopped_reason,
                    usage=result.usage,
                    usage_complete=result.usage_complete,
                    context_trimmed=result.context_trimmed,
                    context_tokens=result.context_tokens,
                )
            )
    except asyncio.CancelledError:
        await _save_session(_get_store(request), session, context)
        await _finish_run(ledger, recorder, "cancelled")
        raise
    except Exception:
        await _save_session(_get_store(request), session, context)
        await _finish_run(ledger, recorder, "error")
        raise
    session_saved = await _save_session(
        _get_store(request),
        session,
        context,
        user=payload.message,
        answer=result.answer if result.stopped_reason == "finished" else "",
        tokens=result.usage.total_tokens,
        summary=result.tool_summary,
    )
    record_saved = await _finish_run(ledger, recorder)

    return ChatResponse(
        run_id=recorder.record.run_id,
        record_saved=record_saved,
        session_saved=session_saved,
        answer=result.answer,
        steps_used=result.steps_used,
        usage=result.usage,
        usage_complete=result.usage_complete,
        tool_calls=result.tool_calls,
        stopped_reason=result.stopped_reason,
        error=result.error,
        tool_summary=result.tool_summary,
        context_trimmed=result.context_trimmed,
        context_tokens=result.context_tokens,
    )


# ============================================================
# 对话（SSE 流式）
# ============================================================
@router.post("/api/chat/stream", summary="对话（SSE 流式）")
async def chat_stream(payload: ChatRequest, request: Request) -> EventSourceResponse:
    lease = await _lease(request, payload)
    try:
        response = await _chat_stream(payload, request)
    except BaseException:
        lease.release()
        raise
    source, background = response.body_iterator, response.background

    async def close():
        try:
            if background:
                await background()
        finally:
            lease.release()

    async def stream():
        try:
            async with aclosing(source):
                async for event in source:
                    yield event
        finally:
            lease.release()

    response.body_iterator = stream()
    response.background = BackgroundTask(close)
    return response


async def _chat_stream(payload: ChatRequest, request: Request) -> EventSourceResponse:
    """流式端点。

    用 POST + SSE 而不是 GET：消息内容放在请求体里更自然，也不受 URL 长度限制。
    前端要用 fetch + ReadableStream 消费（`EventSource` 只支持 GET）。

    `ping` 心跳用于穿透可能存在的反向代理空闲超时 —— 长连接最容易被中间层掐断。
    """
    # 限流必须放在最前面：被限流的请求不该走到任何装配或 LLM 调用。
    # 放在 `_resolve` 之后的话，一次被拒的请求也已经付出了建 Agent、
    # 读会话历史、构造提示词的代价 —— 而这些正是限流想省下来的。
    await _enforce_rate_limit(request, payload)
    agent, session = await _resolve(request, payload)
    store = _get_store(request)
    history = None if session is not None else _to_history(payload.history)
    replayer = getattr(request.app.state, "replayer", None)
    ledger, recorder, context = await _start_run(
        request, payload, session, replay=replayer is not None
    )
    context.session_memory = getattr(agent, "_memory", None) if session else None

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        final_answer = ""
        total_tokens = 0
        tool_summary = ""
        stopped_reason = "error"
        saw_done = False
        session_save_completed = False
        broker = ApprovalBroker(get_settings().agent.file_approval_timeout)
        context.approvals = broker
        brokers = getattr(request.app.state, "file_approvals", None)
        if brokers is None:
            brokers = {}
            request.app.state.file_approvals = brokers
        brokers[recorder.record.run_id] = broker

        # 【离线回放：只换数据源，不换任何下游逻辑】
        # 下面的 `async for` 循环体完全不变 —— 同一套事件序列化、同一套指标采集、
        # 同一套持久化。回放之所以可信，正是因为它走的是**完全相同的路径**，
        # 区别只在事件从哪来。这正是一个适配器应该做到的事。
        try:
            source = (
                replayer.stream(speed=request.app.state.settings.demo_replay_speed)
                if replayer is not None
                else agent.run_stream(payload.message, history, **_run_kwargs(agent, context))
            )
            source = merge_approval_events(source, broker)
            async with aclosing(source):
                async for event in source:
                    if not recorder.runtime_observed:
                        recorder.observe(event)
                    event = event.model_copy(update={"run_id": recorder.record.run_id})
                    # 边转发边收集需要持久化的信息。
                    # 不能等流结束再重跑一遍 —— 那会重复调用模型与工具。
                    if event.type is EventType.FINAL:
                        final_answer = event.content
                    elif event.type is EventType.DONE:
                        saw_done = True
                        stopped_reason = event.stopped_reason
                        if event.usage:
                            total_tokens = event.usage.total_tokens
                        # 工具摘要随 DONE 一起下发（技术债 T07）：
                        # 持久化它之后，下一轮的历史里才会有"我查过什么"那一行。
                        # 从事件里取而不是重新算一遍 —— Agent 内部才知道完整的调用轨迹。
                        tool_summary = event.tool_summary
                        event.session_saved = await _save_session(
                            store,
                            session,
                            context,
                            user=payload.message,
                            answer=final_answer if stopped_reason == "finished" else "",
                            tokens=total_tokens,
                            summary=tool_summary,
                        )
                        session_save_completed = True
                        event.record_saved = await _finish_run(ledger, recorder)

                    # 指标采集放在**消费端**而不是 Agent 内核里：
                    # 内核有 CLI / HTTP / 测试等多种调用方式，让它直接打点会把
                    # "跑一次测试"也变成"污染全局指标"。在事件流经的地方统一采集，
                    # 既覆盖所有 Agent 形态（react/plan/multi），内核又保持纯粹。
                    record_agent_event(event, mode=payload.mode)

                    yield event.to_sse()
                    if saw_done:
                        break
            if not saw_done:
                recorder.record.usage_complete = False
                done = AgentEvent(
                    type=EventType.DONE,
                    stopped_reason="error",
                    usage_complete=False,
                    run_id=recorder.record.run_id,
                )
                done.record_saved = await _finish_run(ledger, recorder, "error")
                done.session_saved = await _save_session(store, session, context)
                saw_done = True
                yield AgentEvent(
                    type=EventType.ERROR,
                    content="事件流提前结束，请在运行记录中查看摘要",
                    run_id=recorder.record.run_id,
                ).to_sse()
                yield done.to_sse()
        except asyncio.CancelledError:
            if not saw_done:
                await _finish_run(ledger, recorder, "cancelled")
            raise
        except Exception as exc:
            # 流已经开始后无法改 HTTP 状态码，只能以事件形式告知前端。
            #
            # 【踩坑修正】初版用 f-string + repr() 手拼 JSON：
            #     f'{{"type":"error","content":{exc!r}}}'
            # 这不是合法 JSON —— Python 的 repr 用单引号，且不转义内容里的
            # 引号与换行。异常信息里只要出现引号，前端 JSON.parse 就会抛异常，
            # 而这时流已经开始了，用户只会看到"连接中断"而非真正的错误原因。
            # 正确做法是复用 AgentEvent 自己的序列化 —— 单一事实来源。
            logger.exception("流式对话异常")
            yield AgentEvent(
                type=EventType.ERROR, content=f"服务内部错误：{exc}", run_id=recorder.record.run_id
            ).to_sse()
            if not saw_done:
                saved = await _finish_run(ledger, recorder, "error")
                session_saved = await _save_session(store, session, context)
                saw_done = True
                yield AgentEvent(
                    type=EventType.DONE,
                    stopped_reason="error",
                    usage_complete=False,
                    run_id=recorder.record.run_id,
                    record_saved=saved,
                    session_saved=session_saved,
                ).to_sse()
        finally:
            broker.close()
            brokers.pop(recorder.record.run_id, None)
            context.approvals = None
            if not session_save_completed:
                await _save_session(store, session, context)
            if recorder.record.finished_at is None:
                await _finish_run(ledger, recorder, "cancelled")

    return EventSourceResponse(
        event_generator(),
        ping=15,
        headers={"X-Run-Id": recorder.record.run_id},
        background=BackgroundTask(_finish_unstarted_run, ledger, recorder),
    )
