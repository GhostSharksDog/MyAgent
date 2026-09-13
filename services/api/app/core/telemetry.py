"""可观测性：链路追踪 + 指标采集。

【为什么 Agent 项目比普通服务更需要它】

一次失败的 Agent 请求跨了**多跳**：模型调用 → 工具执行 → 再调用模型 → …
每跳都可能失败，而且失败往往不是"报错"而是"结果不对"。
没有 trace id 的话，你在日志里看到的是互不相干的三段记录，
根本拼不出"这次请求到底经历了什么"。

这是"可观测性"和"打日志"的本质区别：**打日志是记录事件，可观测性是能回答问题。**

【两个必须理解的技术点】

1. **为什么用 ContextVar 而不是全局变量或 thread-local**

   全局变量：并发请求会互相覆盖 trace_id，日志全部串味。
   thread-local：asyncio 里多个协程跑在**同一个线程**上，
   于是同一个线程里的所有请求共享一份 thread-local —— 同样是串味。

   `ContextVar` 是 asyncio 时代的正确答案：它随**任务（Task）**复制,
   每个请求处理协程持有自己的副本，且会正确传播到该协程派生的子任务。
   这也是为什么中间件里 `set()` 之后，深层函数能直接 `get()` 到正确的值。

2. **为什么要接受上游传进来的 trace id**

   服务拆分之后（P4 的下一步），一个用户请求会经过网关 → agent 服务 → rag 服务。
   如果每个服务各生成一个 id，你就有三个互不相关的 id，链路依然是断的。
   接受上游 `X-Trace-Id` 并原样透传，才能把跨进程的调用串成一条链。

【指标选型的依据】

   counter   —— 只增不减的累计量（请求数、token 数、错误数）
   histogram —— 需要看分位数的量（延迟）**不能只记平均值**

为什么延迟要看 p95 而不是平均：平均 100ms 完全可能由"90% 请求 20ms +
10% 请求 800ms"产生 —— 而那 10% 才是用户真正抱怨的部分。
平均值会把长尾抹平，这是性能分析里最常见的自欺。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final

logger = logging.getLogger(__name__)

# ============================================================
# 链路上下文
# ============================================================
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    """生成一个短 trace id。

    用 12 位十六进制而不是完整 uuid：trace id 会出现在每一行日志里，
    太长会显著挤压日志的有效信息密度，而 48 bit 的随机空间
    对"单机一段时间内不冲突"这个需求远远够用。
    """
    return uuid.uuid4().hex[:12]


def set_trace_id(trace_id: str | None = None) -> str:
    """设置当前上下文的 trace id，返回最终使用的值。"""
    value = trace_id or new_trace_id()
    _trace_id.set(value)
    return value


def get_trace_id() -> str:
    return _trace_id.get()


def clear_trace_id() -> None:
    """清空当前上下文的 trace id。

    用于测试，以及"一个请求处理完之后"的显式清理 ——
    虽然 ContextVar 随 Task 结束自然失效，但在长驻任务
    （如队列 worker）里显式清理能避免 id 被后续工作复用。
    """
    _trace_id.set("")


class TraceIdFilter(logging.Filter):
    """把 trace_id 注入每条日志记录。

    做成 Filter 而不是在每个 logger 调用点手工拼字符串：
    后者一定会有人漏写，而漏写的那条日志恰好就是排查故障时最需要的那条。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or "-"
        return True


# ============================================================
# 指标
# ============================================================
# 直方图的桶。单位毫秒。
#
# 桶的划分不是随便定的：LLM 调用的延迟跨度极大（本地工具 <1ms、
# 模型调用数秒），所以覆盖 1ms ~ 30s。桶太密会让指标体量爆炸，
# 太疏则分位数失去意义（p95 会退化成"落在这个大桶里"）。
_LATENCY_BUCKETS_MS: Final[tuple[float, ...]] = (
    1,
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
    2500,
    5000,
    10000,
    30000,
)


