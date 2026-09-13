"""熔断器与限流器的测试。

【这组测试要证明的核心命题】

熔断器的价值可以用一句话量化，所以测试也应该量化它：

    下游挂掉时，第 1 个请求要等满超时（这是不可避免的），
    但**第 N 个请求必须是微秒级返回**。

如果做不到这一点，熔断器就只是"记录了一下失败次数"，
没有任何实际作用 —— 而这一点用"状态变没变"是测不出来的
（状态可以正确地变成 open，同时每个请求照样等满超时）。

所以这里的关键用例是 `test_open_circuit_returns_without_waiting`，
它比较的是**真实耗时**，不是状态字段。
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from app.core.resilience import CircuitBreaker, CircuitOpen, CircuitState, TokenBucket
from app.rag.backend import (
    EmptyKnowledgeBase,
    KnowledgeBackendError,
    RemoteKnowledgeBackend,
)


# ============================================================
# 令牌桶
# ============================================================
class TestTokenBucket:
    async def test_initial_burst_allowed(self) -> None:
        """冷启动必须放行一整桶令牌。

        否则每个进程刚启动时都会拒绝一批完全正常的请求 ——
        桶的初始值就是"这个服务允许的瞬时突发有多大"。
        """
        bucket = TokenBucket(rate=1.0, burst=5)
        assert all([await bucket.acquire() for _ in range(5)])
        assert not await bucket.acquire()

    async def test_refills_over_time(self) -> None:
        bucket = TokenBucket(rate=100.0, burst=1)
        assert await bucket.acquire()
        assert not await bucket.acquire()
        await asyncio.sleep(0.05)  # 100/s → 50ms 补 5 个
        assert await bucket.acquire()

    async def test_retry_after_is_actionable(self) -> None:
        """Retry-After 必须是个能用的数字。

        只回 429 不告诉对方等多久，等于逼调用方猜测重试节奏 ——
        那会制造出比原来更不规则的流量。
        """
        bucket = TokenBucket(rate=2.0, burst=1)
        assert await bucket.acquire()
        wait = bucket.retry_after()
        assert 0.3 < wait <= 0.6, f"2/s 的桶补 1 个令牌应约 0.5s，实际 {wait}"

    async def test_retry_after_is_zero_when_available(self) -> None:
        bucket = TokenBucket(rate=10.0, burst=3)
        assert bucket.retry_after() == 0.0

    async def test_never_blocks(self) -> None:
        """限流是**立刻拒绝**，不是排队等待。

        排队等待在高负载下会把延迟推向无穷：请求都堆在内存里等，
        等到超时才发现白等了。而且"等多久"这个决定，
        调用方比被调用方更有资格做。
        """
        bucket = TokenBucket(rate=0.001, burst=1)
        assert await bucket.acquire()
        started = time.perf_counter()
        for _ in range(50):
            assert not await bucket.acquire()
        elapsed = time.perf_counter() - started
        # 50 次拒绝应该在一瞬间完成；如果实现了等待，这里会是几十秒
        assert elapsed < 0.1, f"50 次限流判定耗时 {elapsed:.3f}s —— 它在阻塞"

    def test_rejects_invalid_params(self) -> None:
        with pytest.raises(ValueError, match="rate"):
            TokenBucket(rate=0, burst=1)
        with pytest.raises(ValueError, match="burst"):
            TokenBucket(rate=1, burst=0)


# ============================================================
# 熔断器状态机
# ============================================================
class TestCircuitBreakerStates:
    async def _fail(self) -> None:
        raise KnowledgeBackendError("模拟下游故障")

    async def test_closed_below_threshold(self) -> None:
        cb = CircuitBreaker("t", failure_threshold=3)
        for _ in range(2):
            with pytest.raises(KnowledgeBackendError):
                await cb.call(self._fail)
        assert cb.state is CircuitState.CLOSED
        assert cb.snapshot()["consecutive_failures"] == 2

    async def test_opens_at_threshold(self) -> None:
        cb = CircuitBreaker("t", failure_threshold=3)
        for _ in range(3):
            with pytest.raises(KnowledgeBackendError):
                await cb.call(self._fail)
        assert cb.state is CircuitState.OPEN

    async def test_success_resets_counter(self) -> None:
        """成功一次就清零 —— 这是"连续失败"策略的定义。

        好处是简单可预测；代价是对间歇性故障不敏感。
        对当前场景（下游要么活着要么挂了）这个取舍是划算的。
        """
        cb = CircuitBreaker("t", failure_threshold=3)

        async def ok() -> str:
            return "fine"

        for _ in range(2):
            with pytest.raises(KnowledgeBackendError):
                await cb.call(self._fail)
        assert await cb.call(ok) == "fine"
        assert cb.snapshot()["consecutive_failures"] == 0

    async def test_open_circuit_returns_without_waiting(self) -> None:
        """**这是熔断器存在的全部理由**，所以用耗时来证明。

        下游挂掉时：第 1 个请求必须等满超时（不可避免），
        但之后的请求必须**立刻**返回 —— 我们根本没发起调用。

        如果只断言"状态变成 open"，一个有 bug 的实现照样能通过：
        它可以把状态标成 open 然后继续傻等超时。
        """
        cb = CircuitBreaker("t", failure_threshold=2, recovery_timeout=60.0)

        async def slow_fail() -> str:
            await asyncio.sleep(0.05)  # 模拟一次真实的超时等待
            raise KnowledgeBackendError("超时")

        for _ in range(2):
            with pytest.raises(KnowledgeBackendError):
                await cb.call(slow_fail)

        started = time.perf_counter()
        for _ in range(20):
            with pytest.raises(CircuitOpen):
                await cb.call(slow_fail)
        elapsed = time.perf_counter() - started

        # 若熔断无效，20 次需 1.0s；有效则是微秒级
        assert elapsed < 0.05, (
            f"熔断后 20 次调用耗时 {elapsed:.3f}s —— 熔断器没有真正短路，"
            f"调用仍然在下游等待，那就等于没有熔断"
        )

    async def test_half_open_probe_success_closes(self) -> None:
        cb = CircuitBreaker("t", failure_threshold=2, recovery_timeout=0.05)

        async def ok() -> str:
            return "recovered"

        for _ in range(2):
            with pytest.raises(KnowledgeBackendError):
                await cb.call(self._fail)
        assert cb.state is CircuitState.OPEN

        await asyncio.sleep(0.06)
        assert await cb.call(ok) == "recovered"
        assert cb.state is CircuitState.CLOSED

    async def test_half_open_probe_failure_reopens_immediately(self) -> None:
        """探测失败立刻重新打开，**不等** failure_threshold。

        探测本身就是为了回答"能不能恢复"，失败已经给出答案了。
        再等阈值次只会让下游被打更多次无谓的请求。
        """
        cb = CircuitBreaker("t", failure_threshold=5, recovery_timeout=0.05)
        for _ in range(5):
            with pytest.raises(KnowledgeBackendError):
                await cb.call(self._fail)

        await asyncio.sleep(0.06)
        with pytest.raises(KnowledgeBackendError):
            await cb.call(self._fail)
        assert cb.state is CircuitState.OPEN

    async def test_half_open_limits_concurrent_probes(self) -> None:
        """半开只放行少量探测请求。

        如果时间一到就把所有请求都放回去，而下游还在启动/预热，
        一瞬间的大量请求会把它再打死一次 —— 这就是**重试风暴**。
        半开是"探测"而不是"恢复"，区别就在放行的量上。

        【为什么用 Event 而不是 sleep(0.01) 来同步】
        最初这里写的是 `create_task` + `await asyncio.sleep(0.01)`：
        单独跑稳定通过，**整个测试套件一起跑就间歇失败**。

        原因：sleep 不能保证第一个任务已经执行到"占住探测名额"那一步。
        如果它还没开始，第二次调用会自己完成 OPEN→HALF_OPEN 的转换
        并顺利拿到名额 —— 于是"应该被拒绝"的断言失败。

        这类"靠 sleep 猜调度顺序"的同步是 flaky 测试的头号来源，
        而且它在空闲的机器上几乎永远看不出问题
        （本机上单独跑 20 次全过，混在 590 个测试里才暴露）。
        正确做法是**等一个明确的事件**：`entered` 被 set 就说明
        `_before_call` 已经跑完、名额一定被占住了。
        """
        cb = CircuitBreaker("t", failure_threshold=2, recovery_timeout=0.05, half_open_max_calls=1)
        for _ in range(2):
            with pytest.raises(KnowledgeBackendError):
                await cb.call(self._fail)
        await asyncio.sleep(0.06)

        release = asyncio.Event()
        entered = asyncio.Event()

        async def slow_ok() -> str:
            entered.set()  # 能跑到这里，说明熔断器已放行且名额已被占用
            await release.wait()
            return "ok"

        first = asyncio.create_task(cb.call(slow_ok))
        # 等到确定名额被占用，而不是猜 10ms 够不够
        await asyncio.wait_for(entered.wait(), timeout=2.0)

        # 此时探测名额已满，并发的第二个必须被拒
        with pytest.raises(CircuitOpen, match="探测名额已满"):
            await cb.call(slow_ok)

        release.set()
        assert await first == "ok"
        assert cb.state is CircuitState.CLOSED

    async def test_business_state_does_not_trip_breaker(self) -> None:
        """**本文件里最重要的一条。**

        `EmptyKnowledgeBase`（知识库为空）是正常的业务状态，不是故障。
        如果它计入失败，一个"语料还没准备好"的部署会在 5 次检索后
        把自己的熔断器打开，之后所有请求都报"知识库服务不可用" ——
        而服务完全健康，只是没有数据。

        运维会去查"服务为什么挂了"，排查方向从一开始就是错的。

        **故障（fault）与业务状态（state）必须分开统计。**
        """
        cb = CircuitBreaker("t", failure_threshold=3)

        async def empty() -> str:
            raise EmptyKnowledgeBase("知识库为空")

        for _ in range(10):  # 远超阈值
            with pytest.raises(EmptyKnowledgeBase):
                await cb.call(
                    empty, count_as_failure=lambda e: not isinstance(e, EmptyKnowledgeBase)
                )

        assert cb.state is CircuitState.CLOSED, "业务状态把熔断器打开了"
        assert cb.snapshot()["consecutive_failures"] == 0

    async def test_business_state_releases_half_open_slot(self) -> None:
        """业务状态必须归还半开探测名额。

        否则一次"知识库为空"就把唯一的名额永久占住，
        熔断器再也不会恢复 —— 从 open 变成"永远 open"。
        """
        cb = CircuitBreaker("t", failure_threshold=2, recovery_timeout=0.05, half_open_max_calls=1)
        for _ in range(2):
            with pytest.raises(KnowledgeBackendError):
                await cb.call(self._fail)
        await asyncio.sleep(0.06)

        async def empty() -> str:
            raise EmptyKnowledgeBase("空")

        with pytest.raises(EmptyKnowledgeBase):
            await cb.call(empty, count_as_failure=lambda e: not isinstance(e, EmptyKnowledgeBase))

        # 名额已归还：现在应该还能再进来一个
        async def ok() -> str:
            return "ok"

        assert await cb.call(ok) == "ok"
        assert cb.state is CircuitState.CLOSED

    def test_rejects_invalid_params(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold"):
            CircuitBreaker("t", failure_threshold=0)
        with pytest.raises(ValueError, match="recovery_timeout"):
            CircuitBreaker("t", recovery_timeout=0)

    def test_snapshot_is_renderable(self) -> None:
        """快照要能直接进 /healthz 的 JSON。"""
        snap = CircuitBreaker("rag:http://x").snapshot()
        assert snap["state"] == "closed"
        assert isinstance(snap["retry_in_seconds"], float)


# ============================================================
# 与 RAG 后端的集成
# ============================================================
class TestBreakerWithRagBackend:
    async def test_breaker_short_circuits_dead_backend(self) -> None:
        """端到端：一个真实不可达的地址，熔断后必须快速失败。

        这是"RAG 服务挂掉时 agent 会不会被拖死"的直接答案。
        """
        cb = CircuitBreaker("rag:dead", failure_threshold=2, recovery_timeout=60.0)
        backend = RemoteKnowledgeBackend("http://127.0.0.1:9", timeout=1.0, breaker=cb)

        # 前两次真实尝试（会失败并计入熔断）
        for _ in range(2):
            with pytest.raises(KnowledgeBackendError):
                await backend.context("任何")
        assert cb.state is CircuitState.OPEN

        started = time.perf_counter()
        for _ in range(10):
            with pytest.raises(CircuitOpen):
                await backend.context("任何")
        elapsed = time.perf_counter() - started
        assert elapsed < 0.05, f"熔断后 10 次调用耗时 {elapsed:.3f}s，没有短路"

        await backend.aclose()

    async def test_empty_corpus_never_opens_breaker(self) -> None:
        """走真实 HTTP 路径确认 503 不会累积失败计数。

        用 ASGITransport 直连一个语料为空的 RAG 服务 ——
        这是"语料没准备好"的真实部署形态。
        """
        import app.rag.factory as factory
        import app.rag_service.main as svc
        from app.rag.embedder import TfidfEmbedder
        from app.rag.retriever import Retriever
        from app.rag_service.main import app as rag_app

        emb = TfidfEmbedder()
        emb.fit(["占位"])
        empty_retriever = Retriever([], emb)

        original = factory.get_shared_retriever
        factory.get_shared_retriever = lambda *a, **kw: empty_retriever  # type: ignore[assignment]
        svc.get_shared_retriever = factory.get_shared_retriever  # type: ignore[assignment]
        try:
            cb = CircuitBreaker("rag:empty", failure_threshold=2)
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=rag_app), base_url="http://rag"
            )
            backend = RemoteKnowledgeBackend("http://rag", client=client, breaker=cb)

            for _ in range(6):
                with pytest.raises(EmptyKnowledgeBase):
                    await backend.context("任何")

            assert cb.state is CircuitState.CLOSED, (
                "服务端的 503（知识库为空）把熔断器打开了 —— "
                "运维会去查服务为什么挂了，而实际只是没有数据"
            )
            await client.aclose()
        finally:
            factory.get_shared_retriever = original  # type: ignore[assignment]
            svc.get_shared_retriever = original  # type: ignore[assignment]

    async def test_tool_tells_model_not_to_retry_when_open(self) -> None:
        """熔断打开时，工具必须告诉模型"不要重试"。

        对模型来说，两种情况该做的事完全不同：
          · 下游故障 → 可能是瞬时问题，换一种检索方式值得再试
          · 熔断打开 → 重试**一定**失败（我们根本没发起调用），
                       正确动作是改用别的工具或直接回答

        如果把熔断报成普通的"服务不可用"，模型会去重试 ——
        而重试恰好是熔断器最想阻止的那个行为。
        """
        from app.tools.knowledge import KnowledgeSearchTool, SearchKnowledgeParams

        cb = CircuitBreaker("rag:dead", failure_threshold=1, recovery_timeout=60.0)
        backend = RemoteKnowledgeBackend("http://127.0.0.1:9", timeout=1.0, breaker=cb)
        tool = KnowledgeSearchTool(backend=backend)

        first = await tool.run(SearchKnowledgeParams(query="任何"))
        assert not first.ok
        assert "不可用" in first.content  # 第一次是真实故障

        second = await tool.run(SearchKnowledgeParams(query="任何"))
        assert not second.ok
        assert "熔断" in second.content
        assert "不要重试" in second.content

        await backend.aclose()


# ============================================================
# 限流接入 HTTP 层
# ============================================================
class TestRateLimitOverHttp:
    """验证限流真的作用在请求路径上，且拒绝方式对调用方友好。

    【为什么要测到 HTTP 层】
    留出一个"只测 TokenBucket 本身"的测试是很容易的，但那证明不了
    限流接进去了 —— 中间件顺序、依赖注册、配置读取，任何一处错了
    都会让限流静默失效。**限流静默失效比限流阈值设错更危险**：
    你以为自己有保护，实际在裸奔。
    """

    @pytest.fixture
    def limited_client(self, client):  # type: ignore[no-untyped-def]
        """临时把 app 的配置换成"限流开启、桶很小"。"""
        from app.core.config import ResilienceSettings

        app = client.app
        original = app.state.settings
        original_buckets = getattr(app.state, "rate_buckets", None)

        app.state.settings = original.model_copy(
            update={
                "resilience": ResilienceSettings(
                    rate_limit_enabled=True,
                    rate_limit_rps=0.01,  # 极慢，用完就不会补
                    rate_limit_burst=2,
                    rate_limit_global_rps=0.0,
                )
            }
        )
        # 清空桶，避免上一个用例的令牌残留
        app.state.rate_buckets = {}
        app.state.rate_global = None
        try:
            yield client
        finally:
            app.state.settings = original
            app.state.rate_buckets = original_buckets if original_buckets is not None else {}
            app.state.rate_global = None

    def test_burst_allowed_then_429(self, limited_client) -> None:  # type: ignore[no-untyped-def]
        """桶容量内的请求放行，超出后返回 429。

        注意这里**不关心 200 还是 500** —— `/api/chat` 会在没有
        LLM 密钥时返回配置错误，那不是本用例要测的东西。
        所有 < 400 或明确的业务错误都算"没被限流"。
        """
        codes = []
        for _ in range(4):
            r = limited_client.post(
                "/api/chat",
                json={"message": "hi", "session_id": "rl-test-1"},
            )
            codes.append(r.status_code)

        assert 429 not in codes[:2], f"桶容量 2 之内不该被限流：{codes}"
        assert 429 in codes[2:], f"超出桶容量后必须开始限流：{codes}"

    def test_429_carries_retry_after(self, limited_client) -> None:  # type: ignore[no-untyped-def]
        """429 必须带 `Retry-After`，且是个能用的秒数。

        只回 429 不告诉对方等多久，等于逼调用方用猜测的节奏重试 ——
        猜出来的节奏通常比原来更糟（同步重试、固定间隔），
        会把一个平稳的过载变成周期性的尖峰。
        """
        for _ in range(4):
            r = limited_client.post("/api/chat", json={"message": "hi", "session_id": "rl-test-2"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers, "429 缺少 Retry-After 头"
        assert int(r.headers["Retry-After"]) >= 1
        assert "秒后重试" in r.json()["detail"]

    def test_limit_is_per_session(self, limited_client) -> None:  # type: ignore[no-untyped-def]
        """限流按会话隔离：一个会话被限不该影响另一个。

        这正是选择"按会话"而不是"按 IP"的原因 —— 同一 IP 后面
        可能是很多人（公司出口、移动网络），互相不该牵连。
        """
        for _ in range(4):
            limited_client.post("/api/chat", json={"message": "hi", "session_id": "rl-a"})

        r = limited_client.post("/api/chat", json={"message": "hi", "session_id": "rl-b"})
        assert r.status_code != 429, "会话 B 被会话 A 的限流牵连了"

    def test_disabled_by_default(self) -> None:
        """默认关闭。

        限流阈值强依赖业务容量，**没有标定过的阈值比没有阈值更危险** ——
        它会直接拒掉正常用户。所以默认关闭，由部署方按实际容量标定。
        """
        from app.core.config import ResilienceSettings

        assert ResilienceSettings().rate_limit_enabled is False

    def test_circuit_enabled_by_default(self) -> None:
        """熔断默认开启。

        它不是优化，是防级联故障的必需件。关掉不会让系统更快，
        只会让"下游挂了"升级成"整个服务挂了"。
        """
        from app.core.config import ResilienceSettings

        assert ResilienceSettings().circuit_enabled is True

    def test_healthz_exposes_resilience_config(self, client) -> None:  # type: ignore[no-untyped-def]
        body = client.get("/healthz").json()
        assert "circuit_enabled" in body
        assert "rate_limit_enabled" in body
