"""LLM 客户端：直连 OpenAI 兼容协议，不经过任何框架。

【为什么手写而不是用 openai SDK / LangChain】

框架会替你隐藏三件事，而这三件事恰好是 Agent 面试最爱问的：
  1. 消息是怎么拼装的（system / user / assistant.tool_calls / tool 的顺序与配对）
  2. 流式响应里 tool_calls 是**分片**到达的，需要按 index 重组
  3. 模型输出的 arguments 可能不是合法 JSON，需要容错与自修复

手写一遍之后，你再看任何框架的源码都能秒懂它在做什么。

【流式 tool_calls 分片长什么样】

服务端逐个 SSE 事件推送，同一个工具调用的参数会被切成多个片：

    data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"calculator","arguments":""}}]}}]}
    data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"expr"}}]}}]}
    data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ession\":\"2+2\"}"}}]}}]}
    data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}
    data: [DONE]

注意 index 才是聚合键：id 和 name 只在第一个分片出现，后续分片只有 arguments 片段。
拼接缺失 id 会直接导致服务端 400 —— 这是新手最常踩的坑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.config import LLMSettings
from app.llm.types import (
    ChatMessage,
    ChatResponse,
    Role,
    StreamDelta,
    ToolCall,
    Usage,
)

logger = logging.getLogger(__name__)


# ============================================================
# 异常体系：让调用方能够按"可重试/不可重试"分流
# ============================================================
class LLMError(RuntimeError):
    """LLM 调用相关错误基类。"""


class LLMConfigError(LLMError):
    """配置问题（缺 key 等），重试无意义。"""


class LLMAuthError(LLMError):
    """401/403，密钥错误，重试无意义。"""


class LLMTransientError(LLMError):
    """可重试错误：429 限流、5xx、网络抖动、超时。"""


class LLMBadRequestError(LLMError):
    """400，请求本身有问题（如消息配对错误），重试无用但需要暴露细节。"""


# ============================================================
# 工具 Schema 的表示（与 tools 模块解耦，避免循环依赖）
# ============================================================
ToolSchema = dict[str, Any]


def _messages_to_wire(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    return [m.to_wire() for m in messages]


class LLMClient:
    """异步 LLM 客户端。

    生命周期由调用方管理（FastAPI 里挂在 lifespan 上，复用连接池）。
    直接用 `async with LLMClient(settings) as client:` 也可以。
    """

    def __init__(self, settings: LLMSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout, connect=15.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={
                "Authorization": f"Bearer {settings.api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
        )

    # ---------- 生命周期 ----------

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------- 请求体构造 ----------

    def _build_payload(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] | None,
        *,
        stream: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._s.is_configured:
            raise LLMConfigError("未配置 LLM_API_KEY，请在 .env 中填写")

        payload: dict[str, Any] = {
            "model": self._s.model,
            "messages": _messages_to_wire(messages),
            "temperature": self._s.temperature if temperature is None else temperature,
            "max_tokens": self._s.max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }

        # 有工具时才传 tools，避免部分服务端对空数组报错
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"

        # 结构化输出（JSON mode）。不是所有兼容端点都支持，遇到 400 会自动降级重试。
        if response_format is not None:
            payload["response_format"] = response_format

        # 流式请求要显式要求服务端在最后一个 chunk 返回 usage，
        # 否则流式调用无法统计 token 成本。
        if stream:
            payload["stream_options"] = {"include_usage": True}

        return payload

    # ---------- 可重试的底层发送 ----------

    async def _post_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        """非流式请求 + 指数退避重试。

        重试策略（工程经验）：
          - 429 / 5xx / 超时 / 连接错误  => 重试，退避 1s, 2s, 4s... 并加随机抖动
          - 401/403                    => 立刻失败，重试只会浪费时间和可能触发风控
          - 400                        => 立刻失败，通常是消息不合法（如 tool 消息没配对）
        """
        last_exc: Exception | None = None

        for attempt in range(self._s.max_retries + 1):
            try:
                resp = await self._client.post("/chat/completions", json=payload)

                if resp.status_code in (401, 403):
                    raise LLMAuthError(
                        f"鉴权失败({resp.status_code})：请检查 LLM_API_KEY / LLM_BASE_URL。{resp.text[:300]}"
                    )

                if resp.status_code == 400:
                    raise LLMBadRequestError(f"请求被拒绝(400)：{resp.text[:500]}")

                if resp.status_code == 429 or resp.status_code >= 500:
                    raise LLMTransientError(f"可重试状态码 {resp.status_code}：{resp.text[:200]}")

                resp.raise_for_status()
                return resp

            except (LLMAuthError, LLMBadRequestError):
                raise
            except (httpx.TimeoutException, httpx.TransportError, LLMTransientError) as exc:
                last_exc = exc
                if attempt >= self._s.max_retries:
                    break
                delay = min(2**attempt, 20) + random.uniform(0, 0.5)
                logger.warning(
                    "LLM 请求失败(第 %d/%d 次)：%s — %.1fs 后重试",
                    attempt + 1,
                    self._s.max_retries + 1,
                    str(exc)[:160],
                    delay,
                )
                await asyncio.sleep(delay)

        raise LLMTransientError(f"重试 {self._s.max_retries} 次后仍失败：{last_exc}") from last_exc

    # ============================================================
    # 对外 API 1：非流式补全
    # ============================================================

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """发一次补全请求并返回完整结果。Agent 循环内部主要用这个。"""
        payload = self._build_payload(
            messages,
            tools,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

        try:
            resp = await self._post_with_retry(payload)
        except LLMBadRequestError:
            # 服务端不支持 response_format 时自动降级重试一次
            if response_format is not None:
                logger.warning("服务端不支持 response_format，降级为普通模式重试")
                payload.pop("response_format", None)
                resp = await self._post_with_retry(payload)
            else:
                raise

        return self._parse_response(resp.json())

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> ChatResponse:
        if not data.get("choices"):
            raise LLMError(f"响应缺少 choices 字段：{json.dumps(data, ensure_ascii=False)[:400]}")

        choice = data["choices"][0]
        msg = choice.get("message") or {}

        tool_calls = [ToolCall.from_wire(tc) for tc in (msg.get("tool_calls") or [])] or None

        raw_usage = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=raw_usage.get("prompt_tokens", 0) or 0,
            completion_tokens=raw_usage.get("completion_tokens", 0) or 0,
            total_tokens=raw_usage.get("total_tokens", 0) or 0,
        )

        return ChatResponse(
            message=ChatMessage(
                role=Role.ASSISTANT,
                content=msg.get("content"),
                tool_calls=tool_calls,
            ),
            finish_reason=choice.get("finish_reason") or "stop",
            usage=usage,
            model=data.get("model") or "",
        )

    # ============================================================
    # 对外 API 2：流式补全
    # ============================================================

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """流式补全，逐个 yield 增量块。

        注意：这里只负责**解析协议**，不做聚合。
        tool_calls 的重组交给 StreamAccumulator —— 职责分离，
        也让"聚合逻辑"可以脱离网络单独做单元测试（难测的东西要隔离）。
        """
        payload = self._build_payload(
            messages, tools, stream=True, temperature=temperature, max_tokens=max_tokens
        )

        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            if resp.status_code in (401, 403):
                body = (await resp.aread()).decode("utf-8", "replace")
                raise LLMAuthError(f"鉴权失败({resp.status_code})：{body[:300]}")
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise LLMBadRequestError(f"流式请求失败({resp.status_code})：{body[:400]}")

            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue  # SSE 允许空行和注释行(: keep-alive)

                data = line[5:].strip()
                if data == "[DONE]":
                    break
                if not data:
                    continue

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    logger.debug("跳过无法解析的 SSE 数据：%s", data[:120])
                    continue

                yield self._parse_chunk(chunk)

    @staticmethod
    def _parse_chunk(chunk: dict[str, Any]) -> StreamDelta:
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}

        usage = None
        if raw := chunk.get("usage"):
            usage = Usage(
                prompt_tokens=raw.get("prompt_tokens", 0) or 0,
                completion_tokens=raw.get("completion_tokens", 0) or 0,
                total_tokens=raw.get("total_tokens", 0) or 0,
            )

        # 取**全部**分片而不是第一个：协议允许一个 chunk 携带多个 tool_calls，
        # 只取 [0] 会静默丢弃其余调用（见 StreamDelta 的说明）。
        tc_deltas = list(delta.get("tool_calls") or [])

        return StreamDelta(
            content=delta.get("content") or "",
            tool_call_deltas=tc_deltas,
            finish_reason=choice.get("finish_reason"),
            usage=usage,
        )


# ============================================================
# 流式增量聚合器
# ============================================================
class StreamAccumulator:
    """把流式增量块重组成一条完整的 assistant 消息。

    这是流式 Agent 的心脏。三个必须处理的细节：

    1. **按 index 聚合 tool_calls**（不是按 id，id 只在首个分片出现）
    2. **参数是字符串拼接**，最后才尝试 json.loads
    3. **usage 通常只在最后一个 chunk 出现**，且此时 choices 可能为空数组
    """

    def __init__(self) -> None:
        self.content_parts: list[str] = []
        self._tool_parts: dict[int, dict[str, str]] = {}
        self.finish_reason: str = "stop"
        self.usage = Usage()

    def feed(self, delta: StreamDelta) -> None:
        if delta.content:
            self.content_parts.append(delta.content)

        # 遍历全部内容：一个 chunk 可能同时携带多个工具调用的分片
        for tc_delta in delta.tool_call_deltas:
            idx = tc_delta.get("index", 0)
            slot = self._tool_parts.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc_id := tc_delta.get("id"):
                slot["id"] = tc_id
            fn = tc_delta.get("function") or {}
            if name := fn.get("name"):
                slot["name"] = name
            if args := fn.get("arguments"):
                slot["arguments"] += args  # 关键：字符串累加，不是覆盖

        if delta.finish_reason:
            self.finish_reason = delta.finish_reason
        if delta.usage:
            self.usage = delta.usage

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    def tool_calls(self) -> list[ToolCall] | None:
        if not self._tool_parts:
            return None
        calls = [
            ToolCall.from_wire(
                {
                    "id": slot["id"] or f"call_{idx}",
                    "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
                }
            )
            for idx, slot in sorted(self._tool_parts.items())
        ]
        return [c for c in calls if c.name] or None

    def build_message(self) -> ChatMessage:
        return ChatMessage(
            role=Role.ASSISTANT,
            content=self.content or None,
            tool_calls=self.tool_calls(),
        )
