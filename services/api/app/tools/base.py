"""工具系统：Agent 与真实世界交互的唯一出口。

【核心认知】模型自己不执行任何东西，它只是"申请"调用一个函数。
真正的执行、参数校验、错误处理、超时控制，全部发生在下面这段代码里。
所以 Agent 的可靠性，一大半取决于工具层的设计，而不是模型本身。

【设计要点】

1. **参数用 Pydantic 建模**，JSON Schema 从模型自动生成。
   好处：一处定义，双向受益——既约束模型输出，又校验模型输出。
   手写 JSON Schema 迟早会和 Python 函数签名脱节。

2. **错误是数据，不是异常**。
   工具抛异常绝不能中断整个 Agent 循环。
   正确做法：把错误信息作为 observation 回灌给模型，模型通常能自我修正
   （比如参数名写错、少了必填字段）。这叫 self-healing，是 Agent 与
   "调一次 API 就完事"的本质区别。

3. **默认不可信**。
   模型给出的参数是"用户输入级别的不可信数据"：
   要防路径穿越、防任意代码执行、防超长输出撑爆上下文。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.types import ToolCall
from app.tools.errors import ToolError, ToolTimeout, ToolValidationError

logger = logging.getLogger(__name__)

TParams = TypeVar("TParams", bound=BaseModel)

# 单次工具输出的最大字符数。超出会被截断——
# 否则一个"读取大文件"的工具能瞬间把上下文窗口吃掉，
# 表现为"Agent 突然失忆"或 token 费用暴涨。
MAX_OBSERVATION_CHARS = 8_000


class ToolResult(BaseModel):
    """工具执行结果。统一的结构让上层可以无差别处理成功与失败。"""

    ok: bool
    content: str
    error: str | None = None
    # 便于做可观测：工具耗时、是否被截断
    duration_ms: int = 0
    truncated: bool = False

    @classmethod
    def success(cls, content: str, **kw: Any) -> ToolResult:
        return cls(ok=True, content=content, **kw)

    @classmethod
    def failure(cls, error: str, **kw: Any) -> ToolResult:
        return cls(ok=False, content=error, error=error, **kw)

    def as_observation(self) -> str:
        """转成回灌给模型的文本。

        失败时**不隐藏错误**而是明确告知——模型需要知道"这条路走不通"，
        才会换策略。把错误吞掉伪装成空结果，是 Agent 陷入死循环的常见原因。
        """
        if self.ok:
            return self.content
        return f"[工具执行失败] {self.error}\n请检查参数是否正确，或改用其他方式完成任务。"


class Tool(ABC):
    """工具抽象基类。

    子类只需声明三样东西：名字、给模型看的描述、参数模型。
    """

    name: str
    description: str
    params_model: type[BaseModel]
    # 单次执行超时（秒）。防止某个工具卡住导致整个 Agent 永久挂起。
    timeout: float = 30.0

    @abstractmethod
    async def run(self, params: BaseModel) -> ToolResult:
        """执行工具。参数已经过 Pydantic 校验，可放心使用。"""
        raise NotImplementedError

    def json_schema(self) -> dict[str, Any]:
        """转成 OpenAI 协议要求的 tools[].function 结构。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_model.model_json_schema(),
            },
        }

    async def execute(self, call: ToolCall) -> ToolResult:
        """带校验、超时、异常兜底的执行入口。Agent 循环只调用这个方法。"""
        started = asyncio.get_running_loop().time()

        # ---- 阶段 1：参数校验 ----
        try:
            params = self.params_model.model_validate(call.arguments)
        except ValidationError as exc:
            # 把 Pydantic 的报错精简成模型能看懂的形式（原始报错对模型太啰嗦）
            detail = "; ".join(
                f"字段 {'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
            )
            return ToolResult.failure(
                f"参数校验失败（{self.name}）：{detail}。"
                f"请按此 Schema 重新调用：{json.dumps(self.params_model.model_json_schema().get('properties', {}), ensure_ascii=False)}"
            )
        except Exception as exc:
            return ToolResult.failure(f"参数解析失败（{self.name}）：{exc}")

        # ---- 阶段 2：执行 + 超时 ----
        try:
            result = await asyncio.wait_for(self.run(params), timeout=self.timeout)
        except TimeoutError:
            return ToolResult.failure(f"工具 {self.name} 执行超时（>{self.timeout}s）")
        except ToolError as exc:
            return ToolResult.failure(str(exc))
        except asyncio.CancelledError:
            raise  # 取消是控制流，必须透传
        except Exception as exc:
            logger.exception("工具 %s 执行异常", self.name)
            return ToolResult.failure(f"工具 {self.name} 内部错误：{type(exc).__name__}: {exc}")

        # ---- 阶段 3：输出裁剪 ----
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        content, truncated = _truncate(result.content)
        return result.model_copy(
            update={"duration_ms": duration_ms, "truncated": truncated, "content": content}
        )


def _truncate(text: str, limit: int = MAX_OBSERVATION_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    omitted = len(text) - limit
    return f"{head}\n\n... [已省略 {omitted} 字符] ...\n\n{tail}", True


class FunctionTool(Tool):
    """把一个（同步或异步）函数包装成 Tool。

    用参数模型的 docstring/Field description 自动生成工具描述，
    因为**工具描述的质量直接决定模型会不会正确使用它**——
    描述写得含糊，模型就会乱调或漏调。
    """

    def __init__(
        self,
        name: str,
        description: str,
        params_model: type[BaseModel],
        fn: Callable[[BaseModel], Awaitable[ToolResult] | ToolResult],
        *,
        timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.description = description
        self.params_model = params_model
        self.timeout = timeout
        self._fn = fn

    async def run(self, params: BaseModel) -> ToolResult:
        out = self._fn(params)
        if inspect.isawaitable(out):
            return await out
        return out


class ToolRegistry:
    """工具注册表。Agent 从这里取 schema 给模型，也从这里执行调用。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具名重复：{tool.name}")
        self._tools[tool.name] = tool
        return tool

    def register_fn(
        self,
        name: str,
        description: str,
        params_model: type[BaseModel],
        fn: Callable[[BaseModel], Awaitable[ToolResult] | ToolResult],
        *,
        timeout: float = 30.0,
    ) -> Tool:
        return self.register(FunctionTool(name, description, params_model, fn, timeout=timeout))

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """给模型看的工具清单。为空时返回空列表（请求体里就不带 tools 字段）。"""
        return [t.json_schema() for t in self._tools.values()]

    async def execute(self, call: ToolCall) -> ToolResult:
        """按名字分发执行。未知工具名也要优雅处理——
        模型偶尔会"幻觉"出一个不存在的工具名，这不该让整个会话崩溃。"""
        tool = self._tools.get(call.name)
        if tool is None:
            available = "、".join(self.names()) or "（无）"
            return ToolResult.failure(f"不存在名为 {call.name!r} 的工具。当前可用工具：{available}")
        return await tool.execute(call)


__all__ = [
    "MAX_OBSERVATION_CHARS",
    "FunctionTool",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolTimeout",
    "ToolValidationError",
]
