"""知识库访问的**显式接口**，以及本地/远程两种实现。

【为什么必须先把接口抽出来，才能拆服务】

拆分之前，`KnowledgeSearchTool` 与 `Retriever` 之间有一个**隐式契约**：

    len(retriever.chunks) == 0   →  知识库为空
    await retriever.aretrieve_context(...) -> str

这个契约在**同进程**里是成立的，因为工具拿着的是真实对象。
但一旦跨越网络边界，它就全部失效了：

  · `chunks` 是一个内存列表。远程调用方拿不到它 —— 要么不能访问，
    要么只能"为了判断空不空"而把整个语料传过来（荒谬）。
  · `Retriever` 是具体类。远程实现根本不可能继承它。

**隐式契约是拆服务时最容易翻车的地方**：它在本地测试里永远通过，
只在真正跨进程时才暴露。所以正确顺序是：

    1. 先把契约写成显式接口（本模块）
    2. 再让工具只依赖接口
    3. 最后才加远程实现

做完前两步，第 3 步几乎是免费的。

【接口为什么这么窄】
只暴露 `context()` + `stats()` 两个方法。**接口的宽度决定拆分的成本** ——
暴露 10 个方法意味着远程实现要写 10 个 HTTP 调用、10 个错误分支。
窄接口是"这个边界划得对不对"的唯一判据。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import httpx

from app.core.config import Settings, get_settings
from app.core.resilience import CircuitBreaker
from app.rag.factory import get_shared_retriever
from app.rag.loaders import DocType

logger = logging.getLogger(__name__)

# Sentinel：区分"没传 breaker 参数"与"显式传了 None 想关掉熔断"。
# Python 没有别的干净方法表达这个区别，而这里的区别很重要 ——
# 见 RemoteKnowledgeBackend.__init__ 的说明。
_DEFAULT_BREAKER: object = object()


class EmptyKnowledgeBase(RuntimeError):
    """知识库为空。

    【为什么这是一个独立的异常类型，而不是返回空字符串】

    "知识库为空"和"检索到了但没命中"是两个**语义完全不同**的状态：

      · 知识库为空 → 是**配置/数据问题**，用户需要去准备数据，
        再怎么换提问方式都没用。
      · 没命中     → 是**提问问题**，换个说法或扩大 scope 就可能找到。

    如果两者都返回空字符串，工具层只能给出"没检索到相关内容"这一句提示 ——
    对第一种情况，这句提示是**误导**：用户会一直换问法，
    而真正该做的是去放一份简历。

    【为什么这个区分在拆服务后变得更关键】
    跨进程之后，调用方无法再靠 `len(chunks) == 0` 自己判断。
    服务必须**把状态说出来**（HTTP 503 + 明确 detail），
    而不是用一个 200 + 空结果让调用方去猜。
    **网络边界上的"沉默"会被错误解读。**
    """


class KnowledgeBackendError(RuntimeError):
    """后端不可用（网络故障、服务 5xx、协议错误）。"""


@runtime_checkable
class KnowledgeBackend(Protocol):
    """知识库访问接口。

    【为什么用 Protocol 而不是抽象基类 ABC】

    Protocol 是**结构化**的：任何拥有匹配方法的对象都自动满足它，
    不需要显式继承。好处是 `Retriever` 这类已有类不必改动
    （本地实现直接包一层现有对象），测试里也可以塞一个 5 行的假对象。

    ABC 要求继承，而继承意味着**改动被拆分的那些类** ——
    这正是拆分时最不想动的东西。
    """

    async def context(
        self,
        query: str,
        *,
        k: int = 4,
        doc_types: list[DocType | str] | None = None,
        min_score: float = 0.0,
        max_chars: int = 3000,
    ) -> str:
        """返回可直接塞进提示词的上下文文本。

        Raises:
            EmptyKnowledgeBase: 知识库为空（数据问题，不是提问问题）
            KnowledgeBackendError: 后端不可用
        """
        ...

    async def stats(self) -> dict[str, Any]:
        """后端自省信息，用于 /healthz 与 /api/meta 暴露真实配置。"""
        ...


# ============================================================
# 本地实现：同进程直接调用（单体模式，默认）
# ============================================================
class LocalKnowledgeBackend:
    """在同一个进程里直接调用检索器。

    【为什么保留本地实现而不是全量切远程】

    1. **开发体验**：改一行检索代码不需要重启两个服务。
    2. **降级路径**：RAG 服务挂掉时，如果本进程还能自己检索，
       整个对话就不会因为一个非核心依赖而不可用。
    3. **可回退**：微服务化出问题时，改一个环境变量就能回到单体。

    "能一个变量切回单体"是微服务改造的安全网 ——
    没有这条退路，一次线上故障就只能靠回滚代码。
    """

    def __init__(self, settings: Settings | None = None, retriever: Any | None = None) -> None:
        self._settings = settings or get_settings()
        # 可注入检索器：测试可以塞合成语料构建的实例，从而不碰文件系统、
        # 也不受进程内共享单例的影响。**依赖注入是拆服务后
        # 让实现保持可测的唯一手段** —— 否则测远程实现就得真起一个服务。
        self._injected = retriever

    def _retriever(self):  # type: ignore[no-untyped-def]
        if self._injected is not None:
            return self._injected
        try:
            return get_shared_retriever(self._settings)
        except Exception as exc:
            raise KnowledgeBackendError(f"构建本进程检索索引失败：{exc}") from exc

    async def context(
        self,
        query: str,
        *,
        k: int = 4,
        doc_types: list[DocType | str] | None = None,
        min_score: float = 0.0,
        max_chars: int = 3000,
    ) -> str:
        retriever = self._retriever()
        # 本地实现里"空语料"可以直接看出来，于是就地转成语义明确的异常。
        # 关键点是：**调用方看到的异常类型与远程实现完全一致**。
        if len(retriever.chunks) == 0:
            raise EmptyKnowledgeBase("知识库为空，未建立索引。")
        return await retriever.aretrieve_context(
            query, k=k, doc_types=doc_types, min_score=min_score, max_chars=max_chars
        )

    async def stats(self) -> dict[str, Any]:
        try:
            retriever = self._retriever()
        except KnowledgeBackendError as exc:
            return {"backend": "local", "available": False, "error": str(exc)}
        s = retriever.stats()
        return {"backend": "local", "available": True, **s}


# ============================================================
# 远程实现：通过 HTTP 调用独立的 RAG 服务
# ============================================================
class RemoteKnowledgeBackend:
    """通过 HTTP 调用独立的 RAG 检索服务。

    【超时为什么必须显式设置，而且不能太长】
    httpx 默认**没有超时**（或者说是不限时等待）。这意味着 RAG 服务
    一旦卡死（比如在重建大索引），调用方会**无限期挂住** ——
    表现为"用户体验是页面永远转圈"，比直接报错糟糕得多。

    设置一个明确的超时，就是选择"宁可失败也不要静默挂起"。
    这个值要比平均检索耗时高一个数量级（正常几十毫秒，给 15 秒），
    否则会把偶发的慢查询误判为故障。

    【为什么没有重试】
    检索是**幂等只读**的，重试听起来安全。但这里不重试的理由是：
    RAG 服务慢通常意味着它已经过载，重试会**放大过载**（重试风暴）。
    真正的处理方式是快速失败 + 让上游降级。
    只有在"瞬时抖动"是主要故障模式时，重试才划算 ——
    对内存计算型服务，过载才是主要模式。
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
        breaker: CircuitBreaker | None = _DEFAULT_BREAKER,  # type: ignore[assignment]
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # 可注入客户端：测试用 ASGITransport 直连 RAG 服务的 ASGI app，
        # 从而**在不起进程、不占端口的前提下真实走一遍 HTTP 协议**。
        # 这是唯一能验证"客户端与服务端对协议的理解一致"的办法 ——
        # 用假的返回值去测，只能验证客户端自己编的那套约定。
        self._client: httpx.AsyncClient | None = client
        # 熔断器默认**存在**，因为它是防级联故障的必需件而不是可选优化：
        # 没有它，RAG 挂掉时每个检索请求都要白等满 timeout，
        # agent 的待处理请求越堆越多 —— 一个非核心依赖的故障
        # 就这样传染成整个对话服务不可用。详见 core/resilience.py。
        #
        # 传 `None` 可以显式关闭（调试用）。这里必须用哨兵值区分
        # "没传参数"和"显式传了 None" —— 若默认值就是 None，
        # 那么"忘记传"和"故意关闭"会变成同一个行为，
        # 而前者是本该有熔断却没有，属于安全默认值被破坏。
        self._breaker = (
            CircuitBreaker(f"rag:{self._base_url}") if breaker is _DEFAULT_BREAKER else breaker
        )

    def _get_client(self) -> httpx.AsyncClient:
        """惰性创建客户端。

        【为什么要复用客户端而不是每次 new 一个】
        httpx.AsyncClient 内部维护连接池。每次请求新建客户端意味着
        **每次都要重新握手（TCP + TLS）**，在高频调用下延迟和端口
        消耗都会明显上升。这是跨进程调用最容易忽视的性能陷阱：
        把本地函数调用换成 HTTP 之后，延迟从微秒变成毫秒，
        而连接复用是这个量级差里最容易拿回来的一部分。

        【`trust_env=False` 不是优化，是修一个会给出错误诊断的 bug】

        httpx 默认 `trust_env=True`，会去读**系统级**代理配置。在 Windows 上
        那意味着注册表里的 `ProxyEnable/ProxyServer` —— 实测（开着本地代理时）：

            urllib.request.getproxies() -> {'http': 'http://127.0.0.1:7890', ...}
            连 http://127.0.0.1:1（本机一个没人监听的端口）
              默认 trust_env=True : httpx.ReadTimeout  ← 请求被塞给了代理
              trust_env=False     : httpx.ConnectTimeout ← 正确：直连被拒

        两种异常在这段代码里走的是**不同的分支、给出不同的结论**：
        `ReadTimeout` 被归类成"服务可能过载"，于是运维会去查 RAG 服务为什么慢，
        而真相是这个请求根本没到 RAG 服务那里 —— 它去了桌面代理。

        这不是配置问题而是**边界问题**：RAG 服务是部署内部的依赖
        （compose 里它是 `http://rag:8001`），内部调用不该经过用户桌面的代理。
        （对外部 API 的调用不这么做 —— 那里的代理往往是用户**需要**的，
        见 `llm/client.py` 的说明。）
        """
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={"User-Agent": "legacy-agent/0.1"},
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def context(
        self,
        query: str,
        *,
        k: int = 4,
        doc_types: list[DocType | str] | None = None,
        min_score: float = 0.0,
        max_chars: int = 3000,
    ) -> str:
        payload = {
            "query": query,
            "k": k,
            "doc_types": [str(t) for t in doc_types] if doc_types else None,
            "min_score": min_score,
            "max_chars": max_chars,
        }
        # 【这一行是熔断器最容易写错的地方】
        # `count_as_failure` 明确把 EmptyKnowledgeBase 排除在故障之外。
        #
        # 如果不过滤，后果是：一个"语料还没准备好"的部署，
        # 前 5 次检索（每次都正常返回 503）会把熔断器打开，
        # 之后所有请求都报"知识库服务不可用" ——
        # 而服务其实完全健康，只是没有数据。
        # 运维会去查服务为什么挂了，方向从一开始就错了。
        #
        # **故障（fault）与业务状态（state）必须分开统计。**
        if self._breaker is None:
            # 显式关闭熔断（调试用）。这里保留一条探通的路径而不是
            # 把开关藏在调用方，是为了让"有没有熔断"在代码里看得见。
            return await self._request_context(payload)
        return await self._breaker.call(
            lambda: self._request_context(payload),
            count_as_failure=lambda exc: not isinstance(exc, EmptyKnowledgeBase),
        )

    async def _request_context(self, payload: dict[str, Any]) -> str:
        from app.core.telemetry import get_trace_id

        try:
            resp = await self._get_client().post(
                "/context",
                json=payload,
                # 把当前 trace id 透传下去：网关 → agent → rag 三段能串成
                # 一条链路，靠的就是每个服务都转发这个头。
                headers={"X-Trace-Id": get_trace_id()},
            )
        # 【异常分支的顺序在这里至关重要，写反了会给出误导性的诊断】
        #
        # `httpx.ConnectTimeout` **同时继承** TimeoutException 和 TransportError，
        # 所以它是一个"连接失败"而不是"服务太慢"。如果先捕获 TimeoutException，
        # 连不上服务时就会报成"RAG 服务超时" —— 运维会去查服务为什么慢，
        # 而真正该做的是去查服务为什么没起来。**两者是完全不同的排查方向。**
        #
        # 这个坑很隐蔽：代码能跑、异常也被处理了，只有提示信息是错的。
        # 兜底原则是 —— **先捕获最具体的异常，再捕获它的父类。**
        except httpx.ConnectTimeout as exc:
            raise KnowledgeBackendError(
                f"连接 RAG 服务超时（地址可能不通）：{self._base_url}"
            ) from exc
        except httpx.ConnectError as exc:
            raise KnowledgeBackendError(f"无法连接 RAG 服务 {self._base_url}：{exc}") from exc
        except httpx.TimeoutException as exc:
            # 走到这里说明连接已建立、只是响应慢 —— 典型的服务过载
            raise KnowledgeBackendError(
                f"RAG 服务响应超时（>{self._timeout}s，服务可能过载）：{self._base_url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise KnowledgeBackendError(
                f"无法连接 RAG 服务 {self._base_url}：{type(exc).__name__}: {exc}"
            ) from exc

        if resp.status_code == 503:
            # 服务用 503 明确表达"知识库为空"。**必须与原样识别这个状态**，
            # 不能笼统地当成失败 —— 这正是接口设计要传达的语义。
            raise EmptyKnowledgeBase(resp.json().get("detail", "知识库为空。"))
        if resp.status_code >= 400:
            raise KnowledgeBackendError(f"RAG 服务返回 {resp.status_code}：{resp.text[:200]}")

        data = resp.json()
        return str(data.get("context", ""))

    @property
    def breaker(self) -> CircuitBreaker | None:
        """暴露熔断器状态，供 /healthz 与 /api/meta 观测。

        **熔断器的状态必须可见**：它打开时表现为"检索功能莫名其妙不工作"，
        如果没有地方能看到"它现在是 open、还有 12 秒恢复"，
        排查会从"检索为什么没结果"这个完全错误的方向开始。
        """
        return self._breaker

    async def stats(self) -> dict[str, Any]:
        try:
            resp = await self._get_client().get("/healthz")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            return {
                "backend": "remote",
                "available": False,
                "url": self._base_url,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {"backend": "remote", "url": self._base_url, "available": True, **data}


# ============================================================
# 工厂
# ============================================================
def describe_knowledge_backend(settings: Settings | None = None) -> dict[str, Any]:
    """返回当前检索后端的**配置拓扑**（不发起任何网络请求）。

    【为什么健康检查里只报配置、不去探活】
    这是健康检查最容易犯的错误：让 `/healthz` 去 ping 下游依赖。

    后果是**级联故障**：RAG 服务变慢 → agent 的 /healthz 超时 →
    编排系统认为 agent 副本不健康 → 重启/摘除所有 agent 副本 →
    原本只是检索降级，现在整个对话服务全挂。

    健康检查要回答的是"**我这个进程本身还能不能干活**"，
    而不是"我所有的依赖现在都好吗"。后者属于就绪/依赖探针，
    应该单独一个端点、单独一套超时策略。

    所以这里只读配置：它永远快速、永远可用，
    而且恰好能暴露最常见的那类错误 —— **配置压根没生效**。
    """
    s = settings or get_settings()
    url = s.rag_service_url.strip()
    if url:
        return {"backend": "remote", "url": url, "timeout": s.rag_service_timeout}
    return {"backend": "local"}


def build_knowledge_backend(
    settings: Settings | None = None, url: str | None = None
) -> KnowledgeBackend:
    """按配置选择本地或远程实现。

    `RAG_SERVICE_URL` 为空 → 本地（单体，默认）。
    非空 → 远程（拆分为独立服务）。

    【为什么默认是本地】
    让"没配置"等价于"单体可用"。如果默认连远程，那么一个没读过文档
    的人在本地起服务就会得到"连接 127.0.0.1:8001 失败" ——
    一个纯粹由默认值造成的、毫无信息量的报错。
    **默认值应当让最常见的场景零配置可用。**
    """
    s = settings or get_settings()
    target = (url if url is not None else s.rag_service_url).strip()
    if target:
        logger.info("使用远程 RAG 服务：%s", target)
        rc = s.resilience
        # 熔断器在这里构造（而不是在类默认值里），是为了让阈值可配置。
        # 关掉熔断需要显式配置 —— 默认必须是开的，见 ResilienceSettings 的说明。
        breaker = (
            CircuitBreaker(
                f"rag:{target}",
                failure_threshold=rc.circuit_failure_threshold,
                recovery_timeout=rc.circuit_recovery_timeout,
                half_open_max_calls=rc.circuit_half_open_calls,
            )
            if rc.circuit_enabled
            else None
        )
        return RemoteKnowledgeBackend(target, timeout=s.rag_service_timeout, breaker=breaker)
    return LocalKnowledgeBackend(s)
