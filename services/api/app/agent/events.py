"""Agent 事件模型：内核与前端之间的**唯一契约**。

为什么要单独定义事件，而不是让前端直接看原始 SSE？

1. **解耦**：前端不需要知道 OpenAI 协议的细节。协议变了只改客户端解析层。
2. **可测试**：事件是可序列化的小对象，单测里可以断言完整的事件序列。
3. **可观测**：同一份事件流既喂给 UI，也能直接落日志做链路追踪（P4 的基础）。

一个典型的多步工具调用会产生这样的事件序列：

    start → step(1) → token*  → tool_call(calculator) → tool_result(ok) →
            step(2) → token*  → final → done

前端拿这份流就能画出"思考中 → 调用了什么工具 → 结果如何 → 最终回答"的完整时间线。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.llm.types import Usage


class EventType(StrEnum):
    START = "start"  # 一轮对话开始
    STEP = "step"  # 进入第 N 步「模型思考 + 决策」
    TOKEN = "token"  # 文本增量（用于打字机效果）
    TOOL_CALL = "tool_call"  # 模型请求调用工具
    TOOL_RESULT = "tool_result"  # 工具执行完毕（含成功/失败）
    FINAL = "final"  # 最终答案（完整文本）
    ERROR = "error"  # 出错，正常终止
    DONE = "done"  # 流结束哨兵，携带累计用量


class AgentEvent(BaseModel):
    """单一事件。字段是各类型的并集，未用到的字段保持默认值。"""

    type: EventType
    step: int = 0
    content: str = ""

    # 工具相关
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_ok: bool | None = None
    duration_ms: int | None = None
    # 观察结果是否被截断。必须下发到事件流：
    # 截断意味着模型看到的不是完整内容，如果 UI 和日志都不体现这一点，
    # 出现"模型漏答了文件后半部分"这类问题时根本无从定位。
    truncated: bool | None = None

    # 结束时的累计统计
    usage: Usage | None = None
    steps_used: int = 0
    # 终止原因：finished | max_steps | loop_detected | error
    # 仅 DONE 事件携带。单独一个字段而不是靠"有没有 ERROR 事件"推断：
    # "步数耗尽"和"死循环"是**可预期的预算终止**，而"模型调用失败"是故障。
    # 三者混在一起会让指标统计失真——例如"错误率"会把正常的预算耗尽也算进去。
    stopped_reason: str = "finished"

    def to_sse(self) -> dict[str, str]:
        """转成 SSE 事件（sse-starlette 的 ServerSentEvent 参数形式）。"""
        return {"event": str(self.type), "data": self.model_dump_json(exclude_none=True)}


class AgentRunResult(BaseModel):
    """非流式调用（一次性拿完整结果）的返回。"""

    answer: str
    steps_used: int = 0
    usage: Usage = Field(default_factory=Usage)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    stopped_reason: str = "finished"  # finished | max_steps | loop_detected | error
    error: str | None = None
