"""访问控制：API Key 校验 + "暴露且有风险"的启动前拒绝（技术债 T03 / T14）。

《为什么这两件事必须在同一个文件里》

它们其实是**同一件事的两半**：T03 是"谁能调用"，T14 是"谁能在浏览器里调用"。
分开做的后果是典型的半成品 —— 加了密钥却仍然放行任意 localhost 来源，
或者收紧了 CORS 却让服务裸奔在 `0.0.0.0` 上。**安全边界不能只补一半。**

《默认策略：什么都不变，但危险的组合拒绝启动》

在这之前，本项目的安全边界只有一句"只监听 127.0.0.1"。那是**部署约束**，
不是安全机制：把 `APP_HOST` 改成 `0.0.0.0` 就能绕过它，而那一刻**界面上
没有任何提示** —— 用户以为只是"让同事也能访问"，实际是把
"用你的额度、读你的文件工作区、看你的会话历史"一起交出去了。

所以这里不做"默认开鉴权"（那会让本地开发多一步配置，而本项目
一直坚持**默认值是最无害且零配置可用的那个**），而是：

    回环地址 + 无密钥   → 照常启动（本地开发，零配置）
    非回环地址 + 密钥   → 照常启动，并记录一条 INFO
    非回环地址 + 无密钥 → **拒绝启动**，并说清三条出路

判断依据是**实际绑定地址**，因为它就是"谁能访问到它"的准确答案 ——
比任何"环境名/是否生产"的推断都可靠。
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import Settings

logger = logging.getLogger(__name__)

# 回环地址的等价写法。`localhost` 也算 —— 它虽然是名字，但解析结果只可能是回环，
# 而此时服务端 bind 的是它，说明只服务本机。
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# 需要密钥保护的路径前缀。
#
# 【为什么不保护 `/`（前端静态资源）】
# 保护它意味着浏览器连页面都拿不到，用户就没有地方输入密钥了 ——
# 界面本身不含任何隐私（它是构建产物），**能读到你的数据的是 API**。
# 这个划分也正是"前端只是外壳、真正的东西在服务端"的体现。
#
# `/healthz` 也不保护：容器编排与探针需要一个不需要凭据的健康端点，
# 给它加鉴权会让所有探针失败，然后人被逼着把密钥写进探活配置里 ——
# 那是把密钥分发到更多地方。它暴露的信息（模型名、backend 类型）
# 属于"部署形态"，不是用户数据。
_PROTECTED_PREFIXES = ("/api/", "/metrics", "/docs", "/redoc", "/openapi.json")


def is_public_path(path: str) -> bool:
    """这个路径是否**不需要**密钥。"""
    normalized = path or "/"
    if normalized == "/healthz":
        return True
    return not normalized.startswith(_PROTECTED_PREFIXES)


def extract_api_key(request: Request) -> str:
    """从请求里取密钥：`Authorization: Bearer` 优先，其次 `X-API-Key`。

    【为什么两种都收】
    `Authorization: Bearer` 是标准写法（企业的网关、SDK、curl 示例默认都用它），
    而 `X-API-Key` 更直白、写起来短。只收一种就会让"按标准写"和"图省事写"的
    两类调用方互相以为对方的方式是对的 —— 收到的却是一个 401，
    而错误信息里不会说明该用哪种头。
    """
    header = request.headers.get("authorization", "")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return request.headers.get("x-api-key", "").strip()


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """在进入业务代码之前校验密钥。

    【为什么用中间件而不是每个路由加依赖】
    路由是会新增的，而**新增路由的人不会记得加鉴权** —— 这正是
    "默认拒绝"比"逐个声明"可靠的地方。中间件按路径前缀统一判断，
    新增 `/api/xxx` 自动受保护，漏掉的可能性只剩下"前缀写错"。

    【为什么 OPTIONS 必须放行】
    浏览器的 CORS 预检请求**不带自定义请求头**（这是规范：预检就是去问
    "我能不能发这个头"）。所以对 OPTIONS 校验密钥必然失败，
    而失败会表现为"所有跨域请求都 401"，排查方向会被引到密钥上，
    真正的原因却是预检被拦了。**这一条不写清楚，下一个改动的人迟早会踩。**
    """

    def __init__(self, app, settings: Settings) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._settings = settings
        self._expected = settings.security.api_key.get_secret_value()

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._expected or request.method == "OPTIONS" or is_public_path(request.url.path):
            return await call_next(request)

        provided = extract_api_key(request)
        # **必须用常数时间比较**：`==` 会在第一个不同的字符处返回，
        # 于是响应时间泄漏了"前几个字符猜对了"。对本地服务这看着无关紧要，
        # 但这条代码路径将来会被复制到别处 —— 而"照抄一段不安全的比较"
        # 正是这类问题扩散的方式。
        if provided and hmac.compare_digest(provided, self._expected):
            return await call_next(request)

        logger.warning(
            "拒绝未授权的请求：%s %s（来自 %s）",
            request.method,
            request.url.path,
            request.client.host if request.client else "?",
        )
        return Response(
            status_code=401,
            media_type="application/json",
            # 提示里**绝不能带上正确密钥**，也不要说"密钥错误"还是"没带密钥"，
            # 那对攻击者是免费的信息（而对用户这两件事的修法是一样的）
            content=(
                '{"detail":"没有权限：这个服务启用了访问密钥。'
                "请在请求头带上 X-API-Key: <key>（或 Authorization: Bearer <key>）。"
                '若你不知道密钥，请联系部署方；界面上可以在「设置 → 访问控制」里填入。"}'
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )


class ExposureRefused(RuntimeError):
    """配置组合会导致服务无保护地暴露在网络上 —— 拒绝启动。"""


def check_exposure_posture(settings: Settings) -> str:
    """启动前的安全姿态检查。返回一句可以直接打日志的结论。

    Raises:
        ExposureRefused: 以非回环地址暴露、且既没有密钥也没有显式豁免。

    【为什么是"拒绝启动"而不是"打一条 WARNING"】
    WARNING 会被忽略 —— 这是它的设计意图（不阻断流程）。而这里的后果不是
    "性能差一点"或"功能少一个"，而是**你的额度和文件在公网上开着**。
    对这类后果，唯一的有效提示是让它起不来，并把话说到能照着做：

      · 只想本地用  → APP_HOST 保持 127.0.0.1（默认值，什么都不用改）
      · 要给人访问  → 设 SECURITY_API_KEY=<一段足够长的随机串>
      · 前面有网关  → 设 SECURITY_ALLOW_UNAUTHENTICATED_EXPOSURE=true（明确认领风险）

    本项目在"把任务投递出去但没有消费者"那个组合上用的是同一套做法
    （见 `build_task_queue`：直接拒绝启动，而不是静默失效）。
    **静默失效是这个项目最想消灭的一类问题，而安全上的静默失效代价最大。**
    """
    bind = (settings.app_host or "").strip().lower()
    loopback = bind in _LOOPBACK_HOSTS
    security = settings.security

    if loopback:
        # 【这一条是端到端跑出来才发现的】
        # 我最初在这里直接 return "仅监听回环地址，未启用密钥鉴权" —— 于是
        # **在回环地址上配了密钥时，启动日志会说"未启用鉴权"**。
        # 而这一行是运维第一眼看到的东西：一个说反了的结论比没有结论危险得多，
        # 它会让人以为"配了没生效"（然后去改本来没问题的配置）。
        # **启动日志属于要被信任的输出，必须如实。**
        if security.enabled:
            return "仅监听回环地址，并已启用 API Key 鉴权"
        return "仅监听回环地址，未启用密钥鉴权（本地开发默认形态）"

    if security.enabled:
        return f"以 {settings.app_host} 对外提供服务，已启用 API Key 鉴权"

    if security.allow_unauthenticated_exposure:
        logger.warning(
            "⚠ 以 %s 暴露且**没有**启用 API Key 鉴权 —— 访问控制依赖外部（网关/反向代理）。"
            "请确认这确实是你想要的。",
            settings.app_host,
        )
        return f"以 {settings.app_host} 暴露且未启用鉴权（已由 SECURITY_ALLOW_UNAUTHENTICATED_EXPOSURE 显式豁免）"

    raise ExposureRefused(
        f"拒绝启动：APP_HOST={settings.app_host} 会把服务暴露到网络，"
        f"而当前**没有**启用 API Key 鉴权 —— 任何人都能刷你的模型额度、"
        f"读你的文件工作区与会话历史。三条出路：\n"
        f"  1. 只想本地用：APP_HOST 保持 127.0.0.1（默认值，改回去即可）\n"
        f"  2. 要给别人访问：设置 SECURITY_API_KEY=<一段足够长的随机串>\n"
        f'     （生成：python -c "import secrets;print(secrets.token_urlsafe(32))"）\n'
        f"  3. 前面确实有网关做鉴权：设置 SECURITY_ALLOW_UNAUTHENTICATED_EXPOSURE=true，"
        f"明确认领这个风险"
    )
