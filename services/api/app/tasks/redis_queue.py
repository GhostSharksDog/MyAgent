"""Redis 版任务队列：支持多进程 / 多副本部署。

【必须先讲清的正确性问题：这个实现是"至多一次"】

一个朴素的消息队列用 `LPUSH` + `BRPOP` 就够了：

    生产：LPUSH jobpilot:tasks:pending <task_id>
    消费：BRPOP jobpilot:tasks:pending 1

但 **BRPOP 在取走消息的瞬间就从 Redis 删除了它**。如果 worker 在
"已取走、还没执行完"之间崩溃（进程被杀、机器断电），这条任务就**永久丢失**了，
而且没有任何痕迹 —— 用户看到任务一直停在"运行中"，最后查不到结果。

这是"至多一次"（at-most-once）语义。它适合什么场景？
  - 任务可重跑且重跑的代价可接受
  - 或者有外部机制兜底（比如定时全量重建索引，丢一次无所谓）

【要升级成"至少一次"（at-least-once），有两条成熟路径】

1. **可靠队列模式**（Redis 官方文档的可靠队列）
   `BRPOPLPUSH pending processing` —— 取走时同时放进 processing 列表，
   执行完再 `LREM`。worker 崩溃时任务留在 processing 里，
   由守护任务按租约超时搬回 pending。

2. **Redis Streams + 消费者组**（推荐）
   `XADD` 生产、`XREADGROUP` 消费、`XACK` 确认、`XAUTOCLAIM` 处理
   卡住的 pending 条目。原生支持确认与重投递，不用自己维护两个结构的一致性。

**当前选择路径 1 的简化版（只做 BRPOP）并明确记录这笔技术债**，
理由是：可靠投递的边界情况（守护任务的租约超时设多少、重投递如何去重、
重复执行如何幂等）需要配套的幂等设计才能真正正确 ——
半套实现比诚实的不实现更危险。升级到路径 2 是 P4 的事，风险点已经写明。
"""

from __future__ import annotations

import asyncio
import json
import logging

from app.tasks.models import TaskRecord, TaskStatus, TaskSummary
from app.tasks.queue import TaskQueue

logger = logging.getLogger(__name__)


