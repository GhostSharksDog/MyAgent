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
    history: list[HistoryMessage] = Field(
        default_factory=list, description="之前轮次的消息（不含本轮），最近的在最后"
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
