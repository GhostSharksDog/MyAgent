"""会话存储：内存实现与 Redis 实现。

【为什么要有这一层抽象】
"会话存哪里"是个会随规模变化而变化的问题：
  - 本地开发 / 单进程 demo：内存字典最省事，零依赖
  - 多进程 / 多副本部署：内存不行 —— 请求落到另一个实例就读不到会话

抽象出 `SessionStore` 之后，切换只需要改一个配置项，业务代码一行不动。
这就是"依赖倒置"在工程上的实际价值，而不是教科书上的名词。

【Redis 实现必须处理的四件事】
1. **TTL**：会话不能无限增长。给每个 key 设过期时间，让 Redis 自动回收。
2. **索引维护**：列表页需要按更新时间排序。用有序集合（ZSET）做索引，
   但**过期 key 不会自动从 ZSET 里消失** —— 必须在读取时剔除空成员，
   否则列表里会出现点进去是空白的"幽灵会话"。这是 Redis 做二级索引的经典坑。
3. **并发写**：读-改-写（读会话 → 追加一轮 → 写回）在并发下会丢更新。
   当前实现是朴素版本，对单用户场景足够；真正的多客户端并发需要
   WATCH/MULTI 乐观锁或 Lua 脚本原子化。**这是明确的技术债，不是疏忽。**
4. **序列化**：存 JSON 字符串而不是 Redis Hash。会话是一整个聚合根，
   整体读写比逐字段更新更简单，也避免了 hash 字段名与模型字段漂移。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

from app.agent.operations import merge_facts
from app.session.models import Session, SessionSummary, new_session_id

logger = logging.getLogger(__name__)

# 会话默认保留 7 天。太短会让用户几天后回来发现历史没了；
# 太长则内存/存储无限增长。这个值应该按产品定位调，不是技术常数。
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


class SessionStore(ABC):
    """会话存储接口。所有方法都是异步的 —— Redis 实现要走网络。"""

    @abstractmethod
    async def create(self, *, title: str = "") -> Session: ...

    @abstractmethod
    async def get(self, session_id: str) -> Session | None: ...

    @abstractmethod
    async def save(self, session: Session) -> None: ...

    @abstractmethod
    async def list(self, *, limit: int = 20) -> list[SessionSummary]: ...

    @abstractmethod
    async def delete(self, session_id: str) -> bool: ...

    async def append_turn(
        self,
        session_id: str,
        user: str,
        assistant: str,
        *,
        tokens: int = 0,
        tool_summary: str = "",
    ) -> Session | None:
        """追加一轮对话。会话不存在时返回 None（而不是自动创建）。

        刻意不自动创建：那会把"客户端传了错误的 session_id"这种 bug
        变成"悄悄多出一个会话"，问题被掩盖而不是暴露。

        `tool_summary` 是"本轮调用过哪些工具"的一行摘要（技术债 T07）：
        它随轮次一起持久化，下一轮组装历史时出现在助手那条消息的末尾，
        让模型知道自己查过什么，不必重复调用。
        """
        session = await self.get(session_id)
        if session is None:
            return None
        session.append_turn(user, assistant, tokens=tokens, tool_summary=tool_summary)
        await self.save(session)
        return session

    async def merge_execution_facts(self, session_id: str, facts: list[dict]) -> bool:
        raise NotImplementedError("会话存储未实现执行事实的原子合并")

    async def merge_summary(self, session_id: str, state: dict) -> bool:
        raise NotImplementedError("会话存储未实现摘要的原子合并")

    @property
    @abstractmethod
    def backend(self) -> str: ...

    @abstractmethod
    async def aclose(self) -> None: ...


# ============================================================
# 内存实现
# ============================================================
class InMemorySessionStore(SessionStore):
    """进程内会话存储。

    【并发安全说明】
    asyncio 是单线程事件循环，字典的单次操作不会被抢占，但
    **读-改-写跨越了 await 点**（`append_turn` 里 get 与 save 之间），
    两个请求交错时就会丢更新。所以要加显式锁 ——
    "单线程所以不用锁"是 asyncio 里最常见的错误直觉。

    【为什么要有 max_sessions】
    没有上限的内存字典就是一个内存泄漏。demo 场景下不会有人发现，
    但在真实服务上这是"跑几天后 OOM"的经典成因。
    """

    def __init__(self, *, max_sessions: int = 500, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds

    @property
    def backend(self) -> str:
        return "memory"

    async def append_turn(self, session_id, user, assistant, *, tokens=0, tool_summary=""):
        async with self._lock:
            session = await self.get(session_id)
            if session is not None:
                session.append_turn(user, assistant, tokens=tokens, tool_summary=tool_summary)
            return session

    async def merge_execution_facts(self, session_id: str, facts: list[dict]) -> bool:
        async with self._lock:
            session = await self.get(session_id)
            if session is None:
                return False
            session.meta = merge_facts(session.meta, facts)
            session.updated_at = time.time()
            return True

    async def merge_summary(self, session_id: str, state: dict) -> bool:
        async with self._lock:
            session = await self.get(session_id)
            if session is None:
                return False
            session.meta = {**session.meta, "conversation_summary": state.copy()}
            return True

    async def create(self, *, title: str = "") -> Session:
        session = Session(title=title)
        async with self._lock:
            self._evict_if_needed()
            self._sessions[session.id] = session
        logger.info("创建会话 %s（当前 %d 个）", session.id, len(self._sessions))
        return session

    async def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if self._is_expired(session):
            # 惰性过期：读取时发现过期才删。省掉了后台清理任务，
            # 代价是"永远不会被读到"的会话会一直占着内存 ——
            # 所以还需要容量淘汰兜底（见 _evict_if_needed）。
            self._sessions.pop(session_id, None)
            logger.info("会话 %s 已过期，惰性清理", session_id)
            return None
        return session

    async def save(self, session: Session) -> None:
        """持久化。

        **刻意不在这里改 `updated_at`。**
        初版在 save() 里做了 `session.updated_at = time.time()`，理由是
        "保存意味着刚刚活跃过"。但它带来两个问题：

        1. **双重所有权**：`Session.append_turn()` 也会设置 `updated_at`。
           两处都写，谁生效取决于调用顺序 —— 而这类隐含依赖极难排查。
        2. **不可测**：调用方（以及测试）无法显式指定时间戳，
           因为一 save 就被覆盖。想测"按更新时间倒序"就只能靠 sleep
           制造时间差，而那会撞上 `time.time()` 的分辨率（见下方说明）。

        现在的分工是清晰的：**领域模型负责时间戳，存储只负责持久化**。
        需要更新时间的调用方应该走 `append_turn()` 或显式设置字段。
        """
        async with self._lock:
            self._sessions[session.id] = session

    async def list(self, *, limit: int = 20) -> list[SessionSummary]:
        # 顺带清理过期项：列表是个天然的清理时机，因为用户看得见结果
        for sid in [s.id for s in self._sessions.values() if self._is_expired(s)]:
            self._sessions.pop(sid, None)

        ordered = sorted(self._sessions.values(), key=lambda s: s.updated_at, reverse=True)
        return [_summary(s) for s in ordered[:limit]]

    async def delete(self, session_id: str) -> bool:
        async with self._lock:
            return self._sessions.pop(session_id, None) is not None

    async def aclose(self) -> None:
        self._sessions.clear()

    # ---------- 内部 ----------

    def _is_expired(self, session: Session) -> bool:
        return self.ttl_seconds > 0 and (time.time() - session.updated_at) > self.ttl_seconds

    def _evict_if_needed(self) -> None:
        """超出容量时淘汰最久未更新的会话。调用方必须已持锁。"""
        if len(self._sessions) < self.max_sessions:
            return
        overflow = len(self._sessions) - self.max_sessions + 1
        oldest = sorted(self._sessions.values(), key=lambda s: s.updated_at)[:overflow]
        for s in oldest:
            self._sessions.pop(s.id, None)
        logger.warning("会话数达上限 %d，淘汰最久未更新的 %d 个", self.max_sessions, len(oldest))

    def stats(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "sessions": len(self._sessions),
            "max_sessions": self.max_sessions,
            "ttl_seconds": self.ttl_seconds,
        }


# ============================================================
# Redis 实现
# ============================================================
class RedisSessionStore(SessionStore):
    """Redis 会话存储。

    key 设计：
        legacy:session:{id}      会话 JSON 字符串（带 TTL）
        legacy:sessions:index    有序集合，score = updated_at

    加统一前缀是为了多环境共用同一个 Redis 时不互相踩。
    """

    PREFIX = "legacy:session:"
    INDEX_KEY = "legacy:sessions:index"

    def __init__(self, client: object, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._redis = client
        self.ttl_seconds = ttl_seconds

    @property
    def backend(self) -> str:
        return "redis"

    def _key(self, session_id: str) -> str:
        return f"{self.PREFIX}{session_id}"

    async def _update(self, session_id: str, mutate: Callable[[Session], None]) -> Session | None:
        from redis.exceptions import WatchError

        key = self._key(session_id)
        for _ in range(100):
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    if raw is None:
                        return None
                    session = Session.model_validate_json(raw)
                    mutate(session)
                    pipe.multi()
                    pipe.set(key, session.model_dump_json(), ex=self.ttl_seconds or None)
                    pipe.zadd(self.INDEX_KEY, {session_id: session.updated_at})
                    pipe.expire(
                        self.INDEX_KEY, self.ttl_seconds * 2
                    ) if self.ttl_seconds else pipe.persist(self.INDEX_KEY)
                    await pipe.execute()
                    return session
                except WatchError:
                    continue
        raise RuntimeError("会话并发更新过多，请稍后重试")

    async def append_turn(self, session_id, user, assistant, *, tokens=0, tool_summary=""):
        return await self._update(
            session_id,
            lambda s: s.append_turn(user, assistant, tokens=tokens, tool_summary=tool_summary),
        )

    async def merge_execution_facts(self, session_id: str, facts: list[dict]) -> bool:
        def mutate(session):
            session.meta = merge_facts(session.meta, facts)
            session.updated_at = time.time()

        return await self._update(session_id, mutate) is not None

    async def merge_summary(self, session_id: str, state: dict) -> bool:
        def mutate(session):
            session.meta = {**session.meta, "conversation_summary": state.copy()}

        return await self._update(session_id, mutate) is not None

    async def create(self, *, title: str = "") -> Session:
        session = Session(title=title)
        await self.save(session)
        logger.info("创建会话 %s（Redis）", session.id)
        return session

    async def get(self, session_id: str) -> Session | None:
        raw = await self._redis.get(self._key(session_id))  # type: ignore[attr-defined]
        if raw is None:
            return None
        try:
            return Session.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            # 存储里的脏数据不该导致 500：删掉它并从"会话不存在"开始
            logger.warning("会话 %s 数据损坏，已删除：%s", session_id, exc)
            await self.delete(session_id)
            return None

    async def save(self, session: Session) -> None:
        """持久化（不改动 `updated_at`，理由见内存实现的同名方法）。

        写入两个结构：会话本体（带 TTL）与列表索引（ZSET）。
        用 pipeline 合并为一次往返 —— 分成两次写入会留下
        "本体已写、索引未更新"的中间状态窗口。
        """
        payload = session.model_dump_json()
        pipe = self._redis.pipeline()  # type: ignore[attr-defined]
        pipe.set(self._key(session.id), payload, ex=self.ttl_seconds or None)
        pipe.zadd(self.INDEX_KEY, {session.id: session.updated_at})
        # 索引也设 TTL 兜底：即使某个会话 key 先过期，
        # 索引整体也不会永久驻留
        pipe.expire(self.INDEX_KEY, self.ttl_seconds * 2) if self.ttl_seconds else pipe.persist(
            self.INDEX_KEY
        )
        await pipe.execute()

    async def list(self, *, limit: int = 20) -> list[SessionSummary]:
        ids = await self._redis.zrevrange(self.INDEX_KEY, 0, limit - 1)  # type: ignore[attr-defined]

        out: list[SessionSummary] = []
        stale: list[str] = []
        for raw_id in ids:
            sid = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
            session = await self.get(sid)
            if session is None:
                # 【Redis 做二级索引的经典坑】会话 key 过期后，
                # ZSET 里的成员**不会自动消失**。不在这里剔除，
                # 列表就会出现点进去是空白的"幽灵会话"。
                stale.append(sid)
                continue
            out.append(_summary(session))

        if stale:
            await self._redis.zrem(self.INDEX_KEY, *stale)  # type: ignore[attr-defined]
            logger.info("清理了 %d 个索引中的幽灵会话", len(stale))

        return out

    async def delete(self, session_id: str) -> bool:
        pipe = self._redis.pipeline()  # type: ignore[attr-defined]
        pipe.delete(self._key(session_id))
        pipe.zrem(self.INDEX_KEY, session_id)
        results = await pipe.execute()
        return bool(results[0])

    async def aclose(self) -> None:
        # 客户端生命周期由外部管理（连接池可能被多处共享），这里不关闭
        return None

    def stats(self) -> dict[str, object]:
        return {"backend": self.backend, "ttl_seconds": self.ttl_seconds}


def _summary(session: Session) -> SessionSummary:
    return SessionSummary(
        id=session.id,
        title=session.title or "（未命名会话）",
        created_at=session.created_at,
        updated_at=session.updated_at,
        turn_count=session.turn_count,
        total_tokens=session.total_tokens,
    )


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "InMemorySessionStore",
    "RedisSessionStore",
    "SessionStore",
    "new_session_id",
]
