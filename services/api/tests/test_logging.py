"""日志形态测试（技术债 T05 的 JSON 那一半）。

【这个文件盯的是什么】

结构化日志最容易做成"看起来是 JSON、实际没法用"：

  · 每个字段都是字符串、额外字段被丢掉 → 它只是给文本加了一层引号，
    聚合查询依然做不了（那正是做这件事的**唯一**理由）；
  · 异常堆栈塞进 message → message 每一行都不同，聚合结果的基数爆炸，
    而高基数标签/字段是日志系统里最贵的反模式；
  · 中文被转义成 \\uXXXX → 体积白涨一倍，人也没法直接看；
  · 调用方通过 `extra=` 传进来一个对象时序列化抛异常 → **那条日志直接丢失**，
    而且报错文本与被记录的事件毫无关系（最坏的一种：出故障时日志消失）。

所以这里的断言都对着"可用性"而不是"格式正确"。
"""

from __future__ import annotations

import json
import logging

import pytest
from app.core.logging import JsonFormatter
from app.core.telemetry import TraceIdFilter, clear_trace_id, set_trace_id


def _format(
    message: str,
    *,
    level: int = logging.INFO,
    extra: dict[str, object] | None = None,
    exc_info: bool = False,
) -> dict[str, object]:
    """造一条日志记录、过一遍 JsonFormatter，返回解析后的对象。"""
    record = logging.LogRecord(
        name="app.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    if extra:
        for key, value in extra.items():
            setattr(record, key, value)
    if exc_info:
        try:
            raise ValueError("故意抛的")
        except ValueError:
            import sys

            record.exc_info = sys.exc_info()
    TraceIdFilter().filter(record)  # 生产上挂在 handler 上，这里手动过一遍
    return json.loads(JsonFormatter().format(record))


class TestJsonShape:
    def test_one_json_object_per_line(self) -> None:
        """JSON Lines：每行一个独立对象，可以被采集器逐行读走。"""
        raw = JsonFormatter().format(
            logging.LogRecord("app.x", logging.INFO, __file__, 1, "一行日志", None, None)
        )
        assert "\n" not in raw
        assert json.loads(raw)["message"] == "一行日志"

    def test_required_fields_present(self) -> None:
        """字段名与主流采集器的默认约定一致，不改配置就能被识别。"""
        payload = _format("你好")
        for key in ("ts", "level", "logger", "trace_id", "message"):
            assert key in payload, f"缺少字段 {key}"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "app.test"
        # ts 用 ISO8601 且带时区 —— 没有时区的时间戳在跨机器排查时是个陷阱
        assert str(payload["ts"]).startswith("20")
        assert "+00:00" in str(payload["ts"]) or "Z" in str(payload["ts"])

    def test_trace_id_comes_from_the_context(self) -> None:
        """trace_id 必须来自当前上下文，否则整条链路串不起来。"""
        set_trace_id("abc123def456")
        try:
            assert _format("x")["trace_id"] == "abc123def456"
        finally:
            clear_trace_id()
        # 清空之后**不能残留上一个 id** —— 否则并发复用同一个 worker 时，
        # 两个请求的日志会互相串味，而串味的日志比没有日志更误导人。
        # （没有 id 时 filter 会填占位符 "-"，所以这里断言的是"不再是旧值"。）
        assert _format("x")["trace_id"] != "abc123def456"

    def test_extra_fields_are_kept(self) -> None:
        """**这条是整件事的重点**：`extra=` 的字段必须进 JSON。

        丢掉了它们，结构化日志就只是"给文本加一层引号" ——
        而能被聚合的恰恰是这些字段（step / tool / ok / duration_ms）。
        """
        payload = _format("工具执行完成", extra={"step": 3, "tool": "grep", "ok": True})
        assert payload["step"] == 3
        assert payload["tool"] == "grep"
        assert payload["ok"] is True

    def test_exception_goes_to_its_own_field(self) -> None:
        """堆栈放 `exc`，不混进 message —— message 要能被聚合。"""
        payload = _format("工具崩了", exc_info=True)
        assert payload["message"] == "工具崩了"
        assert "ValueError" in str(payload["exc"])
        assert "Traceback" in str(payload["exc"])

    def test_non_serializable_extra_does_not_lose_the_log(self) -> None:
        """extra 里塞了任意对象时，降级成字符串而不是丢日志。

        【为什么这条必须有】
        `json.dumps` 抛 TypeError 的位置在 formatter 里，
        而 logging 的异常处理会让**这条日志消失**（或者打到 stderr 上，
        与被记录的事件看不出关系）。出故障的那一刻日志丢了，是最坏的组合。
        """

        class Weird:
            def __str__(self) -> str:  # pragma: no cover - 只要求它有 __str__
                return "weird-object"

        payload = _format("带了个怪东西", extra={"thing": Weird()})
        assert payload["thing"] == "weird-object"

    def test_chinese_is_not_escaped(self) -> None:
        """中文保持原样：ensure_ascii=False 省一半体积，且人能直接看。"""
        raw = JsonFormatter().format(
            logging.LogRecord("app.x", logging.INFO, __file__, 1, "中文日志", None, None)
        )
        assert "中文日志" in raw
        assert "\\u" not in raw


class TestSetupLogging:
    @pytest.fixture(autouse=True)
    def _reset(self):  # type: ignore[no-untyped-def]
        """setup_logging 是"只配置一次"的，测试之间必须复位。"""
        import app.core.logging as mod

        original = mod._CONFIGURED
        mod._CONFIGURED = False
        yield
        mod._CONFIGURED = False
        logging.getLogger().handlers.clear()
        mod._CONFIGURED = original
        mod.setup_logging("INFO", fmt="text")  # 让后续用例回到文本形态

    def test_json_mode_installs_the_json_formatter(self) -> None:
        from app.core.logging import setup_logging

        setup_logging("INFO", fmt="json")
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, JsonFormatter)

    def test_json_mode_ignores_colorful(self) -> None:
        """给机器看的东西不该带 ANSI 转义 —— 那会污染字段值。"""
        from app.core.logging import setup_logging

        setup_logging("INFO", colorful=True, fmt="json")
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, JsonFormatter)

    def test_text_mode_is_the_default(self) -> None:
        from app.core.logging import setup_logging

        setup_logging("INFO")
        handler = logging.getLogger().handlers[0]
        assert not isinstance(handler.formatter, JsonFormatter)
