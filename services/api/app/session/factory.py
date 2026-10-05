"""会话存储装配。

放在独立模块的理由与前两个 factory 一致：**服务、CLI、测试必须用同一套
装配逻辑**，否则会出现"服务上会话能共享、测试里不行"这类只在单条路径
复现的问题。

`auto` 模式的降级行为是这里最需要小心的地方：静默降级会让人误以为
多进程共享已生效，实际表现是"用户偶尔丢历史"这种间歇性故障。
所以降级一律打 WARNING，并且把最终选用的后端暴露在 /healthz 里。

【五个后端，为什么 `auto` 不去猜 SQL】

    memory    单进程 demo / 测试，零依赖，**重启丢历史**
    fake      本地开发走真实 Redis 代码路径（不需要 Docker）
    sql       SQLite（`DATABASE_URL`）：**不需要额外服务，且历史活过重启**
    redis     多副本部署
    auto      默认：能连上 Redis 就用，否则内存

`auto` 明明可以再加一句"否则用 SQL"，那样本地体验还更好（历史不丢）。
不这么做的理由是这个变量的名字：**`auto` 的语义是"探测环境"，不是"挑一个我喜欢的"**。
让它悄悄开始写文件（`data/legacy.db`）会让"我只是启动一下"变成"多了个数据库文件"，
而用户根本没要求持久化。需要持久化是一个**明确的意图**，就该明确写 `sql`。
（降级到内存时会打一行 INFO 提示这个选项，见文件末尾。）
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


def _build_sql(settings: Settings) -> SessionStore:
    """按 `DATABASE_URL` 建 SQL 会话存储（默认 SQLite）。

    【为什么建引擎不会失败，但启动仍然可能因此报错】
    `create_async_engine` 是惰性的：它不连接数据库。所以"路径写错/驱动没装"
    这类问题不会在这里暴露 —— 第一次查询时才会。而那时错误出现在
    某个用户请求里，看起来像业务 bug。
    对策不是在这里强行连一次，而是**让它在启动时就暴露**：
    `main.py` 的 lifespan 会调用 `ensure_ready()`（见那里的说明）。
    """
    from app.session.sqlite_store import SqlSessionStore

    return SqlSessionStore.from_url(settings.database_url, ttl_seconds=settings.session.ttl_seconds)


async def _build_real_redis(settings: Settings) -> SessionStore:
    from redis.asyncio import from_url

    client = from_url(
        settings.redis_url,
        decode_responses=True,
        # 只限定**建连**阶段；命令本身的超时不受影响（socket_timeout 默认不限）
        socket_connect_timeout=settings.redis_connect_timeout,
    )
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

    if backend in ("sql", "sqlite"):
        # 这两个值都指向"用 DATABASE_URL 建 SQL 存储"。
        # 为什么两个都收：README 与 .env 里写的是 `sql`（与后端实现同名），
        # 而人第一反应会打字成 `sqlite`。为一个别名让用户撞上"未知后端"
        # 的启动失败，收益是零。
        store = _build_sql(s)
        logger.info(
            "会话存储：%s（%s）—— 历史会活过重启，且不需要额外服务",
            store.backend,
            s.database_url,
        )
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
        # 【这条日志的措辞是刻意的：它描述的是"决定"，不是"故障"】
        # 本地开发没有 Redis 是**正常状态**，而原来的写法读起来像出了错
        # （"连接 Redis 失败…原因：Error 10061"），会让人去排查一个不存在的问题。
        # 但也不能不提：静默降级会让人误以为多进程共享已经生效，
        # 表现为"用户偶尔丢历史"这种极难定位的间歇性故障。
        # 所以：先说清楚"现在用的是什么"，再说"什么情况下这才是问题"。
        logger.warning(
            "未使用 Redis（%s，%.1fs 内没能连上）：会话存储用**内存**实现，"
            "单进程可用。多副本部署必须启动 Redis，否则表现为「用户偶尔丢历史」。"
            "（本地开发这是正常状态；建连超时可用 REDIS_CONNECT_TIMEOUT 调整）",
            s.redis_url,
            s.redis_connect_timeout,
        )
        logger.debug("探测 Redis 的具体原因：%s", exc)
        # 内存后端有个很具体、又很难自己想到的后果：**重启就丢历史**。
        # 与其让用户自己发现，不如在这里说一句怎么办 —— 这正是 SQL 后端存在的理由。
        logger.info(
            "提示：想让会话历史活过重启又不想装 Redis，设 SESSION_BACKEND=sql（用 %s）",
            s.database_url,
        )
        return InMemorySessionStore(max_sessions=cfg.max_sessions, ttl_seconds=cfg.ttl_seconds)
