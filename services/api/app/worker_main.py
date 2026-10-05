"""独立的异步任务 worker 进程。

【为什么 worker 必须能独立于 API 进程运行】

API 进程内启动 worker 有两个**具体的**问题：

1. **生命周期绑死，扩缩容只能一起做**。API 一般是多副本的。
   如果每个副本都带一个 worker，那么 3 个副本就有 3 组 worker 在抢
   同一个队列 —— 想多开两个 worker 就得白白多起两个 API 副本，
   而多出来的 API 副本对吞吐毫无帮助。

2. **重启即中断**。部署、改配置、崩溃恢复都会重启 API 进程，
   正在跑的任务（例如耗时 1.9 秒的重建索引、几分钟的批量匹配）会被打断。
   worker 独立之后，可以做到"API 滚动升级时任务不中断"。

【为什么现在写这个进程是有意义的，而不是过早优化】
因为队列**已经是 Redis 后备**的了（见 tasks/redis_queue.py）：
任务存在 Redis 里而不是进程内存里，所以"谁在消费"这件事本来
就是可以分开的。这个入口只是把这件已经成立的事说出来。
如果队列还是纯内存的，拆 worker 就没有意义 ——
**拆分的顺序应该是"先让状态离开进程，再让执行离开进程"。**

启动：
    python -m app.worker_main          # 从 services/api 目录
    uv run python -m app.worker_main
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.telemetry import set_trace_id
from app.tasks.factory import build_task_queue

logger = logging.getLogger(__name__)


async def main() -> int:
    settings = get_settings()
    # 形态跟着配置走：worker 是部署态进程，它的日志同样要被采集器聚合 ——
    # 一个进程写彩文本、另一个写 JSON，采集器只会收到一半的结构化数据。
    setup_logging(settings.log_level, fmt=settings.log_format)

    tasks = await build_task_queue(settings)

    logger.info("任务 worker 启动：backend=%s", tasks.backend)

    # 【为什么必须显式检查 backend，并且在这种场景下报警】
    #
    # 独立 worker 唯一能工作的前提是：队列状态**不在本进程内存里**。
    # 如果配置成了 `memory` 后端，这个进程会对着一个空的内存队列傻等，
    # 而 API 进程把任务塞进它自己的内存队列 —— 两边永远碰不上。
    #
    # 更糟的是它**不报错**：进程健康、日志正常、只是永远不干活。
    # 这是分布式系统里最难排查的一类故障（静默失联），
    # 所以必须在这里主动、大声地说出来，而不是等有人发现"任务一直 pending"。
    if tasks.backend != "redis":
        logger.error(
            "任务 worker 的 backend=%s，不是 redis —— "
            "独立 worker 进程需要一个**跨进程共享**的队列，否则它永远收不到任务。"
            "请设置 TASK_BACKEND=redis 并确保 Redis 可用。",
            tasks.backend,
        )
        await tasks.aclose()
        return 2

    stop = asyncio.Event()

    def _handle(sig: int) -> None:
        logger.info("收到信号 %s，等待当前任务结束后退出", sig)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # 【为什么要处理信号而不是直接 KeyboardInterrupt 退出】
        # 硬退出会在任务执行到一半时切断它：对一个 `reindex` 来说，
        # 结果是索引半新半旧；对 `ingest_resume` 来说，是写了一半的文件。
        # 优雅退出的成本是几行代码，代价是多等一次任务的时间 ——
        # 但换来的是"不会有半成品状态"这个**可以依赖的性质**。
        with contextlib.suppress(NotImplementedError, AttributeError):
            # Windows 上 SIGTERM 的注册支持有限，容错处理
            loop.add_signal_handler(sig, _handle, sig)

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    await tasks.aclose()
    logger.info("任务 worker 已退出")
    return 0


def run() -> int:
    # 用显式 trace id 标记"这是 worker 侧的工作"，
    # 免得 worker 的日志和 API 的日志混在一起分不清。
    set_trace_id("worker")
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(run())
