"""韧性组件：熔断器与限流器。

【为什么这两个东西必须一起出现】

它们解决的是**同一类故障的两个阶段**：

    限流：请求太多 → 在入口把多余的挡掉（保护自己）
    熔断：下游挂了 → 停止调用它，直接失败（保护调用方和下游）

只做限流，下游挂了照样把每个请求都拖满超时；只做熔断，
自己被打爆时还是会雪崩。**限流管"量"，熔断管"害"。**

【熔断器要解决的到底是什么问题】

拆分出 RAG 服务之后，引入了一个单体模式**根本不存在**的失败模式：

    RAG 服务挂掉 → agent 每次检索都要等满 15 秒超时

后果不是"检索变慢"，而是**agent 自己被拖死**：事件循环里堆积的
待处理请求越来越多，内存与连接数一起涨，最后连不依赖检索的对话
也一起不可用。这就是级联故障 —— 一个非核心依赖的故障，
传染成了整个系统的故障。

熔断器的做法极其简单：**连续失败 N 次之后，直接不再调用下游**，
在微秒级返回失败。等一段时间后放一个探测请求过去，成功了就恢复。

代价是"下游恢复的那一瞬间我们还在拒绝请求"，收益是
"下游挂掉时我们活着"。**这个交换在绝大多数系统里都是划算的。**

【为什么业务状态不能计入失败】

这一点比熔断算法本身更容易写错：`EmptyKnowledgeBase`（知识库为空）
是一个**正常的业务状态**，不是故障。如果把 503 也计入失败次数，
那么"语料还没准备好"的部署会把自己的熔断器打开 ——
表现为"知识库服务不可用"，而实际服务好得很，只是没有数据。

**故障（fault）与业务状态（state）必须分开统计**，
这是熔断器最容易埋下的一个误导性故障。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, TypeVar

from app.core.telemetry import METRICS

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitOpen(RuntimeError):
    """熔断器处于打开状态，本次调用被直接拒绝（没有真的调用下游）。

    【为什么这是一个独立异常，而不是复用下游的错误】
    调用方需要区分两种情况：
      · 下游真的调用失败了（可能是网络抖动，值得重试）
      · 我们**根本没发起调用**（下游已知不可用，重试只是浪费超时时间）

    后者的正确动作是立即降级，而不是重试。把两者混成一个异常，
    调用方就只能猜 —— 而猜错的方向恰好是最糟的那个（重试）。
    """


class CircuitState(StrEnum):
    CLOSED = "closed"  # 正常：放行全部请求
    OPEN = "open"  # 熔断：直接拒绝，不调用下游
    HALF_OPEN = "half_open"  # 半开：放行少量探测请求


class CircuitBreaker:
    """三态熔断器。

    【为什么是"连续失败"而不是"窗口内失败率"】
    失败率版本需要维护一个滑动窗口、需要设定最小样本数
    （否则 1 次失败 / 1 次请求 = 100% 就误判），参数多、边界多。

    连续失败版本的状态只有一个计数器，行为可预测、容易解释。
    缺点是对间歇性故障不敏感（成功一次就清零）。

    对当前的场景（调用一个可能整个挂掉的内存计算服务），
    连续失败恰好是对的模式：RAG 要么活着要么死了，很少"半死不活"。
    **选简单的那个，直到有证据说明它不够用。**

    【为什么必须有 half_open 而不是等时间到了就完全恢复】
    如果 recovery_timeout 一到就把所有请求都放回去，
    而此时下游还没恢复（比如在启动、在预热），
    那么一瞬间会有大量请求同时打过去 —— **这就是重试风暴**，
    会把刚爬起来的服务再打死一次。

    半开状态只放行很少的请求：成功则完全恢复，失败则重新打开。
    这是"探测"而不是"恢复"，区别就在放行的量上。
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold 必须 >= 1")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout 必须 > 0")

        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = 0
        # 用 asyncio.Lock 而不是 threading.Lock：
        # 整个调用链在同一个事件循环里，用线程锁会阻塞事件循环 ——
        # 一把等待中的锁足以让所有并发请求一起卡住。
        self._lock = asyncio.Lock()

    # ---------- 状态 ----------

    @property
    def state(self) -> CircuitState:
        return self._state

    def _should_attempt_reset(self) -> bool:
        return (
            self._state is CircuitState.OPEN
            and (time.monotonic() - self._opened_at) >= self._recovery_timeout
        )

    async def _before_call(self) -> None:
        async with self._lock:
            if self._should_attempt_reset():
                # 时间到了：进入半开，准备放行一个探测请求
                self._state = CircuitState.HALF_OPEN
                self._half_open_in_flight = 0
                logger.info("熔断器 %s 进入半开状态，开始探测下游", self.name)

            if self._state is CircuitState.OPEN:
                raise CircuitOpen(
                    f"{self.name} 熔断中（连续失败 {self._consecutive_failures} 次），"
                    f"将在 {self._remaining_open_seconds():.0f} 秒后重试"
                )

            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_in_flight >= self._half_open_max_calls:
                    # 探测名额已满。这里也要拒绝，否则"半开"就退化成"全开"了。
                    raise CircuitOpen(f"{self.name} 半开探测名额已满，拒绝并发请求")
                self._half_open_in_flight += 1

    def _remaining_open_seconds(self) -> float:
        return max(0.0, self._recovery_timeout - (time.monotonic() - self._opened_at))

    async def _on_success(self) -> None:
        async with self._lock:
            was = self._state
            self._consecutive_failures = 0
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            if was is not CircuitState.CLOSED:
                logger.info("熔断器 %s 恢复为关闭状态（下游已恢复正常）", self.name)
            self._state = CircuitState.CLOSED

    async def _on_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)

            if self._state is CircuitState.HALF_OPEN:
                # 探测失败 → 立刻重新打开。
                # 这里**不等** failure_threshold：探测本身就是为了
                # 判断"能不能恢复"，探测失败已经给出答案了。
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.warning("熔断器 %s 探测失败，重新打开", self.name)
                return

            if self._consecutive_failures >= self._failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "熔断器 %s 打开：连续失败 %d 次（阈值 %d）。"
                    "接下来 %.0f 秒内不再调用下游，改为快速失败 —— "
                    "这是为了避免每个请求都白等一次超时。",
                    self.name,
                    self._consecutive_failures,
                    self._failure_threshold,
                    self._recovery_timeout,
                )
                METRICS.inc("jobpilot_circuit_open_total", circuit=self.name)

    # ---------- 调用 ----------

    async def call(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        count_as_failure: Callable[[BaseException], bool] | None = None,
    ) -> T:
        """在熔断保护下调用 `fn`。

        Args:
            count_as_failure: 判断某个异常是否算"故障"。
                **默认全部算**，但调用方应当把业务状态排除掉 ——
                详见模块文档里关于 `EmptyKnowledgeBase` 的说明。
                返回 False 的异常会原样抛出，且**不改变熔断器状态**。
        """
        await self._before_call()
        try:
            result = await fn()
        except BaseException as exc:
            is_fault = count_as_failure(exc) if count_as_failure is not None else True
            if is_fault:
                await self._on_failure()
            else:
                # 业务状态：既不算成功也不算失败。
                # 必须归还半开名额，否则一次"知识库为空"就把唯一的
                # 探测名额永久占住，熔断器再也不会恢复。
                async with self._lock:
                    self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            raise
        else:
            await self._on_success()
            return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": str(self._state),
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self._failure_threshold,
            "retry_in_seconds": round(self._remaining_open_seconds(), 1),
        }


