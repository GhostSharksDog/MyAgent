"""任务队列：抽象接口 + 进程内实现。

【设计要点：队列本身不能阻塞事件循环】

把 CPU 密集的活"拆到后台"是目的，但如果 worker 直接在事件循环里跑那段
CPU 密集代码，事件循环照样被阻塞 —— 拆了等于没拆。

所以处理器必须自己把计算丢进线程池（`asyncio.to_thread`），
worker 只负责调度。这一点在 `handlers.py` 里落实。

【为什么待处理队列与任务记录分开存】

    pending 队列  —— 只需要"下一个该跑谁"，是个 FIFO
    task 记录     —— 需要状态、进度、结果、错误，是可查询的实体

混在一起（比如把整个记录塞进队列）会导致：想查任务状态就得遍历队列，
而队列里的元素跑完就没了。分开之后查询走记录、调度走队列，各司其职。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.tasks.models import (
    TaskRecord,
    TaskStatus,
    TaskSummary,
    new_task_id,
)

logger = logging.getLogger(__name__)

# 处理器签名：接收上下文，返回结果字典
TaskHandler = Callable[["TaskContext"], Awaitable[dict | None]]


@dataclass
class TaskContext:
    """传给处理器的上下文。

    载荷直接挂在上下文上，而不是让处理器回头去队列里查 ——
    后者在 Redis 实现下会多一次网络往返，而且要求处理器知道
    "该从哪个接口取参数"这种不该由它关心的细节。
    """

    task_id: str
    queue: TaskQueue
    task_type: str = ""
    payload: dict | None = None

    async def report(self, progress: int, message: str = "") -> None:
        """上报进度（0~100）。

        进度不是装饰：一个跑 20 秒的任务如果没有任何反馈，
        用户会以为它卡死了并刷新页面 —— 而刷新并不能让任务变快。
        """
        await self.queue.update_progress(self.task_id, progress, message)

    def arg(self, key: str, default: object = None) -> object:
        """读一个参数，避免到处写 `(ctx.payload or {}).get(...)`。"""
        return (self.payload or {}).get(key, default)


class TaskQueue(ABC):
    """任务队列接口。三种实现：进程内、Redis、以及测试用的立即执行版。"""

    # ---------- 生命周期 ----------

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...

    @property
    @abstractmethod
    def backend(self) -> str: ...

    # ---------- 接口 ----------

    @abstractmethod
    async def submit(
        self, task_type: str, *, payload: dict | None = None, session_id: str | None = None
    ) -> TaskRecord: ...

    @abstractmethod
    async def get(self, task_id: str) -> TaskRecord | None: ...

    @abstractmethod
    async def list(self, *, limit: int = 20) -> list[TaskSummary]: ...

    @abstractmethod
    async def cancel(self, task_id: str) -> bool: ...

    @abstractmethod
    async def update_progress(self, task_id: str, progress: int, message: str = "") -> None: ...

    # ---------- 处理器注册（各实现共享） ----------

    def register(self, task_type: str, handler: TaskHandler) -> None:
        self._handlers[task_type] = handler

    def known_types(self) -> list[str]:
        return sorted(self._handlers)

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    # ---------- 执行（各实现共享） ----------

    async def _execute(self, record: TaskRecord) -> None:
        """执行一条任务。异常一律转成 failed 状态，绝不向外抛。

        任务失败不能让 worker 死掉 —— 一个 worker 崩了会让整个队列停摆，
        而失败的任务只是失败而已。
        """
        handler = self._handlers.get(record.type)
        if handler is None:
            record.mark_failed(
                f"未知任务类型 {record.type!r}。已注册的类型：{', '.join(self.known_types()) or '（无）'}"
            )
            await self._save(record)
            return

        record.mark_running("开始执行")
        await self._save(record)
        logger.info("任务 %s（%s）开始", record.id, record.type)

        try:
            ctx = TaskContext(
                task_id=record.id,
                queue=self,
                task_type=record.type,
                payload=dict(record.payload),
            )
            result = await handler(ctx)
            record.mark_succeeded(result or {})
            logger.info("任务 %s 完成，耗时 %sms", record.id, record.duration_ms)
        except asyncio.CancelledError:
            # 取消是控制流，必须区分于失败：前端对两者的处理完全不同
            record.mark_cancelled()
            logger.info("任务 %s 已取消", record.id)
            raise
        except Exception as exc:
            record.mark_failed(f"{type(exc).__name__}: {exc}")
            logger.exception("任务 %s 失败", record.id)

        await self._save(record)

    @abstractmethod
    async def _save(self, record: TaskRecord) -> None: ...

    @staticmethod
    def _summary(record: TaskRecord) -> TaskSummary:
        return TaskSummary(
            id=record.id,
            type=record.type,
            status=record.status,
            progress=record.progress,
            message=record.message,
            created_at=record.created_at,
            duration_ms=record.duration_ms,
            error=record.error,
        )


# ============================================================
# 进程内实现
# ============================================================
class InProcessTaskQueue(TaskQueue):
    """进程内队列：asyncio.Queue 调度 + 内存记录。

    适用：单进程部署、本地开发、测试。
    限制：进程重启后任务与记录都消失；多副本部署下各自为政。
    """

    def __init__(self, *, worker_count: int = 1, max_tasks: int = 200) -> None:
        super().__init__()
        self._records: dict[str, TaskRecord] = {}
        self._pending: asyncio.Queue[str | None] = asyncio.Queue()
        self._running: dict[str, asyncio.Task[None]] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._worker_count = worker_count
        self._max_tasks = max_tasks
        self._started = False

    @property
    def backend(self) -> str:
        return "memory"

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """启动 worker。**幂等**：重复调用不会产生多批 worker。

        幂等很重要：lifespan 可能因为测试或热重载被多次触发，
        不幂等就会在每次触发时多起一批 worker，
        表现为"任务被重复执行"这种极难理解的故障。
        """
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker_loop(i), name=f"task-worker-{i}")
            for i in range(self._worker_count)
        ]
        logger.info("任务队列已启动：%d 个 worker（进程内）", self._worker_count)

    async def aclose(self) -> None:
        if not self._started:
            return
        self._started = False

        # 优雅关闭：先投哨兵让 worker 在**跑完当前任务后**退出。
        # 直接 cancel 会在任意 await 点中断，可能让正在写记录的任务留下半成品。
        # 但不能无限等 —— 一个卡住的任务会拖住整个进程退出，
        # 所以给一个上限，超时后再强制取消。
        for _ in range(self._worker_count):
            await self._pending.put(None)

        _done, pending = await asyncio.wait(self._workers, timeout=5)
        if pending:
            logger.warning("有 %d 个 worker 未能在 5 秒内退出，强制取消", len(pending))
            for task in pending:
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        self._workers.clear()
        logger.info("任务队列已关闭")

    # ---------- 提交与查询 ----------

    async def submit(
        self, task_type: str, *, payload: dict | None = None, session_id: str | None = None
    ) -> TaskRecord:
        record = TaskRecord(
            id=new_task_id(), type=task_type, session_id=session_id, payload=payload or {}
        )
        self._records[record.id] = record
        self._evict_if_needed()
        await self._pending.put(record.id)
        logger.info("任务入队：%s（%s）", record.id, task_type)
        return record

    async def get(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

    async def list(self, *, limit: int = 20) -> list[TaskSummary]:
        ordered = sorted(self._records.values(), key=lambda r: r.created_at, reverse=True)
        return [self._summary(r) for r in ordered[:limit]]

    async def cancel(self, task_id: str) -> bool:
        record = self._records.get(task_id)
        if record is None or record.is_terminal:
            return False

        if record.status is TaskStatus.PENDING:
            # 还没跑：直接标记取消。worker 取到它时会跳过。
            record.mark_cancelled()
            return True

        # 正在跑：尝试取消对应的 asyncio.Task。
        # 注意这只有在处理器处于 await 点时才生效 —— 同步 CPU 代码
        # 无法被中断（这也是为什么 CPU 密集的活必须丢线程池，
        # 但线程本身也无法被 asyncio 取消，只能靠处理器主动检查）。
        running = self._running.get(task_id)
        if running is not None:
            running.cancel()
            return True
        return False

    async def update_progress(self, task_id: str, progress: int, message: str = "") -> None:
        record = self._records.get(task_id)
        if record is None:
            return
        record.progress = max(0, min(100, progress))
        if message:
            record.message = message

    def payload_of(self, task_id: str) -> dict:
        """取任务载荷（调试与测试用）。

        处理器应该用 `ctx.payload` —— 载荷在创建上下文时就已带上，
        不需要回查队列。
        """
        record = self._records.get(task_id)
        return dict(record.payload) if record else {}

    # ---------- 内部 ----------

    async def _save(self, record: TaskRecord) -> None:
        self._records[record.id] = record

    async def _worker_loop(self, index: int) -> None:
        while True:
            task_id = await self._pending.get()
            if task_id is None:  # 关闭哨兵
                return

            record = self._records.get(task_id)
            if record is None or record.is_terminal:
                continue  # 已被取消或清理

            future = asyncio.create_task(self._execute(record))
            self._running[task_id] = future
            try:
                await future
            except asyncio.CancelledError:
                if record.status is TaskStatus.CANCELLED:
                    continue  # 正常取消，worker 继续跑下一个
                raise  # worker 自身被取消（关闭流程），向上传播
            finally:
                self._running.pop(task_id, None)

    def _evict_if_needed(self) -> None:
        """清理最老的终态记录，避免内存无限增长。

        只淘汰**终态**记录：pending/running 的任务被淘汰会让前端
        永远查不到结果，也不能被取消。
        """
        if len(self._records) <= self._max_tasks:
            return
        terminal = [r for r in self._records.values() if r.is_terminal]
        overflow = len(self._records) - self._max_tasks
        for record in sorted(terminal, key=lambda r: r.created_at)[:overflow]:
            self._records.pop(record.id, None)

    def stats(self) -> dict[str, object]:
        by_status: dict[str, int] = {}
        for record in self._records.values():
            by_status[str(record.status)] = by_status.get(str(record.status), 0) + 1
        return {
            "backend": self.backend,
            "tasks": len(self._records),
            "workers": self._worker_count,
            "queued": self._pending.qsize(),
            "by_status": by_status,
            "known_types": self.known_types(),
        }


# ============================================================
# 测试用：立即执行
# ============================================================
class ImmediateTaskQueue(InProcessTaskQueue):
    """提交即同步执行完，不留后台任务。

    测试里最怕"异步副作用晚于断言"，用它可以把任务处理变成确定性的一步。
    它继承进程内实现，所以走的仍是同一套执行逻辑 ——
    测试覆盖的执行路径与生产一致。缺点是**测不到并发与调度**，
    所以专门的调度测试仍应使用 InProcessTaskQueue。
    """

    @property
    def backend(self) -> str:
        return "immediate"

    async def submit(
        self, task_type: str, *, payload: dict | None = None, session_id: str | None = None
    ) -> TaskRecord:
        record = await super().submit(task_type, payload=payload, session_id=session_id)
        # 从待处理队列里取回来自己执行，保持与生产一致的取消语义
        await self._pending.get()
        if record.is_terminal:  # 提交后立刻被取消
            return record
        await self._execute(record)
        return record


__all__ = [
    "ImmediateTaskQueue",
    "InProcessTaskQueue",
    "TaskContext",
    "TaskHandler",
    "TaskQueue",
]
