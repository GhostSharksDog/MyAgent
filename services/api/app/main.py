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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.agent.factory import build_agent_stack, mount_agent_stack
from app.api.auth import ApiKeyMiddleware, check_exposure_posture
from app.api.files import router as files_router
from app.api.metrics import router as metrics_router
from app.api.models import router as models_router
from app.api.routes import router
from app.api.sessions import router as sessions_router
from app.api.settings import router as settings_router
from app.api.tasks import router as tasks_router
from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.core.telemetry import METRICS, set_trace_id
from app.demo.replay import build_replayer
from app.session.factory import build_session_store
from app.tasks.factory import build_task_queue

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # 日志形态跟着配置走：本地默认彩文本，部署时设 LOG_FORMAT=json 即可被采集器聚合
    setup_logging(settings.log_level, fmt=settings.log_format)

    # 【安全姿态检查放在最前面，而且它会**抛异常**】
    # 一个会暴露到网络、又没有访问控制的服务，不该被启动起来。
    # 详见 app/api/auth.py：三条出路写在异常信息里。
    posture = check_exposure_posture(settings)
    logger.info("访问控制：%s", posture)

    logger.info("启动 Legacy API v%s（env=%s）", __version__, settings.app_env)

    if not settings.llm.is_configured:
        logger.warning(
            "未检测到 LLM_API_KEY —— /api/chat 会返回配置错误。"
            "请复制 .env.example 为 .env 并填写密钥。"
        )

    # 【Agent 全栈的装配只有一份，见 build_agent_stack】
    # 换模型（POST /api/models/{id}/activate）要重跑完全相同的装配，
    # 所以它不能留在这里手写一遍 —— 两份迟早漂移，而漂移的表现是
    # "界面切了模型，但某条路径还在用旧的"。
    stack = build_agent_stack(settings)

    # 会话存储：`auto` 会优先连真 Redis，失败则降级到内存（并打 WARNING）
    sessions = await build_session_store(settings)

    # 任务队列：注册处理器 → 启动 worker（顺序不能反，见 factory 的说明）
    # `run_workers_in_api=false` 时本进程只投递、不消费（配合独立 worker 进程）
    tasks = await build_task_queue(settings)

    # 离线回放（演示兜底）。装配阶段就加载好，让格式问题**在启动时**暴露，
    # 而不是等演示到一半才发现录制文件过期了。
    replayer = build_replayer(settings.demo_replay_file)

    app.state.settings = settings
    mount_agent_stack(app, stack)
    app.state.sessions = sessions
    app.state.tasks = tasks
    app.state.replayer = replayer

    logger.info(
        "装配完成：profile=%s，model=%s，工具 %d 个（%s），max_steps=%d，记忆=%s，会话=%s，任务队列=%s",
        settings.agent.profile,
        settings.llm.model,
        len(stack.tools.names()),
        "、".join(stack.tools.names()),
        settings.agent.max_steps,
        "开启" if stack.memory else "关闭",
        sessions.backend,
        tasks.backend,
    )

    try:
        yield
    finally:
        # 退出前落盘长期记忆：Agent 在交互中积累的事实不该因重启而丢失
        # （注意用 `app.state.long_term` 而不是装配时那个局部变量：
        #   换模型会重建全栈，局部变量指向的已经是**被替换下来的**旧实例，
        #   往它身上 save 等于把记忆存进一个没人再读的对象里。）
        current_long_term = getattr(app.state, "long_term", None)
        if current_long_term is not None:
            current_long_term.save()
            logger.info("长期记忆已落盘：%d 条", len(current_long_term))
        # 先停队列再关连接池：队列的 worker 可能正在用 LLM 客户端
        await tasks.aclose()
        await sessions.aclose()
        # 关闭要**宽容**：这个位置拿到的是鸭子类型的客户端（测试里注入的是替身）。
        # 少一个方法就让关闭抛异常，代价不只是"没关成" —— 它会把整场测试的结果
        # 变成一个 teardown ERROR（828 个用例通过，报告却说失败），
        # 而那种报告会让人从完全错误的方向开始排查。
        current_llm = getattr(app.state, "llm", None)
        close = getattr(current_llm, "aclose", None)
        if close is not None:
            await close()
        logger.info("HTTP 连接池已关闭，服务退出")


