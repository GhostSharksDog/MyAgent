"""会话存储装配。

放在独立模块的理由与前两个 factory 一致：**服务、CLI、测试必须用同一套
装配逻辑**，否则会出现"服务上会话能共享、测试里不行"这类只在单条路径
复现的问题。

`auto` 模式的降级行为是这里最需要小心的地方：静默降级会让人误以为
多进程共享已生效，实际表现是"用户偶尔丢历史"这种间歇性故障。
所以降级一律打 WARNING，并且把最终选用的后端暴露在 /healthz 里。
"""

from __future__ import annotations

import logging

from app.core.config import Settings, get_settings
from app.session.store import InMemorySessionStore, RedisSessionStore, SessionStore

logger = logging.getLogger(__name__)


class FakeRedisSessionStore(RedisSessionStore):
    """基于 fakeredis 的会话存储：**走真实 Redis 代码路径，但不需要服务器**。

    为什么这比直接用 InMemorySessionStore 做开发更好：
    内存实现绕过了 Redis 特有的坑 —— key 前缀、ZSET 索引里过期成员不清、
    pipeline 的返回值顺序、JSON 序列化。这些坑在切到真 Redis 时才暴露，
    而且往往是在生产环境暴露。

    用它开发，等于把 Redis 路径提前真跑了一遍；唯一的差别是没有跨进程共享。
    """

    @property
    def backend(self) -> str:
        return "fake"


def _build_fake() -> SessionStore:
    try:
        from fakeredis import aioredis as fake_aioredis
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("SESSION_BACKEND=fake 需要安装 fakeredis") from exc

    return FakeRedisSessionStore(fake_aioredis.FakeRedis(decode_responses=True))


async def _build_real_redis(settings: Settings) -> SessionStore:
    from redis.asyncio import from_url

    client = from_url(settings.redis_url, decode_responses=True)
    # 必须显式 ping：`from_url` 是惰性的，不 ping 的话连接失败要等到
    # 第一次请求才暴露，而那时错误会以 500 的形式出现在用户面前
    await client.ping()
    return RedisSessionStore(client)


async def build_session_store(settings: Settings | None = None) -> SessionStore:
    s = settings or get_settings()
    cfg = s.session
    backend = cfg.backend.lower()

    if backend == "memory":
        logger.info("会话存储：内存（不可跨进程共享，仅适用于单进程）")
        return InMemorySessionStore(max_sessions=cfg.max_sessions, ttl_seconds=cfg.ttl_seconds)

    if backend == "fake":
        logger.info("会话存储：fakeredis（Redis 代码路径，无跨进程共享）")
        store = _build_fake()
        store.ttl_seconds = cfg.ttl_seconds
        return store

    if backend == "redis":
        # 显式要求 Redis 时失败就失败 —— 静默降级会让运维以为配置生效了
        try:
            store = await _build_real_redis(s)
        except Exception as exc:
            raise RuntimeError(
                f"SESSION_BACKEND=redis 但无法连接 {s.redis_url}：{exc}。"
                f"请启动 Redis，或改用 SESSION_BACKEND=fake 在本地走 Redis 代码路径。"
            ) from exc
        logger.info("会话存储：Redis（%s）", s.redis_url)
        store.ttl_seconds = cfg.ttl_seconds
        return store

    # auto：优先真 Redis，失败降级到内存
    try:
        store = await _build_real_redis(s)
        logger.info("会话存储：Redis（%s）", s.redis_url)
        store.ttl_seconds = cfg.ttl_seconds
        return store
    except Exception as exc:
        logger.warning(
            "连接 Redis 失败（%s），会话存储降级为**内存**。"
            "多进程/多副本部署下会话将无法共享 —— 若这不是预期行为，"
            "请启动 Redis 或显式设置 SESSION_BACKEND。原因：%s",
            s.redis_url,
            exc,
        )
        return InMemorySessionStore(max_sessions=cfg.max_sessions, ttl_seconds=cfg.ttl_seconds)