@dataclass
class Histogram:
    """一个直方图。

    【存储模型：每桶计数，不是累计计数】
    `counts[i]` 表示"落进第 i 个桶的样本数"。**不是**累计值 ——
    累计只在两个地方需要（算分位数、渲染 Prometheus），
    在那里现算比在存储时维护更不容易出错。

    代价是这两个读者都必须自己累加。初版就是因为**两处都忘了累加**而静默出错：
    `_quantile` 把每桶计数当成累计计数，`render_prometheus` 写成了赋值
    （`cumulative = counts[i]`）而不是累加。两处都不会抛异常，
    只是分位数和 Prometheus 输出都算错 —— 这类"数字看起来有点怪"的 bug
    没有测试是发现不了的。
    """

    buckets: tuple[float, ...] = _LATENCY_BUCKETS_MS
    counts: list[int] = field(default_factory=lambda: [0] * (len(_LATENCY_BUCKETS_MS) + 1))
    total: float = 0.0
    count: int = 0

    def observe(self, value_ms: float) -> None:
        self.total += value_ms
        self.count += 1
        for i, upper in enumerate(self.buckets):
            if value_ms <= upper:
                self.counts[i] += 1
                return
        self.counts[-1] += 1  # +Inf 桶

    def cumulative_counts(self) -> list[int]:
        """累计到每个桶的样本数。算分位数与渲染 Prometheus 都用它。

        把累加逻辑收进一个方法，而不是让两个读者各自实现 ——
        初版正是两处各自实现、且**两处都写错**。
        """
        out: list[int] = []
        running = 0
        for count in self.counts:
            running += count
            out.append(running)
        return out

    def snapshot(self) -> dict[str, float]:
        """给出平均值与几个分位数。

        分位数由桶**线性插值**得到 —— 这是近似值，不是精确值。
        想要精确分位数需要保留全部样本或使用 t-digest 之类的结构，
        那对当前规模是过度设计。**但必须知道它是近似的**，
        不能拿它去论证"我们优化了 3.7%"这种精度级别的结论。
        """
        out: dict[str, float] = {"count": float(self.count), "avg": 0.0, "p50": 0.0, "p95": 0.0}
        if self.count == 0:
            return out

        out["avg"] = self.total / self.count
        for label, q in (("p50", 0.50), ("p95", 0.95)):
            out[label] = self._quantile(q)
        return out

    def _quantile(self, q: float) -> float:
        """在**累计**桶上做线性插值。

        逐步推导（i 为命中的桶下标）：
            upper        = buckets[i]                       该桶的上界
            lower        = buckets[i-1] 或 0                该桶的下界
            prev_cum     = cumulative[i-1] 或 0             之前的累计
            bucket_count = cumulative[i] - prev_cum         该桶内样本数
            fraction     = (目标序号 - prev_cum) / bucket_count
            value        = lower + (upper - lower) * fraction
        """
        target = self.count * q
        cumulative = self.cumulative_counts()
        prev_cumulative = 0

        for i, upper in enumerate(self.buckets):
            cum = cumulative[i]
            if cum >= target:
                lower = self.buckets[i - 1] if i > 0 else 0.0
                bucket_count = cum - prev_cumulative
                fraction = (target - prev_cumulative) / max(bucket_count, 1)
                return lower + (upper - lower) * min(max(fraction, 0.0), 1.0)
            prev_cumulative = cum

        # 落在 +Inf 桶：只能报下界（最后一个有限桶的边界），不假装知道上界
        return self.buckets[-1]


