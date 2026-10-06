"""只允许结构化元数据进入运行记录，禁止复制任意事件内容。

SQLite 是显式选择的单机后端；开始和结束各写一次。运行中事件在内存更新，
崩溃后的 running 记录标为 interrupted，不能声称拥有完整 Usage。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.agent.events import AgentEvent, EventType
from app.core.config import RunHistorySettings
from app.llm.types import Usage

if TYPE_CHECKING:
    from app.agent.runtime import RunContext

RunReason = Literal[
    "running",
    "finished",
    "error",
    "cancelled",
    "interrupted",
    "timeout",
    "token_budget",
    "max_steps",
    "loop_detected",
]
REASONS = {
    "finished",
    "error",
    "cancelled",
    "interrupted",
    "timeout",
    "token_budget",
    "max_steps",
    "loop_detected",
}


class RunEvent(BaseModel):
    kind: str
    elapsed_ms: int
    step: int = 0
    scope: Literal["main", "child"] = "main"
    tool_name: str | None = None
    ok: bool | None = None
    duration_ms: int | None = None
    truncated: bool | None = None
    counts: dict[str, int] | None = None


class RunRecord(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str | None = None
    mode: Literal["react", "plan", "multi"]
    source: Literal["agent", "demo_replay"] = "agent"
    started_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    duration_ms: int = 0
    stopped_reason: RunReason = "running"
    usage: Usage = Field(default_factory=Usage)
    usage_complete: bool = False
    steps_used: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    tool_failures: int = 0
    context_trimmed: bool = False
    context_tokens: int = 0
    events_dropped: int = 0
    events: list[RunEvent] = Field(default_factory=list)


class RunRecorder:
    def __init__(self, record: RunRecord, settings: RunHistorySettings, tools: list[str]) -> None:
        self.record = record
        self.limit = settings.max_events
        self.tools = frozenset(tools)
        self.started = time.monotonic()
        self.runtime_observed = False
        self.context: RunContext | None = None

    def observe_runtime(self, event: AgentEvent, root: bool, context: RunContext) -> None:
        self.context = context
        self.runtime_observed = True
        self.observe(event, root=root)

    def observe(self, event: AgentEvent, *, root: bool = True) -> None:
        r = self.record
        # TOKEN / FINAL / ERROR 的文本，参数、结果、专家名与任务描述都不进入记录。
        if event.type not in {
            EventType.START,
            EventType.STEP,
            EventType.TOOL_CALL,
            EventType.TOOL_RESULT,
            EventType.PLAN,
            EventType.PLAN_STEP,
            EventType.REPLAN,
            EventType.DELEGATE,
            EventType.DELEGATE_RESULT,
            EventType.DONE,
            EventType.ERROR,
        }:
            return
        r.context_trimmed |= event.context_trimmed
        r.context_tokens = max(r.context_tokens, event.context_tokens)
        if root:
            r.steps_used = max(r.steps_used, event.step, event.steps_used)
        if event.type is EventType.TOOL_CALL:
            r.tool_calls += 1
        if event.type is EventType.TOOL_RESULT:
            r.tool_results += 1
            r.tool_failures += int(event.tool_ok is False)
        summary = RunEvent(
            kind=event.type.value,
            elapsed_ms=self.elapsed(),
            step=event.step,
            scope="main" if root else "child",
        )
        if event.type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}:
            # 工具名也是模型输出：只保留注册表白名单，防止恶意名称携带原文。
            summary.tool_name = event.tool_name if event.tool_name in self.tools else "unknown_tool"
            summary.ok = event.tool_ok
            summary.duration_ms = event.duration_ms
            summary.truncated = event.truncated
        if event.plan and isinstance(event.plan.get("steps"), list):
            counts = Counter(
                str(s.get("status")) for s in event.plan.get("steps", []) if isinstance(s, dict)
            )
            summary.counts = {
                s: counts[s] for s in ("pending", "running", "done", "failed", "skipped")
            }
        if len(r.events) < self.limit:
            r.events.append(summary)
        else:
            r.events_dropped += 1
        if root and event.type is EventType.DONE:
            r.stopped_reason = event.stopped_reason if event.stopped_reason in REASONS else "error"
            r.usage = event.usage.model_copy() if event.usage else Usage()
            r.usage_complete = event.usage is not None and event.usage_complete

    def elapsed(self) -> int:
        return max(0, int((time.monotonic() - self.started) * 1000))

    def finish(self, reason: str | None = None) -> RunRecord:
        r = self.record
        if r.finished_at is not None:
            return r
        if reason is not None:
            r.stopped_reason = reason if reason in REASONS else "error"
        if r.stopped_reason == "running":
            r.stopped_reason = "error"
        if self.context is not None:
            r.usage = self.context.usage.model_copy()
            r.usage_complete = self.context.usage_complete
            r.context_trimmed |= self.context.context_trimmed
            r.context_tokens = max(r.context_tokens, self.context.context_tokens)
        if r.stopped_reason in {"cancelled", "interrupted", "error"}:
            # 错误/断开可能丢失在途调用 Usage，保留已知数但不宣称完整。
            r.usage_complete = False
        r.finished_at = datetime.now(UTC).isoformat()
        r.duration_ms = self.elapsed()
        return r


class RunHistory:
    """单进程共享。查询返回快照；SQLite I/O 在线程中串行执行。"""

    def __init__(self, settings: RunHistorySettings) -> None:
        self.settings = settings.model_copy()
        self.backend = settings.backend
        self.records: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def ensure_ready(self) -> None:
        if self.backend != "sql":
            return
        async with self._lock:
            try:
                records = await asyncio.to_thread(self._initialize)
            except (OSError, sqlite3.Error, ValueError) as exc:
                raise RuntimeError(
                    "运行摘要存储启动失败。请检查 RUN_HISTORY_PATH 的文件格式与写权限，"
                    "或设 RUN_HISTORY_BACKEND=memory 后重启服务。"
                ) from exc
            self.records = {r.run_id: r for r in records}

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.settings.path, timeout=10)) as db, db:
            yield db

    def _initialize(self) -> list[RunRecord]:
        Path(self.settings.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, summary TEXT NOT NULL)"
            )
            rows = db.execute(
                "SELECT summary FROM runs ORDER BY started_at DESC LIMIT ?",
                (self.settings.max_records,),
            ).fetchall()
            records = [RunRecord.model_validate_json(row[0]) for row in rows]
            for r in records:
                if r.stopped_reason == "running":
                    r.stopped_reason = "interrupted"
                    r.usage_complete = False
                    r.finished_at = datetime.now(UTC).isoformat()
                    r.duration_ms = 0  # 停机时长不伪装成执行时长。
                    db.execute(
                        "UPDATE runs SET summary=? WHERE run_id=?", (r.model_dump_json(), r.run_id)
                    )
            self._prune_db(db)
        return records

    def _prune_db(self, db: sqlite3.Connection) -> None:
        db.execute(
            "DELETE FROM runs WHERE run_id NOT IN (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT ?)",
            (self.settings.max_records,),
        )

    def _write(self, record: RunRecord) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
                (record.run_id, record.started_at, record.model_dump_json()),
            )
            self._prune_db(db)

    async def save(self, record: RunRecord) -> None:
        async with self._lock:
            if self.backend == "sql":
                await asyncio.to_thread(self._write, record.model_copy(deep=True))
            self.records[record.run_id] = record
            keep = sorted(self.records.values(), key=lambda r: r.started_at, reverse=True)[
                : self.settings.max_records
            ]
            self.records = {r.run_id: r for r in keep}

    def get(self, run_id: str) -> RunRecord | None:
        r = self.records.get(run_id)
        return r.model_copy(deep=True) if r else None

    def list(self, *, session_id: str | None = None, reason: str | None = None) -> list[RunRecord]:
        rows = (
            r
            for r in self.records.values()
            if (session_id is None or r.session_id == session_id)
            and (reason is None or r.stopped_reason == reason)
        )
        return [
            r.model_copy(deep=True) for r in sorted(rows, key=lambda r: r.started_at, reverse=True)
        ]

    async def delete(self, run_id: str) -> bool:
        async with self._lock:
            r = self.records.get(run_id)
            if r is None:
                return False
            if r.stopped_reason == "running":
                raise ValueError("运行中的记录不能删除，请先停止任务")
            if self.backend == "sql":
                await asyncio.to_thread(self._delete_db, run_id)
            del self.records[run_id]
            return True

    def _delete_db(self, run_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
