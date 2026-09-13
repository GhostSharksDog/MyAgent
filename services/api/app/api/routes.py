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

import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app import __version__
from app.agent.events import AgentEvent, EventType
from app.agent.loop import Agent
from app.agent.memory import ConversationMemory
from app.agent.multi import SupervisorAgent
from app.agent.planning import PlanAndExecuteAgent
from app.api.schemas import (
    ChatRequest,
    ChatResponse,
    HistoryMessage,
    MetaResponse,
    ToolInfo,
)
from app.core.config import get_settings
from app.core.telemetry import record_agent_event
from app.llm.types import ChatMessage
from app.rag.backend import describe_knowledge_backend
from app.session.models import Session
from app.session.store import SessionStore

logger = logging.getLogger(__name__)

router = APIRouter()


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
    if payload.mode == "react":
        memory = ConversationMemory.from_turns(
            session.turns,
            llm=request.app.state.llm,  # 摘要压缩需要 LLM
            max_turns=settings.memory.max_turns,
            keep_recent=settings.memory.keep_recent,
            max_summary_chars=settings.memory.max_summary_chars,
            # 会话模式下**强制开启摘要**：会话要跨请求延续，
            # 退化成"截断丢历史"会让用户莫名其妙地失去上下文，
            # 而 MEMORY_ENABLED=false 的默认值本意是省掉"记忆装配"的开销，
            # 不是要丢掉会话历史。
            enable_summary=True,
        )
        agent = Agent(
            request.app.state.llm,
            request.app.state.tools,
            settings.agent,
            memory=memory,
            long_term=request.app.state.long_term,
        )
    return agent, session


async def _persist(
    store: SessionStore, session: Session | None, user: str, assistant: str, tokens: int
) -> None:
    """把一轮成功对话写回会话。

    只在**成功产出最终答案**时调用 —— 被预算掐断、死循环中止或报错的轮次
    不是有效上下文，写进会话会让后续对话基于半成品推理。
    这与 Agent 内部写短期记忆的判据保持一致（同一条规则只在一处定义，
    但两处都要遵守；此处若漏掉，会话历史里就会出现半成品答案）。
    """
    if session is None:
        return
    updated = await store.append_turn(session.id, user, assistant, tokens=tokens)
    if updated is None:
        # 会话在流式过程中被删除或过期。不该影响已经返回给用户的结果，
        # 但必须记日志 —— 否则这就是一次静默的数据丢失。
        logger.warning("会话 %s 在对话过程中消失，本轮结果未能持久化", session.id)


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
    return {
        "status": "ok",
        "env": str(settings.app_env),
        "llm_configured": settings.llm.is_configured,
        "model": settings.llm.model,
        "tools": request.app.state.tools.names(),
        # 三个 backend 必须同时可见：它们的共同点是**配错了不报错**，
        # 只会默默以另一种拓扑运行。健康检查是唯一能一眼看出来的地方。
        "session_backend": request.app.state.sessions.backend,
        "task_backend": request.app.state.tasks.backend,
        "task_workers_in_api": settings.tasks.run_workers_in_api,
        "rag_backend": rag_info["backend"],
        "rag_service_url": rag_info.get("url", ""),
    }


@router.get("/api/meta", response_model=MetaResponse, summary="服务元信息")
async def meta(request: Request) -> MetaResponse:
    settings = get_settings()
    return MetaResponse(
        service="jobpilot-api",
        version=__version__,
        env=str(settings.app_env),
        model=settings.llm.model,
        max_steps=settings.agent.max_steps,
        tool_count=len(request.app.state.tools.names()),
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
        out.append(
            ToolInfo(name=fn["name"], description=fn["description"], parameters=fn["parameters"])
        )
    return out


# ============================================================
# 对话（非流式）
# ============================================================
@router.post("/api/chat", response_model=ChatResponse, summary="对话（非流式）")
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    agent, session = await _resolve(request, payload)

    # 会话模式以服务端历史为准，忽略客户端传来的 history。
    # 这是刻意的：两套历史同时生效必然导致重复或错序。
    history = [] if session is not None else _to_history(payload.history)
    result = await agent.run(payload.message, history)

    if result.answer:
        await _persist(
            _get_store(request),
            session,
            payload.message,
            result.answer,
            result.usage.total_tokens,
        )

    return ChatResponse(
        answer=result.answer,
        steps_used=result.steps_used,
        usage=result.usage,
        tool_calls=result.tool_calls,
        stopped_reason=result.stopped_reason,
        error=result.error,
    )


# ============================================================
# 对话（SSE 流式）
# ============================================================
@router.post("/api/chat/stream", summary="对话（SSE 流式）")
async def chat_stream(payload: ChatRequest, request: Request) -> EventSourceResponse:
    """流式端点。

    用 POST + SSE 而不是 GET：消息内容放在请求体里更自然，也不受 URL 长度限制。
    前端要用 fetch + ReadableStream 消费（`EventSource` 只支持 GET）。

    `ping` 心跳用于穿透可能存在的反向代理空闲超时 —— 长连接最容易被中间层掐断。
    """
    agent, session = await _resolve(request, payload)
    store = _get_store(request)
    history = None if session is not None else _to_history(payload.history)

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        final_answer = ""
        total_tokens = 0

        try:
            async for event in agent.run_stream(payload.message, history):
                # 边转发边收集需要持久化的信息。
                # 不能等流结束再重跑一遍 —— 那会重复调用模型与工具。
                if event.type is EventType.FINAL:
                    final_answer = event.content
                elif event.type is EventType.DONE and event.usage:
                    total_tokens = event.usage.total_tokens

                # 指标采集放在**消费端**而不是 Agent 内核里：
                # 内核有 CLI / HTTP / 测试等多种调用方式，让它直接打点会把
                # "跑一次测试"也变成"污染全局指标"。在事件流经的地方统一采集，
                # 既覆盖所有 Agent 形态（react/plan/multi），内核又保持纯粹。
                record_agent_event(event, mode=payload.mode)

                yield event.to_sse()
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
            yield AgentEvent(type=EventType.ERROR, content=f"服务内部错误：{exc}").to_sse()

        # 持久化放在 `async for` 之外：即使流中途出错，只要已经产出了完整答案
        # 就应当保存（异常分支没有 return，控制流会走到这里）。
        if final_answer:
            await _persist(store, session, payload.message, final_answer, total_tokens)

    return EventSourceResponse(event_generator(), ping=15)
