"""FastAPI 应用入口。

【为什么用 lifespan 而不是全局变量】
LLMClient 内部持有一个 httpx.AsyncClient（连接池）。连接池必须在
**事件循环启动后**创建、在**进程退出前**优雅关闭，否则会出现
"Event loop is closed" 或连接泄漏。lifespan 正是为此设计的钩子。

【依赖装配的位置】
所有单例（配置、LLM 客户端、工具表、Agent）都在这里装配并挂到 app.state。
这叫**组合根（Composition Root）**：依赖关系集中在一处，业务代码只管用。
好处是测试时可以直接替换 app.state.agent 注入假的 Agent，不必启动真模型。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.agent.factory import build_memories
from app.agent.loop import Agent
from app.api.metrics import router as metrics_router
from app.api.routes import router
from app.api.sessions import router as sessions_router
from app.api.tasks import router as tasks_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.telemetry import METRICS, set_trace_id
from app.llm.client import LLMClient
from app.session.factory import build_session_store
from app.tasks.factory import build_task_queue
from app.tools.builtin import build_default_registry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info("启动 JobPilot API v%s（env=%s）", __version__, settings.app_env)

    if not settings.llm.is_configured:
        logger.warning(
            "未检测到 LLM_API_KEY —— /api/chat 会返回配置错误。"
            "请复制 .env.example 为 .env 并填写密钥。"
        )

    llm_client = LLMClient(settings.llm)

    # 记忆要先于工具表构造：`remember_fact` 工具需要与 Agent 共享同一个
    # 长期记忆实例，否则工具"记住"的东西 Agent 读不到 —— 这是
    # 依赖注入顺序上最容易踩的坑。
    short_memory, long_term = build_memories(settings, llm=llm_client)

    tools = build_default_registry(long_term_memory=long_term)
    agent = Agent(
        llm_client,
        tools,
        settings.agent,
        memory=short_memory,
        long_term=long_term,
    )

    # 会话存储：`auto` 会优先连真 Redis，失败则降级到内存（并打 WARNING）
    sessions = await build_session_store(settings)

    # 任务队列：注册处理器 → 启动 worker（顺序不能反，见 factory 的说明）
    tasks = await build_task_queue(settings)

    app.state.settings = settings
    app.state.llm = llm_client
    app.state.tools = tools
    app.state.agent = agent
    app.state.memory = short_memory
    app.state.long_term = long_term
    app.state.sessions = sessions
    app.state.tasks = tasks

    logger.info(
        "装配完成：model=%s，工具 %d 个（%s），max_steps=%d，记忆=%s，会话=%s，任务队列=%s",
        settings.llm.model,
        len(tools.names()),
        "、".join(tools.names()),
        settings.agent.max_steps,
        "开启" if short_memory else "关闭",
        sessions.backend,
        tasks.backend,
    )

    try:
        yield
    finally:
        # 退出前落盘长期记忆：Agent 在交互中积累的事实不该因重启而丢失
        if long_term is not None:
            long_term.save()
            logger.info("长期记忆已落盘：%d 条", len(long_term))
        # 先停队列再关连接池：队列的 worker 可能正在用 LLM 客户端
        await tasks.aclose()
        await sessions.aclose()
        await llm_client.aclose()
        logger.info("HTTP 连接池已关闭，服务退出")


app = FastAPI(
    title="JobPilot API",
    description="求职/招聘 AI Agent —— 手写内核的 ReAct Agent 服务",
    version=__version__,
    lifespan=lifespan,
)


# ============================================================
# 链路追踪中间件
# ============================================================
@app.middleware("http")
async def trace_middleware(request: Request, call_next: Callable[[Request], Any]) -> Response:
    """为每个请求建立 trace 上下文，并把 trace id 回写到响应头。

    【为什么接受上游传来的 trace id】
    服务拆分之后，一个用户请求会经过 网关 → agent 服务 → rag 服务。
    若每个服务各生成一个 id，你就有三个互不相关的 id，链路依然是断的。
    接受并透传上游的 `X-Trace-Id`，才能把跨进程调用串成一条链 ——
    这是分布式追踪的第一块基石（更完整的方案是 W3C Trace Context，
    但核心思想就是这个）。

    【为什么用 ContextVar 而不是 thread-local】
    asyncio 里多个协程跑在同一个线程上，thread-local 会被它们共享 ——
    并发请求的日志会串味，而且这种 bug 只在有并发时出现，本地测不出来。
    ContextVar 随 Task 复制，每个请求持有自己的副本，
    并且会正确传播到该请求派生的子任务（比如并发的工具调用）。
    """
    incoming = request.headers.get("X-Trace-Id")
    trace_id = set_trace_id(incoming)

    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        # 回写响应头：客户端拿到它就能在自己的日志里关联这次请求
        response.headers["X-Trace-Id"] = trace_id
        return response
    except Exception:
        # 异常也要计入指标，否则"错误率"这条曲线永远是平的 ——
        # 而那恰恰是最需要被看到的曲线
        METRICS.inc("jobpilot_http_requests_total", method=request.method, status="5xx")
        logger.exception("请求处理异常 %s %s", request.method, request.url.path)
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        # 标签只用**有界的枚举值**（方法 + 状态码大类），绝不用 URL 路径：
        # 带 id 的路径（/api/sessions/{id}）会让指标条数随会话数无限增长。
        # 这是 Prometheus 使用中最经典的事故。
        METRICS.inc(
            "jobpilot_http_requests_total", method=request.method, status=f"{status // 100}xx"
        )
        METRICS.observe(
            "jobpilot_http_duration_ms",
            duration_ms,
            method=request.method,
            status=f"{status // 100}xx",
        )
        logger.info("%s %s → %d（%.0fms）", request.method, request.url.path, status, duration_ms)


# 开发期前端跑在 Vite 的 5173 端口，属于跨域。生产环境应改为精确白名单。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # 前端要能读到 trace id，必须显式暴露 —— CORS 默认不允许 JS 读取
    # 自定义响应头，不暴露的话前端拿到的永远是 null
    expose_headers=["X-Trace-Id"],
)

app.include_router(router)
app.include_router(sessions_router)
app.include_router(tasks_router)
app.include_router(metrics_router)


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.app_host, port=s.app_port, reload=s.is_dev)
