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
            f"{missing}。修复：python scripts/fix_ps1_bom.py"
        )

    def test_dev_ps1_exposes_split_commands(self) -> None:
        """拆分拓扑的启动命令必须能从统一入口拿到。

        否则"怎么起 RAG 服务"只存在于文档里，而文档会过期 ——
        命令行入口是不会过期的那个版本。
        """
        root = Path(__file__).resolve().parents[3]
        text = (root / "scripts" / "dev.ps1").read_text(encoding="utf-8-sig")
        for cmd in ("rag", "worker", "serve-split", "verify-split"):
            assert f"'{cmd}'" in text, f"dev.ps1 缺少 {cmd} 子命令"
