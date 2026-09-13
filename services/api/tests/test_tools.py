"""工具层测试：校验、安全、错误回灌。

重点测的是**失败路径**，不是成功路径。
因为 Agent 的稳定性取决于"模型给错参数时会发生什么"，
而这恰恰是手工点几下测不到的地方。
"""

from __future__ import annotations

import pytest
from app.llm.types import ToolCall
from app.tools.base import ToolRegistry, ToolResult
from app.tools.builtin import (
    CalculatorParams,
    ReadResumeParams,
    SearchJobsParams,
    _safe_resolve,
    build_default_registry,
)
from app.tools.errors import ToolPermissionError
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
        assert registry.names() == ["calculator", "get_current_time", "read_resume", "search_jobs"]

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