# ============================================================
# 限流：令牌桶
# ============================================================
class TokenBucket:
    """令牌桶限流器。

    【为什么用令牌桶而不是"每秒最多 N 次"的计数器】
    固定窗口计数器有两个谁都躲不开的毛病：

    1. **临界问题**：窗口是 [0s,1s) 和 [1s,2s)。如果 100 个请求都挤在
       0.99s 和 1.01s，那就是"两个窗口各 100 次"，实际是 0.02 秒内 200 次 ——
       限制完全失效。
    2. **无法应对突发**：真实流量天然是突发的（用户提交表单、任务批量触发）。
       严格均匀限流会把合理的突发也拒掉。

    令牌桶只用两个参数就同时解决了这两点：
      · `rate`  每秒补充多少令牌 → 长期平均速率
      · `burst` 桶容量           → 允许的瞬时突发上限

    **"长期平均 + 短期突发"这个模型比"每秒 N 次"精确得多，
    而且更容易解释给产品听** —— 后者说不清"为什么这一秒的第 101 个请求被拒"。

    【为什么用单调时钟】
    `time.monotonic()` 不受系统时间调整影响。用 `time.time()` 的话，
    NTP 往回校一次时钟，桶里的令牌就会凭空多出来（或者永远补不满）。
    限流器是最不该被墙上的钟影响的东西。
    """

    def __init__(self, rate: float, burst: int) -> None:
        if rate <= 0:
            raise ValueError("rate 必须 > 0")
        if burst < 1:
            raise ValueError("burst 必须 >= 1")
        self._rate = rate
        self._burst = float(burst)
        self._tokens = float(burst)  # 初始装满：否则冷启动时第一批请求全被拒
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)

    async def acquire(self, tokens: float = 1.0) -> bool:
        """尝试取走令牌。返回 False 表示被限流（**不阻塞**）。

        【为什么是"立刻返回失败"而不是"排队等待"】
        排队等待在高负载下会把延迟推向无穷：每个请求都在等，
        内存里堆着越来越多的等待者，超时时间到了才发现自己白等了。

        更实际的理由是：**限流的目的是保护，不是延迟**。
        让调用方立刻知道"你现在太频繁了"（HTTP 429 + Retry-After），
        它可以自己决定是退避重试还是放弃 —— 这个决定调用方比我们更有资格做。
        """
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    def retry_after(self, tokens: float = 1.0) -> float:
        """还需多少秒才能凑够 tokens。

        这个值要放进 HTTP 的 `Retry-After` 头：**只回一个 429 而不告诉
        对方等多久，等于逼调用方用猜测的重试节奏来打你**，
        反而制造出更不规则的流量。
        """
        self._refill()
        missing = tokens - self._tokens
        if missing <= 0:
            return 0.0
        return missing / self._rate
