"""任务队列装配。

与另外两个 factory 一样的原则：服务、CLI、测试共用同一套装配逻辑。

【一个容易忽略的点：处理器必须在 start() 之前注册】
worker 一启动就会去拉队列。如果此时处理器还没注册，
拉到的任务会因为"未知类型"直接失败 —— 而且失败得理直气壮，
日志里写的是"未知任务类型"，排查时很难想到是启动顺序问题。
所以 `build_task_queue` 保证注册先于启动。
"""

from __future__ import annotations

import logging

from app.core.config import Settings, get_settings
from app.tasks.handlers import register_default_handlers
from app.tasks.queue import InProcessTaskQueue, TaskQueue

logger = logging.getLogger(__name__)


async def _build_real_redis(settings: Settings) -> TaskQueue:
    from redis.asyncio import from_url

    from app.tasks.redis_queue import RedisTaskQueue

    client = from_url(settings.redis_url, decode_responses=True)
    await client.ping()  # from_url 是惰性的，不 ping 的话失败要等到第一次请求
    return RedisTaskQueue(
        client,
        worker_count=settings.tasks.worker_count,
        ttl_seconds=settings.tasks.ttl_seconds,
    )


def _build_memory(settings: Settings) -> TaskQueue:
    return InProcessTaskQueue(
        worker_count=settings.tasks.worker_count,
        max_tasks=settings.tasks.max_tasks,
    )


def _build_fake(settings: Settings) -> TaskQueue:
    from fakeredis import aioredis as fake_aioredis

    from app.tasks.redis_queue import RedisTaskQueue

    return RedisTaskQueue(
        fake_aioredis.FakeRedis(decode_responses=True),
        worker_count=settings.tasks.worker_count,
        ttl_seconds=settings.tasks.ttl_seconds,
    )


async def build_task_queue(
    settings: Settings | None = None, *, autostart: bool = True
) -> TaskQueue:
    """按配置构造任务队列，注册处理器，并在需要时启动 worker。

    Args:
        autostart: 测试里可以关掉，避免后台 worker 让测试变得不确定
            （"异步副作用晚于断言"是测试里最难查的一类问题）。
    """
    s = settings or get_settings()
    cfg = s.tasks
    backend = cfg.backend.lower()

    if backend == "memory":
        queue = _build_memory(s)
    elif backend == "fake":
        queue = _build_fake(s)
    elif backend == "redis":
        try:
            queue = await _build_real_redis(s)
        except Exception as exc:
            # 显式要求 Redis 时失败就失败 —— 静默降级会让运维以为配置生效了
            raise RuntimeError(
                f"TASK_BACKEND=redis 但无法连接 {s.redis_url}：{exc}。"
                f"请启动 Redis，或改用 TASK_BACKEND=memory/fake。"
            ) from exc
    else:  # auto
        try:
            queue = await _build_real_redis(s)
        except Exception as exc:
            logger.warning(
                "连接 Redis 失败（%s），任务队列降级为**进程内**。"
                "多副本部署下任务不会跨实例共享。原因：%s",
                s.redis_url,
                exc,
            )
            queue = _build_memory(s)

    # 注册必须先于启动：worker 一启动就会拉队列，
    # 那时没有处理器的话任务会以"未知类型"失败
    types = register_default_handlers(queue)
    logger.info("任务队列就绪：backend=%s，已知类型=%s", queue.backend, ", ".join(types))

    if autostart:
        await queue.start()

    return queue