app = FastAPI(
    title="Legacy API",
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
        METRICS.inc("legacy_http_requests_total", method=request.method, status="5xx")
        logger.exception("请求处理异常 %s %s", request.method, request.url.path)
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        # 标签只用**有界的枚举值**（方法 + 状态码大类），绝不用 URL 路径：
        # 带 id 的路径（/api/sessions/{id}）会让指标条数随会话数无限增长。
        # 这是 Prometheus 使用中最经典的事故。
        METRICS.inc(
            "legacy_http_requests_total", method=request.method, status=f"{status // 100}xx"
        )
        METRICS.observe(
            "legacy_http_duration_ms",
            duration_ms,
            method=request.method,
            status=f"{status // 100}xx",
        )
        logger.info("%s %s → %d（%.0fms）", request.method, request.url.path, status, duration_ms)


# ============================================================
# 中间件：访问控制（T03）与跨域（T14）
# ============================================================
# 【为什么这里在**导入时**读一次配置】
# 中间件栈必须在应用构造时就定下来（Starlette 不支持运行期增删），
# 所以"要不要鉴权、放行哪些来源"这类决定天生是**进程级**的：
# 改了配置要重启才生效。这与界面里能即时生效的那些设置（模型、工作区、
# 语料）是两类东西，**说清楚比假装能热更新更重要** ——
# 用户改了 .env 却发现跨域规则没变，第一反应会是"改了没生效"。
_settings = get_settings()
_cors_origins = _settings.security.cors_origin_list
_app_host_is_loopback = (_settings.app_host or "").strip().lower() in {
    "127.0.0.1",
    "::1",
    "localhost",
}

# 【中间件顺序：这条最容易被写反，所以先把机制写清楚】
#
# Starlette 的 `add_middleware` 是 **insert(0)**（后添加的排在列表前面），
# 而 `build_middleware_stack` 从列表尾部开始**倒着包装** ——
# 两个效果叠起来：**最后添加的中间件在最外层**。
#
# 所以顺序是刻意反过来的：**先添加鉴权，后添加 CORS**，让 CORS 落在最外层。
# 为什么必须这样：
#
#   · 预检（OPTIONS）由 CORS 层直接应答，不会进到鉴权层 ——
#     而预检**不带自定义请求头**（这是规范），被鉴权拦掉的表现是
#     "所有跨域请求都 401"，人会去怀疑密钥配错了；
#   · 更重要的是：401 响应要带上 CORS 头，浏览器才读得到里面的提示文字。
#     CORS 在内层时，前端拿到的只是一个"跨域被拦"的无语义错误 ——
#     用户永远看不到"请带上 API Key"这句话。
#
# （`ApiKeyMiddleware` 里仍然跳过了 OPTIONS，那是第二道保险：
# 即使将来有人调换了顺序，也不会把跨域整个废掉。）
app.add_middleware(ApiKeyMiddleware, settings=_settings)

# 开发期前端跑在 Vite 的 5173 端口，属于跨域。
#
# 【T14：这里从"放行任意 localhost 端口"改成了**按配置决定**】
#
# 原来的写法是一条正则放行所有本机来源。它对开发很方便，但它是
# **一个无条件的放宽**：只要服务在跑，任何在你本机打开的页面
# （包括你随手访问的一个网页）都能调用它。配合"无鉴权"就是双份风险。
#
# 现在的规则（都要与"绑定地址"一起看，见 check_exposure_posture）：
#
#   显式配了 SECURITY_CORS_ALLOW_ORIGINS → 用它（精确白名单）
#   没配 + 绑定回环                    → 保留原来那条开发正则（零配置可用）
#   没配 + 绑定非回环                  → **不放行任何跨域来源**
#
# 最后一条是关键：对外提供服务时，"任意本机来源"这个假设已经不成立了
# （请求可能来自任何地方），此时应当由部署方显式声明谁可以跨域。
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id"],
    )
elif _app_host_is_loopback:
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
else:
    logger.warning(
        "以 %s 对外提供服务且未配置 SECURITY_CORS_ALLOW_ORIGINS："
        "当前**不放行任何跨域来源**。若是前后端分离部署，请显式声明允许的来源。",
        _settings.app_host,
    )

app.include_router(router)
app.include_router(sessions_router)
app.include_router(tasks_router)
app.include_router(metrics_router)
app.include_router(settings_router)
app.include_router(models_router)
app.include_router(files_router)