class RedisTaskQueue(TaskQueue):
    """基于 Redis 的任务队列。

    key 设计：
        jobpilot:task:{id}        任务记录（JSON 字符串，带 TTL）
        jobpilot:tasks:pending    待处理队列（LIST）
        jobpilot:tasks:index      任务索引（ZSET，score = created_at）
    """

    PREFIX = "jobpilot:task:"
    PENDING_KEY = "jobpilot:tasks:pending"
    INDEX_KEY = "jobpilot:tasks:index"

    def __init__(
        self,
        client: object,
        *,
        worker_count: int = 1,
        ttl_seconds: int = 24 * 3600,
        poll_timeout: int = 1,
    ) -> None:
        super().__init__()
        self._redis = client
        self._worker_count = worker_count
        self._ttl = ttl_seconds
        self._poll_timeout = poll_timeout
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[None]] = {}
        self._started = False

    @property
    def backend(self) -> str:
        return "redis"

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker_loop(i), name=f"redis-task-worker-{i}")
            for i in range(self._worker_count)
        ]
        logger.info(
            "任务队列已启动：%d 个 worker（Redis）。"
            "注意当前为**至多一次**投递语义，worker 崩溃会丢任务",
            self._worker_count,
        )

    async def aclose(self) -> None:
        if not self._started:
            return
        self._started = False
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("任务队列已关闭（Redis）")

    # ---------- 接口 ----------

    async def submit(
        self, task_type: str, *, payload: dict | None = None, session_id: str | None = None
    ) -> TaskRecord:
        record = TaskRecord(type=task_type, payload=payload or {}, session_id=session_id)
        pipe = self._redis.pipeline()  # type: ignore[attr-defined]
        pipe.set(self._key(record.id), record.model_dump_json(), ex=self._ttl)
        pipe.zadd(self.INDEX_KEY, {record.id: record.created_at})
        pipe.expire(self.INDEX_KEY, self._ttl)
        # 入队放在最后：如果前面的写失败，任务不会被消费到一半
        pipe.lpush(self.PENDING_KEY, record.id)
        await pipe.execute()
        logger.info("任务入队：%s（%s）", record.id, task_type)
        return record

    async def get(self, task_id: str) -> TaskRecord | None:
        raw = await self._redis.get(self._key(task_id))  # type: ignore[attr-defined]
        if raw is None:
            return None
        try:
            return TaskRecord.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("任务 %s 记录损坏，已删除：%s", task_id, exc)
            await self._redis.delete(self._key(task_id))  # type: ignore[attr-defined]
            return None

    async def list(self, *, limit: int = 20) -> list[TaskSummary]:
        ids = await self._redis.zrevrange(self.INDEX_KEY, 0, limit - 1)  # type: ignore[attr-defined]
        out: list[TaskSummary] = []
        stale: list[str] = []
        for raw_id in ids:
            tid = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
            record = await self.get(tid)
            if record is None:
                # 与会话索引同样的坑：记录 key 过期后 ZSET 成员不会自动消失。
                # 不清理就会在列表里留下点进去是空的"幽灵任务"。
                stale.append(tid)
                continue
            out.append(self._summary(record))
        if stale:
            await self._redis.zrem(self.INDEX_KEY, *stale)  # type: ignore[attr-defined]
        return out

    async def cancel(self, task_id: str) -> bool:
        record = await self.get(task_id)
        if record is None or record.is_terminal:
            return False

        if record.status is TaskStatus.PENDING:
            record.mark_cancelled()
            await self._save(record)
            # 从待处理队列里移除。它可能已经不在队列里（被 worker 取走），
            # LREM 返回 0 是正常情况，不是错误。
            await self._redis.lrem(self.PENDING_KEY, 0, task_id)  # type: ignore[attr-defined]
            return True

        running = self._running.get(task_id)
        if running is not None:
            running.cancel()
            return True
        return False

    async def update_progress(self, task_id: str, progress: int, message: str = "") -> None:
        record = await self.get(task_id)
        if record is None:
            return
        record.progress = max(0, min(100, progress))
        if message:
            record.message = message
        await self._save(record)

    # ---------- 内部 ----------

    def _key(self, task_id: str) -> str:
        return f"{self.PREFIX}{task_id}"

    async def _save(self, record: TaskRecord) -> None:
        await self._redis.set(  # type: ignore[attr-defined]
            self._key(record.id), record.model_dump_json(), ex=self._ttl
        )

    async def _worker_loop(self, index: int) -> None:
        while True:
            try:
                # BRPOP 的 timeout 不能为 0：0 表示永久阻塞，
                # 那样 worker 在关闭时就无法被取消。给 1 秒的轮询间隔，
                # 既能及时退出，空轮询的开销也远小于一次网络往返。
                item = await self._redis.brpop(  # type: ignore[attr-defined]
                    self.PENDING_KEY, timeout=self._poll_timeout
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Redis 抖动不该让 worker 退出 —— 退出了整个队列就停摆了。
                # 退避后继续轮询。
                logger.warning("worker %d 拉取任务失败，2 秒后重试：%s", index, exc)
                await asyncio.sleep(2)
                continue

            if item is None:  # 超时，继续轮询
                continue

            raw_id = item[1]
            task_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id

            record = await self.get(task_id)
            if record is None or record.is_terminal:
                continue

            future = asyncio.create_task(self._execute(record))
            self._running[task_id] = future
            try:
                await future
            except asyncio.CancelledError:
                if record.status is TaskStatus.CANCELLED:
                    continue
                raise
            finally:
                self._running.pop(task_id, None)

    def stats(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "workers": self._worker_count,
            "known_types": self.known_types(),
            "delivery_semantics": "at-most-once（worker 崩溃会丢任务，见模块文档）",
        }
