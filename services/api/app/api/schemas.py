"""HTTP 接口的请求/响应模型。

显式建模而不是收 dict：接口契约就是文档，FastAPI 会自动生成 OpenAPI，
前端可以直接据此生成 TypeScript 类型（P3 会做这件事）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.llm.types import Usage


class HistoryMessage(BaseModel):
    """历史消息。

    注意这里只允许 user / assistant 两种角色，**刻意不暴露 tool 角色**：
    工具调用是 Agent 的内部实现细节，历史里只保留"问答结果"，
    这样历史长度可控，也不会因为 tool 消息配对问题导致接口 400。
    """

    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000, description="用户本轮输入")
    mode: Literal["react", "plan", "multi"] = Field(
        default="react",
        description=(
            "Agent 形态。三种形态针对不同的任务结构，不是「哪个更高级」："
            "react（默认）= 想一步做一步，适合探索型任务；"
            "plan = 先出完整计划再逐步执行，适合结构型任务且计划对用户可见；"
            "multi = 主管路由到多位专家并发作答，适合跨领域提问。"
        ),
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "会话 id。提供时仅 react 恢复历史与记忆；plan/multi 按独立任务处理，"
            "并把本轮结果写回会话；此时 `history` 被忽略。"
            "不提供则退回无状态模式（历史完全由客户端提供）。"
        ),
    )
    history: list[HistoryMessage] = Field(
        default_factory=list,
        description="之前轮次的消息（不含本轮），最近的在最后。仅 ReAct 且无 session_id 时生效；Plan/Supervisor 本轮不使用会话历史。",
    )


class ChatResponse(BaseModel):
    run_id: str | None = None
    record_saved: bool = True
    session_saved: bool | None = None
    answer: str
    steps_used: int
    usage: Usage
    usage_complete: bool = True
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    stopped_reason: str = "finished"
    error: str | None = None
    tool_summary: str = ""
    context_trimmed: bool = False
    context_tokens: int = 0


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class MetaResponse(BaseModel):
    service: str
    version: str
    env: str
    model: str
    max_steps: int
    tool_count: int
    # 当前的 Agent 形态（general / jobhunt）。
    #
    # 【为什么必须暴露出来】
    # profile 决定三件事：system prompt 是哪一份、注册哪些工具、知识库默认加载
    # 哪些文档。它的取值**只影响行为，不影响健康状态** —— 配错了不会报错，
    # 只会得到另一个形态的助手。把它放进元信息，就是让"我现在是什么形态"
    # 变成前端/运维能一眼看到的事实，而不是靠读 .env 反推。
    #
    # 与 rag_backend / session_backend 是同一条原则：**静默的形态差异必须可观测。**
    profile: str = "general"
    session_backend: str = ""
    # 检索后端：`local` 表示在本进程内检索（单体），
    # `remote` 表示走独立的 RAG 服务。
    #
    # 【为什么这个字段必须暴露出来】
    # 它与 session/task backend 是同一类问题：**配置错了不会报错，
    # 只会默默地用另一种拓扑运行**。比如部署时忘了给 agent 容器设
    # RAG_SERVICE_URL，它会"正常"在本进程建一份索引 —— 服务健康、
    # 回答也对，但你以为的独立检索服务根本没被使用，
    # CPU 依然在抢，扩容也没生效。
    #
    # 把拓扑状态放进元信息，就是把这类静默错误变成**可观测**的。
    rag_backend: str = "local"
    task_backend: str = ""
    # 任务是否在本进程内消费。为 false 时任务由独立 worker 进程消费。
    task_workers_in_api: bool = True
    # 支持的 Agent 形态。前端据此渲染模式选择器 —— 让 UI 从后端**发现**能力，
    # 而不是在前端硬编码一份可能过期的列表（新增形态时前端无需改代码）。
    agent_modes: list[str] = Field(default_factory=lambda: ["react", "plan", "multi"])


# ============================================================
# 会话
# ============================================================
class SessionSummaryModel(BaseModel):
    """会话列表项。刻意不含对话内容（见 sessions.py 的说明）。"""

    id: str
    title: str
    created_at: float
    updated_at: float
    turn_count: int
    total_tokens: int


class SessionListResponse(BaseModel):
    sessions: list[SessionSummaryModel] = Field(default_factory=list)
    # 把后端暴露给前端：`memory` 意味着刷新页面/换标签页可能丢历史，
    # 界面上应该据此给出提示，而不是让用户自己撞上"历史不见了"
    backend: str = "memory"


class SessionDetail(BaseModel):
    id: str
    title: str
    created_at: float
    updated_at: float
    total_tokens: int
    # 与列表项 SessionSummaryModel 保持一致。客户端虽然能从
    # `len(turns) / 2` 推出来，但"同一个概念在两个接口里形状不同"
    # 会让前端不得不同时维护两种算法 —— 而两种算法迟早会不一致。
    turn_count: int = 0
    turns: list[dict[str, str]] = Field(default_factory=list)


# ============================================================
# 异步任务
# ============================================================
class TaskSubmitRequest(BaseModel):
    type: str = Field(
        min_length=1,
        description="任务类型：reindex（重建索引）/ ingest_resume（解析文档）/ batch_match（批量匹配）",
    )
    payload: dict[str, Any] = Field(default_factory=dict, description="任务参数，随类型而定")
    session_id: str | None = Field(default=None, description="触发该任务的会话（可选）")


class TaskStatusModel(BaseModel):
    id: str
    type: str
    status: str
    progress: int = 0
    message: str = ""
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskListResponse(BaseModel):
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    backend: str = "memory"
    known_types: list[str] = Field(default_factory=list)


# ============================================================
# 可观测性
# ============================================================
class MetricsResponse(BaseModel):
    """指标快照。

    结构与 `/metrics` 的 Prometheus 格式刻意不同：这里是**给人看的**
    （数组 + 具名字段），Prometheus 那边是给抓取器看的（行式文本）。
    同一个数据用两种形态暴露，是为了不让任何一方将就。
    """

    counters: list[dict[str, Any]] = Field(default_factory=list)
    histograms: list[dict[str, Any]] = Field(default_factory=list)
    uptime_seconds: float = 0.0
    # 各组件当前的后端选择。`session_backend=memory` 这类信息在多副本部署下
    # 是**最需要一眼看到**的（它意味着会话无法共享），所以放在指标快照里
    components: dict[str, Any] = Field(default_factory=dict)
