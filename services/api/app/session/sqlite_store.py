"""SQLite / SQL 会话存储：不需要 Redis，也能让历史活过重启。

《为什么需要第三个实现 —— 现有两个各缺一半》

| 实现 | 跨进程共享 | 活过重启 | 需要额外服务 |
|---|---|---|---|
| 内存 | ❌ | ❌ | 不需要 |
| Redis | ✅ | ✅ | **需要** |

于是本地开发落在一个很尴尬的位置：`dev.ps1 serve` 跑一会儿，重启一次
聊过的内容就没了；而要让它活下来就得先装 Redis。这个实现补的正是那一格：
**单机、零额外服务、历史持久**。

《为什么用 SQLAlchemy 而不是直接写 sqlite3》

因为 `DATABASE_URL` 本来就是一个 SQLAlchemy URL（`sqlite+aiosqlite:///...`）。
用 SQLAlchemy Core 的话，将来换 Postgres 只是改这一个字符串 ——
而"换存储不用改代码"正是这一层抽象存在的理由（见 store.py 开头的说明）。
直接写 sqlite3 会把 SQLite 焊死在实现里。

《表结构：为什么是两张表，而不是一列 JSON》

Redis 实现把整个会话当一个聚合根存 JSON 字符串，那在 KV 里是对的。
但到了关系库里，一张表 + 一列 JSON 就只是"把 KV 塞进 SQL"，什么好处都没拿到。
两张表（`sessions` + `turns`）换来三件具体的事：

1. **列表页不用读对话内容**。`SessionSummary` 只含元信息，
   `SELECT` 不带 join 就够 —— 这正是 Redis 那边要靠"另存一份索引"绕开的问题。
2. **追加一轮不再是读-改-写**。Redis 实现的 `append_turn` 是
   "读整个会话（JSON 反序列化）→ 改 → 整体写回"，两个并发请求交错时后写的会
   覆盖先写的 —— `store.py` 把它列为明确的技术债。这里退化成
   `INSERT` 一轮 + `UPDATE` 计数，**没有可丢的中间状态**。
   （内存实现侥幸不丢：它 `get()` 返回的是**同一个对象引用**，改的是共享状态。
   那是实现细节而非契约 —— 一旦它改成深拷贝就会开始丢。）
3. **单轮可查**。想做"哪类问题最费 token"这类分析时，不必把整个会话读进内存。

《幂等与并发》

`create` 用 uuid4 主键，冲突概率可忽略；`save` 是整行替换（PUT 语义）。
SQLite 的写是串行的（默认 journal 模式），所以单文件下不会出现两个写者
互相覆盖；真正的多进程写也不是它的目标场景 —— 那该上 Redis 或 Postgres。

《迁移》

**没有迁移工具**。`create_all` 只会在表不存在时建表，**改字段不会生效**。
这是刻意的取舍：现在只有两张表、且这是本地持久化场景，
引入 Alembic 的复杂度大于收益。一旦 schema 要变，要么加迁移工具，
要么明确写成"删掉 data/*.db 重建"（数据是会话历史，可接受）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import (
    JSON,
    Float,
    Integer,
    String,
    Text,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.agent.memory import Turn
from app.session.models import Session, SessionSummary, _default_title
from app.session.store import DEFAULT_TTL_SECONDS, SessionStore

logger = logging.getLogger(__name__)

_METADATA_DEFINED = False


def _define_tables() -> tuple[Any, Any]:
    """惰性建表定义。

    放在函数里是为了避免模块级副作用：`sqlalchemy.MetaData()` 与两张表的
    定义是全局单例式的对象，模块导入时构造一遍没有意义，
    而如果哪天要支持多套 schema（比如测试用不同的表前缀），
    模块级的单例反而会互相污染。
    """
    from sqlalchemy import Column, MetaData, Table

    metadata = MetaData()
    sessions = Table(
        "sessions",
        metadata,
        Column("id", String(64), primary_key=True),
        Column("title", String(200), nullable=False, default=""),
        Column("created_at", Float, nullable=False),
        Column("updated_at", Float, nullable=False),
        Column("total_tokens", Integer, nullable=False, default=0),
        # meta 是自由字典（放 agent 形态、来源等），用 JSON 列而不是拆表：
        # 它的键随功能变化，拆成列意味着每加一个元信息就要改 schema。
        Column("meta", JSON, nullable=False, default=dict),
    )
    turns = Table(
        "turns",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        # ON DELETE CASCADE 交给 SQLAlchemy 的 relationship 太绕，
        # 这里在 delete() 里显式删两处 —— 少一层隐式行为，读代码时更省心。
        Column("session_id", String(64), nullable=False, index=True),
        Column("seq", Integer, nullable=False),
        Column("user", Text, nullable=False),
        Column("assistant", Text, nullable=False),
        # 本轮的工具调用摘要（技术债 T07）。默认空串而不是 NULL：
        # "没调用过工具"与"这列没有值"是两件事，空串让读取端不必处理 None。
        Column("tool_summary", Text, nullable=False, server_default=""),
    )
    return sessions, turns


SESSIONS, TURNS = _define_tables()


def _missing_columns(sync_conn: Any) -> list[str]:
    """用 `PRAGMA table_info` 核对 `sessions` / `turns` 两表的列是否齐全。

    只对 SQLite 有意义（别的方言有各自的信息模式），所以调用方要保证
    传进来的是 SQLite 连接 —— 现在唯一的调用点在 `_ensure_ready`，
    而它只在 SQLite 上才会走到这里（见 `_apply_sqlite_pragmas` 的方言判断）。

    返回形如 `["turns.tool_summary"]` 的清单；空清单表示结构没问题。
    """
    from sqlalchemy import text

    expected = {
        "sessions": {column.name for column in SESSIONS.columns},
        "turns": {column.name for column in TURNS.columns},
    }
    missing: list[str] = []
    for table, columns in expected.items():
        # PRAGMA 的参数不能走绑定变量（它是语法的一部分），所以用 f-string 拼表名；
        # 表名来自代码里的常量而不是用户输入，没有注入面。
        rows = sync_conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        if not rows:
            # 表不存在：create_all 刚建过，说明它连建都没建起来
            missing.append(f"{table}（整张表）")
            continue
        present = {row[1] for row in rows}
        missing.extend(f"{table}.{name}" for name in sorted(columns - present))
    return missing


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """给 SQLite 连接设三条 PRAGMA。

    【为什么需要 —— 以及一次被我猜错的诊断】

    先说结论：这三条是**并发读**的必要条件，但**不是**并发写的解药。
    尾延迟的真正来源是写事务排队（见类里 `_write_lock` 的说明）。

    PRAGMA 本身的作用：
    1. `journal_mode=WAL`：写进独立的 WAL 文件，**读不再被写阻塞**，
       提交也不必每次重写回滚日志。这是 SQLite 支持并发读写的官方答案。
    2. `synchronous=NORMAL`：WAL 下这个档位是安全的 —— 断电最多丢最近几次
       提交，**不会损坏数据库**。对会话历史来说这个取舍划算
       （丢最后一轮对话 vs 每次提交等 fsync）。
    3. `busy_timeout=5000`：撞锁时**等待**而不是立刻抛 "database is locked"。
       它决定了偶发冲突是重试成功还是给用户一个 500。

    注意 `journal_mode` 是**写在库文件里**的（设一次长期有效），
    另外两条是**每个连接**的 —— 所以必须挂在 connect 事件上。
    """
    if engine.dialect.name != "sqlite":
        return

    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


class SqlSessionStore(SessionStore):
    """基于 SQLAlchemy Core 的会话存储（默认落在 SQLite 文件上）。

    《写操作为什么要一把进程内锁 —— 一次实测》

    先剥掉 HTTP 层直接量存储层（本机，60 次操作、并发 16）：

        顺序 create                P50   0.87ms  P95    1.28ms
        顺序 get                   P50   1.22ms  P95    1.59ms
        并发 create（16）           P50   3.16ms  P95  282.17ms   合计 3.11s
        并发 append_turn（16）      P50  71.33ms  P95 1051.44ms   合计 12.37s

    SQLite 单线程下快得不像话（0.9ms 一次写），并发下尾延迟却差两个数量级。
    原因是**每次操作自己开一个事务**，而 SQLite 的写是全局串行的：
    16 个连接各自去抢同一个文件锁，抢不到就等 `busy_timeout`，
    于是"排队长度"直接变成尾延迟 —— 而 P95 恰恰是用户能感觉到的那一档。

    加一把 `asyncio.Lock` 之后，同一时刻只有一个写者，其余在**事件循环里排队**
    （而不是在 SQLite 的文件锁上排队）。这不改变"写是串行的"这个事实，
    只是把不可控的锁等待换成了可控的公平队列。

    《为什么 `list()` 不能每次都做清理》

    清理过期会话是个 DELETE —— 也就是说，带清理的每次列表都是**写事务**。
    "打开会话列表"是最高频的操作之一，让它去抢写锁是本末倒置。
    所以清理按时间摊薄（最多每 `CLEANUP_INTERVAL_SECONDS` 一次），
    而且它本身也在写锁里执行。
    """

    # 清理摊薄间隔（秒）。见类文档：带清理的列表 = 写事务，不能每次都做。
    CLEANUP_INTERVAL_SECONDS = 60.0

    def __init__(self, engine: AsyncEngine, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._engine = engine
        self.ttl_seconds = ttl_seconds
        self._ready = False
        # 写锁：把"抢 SQLite 文件锁"换成"在事件循环里排队"。详见类文档。
        # asyncio.Lock 在 3.10+ 不再于构造时绑定事件循环，所以可以在 __init__ 里建。
        self._write_lock = asyncio.Lock()
        self._last_cleanup = 0.0

    # ---------- 生命周期 ----------
    @classmethod
    def from_url(cls, url: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> SqlSessionStore:
        """按 `DATABASE_URL` 建引擎。

        `create_async_engine` 是惰性的（不在这里连接），所以这个函数不会因为
        数据库暂时不可用而失败 —— 真正的失败会出现在第一次查询上。
        因此下面 `ensure_ready()` 里的 `create_all` 是**第一次真实连接**，
        它也是启动时"数据库配错了"唯一的暴露点。
        """
        engine = create_async_engine(url, future=True)
        _apply_sqlite_pragmas(engine)
        return cls(engine, ttl_seconds=ttl_seconds)

    async def _ensure_ready(self) -> None:
        """建表（只做一次）+ **核对列是否齐全**。

        惰性而不是放在 `__init__` 里：`__init__` 不能是异步的，
        而在构造函数里 `asyncio.run(...)` 会在已有事件循环中直接报错 ——
        这是"同步构造函数里想干异步的事"的经典坑。

        【为什么要核对列 —— "没有迁移工具"这个取舍的代价】
        `create_all` **只建不存在的表**：它对已存在的表什么都不做，
        所以给 `turns` 加一列（比如 T07 的 `tool_summary`）时，
        老库不会有这一列 —— 而错误要到第一次读写才炸出来：

            sqlite3.OperationalError: no such column: tool_summary

        那句话出现在某个用户请求里，看起来像业务 bug。所以这里在启动时
        主动比一次列名，缺了就抛一条**能照做**的信息（删掉库文件重建），
        这正是"要么明确报错，要么别加列"的取舍该有的样子。
        """
        if self._ready:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: SESSIONS.metadata.create_all(sync_conn))
            missing = await conn.run_sync(_missing_columns)
        if missing:
            raise RuntimeError(
                f"{self.backend} 会话库的表结构与当前代码不一致，缺少列：{'、'.join(missing)}。"
                f"本实现**没有迁移工具**（见 app/session/sqlite_store.py 的说明）："
                f"会话历史可以丢弃，所以最简单的修法是删掉数据库文件后重启。"
                f"当前文件：{self._engine.url.database}"
            )
        self._ready = True

    async def ensure_ready(self) -> None:
        """显式建表并**真的连一次**（启动时调用）。

        【为什么启动时要主动调它】
        `create_async_engine` 是惰性的，"驱动没装 / 路径不可写 / URL 写错"
        这些问题都要等到第一次查询才暴露 —— 而那时它们出现在某个用户请求里，
        看起来像业务 bug。启动时调一次，就把它们变成一条清晰的启动失败。
        """
        await self._ensure_ready()

    @property
    def backend(self) -> str:
        # 报**方言名**（sqlite / postgresql）而不是笼统的 "sql"：
        # /healthz 上看到 "sqlite" 与 "postgresql"，运维一眼就知道数据落在哪
        return self._engine.dialect.name

    async def aclose(self) -> None:
        await self._engine.dispose()

    # ---------- 契约实现 ----------
    async def create(self, *, title: str = "") -> Session:
        await self._ensure_ready()
        session = Session(title=title)
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(SESSIONS).values(
                        id=session.id,
                        title=session.title,
                        created_at=session.created_at,
                        updated_at=session.updated_at,
                        total_tokens=session.total_tokens,
                        meta=session.meta,
                    )
                )
        logger.info("创建会话 %s（%s）", session.id, self.backend)
        return session

    async def get(self, session_id: str) -> Session | None:
        await self._ensure_ready()
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(select(SESSIONS).where(SESSIONS.c.id == session_id)))
                .mappings()
                .first()
            )
            if row is None:
                return None
            if self._expired(row["updated_at"]):
                # 惰性过期：读到才发现过期才删（与内存实现同一策略）。
                # 删除要拿写锁，但这是"只有读到过期会话时"才走的分支 ——
                # 正常读路径完全不碰写锁，这正是把清理摊薄的意义。
                logger.info("会话 %s 已过期，惰性清理", session_id)
                await self._delete_rows(session_id)
                return None
            turn_rows = (
                await conn.execute(
                    select(TURNS.c.user, TURNS.c.assistant, TURNS.c.tool_summary)
                    .where(TURNS.c.session_id == session_id)
                    .order_by(TURNS.c.seq)
                )
            ).all()

        return Session(
            id=row["id"],
            title=row["title"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            total_tokens=row["total_tokens"],
            meta=dict(row["meta"] or {}),
            turns=[Turn(user=u, assistant=a, tool_summary=s or "") for u, a, s in turn_rows],
        )

    async def save(self, session: Session) -> None:
        """整行替换（PUT 语义），并把 turns 重写成当前列表。

        【为什么要重写 turns 而不是只增量追加】
        `save()` 的契约是"把内存里这个对象的状态持久化"，而对象可能被
        调用方**改过中间内容**（比如将来做"删除某一轮"或"编辑后重发"）。
        只做增量追加会让磁盘与对象悄悄分叉，而那种不一致最难查。
        轮次数量在会话量级上很小（几十条），重写是可接受的代价。
        """
        await self._ensure_ready()
        async with self._write_lock, self._engine.begin() as conn:
            existing = (
                await conn.execute(select(SESSIONS.c.id).where(SESSIONS.c.id == session.id))
            ).first()
            values = {
                "title": session.title,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "total_tokens": session.total_tokens,
                "meta": session.meta,
            }
            if existing is None:
                await conn.execute(insert(SESSIONS).values(id=session.id, **values))
            else:
                await conn.execute(
                    update(SESSIONS).where(SESSIONS.c.id == session.id).values(**values)
                )
                await conn.execute(delete(TURNS).where(TURNS.c.session_id == session.id))
            if session.turns:
                await conn.execute(
                    insert(TURNS),
                    [
                        {
                            "session_id": session.id,
                            "seq": index,
                            "user": turn.user,
                            "assistant": turn.assistant,
                            "tool_summary": turn.tool_summary,
                        }
                        for index, turn in enumerate(session.turns)
                    ],
                )

    async def merge_execution_facts(self, session_id: str, facts: list[dict]) -> bool:
        from app.agent.operations import merge_facts

        await self._ensure_ready()
        async with self._write_lock, self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(SESSIONS.c.meta, SESSIONS.c.updated_at).where(
                        SESSIONS.c.id == session_id
                    )
                )
            ).first()
            if row is None or self._expired(row[1]):
                return False
            await conn.execute(
                update(SESSIONS)
                .where(SESSIONS.c.id == session_id)
                .values(meta=merge_facts(row[0] or {}, facts), updated_at=time.time())
            )
            return True

    async def merge_summary(self, session_id: str, state: dict) -> bool:
        await self._ensure_ready()
        async with self._write_lock, self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(SESSIONS.c.meta, SESSIONS.c.updated_at).where(
                        SESSIONS.c.id == session_id
                    )
                )
            ).first()
            if row is None or self._expired(row[1]):
                return False
            await conn.execute(
                update(SESSIONS)
                .where(SESSIONS.c.id == session_id)
                .values(meta={**(row[0] or {}), "conversation_summary": state.copy()})
            )
            return True

    async def append_turn(
        self,
        session_id: str,
        user: str,
        assistant: str,
        *,
        tokens: int = 0,
        tool_summary: str = "",
    ) -> Session | None:
        """追加一轮：**INSERT 一条 + UPDATE 计数**，不做读-改-写。

        【为什么值得覆写基类的默认实现】
        基类是 `get() → session.append_turn() → save()` —— Redis 实现里
        那三个 await 点之间两个并发请求会互相覆盖（JSON 反序列化出两份副本，
        后写的整体覆盖先写的）。这里改成两条独立语句：并发追加不会丢任何一轮，
        因为**没有任何"读出来的旧状态"需要写回去**。

        代价是 `updated_at` / `title` 的更新要自己写：
        - `updated_at` 每次追加都要推进（列表按它排序）；
        - `title` 只在为空时用首句填充 —— 用 `COALESCE(NULLIF(...))` 表达
          "仅当为空"，这样并发的两个首轮不会一个覆盖另一个。
        """
        await self._ensure_ready()
        now = time.time()
        async with self._write_lock, self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(SESSIONS.c.title, SESSIONS.c.updated_at).where(
                        SESSIONS.c.id == session_id
                    )
                )
            ).first()
            if row is None or self._expired(row[1]):
                # 契约：会话不存在时返回 None，**不自动创建**（见基类说明）
                return None

            count = (
                await conn.execute(
                    select(func.count()).select_from(TURNS).where(TURNS.c.session_id == session_id)
                )
            ).scalar_one()
            await conn.execute(
                insert(TURNS).values(
                    session_id=session_id,
                    seq=int(count),
                    user=user,
                    assistant=assistant,
                    tool_summary=tool_summary,
                )
            )
            await conn.execute(
                update(SESSIONS)
                .where(SESSIONS.c.id == session_id)
                .values(
                    updated_at=now,
                    total_tokens=SESSIONS.c.total_tokens + tokens,
                    title=func.coalesce(func.nullif(SESSIONS.c.title, ""), _default_title(user)),
                )
            )
        return await self.get(session_id)

    async def list(self, *, limit: int = 20) -> list[SessionSummary]:
        """按更新时间倒序。

        【两处刻意的设计】
        1. **读路径不加写锁**：`SELECT` 用普通连接。SQLite 在 WAL 下读不阻塞写，
           而列表是最高频的操作之一 —— 让它去排写锁的队是本末倒置。
        2. **过期行直接过滤掉**，而不是靠"先删再查"：清理被摊薄到最多每分钟一次
           （见 `_cleanup_if_due`），如果不在这里过滤，过期会话会在列表里
           残留最多一分钟。过滤是免费的（本来就要扫这张表），
           清理只负责回收空间。
        """
        await self._ensure_ready()
        await self._cleanup_if_due()
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(
                            SESSIONS.c.id,
                            SESSIONS.c.title,
                            SESSIONS.c.created_at,
                            SESSIONS.c.updated_at,
                            SESSIONS.c.total_tokens,
                            func.count(TURNS.c.id).label("turn_count"),
                        )
                        .select_from(SESSIONS.outerjoin(TURNS, TURNS.c.session_id == SESSIONS.c.id))
                        .where(SESSIONS.c.updated_at >= self._cutoff())
                        .group_by(SESSIONS.c.id)
                        .order_by(SESSIONS.c.updated_at.desc())
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )

        return [
            SessionSummary(
                id=row["id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                turn_count=int(row["turn_count"]),
                total_tokens=row["total_tokens"],
            )
            for row in rows
        ]

    async def delete(self, session_id: str) -> bool:
        await self._ensure_ready()
        async with self._write_lock, self._engine.begin() as conn:
            result = await conn.execute(delete(SESSIONS).where(SESSIONS.c.id == session_id))
            await conn.execute(delete(TURNS).where(TURNS.c.session_id == session_id))
            return bool(result.rowcount)

    # ---------- 内部 ----------
    def _cutoff(self) -> float:
        return time.time() - self.ttl_seconds if self.ttl_seconds else float("-inf")

    def _expired(self, updated_at: float) -> bool:
        return updated_at < self._cutoff()

    async def _cleanup_if_due(self) -> None:
        """按时间摊薄的清理：最多每 `CLEANUP_INTERVAL_SECONDS` 真删一次。

        【为什么要摊薄】
        清理是个 DELETE，也就是说"带清理的列表"是**写事务**。每次打开会话列表
        都去写一次，等于把最高频的读操作排到写锁后面（实测里那正是尾延迟的来源）。
        过期行由 `list()` 的 WHERE 过滤掉，所以清理只影响磁盘占用，不影响正确性 ——
        那就没必要每次都做。

        用 `time.monotonic()` 而不是 `time.time()`：后者会被 NTP 校时拉动，
        而"距上次清理多少秒"是个间隔度量，不该受挂钟调整影响。
        拿锁后再判一次，是因为等锁期间别人可能已经清过了。
        """
        if time.monotonic() - self._last_cleanup < self.CLEANUP_INTERVAL_SECONDS:
            return
        async with self._write_lock:
            if time.monotonic() - self._last_cleanup < self.CLEANUP_INTERVAL_SECONDS:
                return
            self._last_cleanup = time.monotonic()
            async with self._engine.begin() as conn:
                await conn.execute(delete(SESSIONS).where(SESSIONS.c.updated_at < self._cutoff()))
                # 孤儿轮次也要清：sessions 删了而 turns 留着，会让这个库
                # 随时间无限增长，而那份数据谁也不会再读到
                await conn.execute(
                    delete(TURNS).where(~TURNS.c.session_id.in_(select(SESSIONS.c.id)))
                )

    async def _delete_rows(self, session_id: str) -> None:
        async with self._write_lock, self._engine.begin() as conn:
            await conn.execute(delete(SESSIONS).where(SESSIONS.c.id == session_id))
            await conn.execute(delete(TURNS).where(TURNS.c.session_id == session_id))
