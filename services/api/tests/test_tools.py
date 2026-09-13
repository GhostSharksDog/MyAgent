"""工具层测试：校验、安全、错误回灌。

重点测的是**失败路径**，不是成功路径。
因为 Agent 的稳定性取决于"模型给错参数时会发生什么"，
而这恰恰是手工点几下测不到的地方。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from app.llm.types import ToolCall
from app.tools.base import Tool, ToolRegistry, ToolResult
from app.tools.builtin import (
    CalculatorParams,
    ReadResumeParams,
    SearchJobsParams,
    _safe_resolve,
    build_default_registry,
)
from app.tools.errors import ToolError, ToolPermissionError
from pydantic import BaseModel


def _call(name: str, **args: object) -> ToolCall:
    import json

    return ToolCall(id="call_test", name=name, arguments=args, raw_arguments=json.dumps(args))


# ============================================================
# 计算器
# ============================================================
class TestCalculator:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("2+3*4", "14"),
            # 浮点误差必须被清理：裸算会得到 115200.00000000001
            ("(12000*12)*0.8", "115200"),
            ("2**10", "1024"),
            ("sum([1,2,3])/3", "2"),
            ("-5+3", "-2"),
            ("10 % 3", "1"),
            ("round(3.14159, 2)", "3.14"),
            ("10/4", "2.5"),
        ],
    )
    async def test_valid_expressions(self, expr: str, expected: str) -> None:
        registry = build_default_registry()
        result = await registry.execute(_call("calculator", expression=expr))
        assert result.ok, result.error
        assert result.content.endswith(f"= {expected}")

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("0.1+0.2", "0.3"),  # 经典浮点陷阱：裸算得 0.30000000000000004
            ("1/3*3", "1"),  # 裸算得 1.0（恰好）或 0.9999999999999999
            ("2.675*100", "267.5"),
        ],
    )
    async def test_floating_point_noise_cleaned(self, expr: str, expected: str) -> None:
        """这些数字会原样被模型念给用户，尾数必须干净。"""
        registry = build_default_registry()
        result = await registry.execute(_call("calculator", expression=expr))
        assert result.ok, result.error
        assert result.content.endswith(f"= {expected}")

    @pytest.mark.parametrize(
        "expr",
        [
            "__import__('os').system('echo pwned')",  # 代码注入
            "open('/etc/passwd').read()",  # 文件读取
            "eval('1+1')",  # 嵌套 eval
            "9**9**9",  # CPU 耗尽
            "lambda x: x",  # 不支持的语法
        ],
    )
    async def test_dangerous_expressions_rejected(self, expr: str) -> None:
        """安全边界：模型给出的参数等同用户输入，必须按不可信数据处理。

        断言的是"没能成功执行"这个契约，而不是具体错误文案——
        文案会随迭代变化，契约不会。
        """
        registry = build_default_registry()
        result = await registry.execute(_call("calculator", expression=expr))
        assert not result.ok, f"危险表达式竟然执行成功了：{expr}"
        assert result.error
        # 回灌给模型的观察结果必须明确标记失败，模型才知道此路不通
        assert "工具执行失败" in result.as_observation()

    async def test_syntax_error_is_recoverable(self) -> None:
        """语法错误不能抛异常，必须变成可回灌的观察结果。"""
        registry = build_default_registry()
        result = await registry.execute(_call("calculator", expression="2 +"))
        assert not result.ok
        assert "语法错误" in result.content


# ============================================================
# 参数校验：这是 self-healing 的基础
# ============================================================
class TestValidation:
    async def test_missing_required_field_returns_schema_hint(self) -> None:
        registry = build_default_registry()
        result = await registry.execute(_call("calculator"))  # 缺 expression
        assert not result.ok
        # 报错里必须带上 schema 提示，模型才能自我修正
        assert "参数校验失败" in result.content
        assert "expression" in result.content

    async def test_out_of_range_value_rejected(self) -> None:
        registry = build_default_registry()
        result = await registry.execute(_call("search_jobs", keyword="Python", limit=999))
        assert not result.ok
        assert "参数校验失败" in result.content

    async def test_unknown_tool_lists_available(self) -> None:
        """模型幻觉出不存在的工具名时，要告诉它有哪些工具可用。"""
        registry = build_default_registry()
        result = await registry.execute(_call("nonexistent_tool"))
        assert not result.ok
        assert "不存在" in result.content
        assert "calculator" in result.content

    async def test_malformed_json_arguments_do_not_crash(self) -> None:
        """模型偶尔会吐出非法 JSON，此时参数应为空 dict 而不是崩溃。"""
        call = ToolCall.from_wire(
            {"id": "x", "function": {"name": "calculator", "arguments": "{'expr': broken"}}
        )
        assert call.arguments == {}
        assert call.raw_arguments == "{'expr': broken"

        registry = build_default_registry()
        result = await registry.execute(call)
        assert not result.ok  # 校验失败，但不是进程崩溃


# ============================================================
# 路径穿越防护
# ============================================================
class TestSecurity:
    @pytest.mark.parametrize(
        "evil",
        ["../../.env", "../.env", "..\\..\\.env", "../../../Windows/System32/config"],
    )
    def test_path_traversal_blocked(self, evil: str, tmp_path: pytest.TempPathFactory) -> None:
        from pathlib import Path

        base = Path(str(tmp_path)) / "sandbox"
        base.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ToolPermissionError):
            _safe_resolve(base, evil)

    def test_normal_path_allowed(self, tmp_path: pytest.TempPathFactory) -> None:
        from pathlib import Path

        base = Path(str(tmp_path)) / "sandbox"
        base.mkdir(parents=True, exist_ok=True)
        assert _safe_resolve(base, "resume.md") == (base / "resume.md").resolve()


# ============================================================
# 注册表
# ============================================================
class TestRegistry:
    def test_default_registry_has_expected_tools(self) -> None:
        registry = build_default_registry()
        assert registry.names() == [
            "calculator",
            "get_current_time",
            "read_resume",
            "search_jobs",
            "search_knowledge",
        ]

    def test_schemas_are_openai_compatible(self) -> None:
        registry = build_default_registry()
        for schema in registry.schemas():
            assert schema["type"] == "function"
            fn = schema["function"]
            assert fn["name"] and fn["description"]
            # 必须是合法 JSON Schema
            assert fn["parameters"]["type"] == "object"
            assert "properties" in fn["parameters"]

    def test_duplicate_name_rejected(self) -> None:
        registry = ToolRegistry()

        def fn(params: BaseModel) -> ToolResult:
            return ToolResult.success("ok")

        registry.register_fn("dup", "d", ReadResumeParams, fn)
        with pytest.raises(ValueError, match="工具名重复"):
            registry.register_fn("dup", "d", ReadResumeParams, fn)

    async def test_tool_timeout_is_caught(self) -> None:
        """工具卡死不能拖垮整个 Agent。"""
        import asyncio

        registry = ToolRegistry()

        async def slow(params: BaseModel) -> ToolResult:
            await asyncio.sleep(5)
            return ToolResult.success("never")

        registry.register_fn("slow", "慢工具", ReadResumeParams, slow, timeout=0.05)
        result = await registry.execute(_call("slow"))
        assert not result.ok
        assert "超时" in result.content

    async def test_tool_env_error_returns_observation_not_raise(self) -> None:
        """工具内部抛任意异常也必须降级为观察结果。"""
        registry = ToolRegistry()

        def boom(params: BaseModel) -> ToolResult:
            raise KeyError("内部 bug")

        registry.register_fn("boom", "会炸的工具", ReadResumeParams, boom)
        result = await registry.execute(_call("boom"))
        assert not result.ok
        assert "KeyError" in result.content

    async def test_long_output_truncated(self) -> None:
        """超长输出必须截断，否则会撑爆上下文窗口。"""
        registry = ToolRegistry()

        def verbose(params: BaseModel) -> ToolResult:
            return ToolResult.success("x" * 50_000)

        registry.register_fn("verbose", "话痨工具", ReadResumeParams, verbose)
        result = await registry.execute(_call("verbose"))
        assert result.ok
        assert result.truncated
        assert len(result.content) < 9_000
        assert "已省略" in result.content


# ============================================================
# 岗位检索
# ============================================================
class TestSearchJobs:
    async def test_keyword_hit(self) -> None:
        registry = build_default_registry()
        result = await registry.execute(_call("search_jobs", keyword="Agent", limit=5))
        assert result.ok
        assert "Agent" in result.content

    async def test_city_filter(self) -> None:
        registry = build_default_registry()
        result = await registry.execute(_call("search_jobs", city="北京", limit=10))
        assert result.ok
        assert "北京" in result.content

    async def test_no_match_gives_actionable_error(self) -> None:
        """查不到时要告诉模型有哪些可用选项，而不是简单的"无结果"。"""
        registry = build_default_registry()
        result = await registry.execute(_call("search_jobs", keyword="不存在的岗位xyz", limit=3))
        assert not result.ok
        assert "没有找到" in result.content
        assert "覆盖城市" in result.content

    def test_search_params_defaults(self) -> None:
        p = SearchJobsParams()
        assert p.limit == 3
        assert p.keyword == ""

    def test_calculator_params_requires_expression(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            CalculatorParams()  # type: ignore[call-arg]


# ============================================================
# 失败路径的可观测性（回归测试）
# ============================================================
class TestFailureObservability:
    """失败也必须记录耗时。

    曾经的 bug：`Tool.execute` 只在成功路径计算 duration_ms，阶段 1（参数校验）
    与阶段 2（超时/异常）都提前 return，导致**失败路径的耗时恒为 0**。
    一个跑了 30 秒才超时的工具在 trace 里显示 0ms，排查超时问题会被假数据带偏。
    """

    async def test_timeout_records_elapsed_time(self) -> None:
        registry = ToolRegistry()

        async def slow(params: BaseModel) -> ToolResult:
            await asyncio.sleep(5)
            return ToolResult.success("never")

        registry.register_fn("slow", "慢工具", ReadResumeParams, slow, timeout=0.15)
        result = await registry.execute(_call("slow"))

        assert not result.ok
        assert "超时" in result.content
        assert result.duration_ms >= 100, f"超时耗时未记录：{result.duration_ms}ms"

    async def test_tool_error_records_elapsed_time(self) -> None:
        registry = ToolRegistry()

        async def failing(params: BaseModel) -> ToolResult:
            await asyncio.sleep(0.1)
            raise ToolError("boom")

        registry.register_fn("failing", "会失败", ReadResumeParams, failing)
        result = await registry.execute(_call("failing"))

        assert not result.ok
        assert result.duration_ms >= 80, f"失败耗时未记录：{result.duration_ms}ms"

    async def test_internal_error_still_stamps_duration(self) -> None:
        registry = ToolRegistry()

        def boom(params: BaseModel) -> ToolResult:
            raise KeyError("内部 bug")

        registry.register_fn("boom", "会炸", ReadResumeParams, boom)
        result = await registry.execute(_call("boom"))
        assert not result.ok
        assert isinstance(result.duration_ms, int)  # 字段必须被填充，不能是 None


# ============================================================
# 同步工具不能阻塞事件循环（回归测试）
# ============================================================
class TestSyncToolDoesNotBlockLoop:
    """同步工具必须在线程池里跑。

    曾经的 bug：`FunctionTool.run` 直接调用同步函数（内置的 calculator /
    read_resume / search_jobs 全是同步的），在事件循环线程内执行。
    asyncio 的取消是在 await 点注入的，**一个不含 await 的同步函数会一路跑完，
    期间整个事件循环被阻塞** —— asyncio.wait_for 的超时形同虚设，
    其他请求、心跳、定时器全部停摆。

    验证方法：跑一个同步阻塞工具的同时，起一个每 20ms 自增的心跳协程。
    若事件循环被阻塞，心跳次数会是 0。
    """

    async def test_heartbeat_keeps_ticking_during_sync_tool(self) -> None:
        registry = ToolRegistry()

        def blocking(params: BaseModel) -> ToolResult:
            time.sleep(0.3)  # 纯同步阻塞，不含任何 await
            return ToolResult.success("done")

        registry.register_fn("blocking", "同步阻塞工具", ReadResumeParams, blocking)

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            result = await registry.execute(_call("blocking"))
        finally:
            beat.cancel()

        assert result.ok
        # 0.3s / 0.02s ≈ 15 次；即使调度有抖动也应远大于 3
        assert ticks >= 5, f"事件循环被同步工具阻塞了，心跳只跑了 {ticks} 次"

    async def test_async_tool_still_works(self) -> None:
        """修同步路径不能把异步路径改坏。"""
        registry = ToolRegistry()

        async def async_tool(params: BaseModel) -> ToolResult:
            await asyncio.sleep(0.01)
            return ToolResult.success("async ok")

        registry.register_fn("async_tool", "异步工具", ReadResumeParams, async_tool)
        result = await registry.execute(_call("async_tool"))
        assert result.ok
        assert result.content == "async ok"

    async def test_sync_tool_timeout_is_enforced(self) -> None:
        """放进线程池后超时才能真正生效（否则 wait_for 拦不住同步代码）。"""
        registry = ToolRegistry()

        def very_slow(params: BaseModel) -> ToolResult:
            time.sleep(2.0)
            return ToolResult.success("never")

        registry.register_fn("very_slow", "极慢同步工具", ReadResumeParams, very_slow, timeout=0.1)
        start = time.monotonic()
        result = await registry.execute(_call("very_slow"))
        elapsed = time.monotonic() - start

        assert not result.ok
        assert "超时" in result.content
        # 关键：不应等满 2 秒。线程池化后 wait_for 能及时返回。
        assert elapsed < 1.0, f"超时未生效，实际等待 {elapsed:.2f}s"


# ============================================================
# run 的同步/异步分派（回归测试）
# ============================================================
class TestToolDispatch:
    """Tool 子类把 `run` 写成同步方法时也必须能正常工作。

    【一次真实的 E2E 事故】
    基类把 `run` 声明为 `async def`，但子类很容易写成同步的
    `def run(...)` —— 尤其当工具只是写个文件、查个内存表时，
    写 async 看起来是多余的。此时 `await self.run(params)` 会抛：
        TypeError: object ToolResult can't be used in 'await' expression

    更糟的是**副作用已经执行了**（同步体跑完才轮到 await 报错），
    于是模型看到"失败"并重试，副作用被执行了两遍。

    而直接调用 `tool.run()` 的单元测试完全正常 ——
    只有走 `Tool.execute` 的真实路径才暴露。这就是为什么
    "单元测试全绿"不能替代端到端验证。
    """

    async def test_sync_run_works_through_execute(self) -> None:
        class SyncTool(Tool):
            name = "sync_tool"
            description = "同步实现的工具"
            params_model = ReadResumeParams

            def run(self, params: BaseModel) -> ToolResult:  # type: ignore[override]
                return ToolResult.success("同步执行成功")

        registry = ToolRegistry()
        registry.register(SyncTool())
        result = await registry.execute(_call("sync_tool"))
        assert result.ok
        assert result.content == "同步执行成功"

    async def test_sync_run_does_not_block_event_loop(self) -> None:
        """同步实现必须走线程池 —— 否则它和同步函数一样阻塞事件循环。"""

        class BlockingSyncTool(Tool):
            name = "blocking_sync"
            description = "同步阻塞工具"
            params_model = ReadResumeParams

            def run(self, params: BaseModel) -> ToolResult:  # type: ignore[override]
                time.sleep(0.3)
                return ToolResult.success("done")

        registry = ToolRegistry()
        registry.register(BlockingSyncTool())

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            result = await registry.execute(_call("blocking_sync"))
        finally:
            beat.cancel()

        assert result.ok
        assert ticks >= 5, f"事件循环被同步实现的工具阻塞了，心跳只跑了 {ticks} 次"

    async def test_sync_run_timeout_enforced(self) -> None:
        class SlowSyncTool(Tool):
            name = "slow_sync"
            description = "慢的同步工具"
            params_model = ReadResumeParams
            timeout = 0.1

            def run(self, params: BaseModel) -> ToolResult:  # type: ignore[override]
                time.sleep(2.0)
                return ToolResult.success("never")

        registry = ToolRegistry()
        registry.register(SlowSyncTool())

        start = time.monotonic()
        result = await registry.execute(_call("slow_sync"))
        elapsed = time.monotonic() - start

        assert not result.ok
        assert "超时" in result.content
        assert elapsed < 1.0, f"同步路径的超时未生效，实际等待 {elapsed:.2f}s"

    async def test_sync_run_exception_becomes_observation(self) -> None:
        class FailingSyncTool(Tool):
            name = "failing_sync"
            description = "会抛异常的同步工具"
            params_model = ReadResumeParams

            def run(self, params: BaseModel) -> ToolResult:  # type: ignore[override]
                raise ValueError("同步实现内部错误")

        registry = ToolRegistry()
        registry.register(FailingSyncTool())
        result = await registry.execute(_call("failing_sync"))
        assert not result.ok
        assert "ValueError" in result.content
