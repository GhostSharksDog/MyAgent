"""指标端点。

    GET /metrics         Prometheus 文本暴露格式（给 Prometheus / Grafana 抓）
    GET /api/metrics     结构化 JSON（给前端面板、脚本、以及人看）

【为什么两个都要有】
`/metrics` 是**机器接口**：Prometheus 定期抓取，格式由协议规定，不适合人读。
`/api/metrics` 是**人接口**：前端要画一个"本次会话花了多少 token、哪个工具最慢"
的小面板，解析 Prometheus 文本格式是自找麻烦。

同时提供两者，也就顺带说明了"面向机器的接口"与"面向人的接口"本就该分开 ——
用一套格式同时满足两边，结果是两边都不好用。

【关于多进程】
指标是**进程内**的。多副本部署时每个实例各自持有一份，
Prometheus 会逐个抓取再聚合 —— 这正是拉取式采集的设计意图：
应用不需要维护全局状态，也就不会成为单点。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.schemas import MetricsResponse
from app.core.telemetry import METRICS
from app.session.store import SessionStore
from app.tasks.queue import TaskQueue

router = APIRouter(tags=["observability"])


@router.get("/metrics", summary="Prometheus 指标", response_class=Response)
async def prometheus_metrics() -> Response:
    # 不套 Pydantic 模型：Prometheus 文本格式不是 JSON，
    # 用 Response 直接返回才是诚实的做法（用 JSONResponse 会多套一层引号）
    return Response(
        content=METRICS.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/api/metrics", response_model=MetricsResponse, summary="指标快照（JSON）")
async def metrics_snapshot(request: Request) -> MetricsResponse:
    """结构化指标快照 + 各组件的运行状态。

    把"组件状态"和"指标"放在同一个响应里，是因为它们回答的是同一个问题：
    **这个服务现在健康吗？** 拆成两个端点只会让调用方多写一次请求。
    """
    sessions: SessionStore = request.app.state.sessions
    tasks: TaskQueue = request.app.state.tasks
    mem = request.app.state.memory
    long_term = request.app.state.long_term

    snapshot = METRICS.snapshot()

    components: dict[str, object] = {
        "session_backend": sessions.backend,
        "task_backend": tasks.backend,
        "memory_enabled": mem is not None,
        "long_term_facts": len(long_term) if long_term is not None else 0,
        "tools": request.app.state.tools.names(),
    }

    return MetricsResponse(
        counters=snapshot["counters"],  # type: ignore[arg-type]
        histograms=snapshot["histograms"],  # type: ignore[arg-type]
        uptime_seconds=snapshot["uptime_seconds"],  # type: ignore[arg-type]
        components=components,
    )
