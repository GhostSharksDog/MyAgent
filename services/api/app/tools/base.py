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
    # 是否**不可并发**执行。默认 False = 可以和其他工具同时跑。
    #
    # 【为什么这个标志必须由工具自己声明，而不是由循环去猜】
    # 同一个模型回合里的多个工具调用会被并发执行（延迟从 3T 降到 1T）。
    # 这对**只读**工具是纯收益：读 A 和读 B 之间没有任何关系。
    # 但对**有副作用**的工具不成立 —— 两个协程交错地写同一个文件、
    # 或两次"发消息"，结果是不可复现的，而且只在"模型一次吐出多个调用"时出现。
    #
    # 判断"这个工具能不能并发"需要知道它内部在做什么，那是工具作者的知识，
    # 不是循环能推断出来的。所以由工具声明，循环据此决定策略。
    serial: bool = False

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

    async def prepare_execution(self, call: ToolCall) -> Any:
        """可选的只读准备/人审，发生在副作用互斥和工具执行超时之外。"""
        return None

    async def execute_prepared(self, call: ToolCall, preparation: Any) -> ToolResult:
        return await self.execute(call)

    async def _invoke(self, params: BaseModel) -> ToolResult:
        """按 `run` 的实际形态分派：协程直接等，同步函数丢线程池。

        【为什么需要这一层 —— 一次真实的 E2E 事故】
        基类把 `run` 声明为 `async def`，但子类很容易写成同步的
        `def run(...)`（尤其当工具只是写个文件、查个内存表时，
        写 async 看起来是多余的）。此时 `await self.run(params)` 会抛
        `TypeError: object ToolResult can't be used in 'await' expression`。

        更糟的是**副作用已经执行了**（同步体跑完了才轮到 await 报错），
        于是模型看到"失败"并重试，实际却把副作用又执行了一遍。
        这类问题的隐蔽之处在于：直接调用 `tool.run()` 的单元测试完全正常，
        只有走 `Tool.execute` 的真实路径才会暴露 ——
        这也说明为什么"单元测试全绿"不能替代端到端验证。

        修正方式是把分派逻辑统一放在基类：
          - `run` 是协程函数 → 直接 await
          - `run` 是同步函数 → `asyncio.to_thread` 执行，
            既不阻塞事件循环，`asyncio.wait_for` 的超时也才能真正生效
        """
        from app.agent.operations import record_operation

        record_operation("running")
        if inspect.iscoroutinefunction(self.run):
            return await self.run(params)
        return await self._invoke_sync(self.run, params)

    async def _invoke_sync(self, fn: Callable[..., Any], *args: Any) -> Any:
        if not self.serial:
            return await asyncio.to_thread(fn, *args)
        # 线程中的副作用无法被 asyncio 取消。必须等线程结束后才释放注册表锁，
        # 否则超时/断流会让下一个写入与仍在运行的线程重叠。
        operation = asyncio.create_task(asyncio.to_thread(fn, *args))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not operation.cancelled():
                operation.exception()  # 取走异常，避免后台任务无人消费
            raise

    async def execute(self, call: ToolCall) -> ToolResult:
        """带校验、超时、异常兜底的执行入口。Agent 循环只调用这个方法。

        【为什么所有 return 都要经过 _stamp()】
        最初实现只在成功路径计算耗时，导致**失败路径的 duration_ms 恒为 0**——
        一个跑了 30 秒才超时的工具，在 trace 里显示 0ms，可观测数据完全失真。
        排查超时问题时会被这个假数据带偏，所以失败路径的耗时同样必须记录。
        """
        started = asyncio.get_running_loop().time()

        def stamp(result: ToolResult) -> ToolResult:
            return result.model_copy(
                update={"duration_ms": int((asyncio.get_running_loop().time() - started) * 1000)}
            )

        # ---- 阶段 1：参数校验 ----
        try:
            params = self.params_model.model_validate(call.arguments)
        except ValidationError as exc:
            # 把 Pydantic 的报错精简成模型能看懂的形式（原始报错对模型太啰嗦）
            detail = "; ".join(
                f"字段 {'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
            )
            schema_hint = json.dumps(
                self.params_model.model_json_schema().get("properties", {}), ensure_ascii=False
            )
            return stamp(
                ToolResult.failure(
                    f"参数校验失败（{self.name}）：{detail}。请按此 Schema 重新调用：{schema_hint}"
                )
            )
        except Exception as exc:
            return stamp(ToolResult.failure(f"参数解析失败（{self.name}）：{exc}"))

        # ---- 阶段 2：执行 + 超时 ----
        try:
            result = await asyncio.wait_for(self._invoke(params), timeout=self.timeout)
        except TimeoutError:
            return stamp(ToolResult.failure(f"工具 {self.name} 执行超时（>{self.timeout}s）"))
        except ToolError as exc:
            return stamp(ToolResult.failure(str(exc)))
        except asyncio.CancelledError:
            raise  # 取消是控制流，必须透传
        except Exception as exc:
            logger.exception("工具 %s 执行异常", self.name)
            return stamp(
                ToolResult.failure(f"工具 {self.name} 内部错误：{type(exc).__name__}: {exc}")
            )

        # ---- 阶段 3：输出裁剪 ----
        content, truncated = _truncate(result.content)
        return stamp(
            result.model_copy(
                update={"truncated": result.truncated or truncated, "content": content}
            )
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
        self._is_async = inspect.iscoroutinefunction(fn)

    async def run(self, params: BaseModel) -> ToolResult:
        """执行工具函数。

        【关键：同步函数必须丢到线程池，不能用 asyncio.wait_for 硬等】
        `asyncio.wait_for` 只能"放弃等待"协程，**无法中断正在执行的同步代码**：
        asyncio 的取消是在 await 点注入的，一个不含 await 的同步函数会一路跑完，
        期间整个事件循环被阻塞，其他请求、心跳、定时器全部停摆。

        本项目内置的 calculator / read_resume / search_jobs 都是同步函数，
        读一个大文件或做重计算时就会阻塞整个服务。

        解法与 cli.py 里处理 input() 一致：用 asyncio.to_thread 把同步调用
        移到线程池。这样 wait_for 超时能真正生效（线程会被放弃等待），
        事件循环也始终保持可调度。
        """
        if self._is_async:
            return await self._fn(params)  # type: ignore[misc,return-value]

        result = await self._invoke_sync(self._fn, params)
        if inspect.isawaitable(result):
            # 极少数情况：同步函数返回协程（如包装了 functools.partial）
            return await result
        return result  # type: ignore[return-value]


class ToolRegistry:
    """工具注册表。Agent 从这里取 schema 给模型，也从这里执行调用。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # 跨请求与专家共享，write_file / edit_file 可能操作同一目标。
        self._serial_lock = asyncio.Lock()

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

    def replace_tools(self, registry: ToolRegistry) -> None:
        """配置刷新时保留注册表与共享锁；在途调用继续持有已取出的工具。"""
        self._tools = registry._tools.copy()

    def is_serial(self, name: str) -> bool:
        """这个工具是否声明了"不可并发"。

        未知工具名返回 False（按可并发处理）：它马上会被当成"不存在的工具"
        失败掉，既没有副作用，也没有必要为它把整段执行退回串行。
        """
        tool = self._tools.get(name)
        return bool(tool is not None and tool.serial)

    def schemas(self) -> list[dict[str, Any]]:
        """给模型看的工具清单。为空时返回空列表（请求体里就不带 tools 字段）。"""
        return [t.json_schema() for t in self._tools.values()]

    async def execute(self, call: ToolCall) -> ToolResult:
        """按名字分发执行。未知工具名也要优雅处理——
        模型偶尔会"幻觉"出一个不存在的工具名，这不该让整个会话崩溃。"""
        from app.agent.operations import track_operation
        from app.agent.runtime import current_run_context

        if (context := current_run_context()) is None:
            return await self._execute(call)
        with track_operation(context, call.name) as fact:
            try:
                result = await self._execute(call)
                if fact["status"] not in {"succeeded", "unknown"}:
                    if result.ok:
                        fact["status"] = "succeeded"
                    elif fact["status"] == "running":
                        fact["status"] = "failed"
                return result
            finally:
                if fact["status"] == "running":
                    fact["status"] = "unknown"

    async def _execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            available = "、".join(self.names()) or "（无）"
            return ToolResult.failure(f"不存在名为 {call.name!r} 的工具。当前可用工具：{available}")
        preparation = await tool.prepare_execution(call)
        if isinstance(preparation, ToolResult):
            return preparation
        if tool.serial:
            async with self._serial_lock:
                # 排队期间其他专家可能耗尽共享预算；拿到锁后不能继续启动副作用。
                from app.agent.runtime import current_run_context

                if context := current_run_context():
                    context.check()
                return await tool.execute_prepared(call, preparation)
        return await tool.execute_prepared(call, preparation)


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
