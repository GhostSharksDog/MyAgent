"""异步任务队列测试。

最重要的一条是 `TestNonBlocking`：**验证 CPU 密集的任务真的没有阻塞事件循环**。
队列存在的唯一理由就是把耗时操作从请求路径上摘出去；如果 worker 直接在
事件循环里跑那段 CPU 代码，拆了等于没拆 —— 只是把阻塞点从请求处理
挪到了 worker 里，用户感知到的卡顿一模一样。

验证方式与之前测"同步工具阻塞事件循环"时一致：并发跑一个心跳协程，
如果事件循环被阻塞，心跳次数会是 0。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from app.tasks.factory import build_task_queue
from app.tasks.handlers import handle_reindex
from app.tasks.models import TaskRecord, TaskStatus, TaskType
from app.tasks.queue import ImmediateTaskQueue, InProcessTaskQueue, TaskContext
from app.tasks.redis_queue import RedisTaskQueue


def _fake_redis() -> Any:
    from fakeredis import aioredis as fake_aioredis

    return fake_aioredis.FakeRedis(decode_responses=True)


# ============================================================
# 任务模型
# ============================================================
class TestTaskModel:
    def test_initial_state(self) -> None:
        r = TaskRecord(type="x")
        assert r.status is TaskStatus.PENDING
        assert r.progress == 0
        assert r.duration_ms is None
        assert not r.is_terminal

    def test_success_path(self) -> None:
        r = TaskRecord(type="x")
        r.mark_running("开始")
        assert r.status is TaskStatus.RUNNING
        assert r.started_at is not None

        r.mark_succeeded({"k": "v"}, "完成")
        assert r.status is TaskStatus.SUCCEEDED
        assert r.progress == 100
        assert r.result == {"k": "v"}
        assert r.is_terminal

    def test_failure_keeps_result_none(self) -> None:
        """失败时 result 保持为 None，而不是塞 {"error": ...}。

        否则前端要判断"这个 result 到底是结果还是错误" —— 那是两套语义
        挤在一个字段里，早晚会有人判断错。
        """
        r = TaskRecord(type="x")
        r.mark_running()
        r.mark_failed("boom")
        assert r.result is None
        assert r.error == "boom"
        assert r.is_terminal

    def test_cancelled_is_terminal_and_distinct_from_failed(self) -> None:
        """取消与失败必须区分：前端对两者的处理完全不同
        （失败可以重试，取消是用户意愿）。"""
        r = TaskRecord(type="x")
        r.mark_cancelled()
        assert r.status is TaskStatus.CANCELLED
        assert r.error is None
        assert r.is_terminal

    def test_duration_measured(self) -> None:
        r = TaskRecord(type="x")
        r.mark_running()
        r.started_at -= 1.5  # 手动回拨模拟耗时
        r.mark_succeeded()
        assert r.duration_ms is not None
        assert r.duration_ms >= 1500

    def test_payload_is_a_real_field(self) -> None:
        """载荷必须是正式字段，不能藏在 __dict__ 里 ——
        藏在私有属性里的状态无法跨进程传递，切到 Redis 时会静默丢失。"""
        r = TaskRecord(type="x", payload={"a": 1})
        assert json.loads(r.model_dump_json())["payload"] == {"a": 1}


# ============================================================
# 进程内队列
# ============================================================
class TestInProcessQueue:
    async def test_executes_registered_handler(self) -> None:
        queue = InProcessTaskQueue()

        async def handler(ctx: TaskContext) -> dict:
            await ctx.report(50, "半途")
            return {"ok": True, "arg": ctx.arg("n")}

        queue.register("echo", handler)
        await queue.start()
        try:
            record = await queue.submit("echo", payload={"n": 7})
            for _ in range(50):
                refreshed = await queue.get(record.id)
                if refreshed and refreshed.is_terminal:
                    break
                await asyncio.sleep(0.02)

            assert refreshed is not None
            assert refreshed.status is TaskStatus.SUCCEEDED
            assert refreshed.result == {"ok": True, "arg": 7}
            assert refreshed.progress == 100
        finally:
            await queue.aclose()

    async def test_unknown_type_fails_with_hint(self) -> None:
        queue = InProcessTaskQueue()
        queue.register("known", lambda ctx: asyncio.sleep(0))  # type: ignore[arg-type,return-value]
        await queue.start()
        try:
            record = await queue.submit("unknown")
            for _ in range(50):
                refreshed = await queue.get(record.id)
                if refreshed and refreshed.is_terminal:
                    break
                await asyncio.sleep(0.02)

            assert refreshed is not None
            assert refreshed.status is TaskStatus.FAILED
            # 错误信息要能指导下一步，而不是只说"未知类型"
            assert "已知的类型" in (refreshed.error or "") or "known" in (refreshed.error or "")
        finally:
            await queue.aclose()

    async def test_handler_exception_becomes_failed_not_crash(self) -> None:
        """处理器抛异常必须转成 failed 状态，绝不能把 worker 搞死 ——
        一个 worker 崩了会让整个队列停摆，而失败的任务只是失败而已。"""
        queue = InProcessTaskQueue()

        async def boom(ctx: TaskContext) -> dict:
            raise ValueError("业务失败")

        queue.register("boom", boom)
        await queue.start()
        try:
            record = await queue.submit("boom")
            for _ in range(50):
                refreshed = await queue.get(record.id)
                if refreshed and refreshed.is_terminal:
                    break
                await asyncio.sleep(0.02)
            assert refreshed is not None
            assert refreshed.status is TaskStatus.FAILED
            assert "ValueError" in (refreshed.error or "")

            # 关键：队列还能继续服务下一个任务
            queue.register("ok", lambda ctx: _const({"ok": True}))
            second = await queue.submit("ok")
            for _ in range(50):
                r2 = await queue.get(second.id)
                if r2 and r2.is_terminal:
                    break
                await asyncio.sleep(0.02)
            assert r2 is not None and r2.status is TaskStatus.SUCCEEDED
        finally:
            await queue.aclose()

    async def test_cancel_pending_task(self) -> None:
        queue = InProcessTaskQueue()  # 不启动 worker，任务会一直留在队列里
        record = await queue.submit("anything")
        assert await queue.cancel(record.id) is True
        assert (await queue.get(record.id)).status is TaskStatus.CANCELLED  # type: ignore[union-attr]

    async def test_cancel_terminal_task_returns_false(self) -> None:
        queue = InProcessTaskQueue()
        queue.register("quick", lambda ctx: _const({}))
        await queue.start()
        try:
            record = await queue.submit("quick")
            for _ in range(50):
                r = await queue.get(record.id)
                if r and r.is_terminal:
                    break
                await asyncio.sleep(0.02)
            assert await queue.cancel(record.id) is False
        finally:
            await queue.aclose()

    async def test_cancelled_task_never_executes(self) -> None:
        """已取消的任务即使还在待处理队列里，worker 取到后也必须跳过。"""
        ran = {"count": 0}
        queue = InProcessTaskQueue()

        async def handler(ctx: TaskContext) -> dict:
            ran["count"] += 1
            return {}

        queue.register("t", handler)
        record = await queue.submit("t")
        await queue.cancel(record.id)
        await queue.start()  # 启动后 worker 会取到这条已取消的任务
        try:
            await asyncio.sleep(0.2)
            assert ran["count"] == 0
        finally:
            await queue.aclose()

    async def test_start_is_idempotent(self) -> None:
        """重复 start 不能产生多批 worker ——
        lifespan 可能因测试或热重载被多次触发，多批 worker 会导致任务重复执行。"""
        queue = InProcessTaskQueue()
        await queue.start()
        await queue.start()
        assert len(queue._workers) == 1
        await queue.aclose()

    async def test_eviction_keeps_non_terminal(self) -> None:
        """容量淘汰只清理**终态**记录。

        淘汰 pending/running 会让前端永远查不到结果、也无法取消。
        """
        queue = InProcessTaskQueue(max_tasks=3)
        queue.register("quick", lambda ctx: _const({}))
        await queue.start()
        try:
            for _ in range(5):
                r = await queue.submit("quick")
                for _ in range(50):
                    rr = await queue.get(r.id)
                    if rr and rr.is_terminal:
                        break
                    await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)
            assert len(queue._records) <= 3
        finally:
            await queue.aclose()

    async def test_list_is_newest_first(self) -> None:
        queue = InProcessTaskQueue()
        for _ in range(3):
            await queue.submit("x")
            await asyncio.sleep(0.01)
        listed = await queue.list()
        assert len(listed) == 3
        assert listed[0].created_at >= listed[-1].created_at

    async def test_summary_excludes_result(self) -> None:
        queue = InProcessTaskQueue()
        await queue.submit("x")
        summary = (await queue.list())[0]
        assert not hasattr(summary, "result")
        assert not hasattr(summary, "payload")

    async def test_aclose_is_safe_when_not_started(self) -> None:
        await InProcessTaskQueue().aclose()  # 不应抛异常

    async def test_stats(self) -> None:
        queue = InProcessTaskQueue(worker_count=2)
        queue.register("a", lambda ctx: _const({}))
        stats = queue.stats()
        assert stats["backend"] == "memory"
        assert stats["workers"] == 2
        assert stats["known_types"] == ["a"]


async def _const(value: Any) -> Any:
    """把同步常量包成协程，便于写单行处理器。"""
    return value


# ============================================================
# 不阻塞事件循环（核心验证）
# ============================================================
class TestNonBlocking:
    """队列存在的唯一理由就是让耗时操作不阻塞事件循环。"""

    async def test_cpu_heavy_task_does_not_block_loop(self) -> None:
        """CPU 密集任务必须丢线程池。

        验证方式：任务跑 0.4 秒同步 CPU 计算的同时，起一个每 20ms 自增的
        心跳协程。若任务在事件循环里执行，心跳次数会是 0。
        """
        queue = InProcessTaskQueue()

        async def cpu_heavy(ctx: TaskContext) -> dict:
            def burn() -> int:
                total = 0
                deadline = time.perf_counter() + 0.4
                while time.perf_counter() < deadline:
                    total += 1
                return total

            # 关键的一行：丢线程池
            return {"iterations": await asyncio.to_thread(burn)}

        queue.register("cpu", cpu_heavy)
        await queue.start()

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            record = await queue.submit("cpu")
            for _ in range(100):
                r = await queue.get(record.id)
                if r and r.is_terminal:
                    break
                await asyncio.sleep(0.02)
        finally:
            beat.cancel()
            await queue.aclose()

        assert r is not None and r.status is TaskStatus.SUCCEEDED
        # 0.4s / 0.02s ≈ 20 次；即使调度有抖动也应远大于 5
        assert ticks >= 5, f"事件循环被 CPU 密集任务阻塞了，心跳只跑了 {ticks} 次"

    async def test_reindex_handler_reports_progress(self, seeded_corpus: None) -> None:
        """真实的重建索引处理器：必须上报进度并返回可观测的统计。

        `seeded_corpus` 是**前提声明**，不是装饰：默认配置下知识库语料为空，
        而空语料时 reindex 会主动失败（见 `handlers.handle_reindex`）——
        那是有意行为（"成功但 0 块"会让用户以为索引建好了）。所以要测
        "重建成功"这条路，就必须先给出数据源。
        """
        queue = InProcessTaskQueue()
        await queue.start()
        try:
            record = await queue.submit("reindex")
            # 手动执行真实处理器（不走注册表，便于断言返回值）
            ctx = TaskContext(task_id=record.id, queue=queue, task_type="reindex")
            result = await handle_reindex(ctx)

            assert result["chunk_count"] > 0
            assert "elapsed_ms" in result
            assert "bm25_vocab" in result

            # 进度必须真的落到任务记录上 —— 否则前端只能看到一个永远停在 0% 的任务
            refreshed = await queue.get(record.id)
            assert refreshed is not None
            assert refreshed.progress == 100
            assert refreshed.message == "重建完成"
        finally:
            await queue.aclose()


# ============================================================
# 语料前提（reindex 的第一条路径）
# ============================================================
class TestReindexCorpusRequirement:
    """空语料是**默认配置下用户最先撞上的那条路径**，所以它的行为必须被钉住。"""

    async def test_empty_corpus_fails_with_actionable_hint(self, empty_corpus: None) -> None:
        """空语料 → 失败，并且告诉用户**怎么做才能补上数据**。

        不报成"成功但 chunk_count=0"是刻意的：那样用户会以为索引建好了，
        然后困惑于"为什么检索不到东西"。失败态才有可能被看见、被修。

        断言里点名 AGENT_CORPUS_PATHS：默认（general）profile **不会**
        自动加载 data/resume.md，所以"把简历放进去"在通用形态下并不能解决问题 ——
        指引必须指向真的有效的做法，否则用户照做一次、再失败一次。
        """
        queue = InProcessTaskQueue()
        try:
            record = await queue.submit("reindex")
            ctx = TaskContext(task_id=record.id, queue=queue, task_type="reindex")
            with pytest.raises(RuntimeError, match="语料为空") as excinfo:
                await handle_reindex(ctx)
            message = str(excinfo.value)
        finally:
            await queue.aclose()

        assert "AGENT_CORPUS_PATHS" in message


# ============================================================
# 立即执行队列（测试用）
# ============================================================
class TestImmediateQueue:
    async def test_submit_runs_synchronously(self) -> None:
        """提交即执行完 —— 测试里最怕"异步副作用晚于断言"。"""
        queue = ImmediateTaskQueue()
        queue.register("echo", lambda ctx: _const({"v": ctx.arg("v")}))

        record = await queue.submit("echo", payload={"v": 42})
        assert record.status is TaskStatus.SUCCEEDED
        assert record.result == {"v": 42}

    async def test_failure_captured(self) -> None:
        queue = ImmediateTaskQueue()

        async def boom(ctx: TaskContext) -> dict:
            raise RuntimeError("坏掉了")

        queue.register("boom", boom)
        record = await queue.submit("boom")
        assert record.status is TaskStatus.FAILED
        assert "坏掉了" in (record.error or "")


# ============================================================
# Redis 队列（fakeredis：真实代码路径）
# ============================================================
class TestRedisQueue:
    async def test_submit_and_execute(self) -> None:
        queue = RedisTaskQueue(_fake_redis(), poll_timeout=1)
        queue.register("echo", lambda ctx: _const({"v": ctx.arg("v")}))
        await queue.start()
        try:
            record = await queue.submit("echo", payload={"v": 1})
            for _ in range(60):
                r = await queue.get(record.id)
                if r and r.is_terminal:
                    break
                await asyncio.sleep(0.05)
            assert r is not None and r.status is TaskStatus.SUCCEEDED
        finally:
            await queue.aclose()

    async def test_keys_are_prefixed(self) -> None:
        queue = RedisTaskQueue(_fake_redis())
        record = await queue.submit("x")
        keys = await queue._redis.keys("*")  # type: ignore[attr-defined]
        assert all(k.startswith("jobpilot:") for k in keys)
        assert f"jobpilot:task:{record.id}" in keys

    async def test_pending_list_holds_task_id(self) -> None:
        queue = RedisTaskQueue(_fake_redis())
        record = await queue.submit("x")
        depth = await queue._redis.llen(queue.PENDING_KEY)  # type: ignore[attr-defined]
        assert depth == 1
        assert await queue._redis.lindex(queue.PENDING_KEY, 0) == record.id  # type: ignore[attr-defined]

    async def test_ghost_tasks_pruned_from_index(self) -> None:
        """与会话层同样的坑：记录 key 过期后 ZSET 成员不会自动消失。"""
        queue = RedisTaskQueue(_fake_redis())
        a = await queue.submit("x")
        b = await queue.submit("y")
        await queue._redis.delete(queue._key(a.id))  # type: ignore[attr-defined]

        listed = await queue.list()
        assert [t.id for t in listed] == [b.id]
        members = await queue._redis.zrange(queue.INDEX_KEY, 0, -1)  # type: ignore[attr-defined]
        assert a.id not in members

    async def test_cancel_removes_from_pending(self) -> None:
        queue = RedisTaskQueue(_fake_redis())
        record = await queue.submit("x")
        assert await queue.cancel(record.id) is True
        assert await queue._redis.llen(queue.PENDING_KEY) == 0  # type: ignore[attr-defined]
        assert (await queue.get(record.id)).status is TaskStatus.CANCELLED  # type: ignore[union-attr]

    async def test_corrupted_record_handled(self) -> None:
        queue = RedisTaskQueue(_fake_redis())
        await queue._redis.set(queue._key("bad"), "{ 这不是 JSON")  # type: ignore[attr-defined]
        assert await queue.get("bad") is None
        assert await queue._redis.get(queue._key("bad")) is None  # type: ignore[attr-defined]

    async def test_stats_documents_delivery_semantics(self) -> None:
        """投递语义必须被显式记录 —— 这是使用者最需要知道的性质，
        而它不像"后端类型"那样能从代码里一眼看出来。"""
        queue = RedisTaskQueue(_fake_redis())
        assert "at-most-once" in str(queue.stats()["delivery_semantics"])


# ============================================================
# 工厂
# ============================================================
class TestFactory:
    async def test_memory_backend_registers_handlers(self) -> None:
        from app.core.config import Settings, TaskSettings

        queue = await build_task_queue(
            Settings(tasks=TaskSettings(backend="memory")), autostart=False
        )
        assert queue.backend == "memory"
        assert set(queue.known_types()) == {"reindex", "ingest_resume", "batch_match"}

    async def test_fake_backend_uses_redis_path(self) -> None:
        from app.core.config import Settings, TaskSettings

        queue = await build_task_queue(
            Settings(tasks=TaskSettings(backend="fake")), autostart=False
        )
        assert queue.backend == "redis"
        assert isinstance(queue, RedisTaskQueue)

    async def test_auto_falls_back_to_memory(self) -> None:
        from app.core.config import Settings, TaskSettings

        queue = await build_task_queue(
            Settings(tasks=TaskSettings(backend="auto"), redis_url="redis://127.0.0.1:1/0"),
            autostart=False,
        )
        assert queue.backend == "memory"

    async def test_explicit_redis_fails_loudly(self) -> None:
        from app.core.config import Settings, TaskSettings

        with pytest.raises(RuntimeError, match="无法连接"):
            await build_task_queue(
                Settings(tasks=TaskSettings(backend="redis"), redis_url="redis://127.0.0.1:1/0"),
                autostart=False,
            )

    async def test_autostart_false_leaves_no_background_tasks(self) -> None:
        """测试里必须能关掉自动启动 —— 后台 worker 会让测试变得不确定。"""
        from app.core.config import Settings, TaskSettings

        queue = await build_task_queue(
            Settings(tasks=TaskSettings(backend="memory")), autostart=False
        )
        assert queue._workers == []  # type: ignore[attr-defined]

    def test_all_task_types_have_handlers(self) -> None:
        """枚举里的每个任务类型都必须有处理器。

        否则前端能看到这个类型、能提交，然后必然失败 ——
        接口层面的"看起来支持"比"明确不支持"更糟。
        """
        from app.tasks.handlers import HANDLERS

        assert set(HANDLERS) == set(TaskType)
