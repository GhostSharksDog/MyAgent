"""异步任务队列：把耗时操作从请求路径上摘出去。

    models       任务模型与状态机
    queue        TaskQueue 抽象 + 进程内实现 + ImmediateTaskQueue（测试用）
    redis_queue  Redis 实现（多副本部署）
    handlers     真正的干活逻辑（reindex / ingest_resume / batch_match）
    factory      按配置装配

【为什么需要它 —— 一个具体到不能反驳的理由】
知识库重建索引要读全部文档 → 切分 → 拟合 TF-IDF → 建 BM25 倒排表。
当前语料下几百毫秒，涨到几千块时就是几秒到几十秒。
而它是同步 CPU 密集操作，会**阻塞整个事件循环** ——
期间所有 HTTP 请求（包括别人的对话流式响应）全部停摆。

这是"不拆会疼在哪"的具体答案：不是架构洁癖，是明确的可用性问题。
"""

from app.tasks.factory import build_task_queue
from app.tasks.handlers import register_default_handlers
from app.tasks.models import (
    TERMINAL_STATUSES,
    TaskRecord,
    TaskStatus,
    TaskSummary,
    TaskType,
)
from app.tasks.queue import (
    ImmediateTaskQueue,
    InProcessTaskQueue,
    TaskContext,
    TaskQueue,
)
from app.tasks.redis_queue import RedisTaskQueue

__all__ = [
    "TERMINAL_STATUSES",
    "ImmediateTaskQueue",
    "InProcessTaskQueue",
    "RedisTaskQueue",
    "TaskContext",
    "TaskQueue",
    "TaskRecord",
    "TaskStatus",
    "TaskSummary",
    "TaskType",
    "build_task_queue",
    "register_default_handlers",
]
