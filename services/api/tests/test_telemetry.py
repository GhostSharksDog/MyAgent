"""可观测性测试：链路追踪 + 指标。

最重要的两条：

1. **ContextVar 在并发下的隔离性**。这是选它而不选 thread-local 的全部理由 ——
   asyncio 里多个协程跑在同一个线程上，thread-local 会被它们共享，
   于是并发请求的日志全部串味。而这个 bug **只在有并发时出现**，
   本地串行调试永远看不到。所以必须有测试守住。

2. **指标标签的有界性**。把 session_id / URL 路径当标签会让指标条数
   随用户数无限增长，最终撑爆内存 —— Prometheus 最经典的事故。
   这里用测试把"只用枚举标签"这条纪律固定下来。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

import pytest
from app.core.telemetry import (
    METRICS,
    Histogram,
    MetricsRegistry,
    TraceIdFilter,
    clear_trace_id,
    get_trace_id,
    new_trace_id,
    record_agent_event,
    set_trace_id,
)
from app.llm.types import Usage
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _clean_metrics() -> Iterator[None]:
    """全局指标在用例之间会互相污染，每个用例前清空。"""
    METRICS.reset()
    yield
    METRICS.reset()


# ============================================================
# 链路上下文
# ============================================================
class TestTraceContext:
    def test_new_trace_id_is_short_hex(self) -> None:
        trace_id = new_trace_id()
        assert len(trace_id) == 12
        assert all(c in "0123456789abcdef" for c in trace_id)

    def test_ids_are_unique(self) -> None:
        assert len({new_trace_id() for _ in range(100)}) == 100

    def test_set_and_get(self) -> None:
        set_trace_id("abc123")
        assert get_trace_id() == "abc123"

    def test_explicit_id_wins(self) -> None:
        """传入上游 trace id 时必须用它 —— 否则跨进程链路就断了。"""
        assert set_trace_id("upstream-id") == "upstream-id"

    def test_generated_when_absent(self) -> None:
        assert set_trace_id(None) != ""
        assert set_trace_id("") != ""


class TestTraceFilter:
    def test_injects_trace_id_into_record(self) -> None:
        set_trace_id("trace-xyz")
        record = logging.LogRecord("t", logging.INFO, "f", 1, "msg", None, None)
        assert TraceIdFilter().filter(record) is True
        assert record.trace_id == "trace-xyz"  # type: ignore[attr-defined]

    def test_placeholder_when_no_trace(self) -> None:
        """没有 trace 上下文时（如启动日志）用 `-` 占位，而不是空字符串。

        空字符串会让日志行看起来"缺了一块"，`-` 才是"此处无值"的清晰表达。
        """
        clear_trace_id()
        record = logging.LogRecord("t", logging.INFO, "f", 1, "msg", None, None)
        TraceIdFilter().filter(record)
        assert record.trace_id == "-"  # type: ignore[attr-defined]


class TestContextIsolation:
    """ContextVar 的核心价值：并发任务之间互不干扰。"""

    async def test_concurrent_tasks_have_isolated_trace_ids(self) -> None:
        """10 个并发任务各自设置 trace id，读回的必须是自己的。

        如果实现改用 thread-local，这里会全部读到**同一个**值
        （因为 asyncio 里它们跑在同一线程上），测试会立刻失败。
        """
        results: dict[int, str] = {}

        async def worker(i: int) -> None:
            set_trace_id(f"trace-{i}")
            # 制造交错：让出控制权多次，其他任务会在这期间设置自己的 id
            for _ in range(5):
                await asyncio.sleep(0)
            results[i] = get_trace_id()

        await asyncio.gather(*(worker(i) for i in range(10)))

        assert results == {i: f"trace-{i}" for i in range(10)}

    async def test_isolated_after_await_boundary(self) -> None:
        """跨 await 之后 trace id 仍要正确 —— 这是随 Task 复制的直接结果。"""

        async def worker(name: str, delay: float) -> str:
            set_trace_id(name)
            await asyncio.sleep(delay)
            return get_trace_id()

        fast, slow = await asyncio.gather(worker("fast", 0.01), worker("slow", 0.05))
        assert fast == "fast"
        assert slow == "slow"

    async def test_child_task_inherits(self) -> None:
        """子任务应继承父任务的上下文（ContextVar 随 Task 复制）。"""

        async def child() -> str:
            return get_trace_id()

        async def parent() -> str:
            set_trace_id("parent-trace")
            return await asyncio.create_task(child())

        assert await parent() == "parent-trace"


# ============================================================
# 指标
# ============================================================
class TestMetricsRegistry:
    def test_counter_accumulates(self) -> None:
        registry = MetricsRegistry()
        registry.inc("requests", method="GET")
        registry.inc("requests", method="GET")
        registry.inc("requests", method="POST")
        assert registry.counter("requests", method="GET") == 2
        assert registry.counter("requests", method="POST") == 1
        assert registry.counter("requests", method="DELETE") == 0

    def test_label_order_does_not_matter(self) -> None:
        """标签是有序元组做键，字典序归一化后 'a=1,b=2' 与 'b=2,a=1' 应视为同一组。"""
        registry = MetricsRegistry()
        registry.inc("m", a="1", b="2")
        registry.inc("m", b="2", a="1")
        assert registry.counter("m", a="1", b="2") == 2

    def test_histogram_observes(self) -> None:
        registry = MetricsRegistry()
        for value in (5, 15, 25, 100, 500):
            registry.observe("latency", value)
        snapshot = registry.snapshot()["histograms"]  # type: ignore[index]
        assert snapshot[0]["count"] == 5  # type: ignore[index]

    def test_reset_clears(self) -> None:
        registry = MetricsRegistry()
        registry.inc("x")
        registry.observe("y", 10)
        registry.reset()
        assert registry.counter("x") == 0
        assert registry.snapshot()["histograms"] == []

    def test_prometheus_format(self) -> None:
        registry = MetricsRegistry()
        registry.inc("legacy_http_requests_total", method="GET", status="2xx")
        text = registry.render_prometheus()
        assert 'legacy_http_requests_total{method="GET",status="2xx"} 1' in text
        # 直方图必须输出 _bucket / _sum / _count 三件套，
        # 少任何一个 Prometheus 都算不出分位数
        registry.observe("legacy_http_duration_ms", 42, method="GET")
        text = registry.render_prometheus()
        assert "legacy_http_duration_ms_bucket{" in text
        assert "legacy_http_duration_ms_sum{" in text
        assert "legacy_http_duration_ms_count{" in text
        assert 'le="+Inf"' in text


class TestHistogram:
    def test_empty_snapshot(self) -> None:
        snap = Histogram().snapshot()
        assert snap["count"] == 0
        assert snap["p95"] == 0

    def test_avg(self) -> None:
        histogram = Histogram()
        for value in (10, 20, 30):
            histogram.observe(value)
        assert histogram.snapshot()["avg"] == 20

    def test_percentile_interpolation(self) -> None:
        """100 个 10ms 样本 → p50 与 p95 都应落在 10ms 附近。

        分位数是**近似值**（桶线性插值），但只要样本集中在某个桶内，
        插值结果就应该贴近真实值。这里用一个"全部相同"的分布来验证，
        因为它的真值是确定的。
        """
        histogram = Histogram()
        for _ in range(100):
            histogram.observe(10)
        snap = histogram.snapshot()
        assert 5 <= snap["p50"] <= 25
        assert 5 <= snap["p95"] <= 25

    def test_p95_above_p50_for_skewed_distribution(self) -> None:
        """长尾分布下 p95 必须显著高于 p50。

        这正是"不能只看平均值"的原因：平均会被大量快请求拉低，
        而用户抱怨的恰恰是那 10% 的慢请求。

        【为什么用 10% 而不是 5% 的慢样本】
        分位数是**位置**，不是"超过它的比例"。90 快 + 10 慢时，
        第 95 个位置落在慢区间里 → p95 进入慢值。
        但若只有 5 个慢样本（5%），第 95 个位置**恰好是快慢分界线**，
        p95 会等于快值 —— 这不是实现错误，而是分位数定义的直接结果。
        测试用它来验证时若取 5%，就会得到"实现好像坏了"的错误结论。
        """
        histogram = Histogram()
        for _ in range(90):
            histogram.observe(20)
        for _ in range(10):
            histogram.observe(2000)
        snap = histogram.snapshot()

        assert snap["p95"] > snap["p50"]
        assert snap["p95"] > 1000, f"p95 应落在慢区间，实际 {snap['p95']}"
        assert snap["avg"] < snap["p95"], "平均值把长尾抹平了，这正是它不可信的原因"
        assert snap["avg"] == 218.0  # (90*20 + 10*2000) / 100

    def test_overflow_goes_to_inf_bucket(self) -> None:
        """超出最大桶的样本落到 +Inf，不能丢。"""
        histogram = Histogram()
        histogram.observe(999_999)
        assert histogram.count == 1
        assert histogram.counts[-1] == 1

    def test_bucket_counts_are_cumulative_on_render(self) -> None:
        """**渲染出的** Prometheus _bucket 必须是累计计数。

        回归：初版在存储层存每桶计数（这是对的，便于算分位数），
        但渲染时写成了 `cumulative = counts[i]`（赋值而非累加），
        于是输出的 _bucket 序列不是单调的 —— 抓取端算出的分位数全错，
        而且不报错，只是数字看起来"有点怪"。

        这里断言的是**对外契约**（渲染结果单调不减），
        而不是内部存储形态 —— 内部怎么存是可以自由改的。
        """
        registry = MetricsRegistry()
        for value in (1, 10, 100):
            registry.observe("lat", value)

        buckets = [
            float(line.rsplit(" ", 1)[1])
            for line in registry.render_prometheus().splitlines()
            if "_bucket{" in line
        ]
        assert buckets == sorted(buckets), f"_bucket 不是累计计数：{buckets}"
        assert buckets[-1] == 3

    def test_internal_counts_are_per_bucket(self) -> None:
        """内部存储是**每桶**计数 —— 与上一条的渲染契约刻意不同。"""
        histogram = Histogram()
        for value in (1, 10, 100):
            histogram.observe(value)
        assert histogram.counts != sorted(histogram.counts)
        assert histogram.cumulative_counts() == sorted(histogram.cumulative_counts())
        assert histogram.cumulative_counts()[-1] == 3


# ============================================================
# Agent 事件 → 指标
# ============================================================
class _FakeEvent:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class TestAgentEventMetrics:
    def test_tool_result_recorded(self) -> None:
        record_agent_event(
            _FakeEvent(
                type="tool_result", tool_name="search_knowledge", tool_ok=True, duration_ms=42
            ),
            mode="react",
        )
        assert METRICS.counter("legacy_tool_calls_total", tool="search_knowledge", ok="true") == 1

    def test_tool_failure_separate_label(self) -> None:
        """成功与失败必须是不同标签值 —— 混在一起就没法算成功率。"""
        record_agent_event(
            _FakeEvent(type="tool_result", tool_name="t", tool_ok=False, duration_ms=5),
            mode="react",
        )
        assert METRICS.counter("legacy_tool_calls_total", tool="t", ok="false") == 1
        assert METRICS.counter("legacy_tool_calls_total", tool="t", ok="true") == 0

    def test_done_records_tokens(self) -> None:
        record_agent_event(
            _FakeEvent(
                type="done",
                usage=Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                steps_used=3,
                stopped_reason="finished",
            ),
            mode="plan",
        )
        assert METRICS.counter("legacy_llm_tokens_total", kind="prompt") == 100
        assert METRICS.counter("legacy_llm_tokens_total", kind="completion") == 50
        assert METRICS.counter("legacy_chat_requests_total", mode="plan", reason="finished") == 1

    def test_budget_stop_recorded_as_distinct_reason(self) -> None:
        """max_steps 与 error 必须是不同的 reason 标签值。

        它们在后端被刻意区分为"可预期的预算终止"与"故障"（见 events.py），
        指标层如果合并，面板上就再也分不出来了。
        """
        record_agent_event(
            _FakeEvent(type="done", usage=None, steps_used=12, stopped_reason="max_steps"),
            mode="react",
        )
        assert METRICS.counter("legacy_chat_requests_total", mode="react", reason="max_steps") == 1
        assert METRICS.counter("legacy_chat_requests_total", mode="react", reason="error") == 0

    def test_unknown_event_ignored(self) -> None:
        record_agent_event(_FakeEvent(type="some_future_event"), mode="react")
        assert METRICS.snapshot()["counters"] == []

    def test_missing_tool_name_falls_back(self) -> None:
        record_agent_event(
            _FakeEvent(type="tool_result", tool_ok=True, duration_ms=1), mode="react"
        )
        assert METRICS.counter("legacy_tool_calls_total", tool="unknown", ok="true") == 1


# ============================================================
# HTTP 端点
# ============================================================
class TestTelemetryEndpoints:
    def test_trace_id_in_response_header(self, client: TestClient) -> None:
        """每个响应都要带 X-Trace-Id —— 客户端才能在报障时给出可检索的标识。"""
        response = client.get("/healthz")
        trace_id = response.headers.get("X-Trace-Id")
        assert trace_id
        assert len(trace_id) == 12

    def test_upstream_trace_id_preserved(self, client: TestClient) -> None:
        """上游传来的 trace id 必须被沿用，不能另生成一个。

        否则"网关 → agent → rag"三段链路各自有 id，等于没有链路。
        """
        response = client.get("/healthz", headers={"X-Trace-Id": "upstream-abc"})
        assert response.headers.get("X-Trace-Id") == "upstream-abc"

    def test_api_metrics_json(self, client: TestClient) -> None:
        client.get("/healthz")  # 产生一点流量
        body = client.get("/api/metrics").json()

        assert body["uptime_seconds"] >= 0
        assert "session_backend" in body["components"]
        assert "task_backend" in body["components"]
        assert "tools" in body["components"]
        names = {c["name"] for c in body["counters"]}
        assert "legacy_http_requests_total" in names

    def test_prometheus_endpoint(self, client: TestClient) -> None:
        client.get("/healthz")
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "legacy_http_requests_total" in response.text

    def test_metrics_endpoint_has_no_cardinality_explosion(self, client: TestClient) -> None:
        """标签里不能出现具体路径或 id。

        创建若干会话后，指标条数不该随会话数增长 ——
        如果实现了"给每个 URL 打标签"，这里会立刻爆掉。
        """
        before = len(client.get("/api/metrics").json()["counters"])
        for _ in range(5):
            client.post("/api/sessions")
        after = len(client.get("/api/metrics").json()["counters"])
        # 允许因为状态码大类不同而新增个别条目，但不该是 5 条
        assert after - before < 5, "指标条数随请求路径增长了，存在标签基数风险"
