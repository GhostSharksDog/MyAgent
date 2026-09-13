"""OpenAI 兼容协议的**消息与工具类型**。

为什么不直接用 openai SDK 的模型？
—— 因为理解协议本身就是这个项目的学习目标。这里把 wire format
   显式建模，你会在下面看到每个字段为什么存在、模型返回时长什么样。

协议速览（POST /v1/chat/completions）：

请求体::

    {
      "model": "deepseek-chat",
      "messages": [
        {"role": "system",    "content": "你是一个求职助手"},
        {"role": "user",      "content": "帮我看看这份简历"},
        {"role": "assistant", "content": null,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "get_time", "arguments": "{\\"tz\\":\\"Asia/Shanghai\\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "2025-01-01 12:00:00"}
      ],
      "tools": [{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...JSON Schema...}}}],
      "stream": true
    }

关键理解点：
1. **模型从来不"执行"任何东西**。它只是输出一段结构化文本，声明"我想调用这个函数、参数是这些"。
   真正的执行发生在你的进程里 —— 这就是 Tool Use 的全部秘密。
2. `tool_calls[].function.arguments` 是**字符串**不是对象（JSON 被序列化成了字符串），
   而且模型可能输出非法 JSON。所以必须做容错解析 + 把错误回灌给模型让它自己修。
3. 工具执行结果必须作为独立的 `role="tool"` 消息回灌，并用 `tool_call_id` 与请求配对。
   模型靠这个 id 知道"我上次要的那个结果回来了"。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """模型发起的一次工具调用请求。"""

    id: str
    name: str
    # 已经解析成 dict 的参数。解析失败时为 None，原始串保留在 raw_arguments。
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str = ""

    @classmethod
    def from_wire(cls, wire: dict[str, Any]) -> ToolCall:
        """从模型返回的原始结构构造。

        arguments 是 JSON 字符串，且**不保证合法**——模型偶尔会返回
        带尾逗号、单引号、或含未转义换行的"近似 JSON"。这里的容错策略：
        解析失败不抛异常，而是把空 dict + 原始串带出去，
        由上层决定是否把解析错误当作观察结果回灌给模型。
        """
        fn = wire.get("function") or {}
        raw = fn.get("arguments") or "{}"
        parsed: dict[str, Any] = {}
        if isinstance(raw, dict):
            # 少数兼容实现直接给对象
            parsed = raw
            raw = json.dumps(raw, ensure_ascii=False)
        else:
            try:
                candidate = json.loads(raw)
                if isinstance(candidate, dict):
                    parsed = candidate
            except (json.JSONDecodeError, TypeError):
                pass
        return cls(
            id=wire.get("id") or "call_unknown",
            name=fn.get("name") or "",
            arguments=parsed,
            raw_arguments=raw,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments or "{}"},
        }


class ChatMessage(BaseModel):
    """一条对话消息。字段与协议一一对应，None 的字段在序列化时被丢弃。"""

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    # ---------- 构造便捷方法 ----------

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(
        cls, content: str | None = None, tool_calls: list[ToolCall] | None = None
    ) -> ChatMessage:
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str, name: str | None = None) -> ChatMessage:
        return cls(role=Role.TOOL, content=content, tool_call_id=tool_call_id, name=name)

    # ---------- 序列化 ----------

    def to_wire(self) -> dict[str, Any]:
        """转成请求体里的消息对象。空字段必须省略，否则部分服务端会 400。"""
        payload: dict[str, Any] = {"role": str(self.role)}
        if self.content is not None:
            payload["content"] = self.content
        if self.tool_calls:
            payload["tool_calls"] = [tc.to_wire() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            payload["name"] = self.name
        return payload


class Usage(BaseModel):
    """token 用量。做成本可观测必须记录——这是 P4 的基础。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


FinishReason = Literal[
    "stop", "length", "tool_calls", "content_filter", "insufficient_system_resource"
]


class ChatResponse(BaseModel):
    """一次非流式补全的返回。"""

    message: ChatMessage
    finish_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        """模型是否请求调用工具 —— ReAct 循环的判据。

        注意：真实世界里 finish_reason 可能是 "tool_calls"，
        也可能模型给了 tool_calls 但仍标 "stop"，所以以 tool_calls 是否存在为准更稳。
        """
        return bool(self.message.tool_calls)


class StreamDelta(BaseModel):
    """流式返回的一个增量块。

    流式的本质：服务端把同一份响应切成多个 SSE 事件，
    每个事件只带**新增的一小段**。需要客户端自己拼接：
      - content 是字符串，直接累加
      - tool_calls 是"分片"的：第一次出现的分片带 index 和 name，
        后续分片只带 index 和 arguments 的片段，必须按 index 聚合
    """

    content: str = ""
    tool_call_delta: dict[str, Any] | None = None
    finish_reason: str | None = None
    usage: Usage | None = None
