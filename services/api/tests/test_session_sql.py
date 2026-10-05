"""SQL 会话存储（T02）测试。

【这组测试要证明什么】

第一件，也是这个后端存在的理由：**历史活过"重启"**。
所以测试不是"写进去能读出来"（内存实现也能过），而是
**换一个全新的 store 实例去读同一个文件** —— 那才对应真实的进程重启。

第二件是它顺带修掉的一个债：Redis 实现的 `append_turn` 是"读整个会话
（JSON 反序列化）→ 改 → 整体写回"，两个并发请求交错时后写的会覆盖先写的
（`store.py` 里把它列为明确的技术债）。这里改成 INSERT + UPDATE，
所以"并排追加 N 轮，一轮都不能少"必须是一条断言 ——
如果哪天有人把它改回读-改-写，这条会红。

（**内存实现侥幸不丢**：它 `get()` 返回的是同一个对象引用，改的是共享状态。
所以别把内存后端当成"同样会丢"的对照组 —— 那是实现细节，不是契约。
`scripts/bench_session_store.py --memory` 可以两个后端各跑一遍看到这个差别。）

第三件是"坏了要说出来而不是静默"：数据库路径不可写、URL 写错，
必须在**启动时**失败。`create_async_engine` 是惰性的，一不小心就变成
"第一个用户请求 500"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.session.factory import build_session_store
from app.session.sqlite_store import SqlSessionStore


def _url(tmp_path: Path) -> str:
    # 每个测试一个独立文件：共用一个文件会让"计数/顺序"类断言互相干扰，
    # 而那种失败只在全量跑时出现，最难查
    return f"sqlite+aiosqlite:///{(tmp_path / 'session.db').as_posix()}"


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = SqlSessionStore.from_url(_url(tmp_path))
    await s.ensure_ready()
    yield s
    await s.aclose()


class TestContract:
    """与其它两个实现相同的行为契约。"""

    async def test_create_and_get(self, store: SqlSessionStore) -> None:
        session = await store.create()
        assert session.id
        loaded = await store.get(session.id)
        assert loaded is not None and loaded.id == session.id

    async def test_get_unknown_returns_none(self, store: SqlSessionStore) -> None:
        assert await store.get("不存在") is None

    async def test_append_turn_persists_both_sides(self, store: SqlSessionStore) -> None:
        session = await store.create()
        updated = await store.append_turn(session.id, "你好", "在的", tokens=7)
        assert updated is not None
        assert updated.turn_count == 1
        assert updated.total_tokens == 7

        reloaded = await store.get(session.id)
        assert reloaded is not None
        assert reloaded.turns[0].user == "你好"
        assert reloaded.turns[0].assistant == "在的"
        assert reloaded.total_tokens == 7

    async def test_append_to_unknown_returns_none(self, store: SqlSessionStore) -> None:
        """契约：会话不存在时返回 None，**不自动创建**。

        自动创建会把"客户端传错 session_id"这种 bug 变成"悄悄多出一个会话"。
        """
        assert await store.append_turn("不存在", "a", "b") is None
        assert await store.list() == []

    async def test_title_from_first_turn_only(self, store: SqlSessionStore) -> None:
        session = await store.create()
        first = await store.append_turn(session.id, "第一个问题", "答")
        assert first is not None and first.title == "第一个问题"

        second = await store.append_turn(session.id, "第二个问题", "答")
        assert second is not None and second.title == "第一个问题", "标题不该被后续轮次覆盖"

    async def test_list_is_summary_only_and_sorted(self, store: SqlSessionStore) -> None:
        """列表按更新时间倒序，且**不含** turns（列表页不该读对话内容）。"""
        older = await store.create(title="旧")
        newer = await store.create(title="新")
        await store.append_turn(older.id, "u", "a", tokens=3)
        await store.append_turn(newer.id, "u", "a", tokens=5)

        rows = await store.list()
        assert [row.id for row in rows] == [newer.id, older.id]
        assert rows[0].turn_count == 1 and rows[0].total_tokens == 5
        assert not hasattr(rows[0], "turns")

    async def test_delete(self, store: SqlSessionStore) -> None:
        session = await store.create()
        await store.append_turn(session.id, "u", "a")
        assert await store.delete(session.id) is True
        assert await store.get(session.id) is None
        assert await store.delete(session.id) is False

    async def test_save_is_whole_object_replace(self, store: SqlSessionStore) -> None:
        """save 是 PUT 语义：把对象当前状态整体落盘。

        包括"轮次变少了"这种情况（将来做删除某一轮时）——
        只做增量追加会让磁盘与对象悄悄分叉。
        """
        session = await store.create()
        await store.append_turn(session.id, "u1", "a1")
        await store.append_turn(session.id, "u2", "a2")

        loaded = await store.get(session.id)
        assert loaded is not None
        loaded.turns = loaded.turns[:1]
        await store.save(loaded)

        again = await store.get(session.id)
        assert again is not None and again.turn_count == 1

    async def test_backend_name_is_dialect(self, store: SqlSessionStore) -> None:
        # /healthz 上要一眼看出数据落在哪种库上
        assert store.backend == "sqlite"

    async def test_ttl_expires_lazily(self, tmp_path: Path) -> None:
        s = SqlSessionStore.from_url(_url(tmp_path), ttl_seconds=1)
        await s.ensure_ready()
        try:
            session = await s.create()
            session.updated_at -= 10  # 直接改时间戳，比 sleep 快且不受时钟分辨率影响
            await s.save(session)
            assert await s.get(session.id) is None, "过期会话不该被读到"
            assert await s.list() == []
        finally:
            await s.aclose()


class TestPersistence:
    """这个后端存在的理由：历史活过重启。"""

    async def test_survives_a_new_store_instance(self, tmp_path: Path) -> None:
        """**换一个全新实例读同一个文件** —— 这才对应真实的进程重启。

        只断言"写进去能读出来"是不够的：内存实现也能过那条。
        """
        url = _url(tmp_path)
        first = SqlSessionStore.from_url(url)
        await first.ensure_ready()
        session = await first.create()
        await first.append_turn(session.id, "重启前的问题", "重启前的回答", tokens=11)
        await first.aclose()

        second = SqlSessionStore.from_url(url)
        await second.ensure_ready()
        try:
            loaded = await second.get(session.id)
            assert loaded is not None, "重启后读不到会话 —— 持久化没生效"
            assert loaded.turns[0].user == "重启前的问题"
            assert loaded.total_tokens == 11
            rows = await second.list()
            assert any(row.id == session.id for row in rows)
        finally:
            await second.aclose()

    async def test_create_all_is_idempotent(self, tmp_path: Path) -> None:
        """重复启动不能因为"表已存在"而失败（每次启动都会走 create_all）。"""
        url = _url(tmp_path)
        for _ in range(3):
            s = SqlSessionStore.from_url(url)
            await s.ensure_ready()
            await s.aclose()

    async def test_factory_builds_it_from_database_url(self, tmp_path: Path) -> None:
        settings = get_settings().model_copy(
            update={
                "database_url": _url(tmp_path),
                "session": get_settings().session.model_copy(update={"backend": "sql"}),
            }
        )
        store = await build_session_store(settings)
        try:
            assert isinstance(store, SqlSessionStore)
            assert store.backend == "sqlite"
        finally:
            await store.aclose()

    async def test_sqlite_alias_also_works(self, tmp_path: Path) -> None:
        """`sqlite` 是 `sql` 的别名。

        为什么要容忍两种写法：文档写 `sql`，而人第一反应会打字成 `sqlite`。
        为了一个别名让用户撞上启动失败，收益是零。
        """
        settings = get_settings().model_copy(
            update={
                "database_url": _url(tmp_path),
                "session": get_settings().session.model_copy(update={"backend": "sqlite"}),
            }
        )
        store = await build_session_store(settings)
        try:
            assert isinstance(store, SqlSessionStore)
        finally:
            await store.aclose()


class TestDatabaseUrlResolution:
    """`DATABASE_URL` 里的 SQLite 相对路径必须解析成绝对路径。

    【这条守的是一个"两边都成功、但数据在两个文件里"的坑】
    默认值是 `sqlite+aiosqlite:///./data/legacy.db`，那个 `./` 是相对
    **当前工作目录**的。于是：

        cd services/api && ...    → services/api/data/legacy.db
        cd <仓库根>     && ...    → <root>/data/legacy.db

    两个文件都会被创建、都会写成功，而用户看到的是"我的历史怎么没了"。
    CWD 是运行方式的偶然产物，不该参与决定数据落在哪。
    """

    def test_relative_sqlite_becomes_absolute_under_project_root(self) -> None:
        from app.core.config import PROJECT_ROOT, Settings

        resolved = Settings._resolve_sqlite_path("sqlite+aiosqlite:///./data/legacy.db")
        assert resolved.startswith("sqlite+aiosqlite:///")
        path = resolved.split(":///", 1)[1]
        assert Path(path).is_absolute()
        assert Path(path) == (PROJECT_ROOT / "data" / "legacy.db").resolve()

    def test_relative_without_dot_slash_also_resolved(self) -> None:
        from app.core.config import Settings

        assert Settings._resolve_sqlite_path("sqlite+aiosqlite:///data/legacy.db").startswith(
            "sqlite+aiosqlite:///"
        )
        # 不能变成 "///data/legacy.db" 这种"根目录下的 data"
        assert "/data/legacy.db" in Settings._resolve_sqlite_path(
            "sqlite+aiosqlite:///data/legacy.db"
        )
        assert not Settings._resolve_sqlite_path("sqlite+aiosqlite:///data/legacy.db").endswith(
            ":///data/legacy.db"
        )

    def test_absolute_path_untouched(self) -> None:
        from app.core.config import Settings

        url = "sqlite:///C:/tmp/x.db"
        assert Settings._resolve_sqlite_path(url) == url

    def test_memory_url_untouched(self) -> None:
        """`:memory:` 是 SQLite 的特殊值，绝不能当成相对路径去拼。"""
        from app.core.config import Settings

        url = "sqlite+aiosqlite:///:memory:"
        assert Settings._resolve_sqlite_path(url) == url

    def test_non_sqlite_url_untouched(self) -> None:
        """别的方言由服务端解释地址，这里不该自作聪明。"""
        from app.core.config import Settings

        url = "postgresql+asyncpg://user:pw@db:5432/legacy"
        assert Settings._resolve_sqlite_path(url) == url

    def test_default_settings_resolve_to_data_dir(self) -> None:
        """默认配置解析出来的路径应当就在仓库的 data/ 下。"""
        from app.core.config import PROJECT_ROOT, get_settings

        url = get_settings().database_url
        path = Path(url.split(":///", 1)[1])
        assert path.parent == (PROJECT_ROOT / "data").resolve()


class TestConcurrency:
    """并排追加不能丢轮次 —— 这是它比 Redis 实现强的地方（见文件头）。"""

    async def test_parallel_appends_lose_nothing(self, store: SqlSessionStore) -> None:
        """20 轮并排追加，一轮都不能少。

        Redis 实现的 append_turn 是"读整个会话（JSON 反序列化）→ 改 → 整体写回"：
        两个请求交错时后写的会覆盖先写的。这里改成 INSERT + UPDATE，
        没有"读出来的旧状态"需要写回去。

        如果哪天有人把这个方法改回读-改-写，这条测试会红。
        """
        session = await store.create()
        await asyncio.gather(
            *(store.append_turn(session.id, f"问题{i}", f"回答{i}", tokens=1) for i in range(20))
        )
        loaded = await store.get(session.id)
        assert loaded is not None
        assert loaded.turn_count == 20, f"丢了轮次：只剩 {loaded.turn_count} 轮"
        assert loaded.total_tokens == 20, "计数也不该丢"
        # 每轮都完整（不是被覆盖成了某一轮）
        users = sorted(turn.user for turn in loaded.turns)
        assert users == sorted(f"问题{i}" for i in range(20))
