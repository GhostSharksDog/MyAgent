"""HTTP 接口的请求/响应模型。

显式建模而不是收 dict：接口契约就是文档，FastAPI 会自动生成 OpenAPI，
前端可以直接据此生成 TypeScript 类型（P3 会做这件事）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.llm.types import Usage


class HistoryMessage(BaseModel):
    """历史消息。

    注意这里只允许 user / assistant 两种角色，**刻意不暴露 tool 角色**：
    工具调用是 Agent 的内部实现细节，历史里只保留"问答结果"，
    这样历史长度可控，也不会因为 tool 消息配对问题导致接口 400。
    """

    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000, description="用户本轮输入")
    session_id: str | None = Field(
        default=None,
        description=(
            "会话 id。提供时服务端会从会话中恢复历史与记忆，"
            "并把本轮结果写回会话；此时 `history` 被忽略。"
            "不提供则退回无状态模式（历史完全由客户端提供）。"
        ),
    )
    history: list[HistoryMessage] = Field(
        default_factory=list,
        description="之前轮次的消息（不含本轮），最近的在最后。仅在无 session_id 时生效。",
    )


class ChatResponse(BaseModel):
    answer: str
    steps_used: int
    usage: Usage
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    stopped_reason: str = "finished"
    error: str | None = None


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class MetaResponse(BaseModel):
    service: str
    version: str
    env: str
    model: str
    max_steps: int
    tool_count: int
    session_backend: str = ""


# ============================================================
# 会话
# ============================================================
class SessionSummaryModel(BaseModel):
    """会话列表项。刻意不含对话内容（见 sessions.py 的说明）。"""

    id: str
    title: str
    created_at: float
    updated_at: float
    turn_count: int
    total_tokens: int


class SessionListResponse(BaseModel):
    sessions: list[SessionSummaryModel] = Field(default_factory=list)
    # 把后端暴露给前端：`memory` 意味着刷新页面/换标签页可能丢历史，
    # 界面上应该据此给出提示，而不是让用户自己撞上"历史不见了"
    backend: str = "memory"


class SessionDetail(BaseModel):
    id: str
    title: str
    created_at: float
    updated_at: float
    total_tokens: int
    turns: list[dict[str, str]] = Field(default_factory=list)
