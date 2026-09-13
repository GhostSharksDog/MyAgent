"""HTTP 路由。

三个端点对应三种消费方式：
  POST /api/chat         —— 一次性拿完整答案（简单集成、脚本调用）
  POST /api/chat/stream  —— SSE 流式，含工具调用过程（前端体验）
  GET  /api/tools        —— 查看当前注册的工具（调试 & 前端渲染工具卡片）
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app import __version__
from app.agent.events import AgentEvent, EventType
from app.agent.loop import Agent
from app.api.schemas import (
    ChatRequest,
    ChatResponse,
    HistoryMessage,
    MetaResponse,
    ToolInfo,
)
from app.core.config import get_settings
from app.llm.types import ChatMessage

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_agent(request: Request) -> Agent:
    agent: Agent = request.app.state.agent
    return agent


def _to_history(items: list[HistoryMessage]) -> list[ChatMessage]:
    return [ChatMessage(role=m.role, content=m.content) for m in items]


@router.get("/healthz", summary="健康检查")
async def healthz(request: Request) -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "env": str(settings.app_env),
        "llm_configured": settings.llm.is_configured,
        "model": settings.llm.model,
        "tools": request.app.state.tools.names(),
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


@router.post("/api/chat", response_model=ChatResponse, summary="对话（非流式）")
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    agent = _get_agent(request)
    result = await agent.run(payload.message, _to_history(payload.history))
    return ChatResponse(
        answer=result.answer,
        steps_used=result.steps_used,
        usage=result.usage,
        tool_calls=result.tool_calls,
        stopped_reason=result.stopped_reason,
        error=result.error,
    )


@router.post("/api/chat/stream", summary="对话（SSE 流式）")
async def chat_stream(payload: ChatRequest, request: Request) -> EventSourceResponse:
    """流式端点。

    用 POST + SSE 而不是 GET：消息内容和历史放在请求体里更自然，
    也不受 URL 长度限制。前端用 fetch + ReadableStream 消费即可。

    `ping` 心跳用于穿透可能存在的反向代理空闲超时 —— 长连接最容易被中间层掐断。
    """
    agent = _get_agent(request)

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        try:
            async for event in agent.run_stream(payload.message, _to_history(payload.history)):
                yield event.to_sse()
        except Exception as exc:
            # 流已经开始后无法改 HTTP 状态码，只能以事件形式告知前端。
            #
            # 【踩坑修正】初版用 f-string + repr() 手拼 JSON：
            #     f'{{"type":"error","content":{exc!r}}}'
            # 这不是合法 JSON —— Python 的 repr 用单引号，且不会转义内容里的
            # 引号与换行。异常信息里只要出现引号（如 KeyError('a"b')），
            # 前端 JSON.parse 就会抛异常，而这时流已经开始了，
            # 用户看到的是"连接中断"而不是真正的错误原因。
            # 正确做法是复用 AgentEvent 自己的序列化 —— 单一事实来源。
            logger.exception("流式对话异常")
            yield AgentEvent(type=EventType.ERROR, content=f"服务内部错误：{exc}").to_sse()

    return EventSourceResponse(event_generator(), ping=15)
