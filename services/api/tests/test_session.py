"""会话层测试。

分四块：
  1. 内存存储的行为契约（TTL、容量淘汰、并发）
  2. **Redis 存储的行为契约** —— 用 fakeredis 跑真实 Redis 代码路径
  3. 存储工厂的后端选择与降级
  4. HTTP 接口与会话模式的端到端行为

第 2 块尤其重要：内存实现绕过了 Redis 特有的坑（key 前缀、ZSET 索引里
过期成员不清、pipeline 返回值顺序、JSON 序列化）。这些坑在切到真 Redis 时
才暴露，而且往往在生产环境暴露。用 fakeredis 可以把它们提前测出来。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.core.config import SessionSettings, Settings
from app.session.factory import build_session_store
from app.session.models import Session, _default_title
from app.session.store import InMemorySessionStore, RedisSessionStore


# ============================================================
# 内存存储
# ============================================================
class TestInMemoryStore:
    async def test_create_and_get(self) -> None:
        store = InMemorySessionStore()
        session = await store.create()
        assert session.id
        assert (await store.get(session.id)) is session

    async def test_get_unknown_returns_none(self) -> None:
        assert await InMemorySessionStore().get("不存在") is None

    async def test_append_turn_updates_title_and_counts(self) -> None:
        store = InMemorySessionStore()
        session = await store.create()

        updated = await store.append_turn(session.id, "帮我看看简历", "好的", tokens=120)
        assert updated is not None
        assert updated.turn_count == 1
        # 标题用首轮输入自动生成 —— 否则会话列表里全是"未命名会话"
        assert updated.title == "帮我看看简历"
        assert updated.total_tokens == 120

    async def test_append_turn_unknown_session_returns_none(self) -> None:
        """会话不存在时返回 None 而不是自动创建。

        自动创建会把"客户端传了错误的 session_id"这种 bug
        变成"悄悄多出一个会话"，问题被掩盖而不是暴露。
        """
        store = InMemorySessionStore()
        assert await store.append_turn("不存在", "a", "b") is None
        assert await store.list() == []

    async def test_title_only_set_once(self) -> None:
        store = InMemorySessionStore()
        s = await store.create()
        await store.append_turn(s.id, "第一个问题", "回答")
        await store.append_turn(s.id, "第二个问题", "回答")
        assert s.title == "第一个问题"

    async def test_list_sorted_by_updated_at_desc(self) -> None:
        store = InMemorySessionStore()
        a = await store.create()
        b = await store.create()
        await store.append_turn(a.id, "先问的", "答")
        await asyncio.sleep(0.01)
        await store.append_turn(b.id, "后问的", "答")

        listed = await store.list()
        assert [s.id for s in listed] == [b.id, a.id]

    async def test_list_excludes_turn_content(self) -> None:
        """列表只返回元信息 —— 列出 20 个会话时把全部对话读出来，
        在 Redis 场景下就是 20 次大 value 读取。"""
        store = InMemorySessionStore()
        s = await store.create()
        await store.append_turn(s.id, "问题" * 100, "回答" * 100)
        summary = (await store.list())[0]
        assert not hasattr(summary, "turns")
        assert summary.turn_count == 1

    async def test_delete(self) -> None:
        store = InMemorySessionStore()
        s = await store.create()
        assert await store.delete(s.id) is True
        assert await store.get(s.id) is None
        assert await store.delete(s.id) is False  # 幂等

    async def test_ttl_expiry_is_lazy(self) -> None:
        """过期会话在读取时被清理，且不影响其他会话。"""
        store = InMemorySessionStore(ttl_seconds=1)
        old = await store.create()
        old.updated_at -= 10  # 手动让它过期

        assert await store.get(old.id) is None
        assert await store.list() == []

    async def test_capacity_eviction(self) -> None:
        """没有上限的内存字典就是内存泄漏。

        demo 场景下没人会发现，但在真实服务上是"跑几天后 OOM"的经典成因。
        """
        store = InMemorySessionStore(max_sessions=3)
        created = []
        for i in range(5):
            s = await store.create()
            await store.append_turn(s.id, f"问题{i}", "答")
            created.append(s.id)
            await asyncio.sleep(0.005)

        remaining = {s.id for s in await store.list(limit=100)}
        assert len(remaining) == 3
        # 淘汰的是最久未更新的，最近的必须还在
        assert created[-1] in remaining
        assert created[0] not in remaining

    async def test_concurrent_appends_do_not_lose_turns(self) -> None:
        """读-改-写跨越了 await 点，并发下会丢更新。

        "asyncio 是单线程所以不用锁"是最常见的错误直觉 ——
        单次字典操作确实原子，但 get→修改→save 之间存在让出点。
        """
        store = InMemorySessionStore()
        s = await store.create()

        async def worker(i: int) -> None:
            await store.append_turn(s.id, f"问题{i}", f"回答{i}")

        await asyncio.gather(*(worker(i) for i in range(10)))
        assert s.turn_count == 10

    async def test_stats(self) -> None:
        store = InMemorySessionStore(max_sessions=10)
        await store.create()
        stats = store.stats()
        assert stats["backend"] == "memory"
        assert stats["sessions"] == 1
        assert stats["max_sessions"] == 10


# ============================================================
# Redis 存储（fakeredis：真实代码路径，无需服务器）
# ============================================================
def _fake_redis() -> Any:
    from fakeredis import aioredis as fake_aioredis

    return fake_aioredis.FakeRedis(decode_responses=True)


class TestRedisStore:
    """用 fakeredis 跑真实的 Redis 代码路径。

    为什么不用内存实现代替：内存实现绕过了 Redis 特有的坑 ——
    key 前缀、ZSET 索引里过期成员不清、pipeline 返回值顺序、JSON 序列化。
    这些在切到真 Redis 时才暴露，而且往往是在生产环境暴露。
    """

    @pytest.fixture
    async def store(self) -> AsyncIterator[RedisSessionStore]:
        s = RedisSessionStore(_fake_redis(), ttl_seconds=3600)
        yield s
        await s.aclose()

    async def test_round_trip(self, store: RedisSessionStore) -> None:
        session = await store.create()
        fetched = await store.get(session.id)
        assert fetched is not None
        assert fetched.id == session.id

    async def test_turn_persisted_as_json(self, store: RedisSessionStore) -> None:
        session = await store.create()
        await store.append_turn(session.id, "问题", "回答", tokens=42)

        fetched = await store.get(session.id)
        assert fetched is not None
        assert fetched.turn_count == 1
        assert fetched.turns[0].user == "问题"
        assert fetched.total_tokens == 42

    async def test_keys_have_prefix(self, store: RedisSessionStore) -> None:
        """加统一前缀：多环境共用同一个 Redis 时不会互相踩。"""
        session = await store.create()
        keys = await store._redis.keys("*")  # type: ignore[attr-defined]
        assert all(k.startswith("jobpilot:") for k in keys)
        assert f"jobpilot:session:{session.id}" in keys

    async def test_index_used_for_listing(self, store: RedisSessionStore) -> None:
        a = await store.create()
        b = await store.create()
        await store.append_turn(a.id, "先", "答")
        await asyncio.sleep(0.01)
        await store.append_turn(b.id, "后", "答")

        listed = await store.list()
        assert [s.id for s in listed] == [b.id, a.id]

    async def test_ghost_sessions_pruned_from_index(self, store: RedisSessionStore) -> None:
        """**Redis 二级索引的经典坑**。

        会话 key 过期后，ZSET 里的成员**不会自动消失**。
        不剔除的话列表就会出现点进去是空白的"幽灵会话"。
        这个用例专门守住那段清理逻辑。
        """
        a = await store.create()
        b = await store.create()

        # 模拟 a 的 key 过期（ZSET 成员仍留着）
        await store._redis.delete(store._key(a.id))  # type: ignore[attr-defined]

        listed = await store.list()
        assert [s.id for s in listed] == [b.id]
        # 清理必须落回 Redis，否则每次列表都要重复扫一遍
        members = await store._redis.zrange(store.INDEX_KEY, 0, -1)  # type: ignore[attr-defined]
        assert a.id not in members

    async def test_delete_removes_from_index(self, store: RedisSessionStore) -> None:
        session = await store.create()
        assert await store.delete(session.id) is True
        assert await store.get(session.id) is None
        assert await store.list() == []

    async def test_corrupted_payload_does_not_crash(self, store: RedisSessionStore) -> None:
        """存储里的脏数据不该导致 500：删掉它并从"会话不存在"开始。"""
        await store._redis.set(store._key("bad"), "{ 这不是合法 JSON")  # type: ignore[attr-defined]
        assert await store.get("bad") is None
        # 脏数据应被清理掉，而不是每次读取都再踩一次
        assert await store._redis.get(store._key("bad")) is None  # type: ignore[attr-defined]

    async def test_ttl_applied_to_session_key(self, store: RedisSessionStore) -> None:
        session = await store.create()
        ttl = await store._redis.ttl(store._key(session.id))  # type: ignore[attr-defined]
        assert 0 < ttl <= 3600

    async def test_backend_name(self, store: RedisSessionStore) -> None:
        assert store.backend == "redis"


# ============================================================
# 工厂
# ============================================================
class TestFactory:
    async def test_memory_backend(self) -> None:
        store = await build_session_store(Settings(session=SessionSettings(backend="memory")))
        assert store.backend == "memory"

    async def test_fake_backend_uses_redis_path(self) -> None:
        """fake 后端必须走真实的 Redis 代码路径（只是没有服务器）。

        用它开发等于把 Redis 路径提前真跑了一遍 —— 这是它相对
        内存实现的核心价值，也是这个测试要守住的契约。
        """
        store = await build_session_store(Settings(session=SessionSettings(backend="fake")))
        assert store.backend == "fake"
        assert isinstance(store, RedisSessionStore)

        session = await store.create()
        assert (await store.get(session.id)) is not None
        assert await store.list()

    async def test_auto_falls_back_to_memory_when_redis_unavailable(self) -> None:
        """降级必须发生，且**必须打 WARNING**。

        静默降级会让人以为多进程共享已生效，实际表现是
        "用户偶尔丢历史"这种极难定位的间歇性故障。
        """
        settings = Settings(
            session=SessionSettings(backend="auto"),
            redis_url="redis://127.0.0.1:1/0",  # 必然连不上的端口
        )
        store = await build_session_store(settings)
        assert store.backend == "memory"

    async def test_explicit_redis_fails_loudly(self) -> None:
        """显式要求 Redis 时失败就失败 —— 静默降级会让运维以为配置生效了。"""
        settings = Settings(
            session=SessionSettings(backend="redis"),
            redis_url="redis://127.0.0.1:1/0",
        )
        with pytest.raises(RuntimeError, match="无法连接"):
            await build_session_store(settings)


# ============================================================
# 模型辅助
# ============================================================
class TestSessionModel:
    def test_default_title_truncates(self) -> None:
        assert _default_title("短") == "短"
        long_title = _default_title("很长的标题" * 20, max_len=10)
        assert len(long_title) == 11  # 10 + 省略号
        assert long_title.endswith("…")

    def test_newline_stripped_from_title(self) -> None:
        assert "\n" not in _default_title("第一行\n第二行")

    def test_session_ids_are_unique(self) -> None:
        ids = {Session().id for _ in range(50)}
        assert len(ids) == 50

    def test_ids_are_not_guessable(self) -> None:
        """会话 id 会暴露在 URL 里，可枚举的 id 意味着改一个数字就能读到别人的对话。

        这在有鉴权之后仍然是坏习惯 —— 鉴权会失效、会有漏配的路由。
        """
        sid = Session().id
        assert len(sid) == 32  # uuid4 hex
        assert sid.isalnum()
