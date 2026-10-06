"""微服务拆分测试：接口契约、两种实现、以及服务端本身。

【这组测试要证明什么】
拆分服务最危险的失败模式，是**客户端和服务端各自对协议有一套理解**，
而两边都"自测通过"。比如客户端发 `{query, top_k}`、服务端读 `k` ——
服务端测试（直接构造请求）全绿，客户端测试（用 mock 返回值）也全绿，
只有真正联调时才 422。

所以这里最关键的用例是 `TestContractAgainstRealServer`：
它用 ASGITransport 让真实的客户端代码去调用**真实的**服务端 ASGI app。
不起进程、不占端口，但完整走了一遍 HTTP 序列化与状态码 ——
这是单体测试与线上联调之间的那个缺口。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app.core.config import RagSettings, Settings, get_settings
from app.rag.backend import (
    EmptyKnowledgeBase,
    KnowledgeBackendError,
    LocalKnowledgeBackend,
    RemoteKnowledgeBackend,
    build_knowledge_backend,
    describe_knowledge_backend,
)
from app.rag.chunker import ChunkStrategy
from app.rag.embedder import TfidfEmbedder
from app.rag.loaders import DocType, LoadedDocument
from app.rag.retriever import RetrievalMode, Retriever
from app.tools.knowledge import KnowledgeSearchTool, SearchKnowledgeParams


def _corpus() -> list[LoadedDocument]:
    return [
        LoadedDocument(
            source="resume.md",
            doc_type=DocType.RESUME,
            text=(
                "专业技能\n熟练掌握 Kafka、Flink、ClickHouse，"
                "有 Redisson 分布式锁使用经验\n\n"
                "实习经历\n某科技公司 全栈开发工程师，负责缓存一致性方案"
            ),
        ),
    ]


def _retriever() -> Retriever:
    return Retriever.from_documents(
        _corpus(), strategy=ChunkStrategy.SECTION, min_size=0, mode=RetrievalMode.HYBRID
    )


def _empty_retriever() -> Retriever:
    emb = TfidfEmbedder()
    emb.fit(["占位内容"])
    return Retriever([], emb)


# ============================================================
# 1. 拓扑描述（配置驱动，无 I/O）
# ============================================================
class TestDescribeBackend:
    def test_empty_url_means_local(self) -> None:
        s = get_settings().model_copy(update={"rag_service_url": ""})
        assert describe_knowledge_backend(s)["backend"] == "local"

    def test_url_means_remote(self) -> None:
        s = get_settings().model_copy(update={"rag_service_url": "http://rag:8001"})
        info = describe_knowledge_backend(s)
        assert info["backend"] == "remote"
        assert info["url"] == "http://rag:8001"

    def test_whitespace_url_is_local(self) -> None:
        """只有空白的地址等同于没配置 —— 否则会得到一个连不上的 URL。"""
        s = get_settings().model_copy(update={"rag_service_url": "   "})
        assert describe_knowledge_backend(s)["backend"] == "local"

    def test_build_selects_implementation(self) -> None:
        assert isinstance(
            build_knowledge_backend(get_settings().model_copy(update={"rag_service_url": ""})),
            LocalKnowledgeBackend,
        )
        assert isinstance(
            build_knowledge_backend(
                get_settings().model_copy(update={"rag_service_url": "http://rag:8001"})
            ),
            RemoteKnowledgeBackend,
        )


# ============================================================
# 2. 本地实现的语义
# ============================================================
class TestLocalBackend:
    async def test_returns_context(self) -> None:
        backend = LocalKnowledgeBackend(retriever=_retriever())
        ctx = await backend.context("大数据技术", k=3)
        assert any(kw in ctx for kw in ("Kafka", "Flink", "ClickHouse"))
        assert "出处" in ctx

    async def test_empty_corpus_raises_typed_error(self) -> None:
        """空语料必须抛 EmptyKnowledgeBase，而不是返回空字符串。

        这一条是**远程实现能正确工作的前提**：调用方靠异常类型区分
        "该去准备数据"和"该换个问法"，两种情况的处理方式完全不同。
        """
        backend = LocalKnowledgeBackend(retriever=_empty_retriever())
        with pytest.raises(EmptyKnowledgeBase):
            await backend.context("任何")

    async def test_stats_reports_backend(self) -> None:
        stats = await LocalKnowledgeBackend(retriever=_retriever()).stats()
        assert stats["backend"] == "local"
        assert stats["available"] is True
        assert stats["chunk_count"] > 0


# ============================================================
# 3. 契约测试：真客户端 ↔ 真服务端
# ============================================================
class TestContractAgainstRealServer:
    """用 ASGITransport 让 RemoteKnowledgeBackend 调用真实的 RAG 服务 app。

    这组用例覆盖了单元测试最容易漏掉的那一层：**协议本身的正确性**。
    字段名、状态码、JSON 结构任何一处不一致，都会在这里暴露。
    """

    @pytest.fixture
    def rag_app(self):  # type: ignore[no-untyped-def]
        from app.rag_service.main import app as rag_app

        return rag_app

    def _client(self, app, retriever: Retriever) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
        """把 RAG 服务的数据源换成本测试的合成语料。

        服务端通过 `get_shared_retriever` 懒加载语料。测试直接替换掉
        那个单例，就能让服务端处理的是合成语料 —— 不必碰 data/ 下的真实文件。
        """
        import app.rag.factory as factory

        original = factory.get_shared_retriever
        factory.get_shared_retriever = lambda *a, **kw: retriever  # type: ignore[assignment]

        import app.rag_service.main as svc

        svc.get_shared_retriever = factory.get_shared_retriever  # type: ignore[assignment]

        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://rag")

        def restore() -> None:
            factory.get_shared_retriever = original  # type: ignore[assignment]
            svc.get_shared_retriever = original  # type: ignore[assignment]

        self._restore = restore
        return client

    def setup_method(self) -> None:
        self._restore = lambda: None

    def teardown_method(self) -> None:
        self._restore()

    async def test_round_trip_returns_context(self, rag_app) -> None:  # type: ignore[no-untyped-def]
        """端到端：客户端 → HTTP → 服务端 → 检索 → 回到客户端。

        这是拆分能否成立的**唯一判据**。它一旦通过，
        说明字段名、参数语义、返回结构在两端完全对齐。
        """
        client = self._client(rag_app, _retriever())
        backend = RemoteKnowledgeBackend("http://rag", client=client)

        ctx = await backend.context("大数据技术", k=3, doc_types=[DocType.RESUME])
        assert any(kw in ctx for kw in ("Kafka", "Flink", "ClickHouse"))
        assert "出处" in ctx
        await client.aclose()

    async def test_scope_filter_crosses_the_boundary(self, rag_app) -> None:  # type: ignore[no-untyped-def]
        """枚举类型必须能跨越进程边界。

        进程内的 `DocType.RESUME` 在网络上只能是字符串。
        这个用例确保转换没有把过滤条件丢掉 —— 丢了的话检索范围会
        悄悄变成"全部文档"，答案还看起来很正常，极难发现。
        """
        client = self._client(rag_app, _retriever())
        backend = RemoteKnowledgeBackend("http://rag", client=client)

        ctx = await backend.context("Kafka", k=3, doc_types=[DocType.JD])
        # 语料里没有 JD 文档，过滤生效就必须是空
        assert ctx == ""
        await client.aclose()

    async def test_empty_corpus_maps_to_typed_error(self, rag_app) -> None:  # type: ignore[no-untyped-def]
        """服务端的 503 必须被客户端还原成 EmptyKnowledgeBase。

        这里验证的是**错误语义跨边界不丢失**：如果客户端把所有非 200
        都当成"后端故障"，用户就会看到"知识库服务不可用"，
        而实际该做的是去放一份简历 —— 方向完全错了。
        """
        client = self._client(rag_app, _empty_retriever())
        backend = RemoteKnowledgeBackend("http://rag", client=client)

        with pytest.raises(EmptyKnowledgeBase):
            await backend.context("任何")
        await client.aclose()

    async def test_min_score_gate_effective_over_http(self, rag_app) -> None:  # type: ignore[no-untyped-def]
        """相关性闸门参数必须真正传到服务端并生效。"""
        client = self._client(rag_app, _retriever())
        backend = RemoteKnowledgeBackend("http://rag", client=client)

        ctx = await backend.context(
            "外星语言量子纠缠拓扑绝缘体", k=3, min_score=0.5, max_chars=3000
        )
        assert ctx == "", "高闸门下的无关查询不该返回内容"
        await client.aclose()

    async def test_healthz_reports_corpus(self, rag_app) -> None:  # type: ignore[no-untyped-def]
        client = self._client(rag_app, _retriever())
        backend = RemoteKnowledgeBackend("http://rag", client=client)

        stats = await backend.stats()
        assert stats["backend"] == "remote"
        assert stats["available"] is True
        assert stats["chunks"] > 0
        await client.aclose()


# ============================================================
# 4. 远程实现的故障行为
# ============================================================
class TestRemoteFailureModes:
    """网络边界引入的失败面：超时、连不上、5xx。

    这些在单体模式下**根本不存在**，是拆服务实打实付出的代价。
    必须逐个验证它们被转成了有意义的异常，而不是让调用方看到
    一个原始的 httpx 异常（那样工具层没法给出可操作提示）。
    """

    async def test_connect_error_becomes_backend_error(self) -> None:
        """连不上与"响应慢"必须给出**不同**的诊断。

        这两者对运维的含义完全不同：前者去查"服务是不是没起来"，
        后者去查"服务是不是过载了"。报错信息混在一起会让人查错方向。

        httpx 里 `ConnectTimeout` 同时继承 TimeoutException 与 TransportError，
        所以异常分支的顺序是有语义的 —— 这个用例把它固定下来。

        【这条用例真的抓到过东西 —— 值得记下来】

        它原本只接受"无法连接"。某天开始稳定失败，报的是"响应超时（服务可能过载）"。
        查下去发现两件事叠在一起：

          1. 这台机器上环回地址的"连接被拒"要 **2.04 秒**才返回（系统代理在跑）；
          2. 更关键的是 `httpx` 默认 `trust_env=True`，会读**系统级**代理配置 ——
             于是连往 `127.0.0.1:1` 的请求被塞给了桌面代理，拿到的是
             `ReadTimeout`（代理接了连接然后干等），而代码把它归类成"服务过载"。

        也就是说：**这条断言防的正是"给出误导性诊断"**，而它守住了
        （尽管当时的失败看起来像"测试太脆"）。修的是实现：
        `RemoteKnowledgeBackend` 现在用 `trust_env=False` ——
        内部服务调用不该经过用户桌面的代理。

        （那个 2.04 秒同时解释了一个更早的悬案：Redis 探测为什么恰好也是
        2.04 秒、而且"关掉重试"没用 —— 那两秒是**建连本身**，不是重试。）
        """
        backend = RemoteKnowledgeBackend("http://127.0.0.1:1", timeout=1.0)
        with pytest.raises(KnowledgeBackendError) as ei:
            await backend.context("任何")
        msg = str(ei.value)
        assert "无法连接" in msg or "连接 RAG 服务超时" in msg
        # 不能报成"服务响应超时（服务可能过载）"——那会指向错误的排查方向
        assert "服务可能过载" not in msg
        await backend.aclose()

    async def test_slow_response_reported_as_overload(self) -> None:
        """已建立连接但响应慢 → 报"过载"，与连不上区分开。"""

        async def _slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("模拟读取超时", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(_slow), base_url="http://rag")
        backend = RemoteKnowledgeBackend("http://rag", timeout=0.01, client=client)
        with pytest.raises(KnowledgeBackendError, match="服务可能过载"):
            await backend.context("任何")
        await client.aclose()

    async def test_server_error_becomes_backend_error(self) -> None:
        async def _boom(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="内部错误", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(_boom), base_url="http://rag")
        backend = RemoteKnowledgeBackend("http://rag", client=client)
        with pytest.raises(KnowledgeBackendError, match="500"):
            await backend.context("任何")
        await client.aclose()

    async def test_stats_degrades_instead_of_raising(self) -> None:
        """stats 是自省用的，后端挂了也要能返回结果。

        否则 /healthz 会因为下游不可用而失败 —— 正是我们
        在 describe_knowledge_backend 里刻意避免的级联故障。
        """
        backend = RemoteKnowledgeBackend("http://127.0.0.1:1", timeout=1.0)
        stats = await backend.stats()
        assert stats["available"] is False
        assert stats["backend"] == "remote"


# ============================================================
# 5. 工具层：接口抽象后，工具完全不关心实现在哪
# ============================================================
class TestToolIsBackendAgnostic:
    async def test_tool_works_with_remote_backend(self) -> None:
        """工具层只依赖接口 —— 换实现不需要改工具一行代码。"""
        import app.rag.factory as factory
        import app.rag_service.main as svc
        from app.rag_service.main import app as rag_app

        original = factory.get_shared_retriever
        factory.get_shared_retriever = lambda *a, **kw: _retriever()  # type: ignore[assignment]
        svc.get_shared_retriever = factory.get_shared_retriever  # type: ignore[assignment]
        try:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=rag_app), base_url="http://rag"
            )
            tool = KnowledgeSearchTool(backend=RemoteKnowledgeBackend("http://rag", client=client))
            result = await tool.run(SearchKnowledgeParams(query="用了哪些大数据技术"))
            assert result.ok
            assert any(kw in result.content for kw in ("Kafka", "Flink", "ClickHouse"))
            await client.aclose()
        finally:
            factory.get_shared_retriever = original  # type: ignore[assignment]
            svc.get_shared_retriever = original  # type: ignore[assignment]

    async def test_backend_down_gives_actionable_message(self) -> None:
        """后端不可用时的提示必须指向**真正的原因**。

        只说"检索失败"会让人以为知识库里没内容，于是去反复换问法 ——
        而实际问题是服务没起来。
        """
        tool = KnowledgeSearchTool(
            backend=RemoteKnowledgeBackend("http://127.0.0.1:1", timeout=1.0)
        )
        result = await tool.run(SearchKnowledgeParams(query="任何"))
        assert not result.ok
        assert "不可用" in result.content
        assert "RAG 服务" in result.content


# ============================================================
# 6. 配置防护：把静默失效挡在启动阶段
# ============================================================
class TestConfigGuards:
    async def test_memory_backend_without_api_workers_is_rejected(self) -> None:
        """`进程内队列 + API 不启动 worker` 必须启动失败。

        这个组合下接口会返回 202、任务永远停在 pending，而服务完全健康 ——
        是最难排查的一类故障。宁可启动失败，也不要上线一个静默失效的服务。
        """
        from app.tasks.factory import build_task_queue

        settings = get_settings().model_copy(
            update={
                "tasks": get_settings().tasks.model_copy(
                    update={"backend": "memory", "run_workers_in_api": False}
                )
            }
        )
        with pytest.raises(RuntimeError, match="永远无人消费"):
            await build_task_queue(settings, autostart=True)

    async def test_single_mode_still_starts_workers(self) -> None:
        """单体模式（默认）必须照常启动 worker，否则任务没人执行。"""
        from app.tasks.factory import build_task_queue

        settings = get_settings().model_copy(
            update={"tasks": get_settings().tasks.model_copy(update={"backend": "memory"})}
        )
        queue = await build_task_queue(settings, autostart=True)
        assert queue.backend == "memory"
        await queue.aclose()

    def test_settings_defaults_are_single_process(self) -> None:
        """默认配置必须是"单体可用"，否则零配置启动会得到一个连不上的地址。"""
        s = Settings()
        assert s.rag_service_url == ""
        assert s.tasks.run_workers_in_api is True

    def test_rag_gate_default_off(self) -> None:
        assert RagSettings().min_score == 0.0


# ============================================================
# 7. 部署编排的拓扑自洽性
# ============================================================
def _app_host_mismatches(compose: dict) -> list[str]:  # type: ignore[type-arg]
    """找出"启动命令绑定的地址"与 "APP_HOST" 不一致的服务。

    抽成模块级函数是为了**能对它自己的反例写测试** —— 见
    `test_the_check_itself_catches_a_mismatch`。一条永远为真的断言
    比没有断言更糟（它给的是虚假的安全感）。
    """
    bad: list[str] = []
    for name, service in compose.get("services", {}).items():
        cmd = service.get("command")
        if not cmd:
            # 没覆盖 command 的服务用镜像默认 CMD —— 那个值在 Dockerfile 里，
            # 由 TestDockerArtifacts 单独核对（它同样是 0.0.0.0）。
            continue
        parts = [str(c) for c in cmd] if isinstance(cmd, list) else str(cmd).split()
        if "--host" not in parts:
            continue
        bound = parts[parts.index("--host") + 1]
        declared = str((service.get("environment") or {}).get("APP_HOST", "")).strip()
        if declared != bound:
            bad.append(f"{name}: 绑定 {bound} 但 APP_HOST={declared!r}")
    return bad


class TestComposeTopology:
    """校验 docker-compose.yml 里那些**必须成对出现**的配置。

    【为什么这个测试值得写】
    拆服务的绝大部分线上事故不是代码错，而是配置组合不自洽：
    服务能起来、日志正常、接口 200，只是拓扑和你以为的不一样。

    这类错误在代码里没有任何痕迹，普通的单元测试永远测不到 ——
    它们只存在于编排文件里。所以检查也必须落在编排文件上。

    典型的两条：
      · api 设了 TASK_RUN_WORKERS_IN_API=false，但 compose 里忘了起 worker
        → 任务永远 pending
      · RAG_SERVICE_URL 写的是 rag:8001，但服务名/端口对不上
        → 启动时不报错（连接是惰性的），第一次检索才失败
    """

    @pytest.fixture(scope="class")
    def compose(self) -> dict:  # type: ignore[type-arg]
        import yaml

        root = Path(__file__).resolve().parents[3]
        path = root / "docker-compose.yml"
        assert path.exists(), f"找不到 {path}"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), "compose 文件解析结果不是映射"
        return data

    def _env(self, compose: dict, service: str) -> dict:  # type: ignore[type-arg]
        return compose["services"][service].get("environment", {}) or {}

    def test_expected_services_exist(self, compose: dict) -> None:  # type: ignore[type-arg]
        assert set(compose["services"]) >= {"redis", "rag", "api", "worker"}

    def test_rag_service_url_points_at_real_service(self, compose: dict) -> None:  # type: ignore[type-arg]
        """RAG_SERVICE_URL 的主机名必须是 compose 里**真实存在**的服务名。

        写错的代价很隐蔽：连接是惰性的，启动时完全不报错，
        直到用户第一次提问触发检索才失败 —— 而此时容器状态全是 healthy。
        """
        from urllib.parse import urlparse

        url = self._env(compose, "api")["RAG_SERVICE_URL"]
        parsed = urlparse(url)
        assert parsed.scheme == "http", f"RAG_SERVICE_URL 必须是 http：{url}"
        assert parsed.hostname in compose["services"], (
            f"RAG_SERVICE_URL 指向 {parsed.hostname}，但它不是 compose 里的服务"
        )

        # 端口要和 rag 服务的启动命令一致
        rag_cmd = compose["services"]["rag"]["command"]
        assert str(parsed.port) in [str(c) for c in rag_cmd], (
            f"RAG_SERVICE_URL 的端口 {parsed.port} 与 rag 的启动命令 {rag_cmd} 不一致"
        )

    def test_api_does_not_run_workers_when_worker_service_exists(self, compose: dict) -> None:  # type: ignore[type-arg]
        """api 与 worker 的消费关系必须成对。

        两个都消费 → 任务被执行两次（对于"发送通知"这类副作用是灾难）；
        都不消费   → 任务永远 pending（build_task_queue 已会拦截，但编排层也该自洽）。
        """
        api_env = self._env(compose, "api")
        assert str(api_env.get("TASK_RUN_WORKERS_IN_API", "true")).lower() == "false", (
            "compose 里有独立 worker 服务，api 必须设 TASK_RUN_WORKERS_IN_API=false，"
            "否则任务会被两个进程重复消费"
        )
        assert "worker" in compose["services"]

    def test_worker_uses_shared_queue(self, compose: dict) -> None:  # type: ignore[type-arg]
        """worker 与 api 必须用同一个跨进程队列。

        worker 是独立进程，进程内队列它收不到 —— 这正是
        app/worker_main.py 会在 backend != redis 时直接退出的原因。
        """
        assert self._env(compose, "worker").get("TASK_BACKEND") == "redis"
        assert self._env(compose, "api").get("TASK_BACKEND") == "redis"

    def test_sessions_are_shared_across_replicas(self, compose: dict) -> None:  # type: ignore[type-arg]
        """api 的会话后端不能是 memory —— 多副本下会丢历史。"""
        assert self._env(compose, "api").get("SESSION_BACKEND") == "redis"

    def test_all_services_share_same_corpus_volume(self, compose: dict) -> None:  # type: ignore[type-arg]
        """三个服务必须挂同一份 data/。

        否则会出现"agent 读到 A 版本简历、RAG 服务索引的是 B 版本"这种错位 ——
        答案与出处对不上，而且完全没有报错。
        """
        for svc in ("api", "rag", "worker"):
            mounts = compose["services"][svc].get("volumes", [])
            assert any("/app/data" in str(m) for m in mounts), f"{svc} 没有挂载 data/"

    def test_worker_does_not_publish_ports(self, compose: dict) -> None:  # type: ignore[type-arg]
        """worker 不该对外暴露端口：它没有 HTTP 接口，暴露只是扩大攻击面。"""
        assert not compose["services"]["worker"].get("ports")

    def test_rag_does_not_publish_ports(self, compose: dict) -> None:  # type: ignore[type-arg]
        """rag 也不能发布端口 —— **它没有访问控制**。

        鉴权中间件只挂在 agent 服务上，而 rag 服务能读到语料内容。
        把它 publish 出去等于开了一个任何人都能打的、无鉴权的检索接口。
        要让它在 compose 网络之外可达，正确的顺序是**先给它加鉴权**。
        """
        assert not compose["services"]["rag"].get("ports"), (
            "rag 没有鉴权，不能发布端口；要暴露请先加访问控制"
        )

    def test_app_host_matches_the_host_uvicorn_binds(self, compose: dict) -> None:  # type: ignore[type-arg]
        """启动命令里的 `--host` 必须与 `APP_HOST` 一致。

        【这条断言守的是一个"说反话"的事故】
        访问控制检查（app/api/auth.py 的 check_exposure_posture）判定
        "是否对外暴露"依据的是 **APP_HOST**，而真正决定监听地址的是
        uvicorn 的 `--host`。两者不一致时不会报任何错，只会让启动日志写下
        一句与事实相反的话：

            访问控制：仅监听回环地址，未启用密钥鉴权（本地开发默认形态）

        —— 而容器其实监听在 0.0.0.0 上。**一个说反了的结论比没有结论危险得多。**
        """
        bad = _app_host_mismatches(compose)
        assert not bad, "启动命令的 --host 与 APP_HOST 不一致：\n  " + "\n  ".join(bad)

        # 反证：这条检查本身必须会红（否则它可能只是"恰好没有服务声明 --host"）
        fake = {
            "services": {
                "api": {
                    "command": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
                    "environment": {"APP_HOST": "127.0.0.1"},
                }
            }
        }
        assert _app_host_mismatches(fake), "检查没能发现明显的不一致 —— 它没有在起作用"

    def test_the_check_itself_catches_a_mismatch(self) -> None:
        """上一条的对照组：@code 0.0.0.0 vs 声明 127.0.0.1 必须被判为不一致。

        【为什么单独写一条而不是混在上一条里】
        上一条依赖真实的 compose 文件内容；如果哪天有人给 rag 去掉 `--host`、
        这条检查就会变成"遍历零个服务然后通过"。单独一条对照组把
        "检查逻辑本身有效"这件事独立钉住。
        """
        mismatch = _app_host_mismatches(
            {
                "services": {
                    "svc": {
                        "command": "uvicorn x:app --host 0.0.0.0 --port 1",
                        "environment": {"APP_HOST": "127.0.0.1"},
                    }
                }
            }
        )
        assert mismatch, "字符串形式的 command 也必须被解析"

        ok = _app_host_mismatches(
            {
                "services": {
                    "svc": {
                        "command": ["uvicorn", "x:app", "--host", "0.0.0.0"],
                        "environment": {"APP_HOST": "0.0.0.0"},
                    }
                }
            }
        )
        assert not ok

    def test_exposed_services_require_a_key(self, compose: dict) -> None:  # type: ignore[type-arg]
        """发布了端口的服务必须要求访问密钥（除非显式认领风险）。

        容器里 APP_HOST=0.0.0.0，所以 `check_exposure_posture` 会要求
        SECURITY_API_KEY 或 SECURITY_ALLOW_UNAUTHENTICATED_EXPOSURE ——
        编排层应当把同一件事表达出来，而不是让用户撞上启动失败才发现。
        """
        for name, service in compose["services"].items():
            if not service.get("ports"):
                continue
            env = self._env(compose, name)
            has_key = "SECURITY_API_KEY" in env
            opted_out = "SECURITY_ALLOW_UNAUTHENTICATED_EXPOSURE" in env
            assert has_key or opted_out, (
                f"{name} 发布了端口，却没有 SECURITY_API_KEY（也没有显式豁免）"
            )

    def test_dependencies_gate_on_health_not_just_start(self, compose: dict) -> None:  # type: ignore[type-arg]
        """依赖必须是 service_healthy，不能只是 service_started。

        Redis 没就绪时 api 会连接失败 → 降级到内存后端 →
        多副本共享**静默失效**。这正是依赖健康检查要防的事。
        """
        rag = compose["services"]["api"]["depends_on"]["rag"]
        assert rag["condition"] == "service_healthy", (
            "api 必须等 rag 健康后再启动，否则首个请求会撞上未就绪的检索服务"
        )


class TestDockerArtifacts:
    def test_dockerignore_excludes_secrets_and_pii(self) -> None:
        """构建上下文必须排除 .env 与 data/。

        data/ 里是真实简历，属于个人信息：
        它必须是**挂载**进容器的运行时数据，而不是打包进镜像的内容。
        这一条同时是隐私要求与工程要求。
        """
        root = Path(__file__).resolve().parents[3]
        text = (root / ".dockerignore").read_text(encoding="utf-8")
        lines = {ln.strip() for ln in text.splitlines() if ln.strip()}
        assert ".env" in lines
        assert "data" in lines
        assert ".venv" in lines

    def test_dockerfile_runs_as_non_root(self) -> None:
        root = Path(__file__).resolve().parents[3]
        text = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
        assert "USER appuser" in text, "容器不该以 root 运行"
        assert "useradd" in text

    def test_dockerfile_declares_all_three_entrypoints(self) -> None:
        """镜像必须能跑三种角色，否则拆分部署要靠三个几乎相同的 Dockerfile。"""
        root = Path(__file__).resolve().parents[3]
        text = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        # rag 与 worker 在 compose 里覆盖 command，api 用镜像默认 CMD
        assert "app.rag_service.main:app" in compose
        assert "app.worker_main" in compose
        assert "app.main:app" in text

    # ---------- T22：前端产物必须进镜像 ----------
    def test_image_builds_and_ships_the_frontend(self) -> None:
        """镜像里必须有前端构建阶段，并把产物复制进去。

        【为什么这条能防住"部署成功但界面 404"】
        前端产物缺失时 mount_frontend **不会报错**（它允许缺失，因为开发时
        前端跑在 Vite 里）。于是镜像少一步 COPY 的后果是：容器 healthy、
        接口 200、界面 404 —— 没有任何一处日志会说不对。
        所以"镜像里有没有 dist"必须由测试盯住，而不能指望启动日志。
        """
        root = Path(__file__).resolve().parents[3]
        text = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")

        assert "AS web" in text, "缺少前端构建阶段"
        assert "pnpm" in text and "build" in text, "前端阶段没有真的执行构建"
        assert "COPY --from=web" in text, "前端产物没有被复制进运行镜像"
        assert "apps/web/dist" in text, "复制目标与 WEB_DIST 的约定不符"

    def test_web_dist_is_declared_where_it_matters(self) -> None:
        """镜像里必须显式告诉应用去哪找前端产物（靠路径推断在镜像里会落空）。"""
        root = Path(__file__).resolve().parents[3]
        dockerfile = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")

        assert "ENV WEB_DIST=" in dockerfile
        assert "WEB_DIST" in compose, "compose 里没有把 WEB_DIST 传给 api 服务"

    def test_dependency_filter_uses_the_real_project_name(self) -> None:
        """过滤自身依赖时按 pyproject 里的项目名，而不是写死一个会过期的名字。

        【真实踩过的坑】原来写的是 `d.startswith("jobpilot")`，
        项目改名成 legacy-api 之后这句话永远为真（没有任何依赖以它开头）——
        它不会报错，只是在某天 pyproject 出现自引用时，让 pip 去 PyPI
        找一个不存在的包。
        """
        root = Path(__file__).resolve().parents[3]
        text = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
        # 只看**代码**：注释里提到旧名字是有价值的（它记录了那个坑），
        # 而"检查把解释性注释也当成违规"会让下一个人删掉注释而不是修代码。
        code = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
        assert "jobpilot" not in code.lower(), "还留着旧项目名，说明过滤逻辑是写死的"
        # 项目名必须是从 pyproject 里读出来的（写法可以是 p.get("name") 或 ["name"]）
        assert 'get("name"' in code or '["name"]' in code, "应当从 pyproject 读取项目名来过滤"
        assert "pyproject.toml" in code, "过滤的依据应当来自 pyproject.toml"


class TestPowerShellEncoding:
    """`scripts/*.ps1` 必须带 UTF-8 BOM。

    【为什么这个看似无关的检查值得成为一个测试】
    本机的 PowerShell 是 **5.1**：它只在见到 UTF-8 BOM 时才按 UTF-8 解码
    `.ps1`，否则按 ANSI（这里 GBK）解码。

    GBK 解码 UTF-8 中文时有个隐蔽的"字节吞并"效应：中文字 3 字节，
    GBK 吃掉前两个，剩下尾字节（0x80-0xBF）单独成字 —— 而它在 GBK 里
    是合法首字节，于是继续吞掉**后面那个字节**（常常是换行或引号）。

    结果是行号错位、引号断开、语法树崩溃。最坑的是**报错位置指向的行
    看起来完全正常**（可能只是一句注释），排查方向被彻底带偏；
    而只要不动那个文件它就一直是"好的"—— 直到某次改一行注释突然失败。

    这件事真实发生过：给 dev.ps1 加了几个函数之后，
    `Parser::ParseFile` 报 "line 50: Array index expression is missing"，
    而当前文件第 50 行只是一句中文注释。根因就是这个 BOM。
    """

    def test_ps1_files_have_utf8_bom(self) -> None:
        root = Path(__file__).resolve().parents[3]
        files = list((root / "scripts").rglob("*.ps1"))
        assert files, "没有找到任何 .ps1 脚本"
        missing = [p.name for p in files if not p.read_bytes().startswith(b"\xef\xbb\xbf")]
        assert not missing, (
            f"这些 .ps1 缺少 UTF-8 BOM，PowerShell 5.1 会按 GBK 解码并可能解析失败："
            f"{missing}。修复：python scripts/fix_ps1_bom.py（或用 scripts\\fix-bom.cmd）"
        )

    def test_cmd_files_are_ascii_only(self) -> None:
        """`.cmd` / `.bat` 必须**纯 ASCII**。

        【为什么这条规则不是洁癖，而是必需】
        cmd.exe 和 PowerShell 5.1 一样按系统 ANSI 代码页解码脚本。
        一个 UTF-8 编码、带中文注释的 `.cmd` 会被解成乱码，
        而乱码片段会被 cmd.exe **当成命令去执行** —— 实测输出几百行
        `'xx），' is not recognized as an internal or external command` 然后卡死。

        【为什么这一条比 .ps1 的 BOM 规则更严格】
        `scripts/fix-bom.cmd` 是"`dev.ps1` 已经坏掉时**唯一**的修复入口"。
        它自己**不能有和它要修复的问题同源的脆弱性** ——
        否则两者会一起坏，没有任何东西能救它。

        所以规则是"纯 ASCII"而不是"也加 BOM"：cmd.exe 对 UTF-8 BOM 的
        支持在各版本 Windows 上并不一致，而 **ASCII 在任何代码页下
        解码结果都相同**。一个救援工具应该尽量少依赖环境特性。

        （这条规则是被一次真实失败逼出来的：第一版 fix-bom.cmd 带中文注释，
        执行时爆炸成几百行 "is not recognized as an internal or external command"。）
        """
        root = Path(__file__).resolve().parents[3]
        files = [p for p in (root / "scripts").rglob("*") if p.suffix.lower() in {".cmd", ".bat"}]
        assert files, "没有找到任何 .cmd/.bat —— 恢复脚本 fix-bom.cmd 应该存在"
        bad: list[str] = []
        for p in files:
            try:
                p.read_bytes().decode("ascii")
            except UnicodeDecodeError as exc:
                bad.append(f"{p.name}（偏移 {exc.start}）")
        assert not bad, (
            f"这些批处理文件含非 ASCII 字符，cmd.exe 会按 ANSI 解码成乱码并当作命令执行："
            f"{bad}。修复：python scripts/fix_ps1_bom.py"
        )

    def test_recovery_entrypoint_does_not_need_powershell(self) -> None:
        """恢复入口必须能在 `dev.ps1` 已损坏时运行。

        它只能用 cmd.exe + python，**不能**调用任何 .ps1 或 powershell ——
        否则就成了"用一个可能已损坏的东西去修复另一个已损坏的东西"。

        注意这里**只检查会真正执行的命令**：这个文件里有一段注释在解释
        "为什么不能依赖 PowerShell"，还有若干 `echo` 提示文本提到了
        `scripts\\dev.ps1`。直接对整个文件做字符串匹配会命中这些内容 ——
        这是写这类检查时最容易踩的假阳性，而且它会诱导人把有价值的注释
        和提示删掉（那才是真的损失）。**检查的粒度必须匹配危险的粒度。**
        """
        root = Path(__file__).resolve().parents[3]
        text = (root / "scripts" / "fix-bom.cmd").read_text(encoding="ascii")

        # 剥掉注释行（REM/::）与输出行（echo）—— 它们不执行任何东西
        skipped = ("REM", "::", "@REM", "ECHO", "@ECHO")
        code_lines = [
            ln.strip()
            for ln in text.splitlines()
            if ln.strip() and not ln.strip().upper().startswith(skipped)
        ]
        code = "\n".join(code_lines).lower()

        assert "powershell" not in code, f"恢复入口的可执行部分依赖 PowerShell：{code_lines}"
        assert "pwsh" not in code, "恢复入口的可执行部分依赖 pwsh"
        assert ".ps1" not in code, (
            f"恢复入口不能调用 .ps1 —— 那正是它要修复的东西，可能已损坏：{code_lines}"
        )
        assert "fix_ps1_bom.py" in code, "恢复入口必须调用真正的修复逻辑"

    def test_dev_ps1_exposes_split_commands(self) -> None:
        """拆分拓扑的启动命令必须能从统一入口拿到。

        否则"怎么起 RAG 服务"只存在于文档里，而文档会过期 ——
        命令行入口是不会过期的那个版本。
        """
        root = Path(__file__).resolve().parents[3]
        text = (root / "scripts" / "dev.ps1").read_text(encoding="utf-8-sig")
        for cmd in ("rag", "worker", "serve-split", "verify-split", "loadtest"):
            assert f"'{cmd}'" in text, f"dev.ps1 缺少 {cmd} 子命令"

    def test_switch_labels_are_all_in_validate_set(self) -> None:
        """`switch` 里的每个子命令都必须同时出现在 `ValidateSet` 里。

        【为什么这条测试必须存在 —— 它是由一次真实失误换来的】

        加 rag / worker / serve-split 时，我往 `switch` 里加了分支，
        也更新了文件头的用法注释，但**忘了改 `ValidateSet`**。

        `ValidateSet` 是**参数绑定阶段**执行的：不在名单里的值会在进入
        `switch` 之前就被 PowerShell 拒绝。所以 `dev.ps1 rag` 直接报
        "参数不在集合中"，功能完全不可用。

        而当时的测试只断言了"文件里出现了 'rag' 这个字符串" ——
        它**通过了**。字符串出现在注释里、出现在 switch 里、
        出现在 ValidateSet 里都算通过，这三种情况的行为却完全不同。

        教训：断言"某个符号存在于文本中"几乎不构成验证。
        要断言的是**那个让功能生效的具体结构**。
        这里的结构就是"switch 标签 ⊆ ValidateSet"。
        """
        import re

        root = Path(__file__).resolve().parents[3]
        text = (root / "scripts" / "dev.ps1").read_text(encoding="utf-8-sig")

        validate_match = re.search(r"\[ValidateSet\((.*?)\)\]", text, re.S)
        assert validate_match, "找不到 ValidateSet"
        allowed = set(re.findall(r"'([^']+)'", validate_match.group(1)))

        # switch 主体：从 `switch ($Task) {` 到文件末尾
        switch_match = re.search(r"switch \(\$Task\) \{(.*)", text, re.S)
        assert switch_match, "找不到 switch ($Task)"
        # 每个分支形如： 'name'  { ... }
        labels = set(re.findall(r"^\s*'([^']+)'\s*\{", switch_match.group(1), re.M))

        assert labels, "没有解析到任何 switch 分支 —— 正则可能失效了"
        missing = labels - allowed
        assert not missing, (
            f"这些子命令在 switch 里有分支，但不在 ValidateSet 里，"
            f"调用时会被参数绑定直接拒绝：{sorted(missing)}"
        )

        redundant = allowed - labels - {"help"}
        assert not redundant, (
            f"这些子命令在 ValidateSet 里却没有对应分支，调用会静默走到 default："
            f"{sorted(redundant)}"
        )
