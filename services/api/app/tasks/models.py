"""异步任务模型。

【为什么需要任务队列 —— 一个具体到不能反驳的理由】

知识库重建索引（`reindex`）要做的事：读全部文档 → 切分 → 拟合 TF-IDF →
建 BM25 倒排表。在当前的语料规模下这是**几百毫秒**，看起来无所谓；
但语料涨到几千块时就是**几秒到几十秒**。

而它是一个同步 CPU 密集操作，会**阻塞整个事件循环** ——
期间所有 HTTP 请求（包括别人的对话流式响应）全部停摆。
这正是"不拆会疼在哪"的具体答案：不是架构洁癖，是明确的可用性问题。

拆出去之后：API 立刻返回 `task_id`，前端轮询/订阅进度，
CPU 密集的活在后台线程里跑，事件循环不受影响。

【任务的状态机】

    pending ──→ running ──→ succeeded
                    │
                    └──→ failed
    任意状态 ──→ cancelled

状态只往前走（succeeded/failed/cancelled 是终态），不允许回退 ——
"已完成的任务又变成运行中"会让前端的状态渲染彻底混乱。
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED})


class TaskType(StrEnum):
    """已知任务类型。

    用枚举而不是裸字符串：任务类型是**接口契约**的一部分
    （前端据此决定怎么渲染结果），拼错一个字母就会静默变成"未知任务"。
    """

    REINDEX = "reindex"  # 重建检索索引（CPU 密集）
    INGEST_RESUME = "ingest_resume"  # 解析简历文件（IO + 解析）
    BATCH_MATCH = "batch_match"  # 批量岗位匹配（多次 LLM 调用）


def new_task_id() -> str:
    return uuid.uuid4().hex


class TaskRecord(BaseModel):
    """一条任务记录。"""

    id: str = Field(default_factory=new_task_id)
    type: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    # 进度：0~100。前端用它画进度条；-1 表示"无法预估"
    progress: int = 0
    message: str = ""

    # 结果与错误分开存放：失败时 result 保持为 None，
    # 而不是塞一个 {"error": ...} —— 避免前端要判断"这个 result 到底是结果还是错误"
    result: dict[str, Any] | None = None
    error: str | None = None

    # 提交时的参数。做成正式字段而不是私有属性，
    # 是为了让 Redis 实现能原样序列化整条记录 ——
    # 任何"藏在 __dict__ 里"的状态都无法跨进程传递，
    # 而这类状态在切到分布式时会静默丢失。
    payload: dict[str, Any] = Field(default_factory=dict)

    # 触发者会话（可选），便于前端按会话过滤任务
    session_id: str | None = None

    def mark_running(self, message: str = "") -> None:
        self.status = TaskStatus.RUNNING
        self.started_at = time.time()
        if message:
            self.message = message

    def mark_succeeded(self, result: dict[str, Any] | None = None, message: str = "") -> None:
        self.status = TaskStatus.SUCCEEDED
        self.finished_at = time.time()
        self.progress = 100
        self.result = result
        if message:
            self.message = message

    def mark_failed(self, error: str) -> None:
        self.status = TaskStatus.FAILED
        self.finished_at = time.time()
        self.error = error

    def mark_cancelled(self) -> None:
        self.status = TaskStatus.CANCELLED
        self.finished_at = time.time()

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def duration_ms(self) -> int | None:
        """执行耗时。可观测性的最小单位 —— 没有它就无法回答"这个任务慢在哪"。"""
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return int((end - self.started_at) * 1000)


class TaskSummary(BaseModel):
    """列表视图。与 SessionSummary 同理：不含 result（可能很大）。"""

    id: str
    type: str
    status: TaskStatus
    progress: int
    message: str
    created_at: float
    duration_ms: int | None = None
    error: str | None = None