# ============================================================
# 托管前端产物：让整个应用变成"一条命令、一个端口"
# ============================================================
def mount_frontend(application: FastAPI, dist: Path) -> bool:
    """把构建好的前端挂到 `/`。返回是否挂载成功。

    【为什么必须放在所有 include_router 之后】
    Starlette 按注册顺序匹配路由。`mount("/")` 会匹配**一切**没被前面
    匹配掉的路径 —— 如果放在 API 路由之前注册，`/api/chat` 会被它抢先
    接走，然后返回 index.html。表现是"接口突然返回 HTML"，
    而且状态码是 200，排查起来会先怀疑前端再怀疑后端。

    **顺序在这里不是风格问题，是正确性问题。**

    【为什么需要 SPA 回退】
    前端是单页应用：`/sessions/abc` 这类路径在服务端**没有对应文件**，
    它由前端路由处理。默认的 StaticFiles 会对它返回 404 ——
    用户刷新页面就白屏，而点链接进去却是好的（因为那是前端路由跳转）。
    这种"刷新才坏"的现象很难第一时间联想到服务端配置。

    所以找不到文件时回退到 index.html，把路由权交还给前端。

    【为什么没构建时不报错，只打日志】
    开发时前端跑在 Vite 的 5173（有热更新，比构建产物好用得多），
    此时 dist 可能根本不存在。让服务因为"前端没构建"而起不来，
    会把"后端开发"和"前端开发"强行绑定 —— 那是没必要的耦合。
    所以这里降级为"只提供 API"，并**明确告知怎么构建**。
    """
    index = dist / "index.html"
    if not index.exists():
        logger.info(
            "未找到前端产物（%s），仅提供 API。"
            "构建前端：cd apps/web && pnpm build；"
            "或开发时另起：cd apps/web && pnpm dev（http://localhost:5173）",
            dist,
        )
        return False

    class SPAStaticFiles(StaticFiles):
        """找不到文件时回退到 index.html，交给前端路由处理。

        【一个真实踩过的坑：捕获错了异常类】

        初版写的是 `except HTTPException` —— 用的是 **FastAPI 的**
        HTTPException。但 StaticFiles 是 Starlette 的类，它抛的是
        **Starlette 自己的** `starlette.exceptions.HTTPException`。

        而 `fastapi.HTTPException` 是 Starlette 那个的**子类** ——
        捕获方向反了：子类捕不到父类。结果是回退逻辑永远不会触发，
        未知路径依然 404。

        这类 bug 的特征是**静默失效**：没有报错、没有警告，
        只是那段代码永远不执行。所以捕获异常时要看清**抛的人用的是哪个类**，
        而不是"名字一样就行"。

        【为什么必须排除 API 前缀 —— 第二个坑】

        回退写成"任何 404 都返回 index.html"之后，`/api/nonexistent`
        会返回**状态码 200 的 HTML**。后果是：前端如果调错了一个接口，
        拿到的是 HTML 而不是 404，`response.json()` 会抛一个
        "Unexpected token '<'" 之类的解析错误 ——
        **排查方向会被引到前端的 JSON 处理上，而真正的问题是接口路径写错了。**

        所以只有"浏览器导航类"的路径才回退。API、文档、指标这些
        机器消费的路径必须保留真实的 404，**让错误在最能说明问题的地方呈现**。
        """

        # 这些前缀属于"机器消费"的路径，不做 SPA 回退。
        #
        # 【为什么匹配前必须归一化分隔符 —— 第三个坑，且只在 Windows 上出现】
        #
        # Starlette 1.6 的 `StaticFiles.get_path()` 是这么算的：
        #     os.path.normpath(os.path.join(*route_path.split("/")))
        #
        # 而 **`os.path.join` 用的是平台分隔符** —— 在 Windows 上它是反斜杠。
        # 所以传进 `get_response` 的 path 长这样：
        #     /api/nonexistent  →  api\nonexistent   （不是 api/nonexistent）
        #
        # 于是 `path.startswith("api/")` 在 Windows 上**永远为假**，
        # 而同一份代码在 Linux/Mac 上是对的。这类 bug 最难的地方在于：
        # **它在开发者的机器上不出现，在 CI（Linux）上也不出现**，
        # 只在这台 Windows 机器上悄悄失效，且不报任何错。
        #
        # 所以这里先把 `\` 统一成 `/` 再比较。
        # 判据是：**任何对路径做字符串前缀匹配的地方，都要先归一化分隔符**，
        # 否则就是在假设平台。
        RESERVED = ("api/", "healthz", "metrics", "docs", "redoc", "openapi.json")

        async def _fallback_or_404(self, path: str, scope: Any) -> Any:
            normalized = path.replace("\\", "/").lstrip("/")
            raised = None
            try:
                response = await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code != 404:
                    raise
                raised = exc
                response = None

            # 有些 Starlette 版本不抛异常而是直接返回 404 响应，
            # 所以两条路径都要处理 —— 只处理一条会在升级依赖后静默失效。
            if response is not None and response.status_code != 404:
                return response

            if normalized.startswith(self.RESERVED):
                if raised is not None:
                    raise raised
                return response  # 原样的 404

            return await super().get_response("index.html", scope)

        async def get_response(self, path: str, scope: Any) -> Response:
            return await self._fallback_or_404(path, scope)

    application.mount("/", SPAStaticFiles(directory=str(dist), html=True), name="frontend")
    logger.info("已挂载前端产物：%s（访问 / 即为界面）", dist)
    return True


def resolve_web_dist(settings: Settings) -> Path:
    """前端产物目录：显式配置优先，否则按仓库结构推断。

    【为什么要有"推断"这一半】
    源码树里跑（`dev.ps1 serve`）是绝大多数场景，那时不该逼任何人多配一个变量。
    推断用的是 `__file__` 而不是当前工作目录 —— 后者会随"从哪个目录启动"变化，
    是"本地能跑、换个目录就找不到文件"的经典来源。

    【为什么要有"配置"这一半】
    镜像里的目录结构不是仓库结构（代码在 /app/app/），推断必然落空，
    而落空的后果是**静默的**：mount_frontend 允许前端产物缺失（开发时
    前端跑在 Vite 里，那个宽容是对的），于是容器起来了、接口也正常，
    只有界面 404 —— 见 docs/01-architecture.md 的 T22。
    """
    configured = settings.web_dist.strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[3] / "apps" / "web" / "dist"


_web_dist = resolve_web_dist(_settings)
mount_frontend(app, _web_dist)


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.app_host, port=s.app_port, reload=s.is_dev)
