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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.agent.loop import Agent
from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.llm.client import LLMClient
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
    tools = build_default_registry()
    agent = Agent(llm_client, tools, settings.agent)

    app.state.settings = settings
    app.state.llm = llm_client
    app.state.tools = tools
    app.state.agent = agent

    logger.info(
        "装配完成：model=%s，工具 %d 个（%s），max_steps=%d",
        settings.llm.model,
        len(tools.names()),
        "、".join(tools.names()),
        settings.agent.max_steps,
    )

    try:
        yield
    finally:
        await llm_client.aclose()
        logger.info("HTTP 连接池已关闭，服务退出")


app = FastAPI(
    title="JobPilot API",
    description="求职/招聘 AI Agent —— 手写内核的 ReAct Agent 服务",
    version=__version__,
    lifespan=lifespan,
)

# 开发期前端跑在 Vite 的 5173 端口，属于跨域。生产环境应改为精确白名单。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.app_host, port=s.app_port, reload=s.is_dev)
