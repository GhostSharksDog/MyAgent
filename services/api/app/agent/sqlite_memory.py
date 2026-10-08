"""单用户长期记忆：每次变更独立提交，模型切换不拥有存储生命周期。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from app.agent.memory import Fact, LongTermMemory


class SqliteLongTermMemory(LongTermMemory):
    def __init__(self, path: Path, max_facts: int = 200) -> None:
        super().__init__(path, max_facts)
        self._lock = threading.RLock()
        self.load()

    def _connect(self) -> sqlite3.Connection:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS memory_facts "
                "(id TEXT PRIMARY KEY, text TEXT NOT NULL, tags TEXT NOT NULL, ts REAL NOT NULL)"
            )
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _read(connection: sqlite3.Connection) -> list[Fact]:
        return [
            Fact(id=row[0], text=row[1], tags=json.loads(row[2]), ts=row[3])
            for row in connection.execute("SELECT id,text,tags,ts FROM memory_facts ORDER BY ts,id")
        ]

    def _error(self, exc: Exception) -> RuntimeError:
        return RuntimeError(
            f"长期记忆数据库无法读写：{self.path}。请检查目录权限和剩余空间，"
            "若数据库损坏，请先备份后恢复；不会退回内存存储。"
        )

    def load(self) -> int:
        with self._lock:
            try:
                with self._transaction() as connection:
                    self._facts = self._read(connection)
            except (sqlite3.Error, OSError, ValueError) as exc:
                raise self._error(exc) from exc
            self._store = None
            return len(self._facts)

    def save(self) -> None:
        # 写入已在事务中完成；退出时禁止用旧缓存覆盖数据库。
        return None

    def _change(self, mutate: Callable[[list[Fact]], bool]) -> bool:
        with self._lock:
            try:
                with self._transaction() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        facts = self._read(connection)
                    except ValueError as exc:
                        raise self._error(exc) from exc
                    changed = mutate(facts)
                    if not changed:
                        return False
                    facts = facts[-self.max_facts :]
                    connection.execute("DELETE FROM memory_facts")
                    connection.executemany(
                        "INSERT INTO memory_facts(id,text,tags,ts) VALUES (?,?,?,?)",
                        [
                            (f.id, f.text, json.dumps(f.tags, ensure_ascii=False), f.ts)
                            for f in facts
                        ],
                    )
                # 只有事务成功后才发布新缓存。
                self._facts = facts
                self._store = None
                return True
            except (sqlite3.Error, OSError) as exc:
                raise self._error(exc) from exc

    def remember(self, text: str, tags: list[str] | None = None) -> bool:
        if not self.enabled:
            raise ValueError("长期记忆已关闭，请在记忆与存储设置中开启后再添加。")
        text = text.strip()

        def mutate(facts: list[Fact]) -> bool:
            if not text or any(f.text.casefold() == text.casefold() for f in facts):
                return False
            facts.append(Fact(text=text, tags=tags or []))
            return True

        return self._change(mutate)

    def update_fact(self, fact_id: str, text: str, tags: list[str]) -> bool:
        def mutate(facts: list[Fact]) -> bool:
            if any(f.id != fact_id and f.text.casefold() == text.strip().casefold() for f in facts):
                raise ValueError("相同内容已存在，请编辑已有记忆。")
            for fact in facts:
                if fact.id == fact_id:
                    fact.text, fact.tags = text.strip(), tags
                    return True
            return False

        return self._change(mutate)

    def delete_fact(self, fact_id: str) -> bool:
        def mutate(facts: list[Fact]) -> bool:
            before = len(facts)
            facts[:] = [f for f in facts if f.id != fact_id]
            return before != len(facts)

        return self._change(mutate)

    def clear_facts(self) -> bool:
        def mutate(facts: list[Fact]) -> bool:
            changed = bool(facts)
            facts.clear()
            return changed

        return self._change(mutate)

    def recall(self, query: str, k: int = 3, *, min_score: float = 0.0) -> list[Fact]:
        with self._lock:
            return super().recall(query, k, min_score=min_score)

    @property
    def facts(self) -> list[Fact]:
        with self._lock:
            return [f.model_copy(deep=True) for f in self._facts]
