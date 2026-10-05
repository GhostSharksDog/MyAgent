"""Redis 探测的**耗时**与措辞测试。

【为什么这里要测时间，而不只测"能不能降级"】

降级一直是对的 —— 它的行为没坏，坏的是**代价与观感**：

    SESSION_BACKEND=auto / TASK_BACKEND=auto 每次启动各真连一次 Redis。
    本机没起 Redis 时（redis-py 6.4.0，实测）：
        默认参数                    → 2.04s
        只关重试                     → 2.04s   ← 所以那两秒不是重试造成的
        socket_connect_timeout=0.5  → 0.51s
    两个后端各一次 → 启动白等 4 秒，而结论只是"降级到内存"这个正常状态。

这 4 秒的实际后果不是"慢一点"，而是**让人以为命令卡住了**：实测中它足以
让开发者按下 Ctrl+C，然后看到服务"自己退出"并来问为什么。
所以"启动不会白等"这件事必须由测试守住，否则下一个人把超时参数去掉、
功能测试仍然全绿。

【第二条：日志措辞】
原来的 WARNING 是"连接 Redis 失败…原因：Error 10061"，读起来像故障；
而本地开发没有 Redis 是**正常状态**。措辞改错方向的代价很具体：
用户会去排查一个不存在的问题（这次就发生了）。
"""

from __future__ import annotations

import time

import pytest
from app.core.config import Settings, get_settings
from app.session.factory import build_session_store
from app.session.store import InMemorySessionStore
from app.tasks.factory import build_task_queue

# 一个确定没人监听的地址：连不上，但**立刻**被拒（不是黑洞，不会拖满超时）
DEAD_URL = "redis://127.0.0.1:6399/0"


def _settings(**overrides: object) -> Settings:
    """改一份配置（不碰 .env）。"""
    return get_settings().model_copy(update=overrides)


class TestProbeCost:
    auth: str = ""

    async def test_session_store_falls_back_fast(self) -> None:
        """连不上 Redis 时会话存储快速降级 —— 不许再白等 2 秒。"""
        settings = _settings(redis_url=DEAD_URL)
        started = time.perf_counter()
        store = await build_session_store(settings)
        elapsed = time.perf_counter() - started

        assert isinstance(store, InMemorySessionStore)
        # 默认建连超时 0.5s；给一点余量。修复前这里是 2.04s，所以阈值有牙。
        assert elapsed < 1.2, f"降级花了 {elapsed:.2f}s（修复前是 2.04s）"

    async def test_task_queue_falls_back_fast(self) -> None:
        """任务队列同理 —— 两个后端加起来才是那 4 秒。"""
        settings = _settings(redis_url=DEAD_URL)
        started = time.perf_counter()
        queue = await build_task_queue(settings)
        elapsed = time.perf_counter() - started

        assert queue.backend == "memory"
        assert elapsed < 1.2, f"降级花了 {elapsed:.2f}s"
        await queue.aclose()

    async def test_both_backends_together_stay_under_a_second_and_a_half(self) -> None:
        """启动时两个探测加起来的量级 —— 这才是用户真正感受到的那个数字。"""
        settings = _settings(redis_url=DEAD_URL)
        started = time.perf_counter()
        store = await build_session_store(settings)
        queue = await build_task_queue(settings)
        elapsed = time.perf_counter() - started

        assert isinstance(store, InMemorySessionStore) and queue.backend == "memory"
        assert elapsed < 1.6, f"两次探测共花了 {elapsed:.2f}s（修复前约 4.1s）"
        await queue.aclose()

    async def test_probe_timeout_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置项必须真的被用上，否则它只是文档里的一个摆设。"""
        captured: dict[str, object] = {}

        def fake_from_url(url: str, **kwargs: object):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            raise RuntimeError("不真的连 —— 这里只检查参数")

        import redis.asyncio as redis_asyncio

        monkeypatch.setattr(redis_asyncio, "from_url", fake_from_url)
        settings = _settings(redis_url=DEAD_URL, redis_connect_timeout=1.75)
        await build_session_store(settings)

        assert captured.get("socket_connect_timeout") == 1.75
        # 只限定建连：**不能**顺手把 socket_timeout 也设上，
        # 否则一次慢命令会被误判成"Redis 挂了"，而那与建连无关
        assert "socket_timeout" not in captured

    async def test_memory_backend_never_touches_redis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式 memory 时一次都不该连 —— "零配置可用"要真的一点代价都没有。"""
        import redis.asyncio as redis_asyncio

        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("显式 memory 时不该去连 Redis")

        monkeypatch.setattr(redis_asyncio, "from_url", boom)
        settings = _settings(session=_settings().session.model_copy(update={"backend": "memory"}))
        store = await build_session_store(settings)
        assert isinstance(store, InMemorySessionStore)


class TestProbeMessage:
    """降级时的日志措辞：说清"现在用的是什么"，而不是"出错了"。"""

    async def test_message_says_normal_for_local_dev(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = _settings(redis_url=DEAD_URL)
        with caplog.at_level("WARNING"):
            await build_session_store(settings)

        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "未使用 Redis" in text, f"措辞应当描述「决定」而不是「失败」：{text}"
        assert "内存" in text
        # 必须点明"什么时候这才是问题"，否则就是静默降级
        assert "多副本" in text
        # 也必须点明"本地开发属正常"，否则用户会去查一个不存在的问题
        assert "正常" in text
        # 以及怎么调这个超时
        assert "REDIS_CONNECT_TIMEOUT" in text

    async def test_technical_reason_moves_to_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        """原始异常降级到 DEBUG：它在 WARNING 里只会增加噪音。"""
        settings = _settings(redis_url=DEAD_URL)
        with caplog.at_level("DEBUG"):
            await build_session_store(settings)

        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "应当有一条 WARNING 说明降级"
        assert not any("Error" in w and "connecting" in w for w in warnings), (
            "原始连接错误不该出现在 WARNING 里"
        )
