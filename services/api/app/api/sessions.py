"""会话管理接口。

    POST   /api/sessions            创建会话
    GET    /api/sessions            列出会话（不含对话内容）
    GET    /api/sessions/{id}       会话详情（含全部轮次）
    DELETE /api/sessions/{id}       删除会话

设计上的两个刻意选择：

1. **列表与详情分离**。列表只返回元信息（SessionSummary），不含 turns。
   列出 20 个会话时把全部对话内容读出来，在 Redis 场景下就是 20 次大 value
   读取 —— 这是"接口设计直接决定性能"的典型例子。

2. **会话不存在时返回 404 而不是自动创建**。自动创建会把
   "客户端传了错误的 session_id"这种 bug 变成"悄悄多出一个会话"，
   问题被掩盖而不是暴露。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import SessionDetail, SessionListResponse, SessionSummaryModel
from app.session.models import Session, SessionSummary
from app.session.store import SessionStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.sessions
    return store


def _to_api_summary(item: Session | SessionSummary) -> SessionSummaryModel:
    """把存储层的会话摘要转成 API 模型。

    【为什么要显式转换 —— 一个真实踩过的坑】
    初版直接把存储层返回的 `list[SessionSummary]` 塞进
    `SessionListResponse.sessions`（声明为 `list[SessionSummaryModel]`）。
    **pydantic v2 不会在不同 BaseModel 之间做鸭子类型转换**，
    于是序列化时抛 ValidationError。

    这个 bug 的隐蔽之处在于：会话列表为空时**测试是通过的** ——
    空列表没有元素需要校验。只有创建过会话之后再列列表才会暴露。
    这也是"边界用例要覆盖非空"的具体教训。

    显式转换同时保住了分层：API 模型不直接依赖存储层的内部结构。
    """
    return SessionSummaryModel(
        id=item.id,
        title=item.title or "（未命名会话）",
        created_at=item.created_at,
        updated_at=item.updated_at,
        turn_count=item.turn_count,
        total_tokens=item.total_tokens,
    )


@router.post("", response_model=SessionSummaryModel, summary="创建会话")
async def create_session(request: Request) -> SessionSummaryModel:
    session = await _store(request).create()
    return _to_api_summary(session)


@router.get("", response_model=SessionListResponse, summary="列出会话")
async def list_sessions(request: Request, limit: int = 20) -> SessionListResponse:
    store = _store(request)
    sessions = await store.list(limit=max(1, min(limit, 100)))
    # 必须逐项转换，不能把存储层的模型直接塞进响应模型（见 _to_api_summary 的说明）
    return SessionListResponse(
        sessions=[_to_api_summary(s) for s in sessions], backend=store.backend
    )


@router.get("/{session_id}", response_model=SessionDetail, summary="会话详情")
async def get_session(request: Request, session_id: str) -> SessionDetail:
    session = await _store(request).get(session_id)
    if session is None:
        # 404 而不是 200+空会话：让"会话不存在"这件事显式暴露给客户端
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在或已过期")

    return SessionDetail(
        id=session.id,
        title=session.title or "（未命名会话）",
        created_at=session.created_at,
        updated_at=session.updated_at,
        total_tokens=session.total_tokens,
        turn_count=session.turn_count,
        turns=_interleave(session),
    )


@router.delete("/{session_id}", summary="删除会话")
async def delete_session(request: Request, session_id: str) -> dict[str, bool]:
    deleted = await _store(request).delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
    logger.info("已删除会话 %s", session_id)
    return {"deleted": True}


def _interleave(session: Session) -> list[dict[str, str]]:
    """把轮次展开成交替的 user/assistant 消息列表。

    【为什么不在接口里直接返回 Turn 对象】
    前端的渲染逻辑是按消息列表来的（它与流式事件的形态一致）。
    如果接口返回 `[{user, assistant}, ...]`，前端就得自己写一遍展开逻辑 ——
    而后端在别处（比如导出对话）也得再写一遍。统一成消息列表，
    前后端各自只保留一种表示。
    """
    messages: list[dict[str, str]] = []
    for turn in session.turns:
        messages.append({"role": "user", "content": turn.user})
        messages.append({"role": "assistant", "content": turn.assistant})
    return messages