class MetricsRegistry:
    """进程内指标注册表。

    【为什么不用 prometheus_client】

    它是一个很成熟的库，但本项目要展示的是"指标是怎么算出来的"。
    自己实现一遍计数器与直方图（约 100 行），你就能回答：
      - 为什么 counter 不能减？
      - 分位数为什么不能精确算？
      - 标签基数为什么会炸？
    这些是面试会问、而调库不会让你懂的东西。

    【标签基数（cardinality）—— 本模块唯一的真实风险】
    标签的每一种组合都会占一份独立存储。如果把 session_id / trace_id
    当作标签，指标条数会随用户数无限增长，最终把内存撑爆 ——
    这是 Prometheus 使用中最经典的事故。
    所以本模块**只接受有界标签**（endpoint、status、tool 名这类枚举值），
    并且由调用方保证传入的是枚举而非自由文本。
    """

    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], Histogram] = {}
        self._lock = threading.Lock()
        self._started_at = time.time()

    # ---------- 写入 ----------

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value_ms: float, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            histogram = self._histograms.get(key)
            if histogram is None:
                histogram = Histogram()
                self._histograms[key] = histogram
            histogram.observe(value_ms)

    def counter(self, name: str, **labels: str) -> float:
        return self._counters.get((name, tuple(sorted(labels.items()))), 0.0)

    # ---------- 读取 ----------

    def snapshot(self) -> dict[str, object]:
        """结构化快照，给 `/api/metrics` 用（便于前端/脚本消费）。"""
        with self._lock:
            counters = [
                {"name": name, "labels": dict(labels), "value": value}
                for (name, labels), value in sorted(self._counters.items())
            ]
            histograms = [
                {"name": name, "labels": dict(labels), **(h.snapshot())}
                for (name, labels), h in sorted(self._histograms.items())
            ]
        return {
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "counters": counters,
            "histograms": histograms,
        }

    def render_prometheus(self) -> str:
        """渲染成 Prometheus 文本暴露格式。

        格式本身很简单（`name{label="v"} value`），
        自己拼 20 行就能得到一个可直接被 Prometheus 抓取的端点 ——
        引入一个库只为这点格式化并不划算。
        """
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{_fmt_labels(labels)} {value}")
            for (name, labels), histogram in sorted(self._histograms.items()):
                # Prometheus 的 _bucket 必须是**累计**计数。
                # 输出每桶计数会让抓取端算出的分位数完全错误，而且不报错 ——
                # 只是数字看起来"有点怪"。用统一的 cumulative_counts() 保证只算一次。
                cumulative = histogram.cumulative_counts()
                for i, upper in enumerate(histogram.buckets):
                    merged = (*labels, ("le", str(upper)))
                    lines.append(f"{name}_bucket{_fmt_labels(merged)} {cumulative[i]}")
                merged_inf = (*labels, ("le", "+Inf"))
                lines.append(f"{name}_bucket{_fmt_labels(merged_inf)} {histogram.count}")
                lines.append(f"{name}_sum{_fmt_labels(labels)} {histogram.total}")
                lines.append(f"{name}_count{_fmt_labels(labels)} {histogram.count}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """清空。测试用 —— 全局指标在用例之间会互相污染。"""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


def _fmt_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in labels)
    return "{" + inner + "}"


# 进程内单例。多进程部署时每个进程各自持有自己的指标 ——
# 这正是需要 Prometheus 这种"拉取式"采集的原因：
# 它逐个进程抓取再聚合，不要求应用自己维护全局状态。
METRICS = MetricsRegistry()


# ============================================================
# Agent 事件 → 指标
# ============================================================
# 指标命名遵循 Prometheus 约定：`<namespace>_<name>_<unit>`，
# 计数器以 _total 结尾。统一命名不是洁癖 —— 它是让 Grafana 面板
# 与告警规则可以按前缀批量匹配的前提。
_M_TOKENS = "jobpilot_llm_tokens_total"
_M_TOOLS = "jobpilot_tool_calls_total"
_M_TOOL_LATENCY = "jobpilot_tool_duration_ms"
_M_STEPS = "jobpilot_agent_steps"
_M_REQUESTS = "jobpilot_chat_requests_total"
_M_RUN_LATENCY = "jobpilot_chat_duration_ms"


def record_agent_event(event: object, *, mode: str) -> None:
    """把一条 Agent 事件记进指标。

    【为什么要在这里做，而不是在 Agent 内部】
    Agent 内核不应该依赖可观测性实现 —— 它有 CLI、HTTP、测试等多种调用方式，
    让内核直接打点会把"跑一次测试"也变成"污染全局指标"。
    在事件消费端（routes.py）统一采集，既覆盖了所有形态，
    也让内核保持纯粹。

    【标签必须是枚举】
    这里用到的标签只有 `mode` / `tool` / `ok` / `kind` —— 全部是有限集合。
    绝不能把 session_id、trace_id、用户输入当标签：
    每一种组合都会占一份独立存储，指标条数会随用户数无限增长。
    这是 Prometheus 最经典的事故，也是本函数刻意只读固定字段的原因。
    """
    event_type = str(getattr(event, "type", ""))
    metrics = METRICS

    if event_type == "tool_result":
        tool = str(getattr(event, "tool_name", None) or "unknown")
        ok = bool(getattr(event, "tool_ok", False))
        metrics.inc(_M_TOOLS, tool=tool, ok="true" if ok else "false")
        duration = getattr(event, "duration_ms", None)
        if isinstance(duration, int):
            metrics.observe(_M_TOOL_LATENCY, float(duration), tool=tool)

    elif event_type == "done":
        usage = getattr(event, "usage", None)
        if usage is not None:
            metrics.inc(_M_TOKENS, float(getattr(usage, "prompt_tokens", 0)), kind="prompt")
            metrics.inc(_M_TOKENS, float(getattr(usage, "completion_tokens", 0)), kind="completion")
        steps = getattr(event, "steps_used", 0)
        if steps:
            metrics.observe(_M_STEPS, float(steps), mode=mode)

        # 终止原因按枚举记：这直接支撑"错误率"与"预算耗尽率"两条曲线。
        # 注意 max_steps / loop_detected 不是故障（见 events.py 的分类），
        # 面板上应该分开画，而不是都归到 error 里。
        reason = str(getattr(event, "stopped_reason", "finished") or "finished")
        metrics.inc(_M_REQUESTS, mode=mode, reason=reason)


def record_chat_request(mode: str, *, duration_ms: float) -> None:
    """记录一次完整的对话请求（含耗时）。"""
    METRICS.inc(_M_REQUESTS, mode=mode, reason="_started")
    METRICS.observe(_M_RUN_LATENCY, duration_ms, mode=mode)


__all__ = [
    "METRICS",
    "MetricsRegistry",
    "TraceIdFilter",
    "clear_trace_id",
    "get_trace_id",
    "new_trace_id",
    "record_agent_event",
    "record_chat_request",
    "set_trace_id",
]
