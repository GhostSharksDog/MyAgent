"""知识库检索工具测试（RAG ↔ Agent 的接缝）。

【为什么这个接缝值得单独测】
检索链路的单元测试全绿，不代表 Agent 能真的用上它：
    - 工具的参数 schema 是否让模型能正确填
    - 工具是 async 的，而 `FunctionTool` 对同步/异步函数分派不同
    - 语料为空时是否给出**可操作**的提示（而不是一个内部异常）
    - scope 过滤是否真的生效
这些都属于"集成层"的失败面，只有在接缝上测才能覆盖。

测试用**注入的检索器**（合成语料），因此：
  - 不依赖 data/ 下有没有真实简历
  - 不受进程内共享单例的污染
  - 不产生任何 API 调用
"""

from __future__ import annotations

import pytest
from app.core.config import get_settings
from app.llm.types import ToolCall
from app.rag.backend import LocalKnowledgeBackend
from app.rag.chunker import ChunkStrategy
from app.rag.factory import (
    build_configured_retriever,
    get_shared_retriever,
    reset_shared_retriever,
)
from app.rag.loaders import DocType, LoadedDocument
from app.rag.rerank import LexicalReranker
from app.rag.retriever import RetrievalMode, Retriever
from app.tools.knowledge import KnowledgeSearchTool, SearchKnowledgeParams


def _corpus() -> list[LoadedDocument]:
    return [
        LoadedDocument(
            source="resume.md",
            doc_type=DocType.RESUME,
            text=(
                "教育经历\n某某大学 计算机科学与技术\n\n"
                "专业技能\n熟练掌握 Kafka、Flink、ClickHouse，"
                "有 Redisson 分布式锁使用经验\n\n"
                "实习经历\n某科技公司 全栈开发工程师，负责缓存一致性方案"
            ),
        ),
        LoadedDocument(
            source="job-001 大模型工程师",
            doc_type=DocType.JD,
            text="岗位名称：大模型工程师\n\n任职要求\n熟悉 RAG 与向量数据库，掌握 Python",
        ),
    ]


def _retriever() -> Retriever:
    return Retriever.from_documents(
        _corpus(), strategy=ChunkStrategy.SECTION, min_size=0, mode=RetrievalMode.HYBRID
    )


def _backend() -> LocalKnowledgeBackend:
    """把合成语料检索器包成本地后端。

    工具层现在只依赖 KnowledgeBackend 接口（见 rag/backend.py），
    所以测试注入的是后端而不是检索器 —— 这个改动本身就是拆分的一部分：
    接口变清楚之后，测试也不再需要知道底层是 Retriever。
    """
    return LocalKnowledgeBackend(retriever=_retriever())


def _call(name: str = "search_knowledge", **args: object) -> ToolCall:
    import json

    return ToolCall(id="c1", name=name, arguments=args, raw_arguments=json.dumps(args))


@pytest.fixture(autouse=True)
def _clean_singleton():
    """每个用例前后清空共享检索器，避免用例之间互相污染。"""
    reset_shared_retriever()
    yield
    reset_shared_retriever()


# ============================================================
# Schema 与描述
# ============================================================
class TestToolContract:
    def test_registered_in_default_registry(self) -> None:
        from app.tools.builtin import build_default_registry

        assert "search_knowledge" in build_default_registry().names()

    def test_schema_is_valid_json_schema(self) -> None:
        schema = KnowledgeSearchTool(backend=_backend()).json_schema()
        fn = schema["function"]
        assert fn["name"] == "search_knowledge"
        assert fn["parameters"]["type"] == "object"
        assert "query" in fn["parameters"]["properties"]
        assert "query" in fn["parameters"]["required"]

    def test_description_states_when_not_to_use(self) -> None:
        """描述必须包含"什么时候不该用"，否则模型会拿它去做算术题。"""
        desc = KnowledgeSearchTool().description
        assert "calculator" in desc  # 指向更合适的工具
        assert "search_jobs" in desc  # 说明与结构化检索的分工

    def test_scope_enum_constrained(self) -> None:
        props = SearchKnowledgeParams.model_json_schema()["properties"]
        assert set(props["scope"]["enum"]) == {"all", "resume", "jobs", "notes"}

    def test_query_is_required(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            SearchKnowledgeParams()  # type: ignore[call-arg]


# ============================================================
# 检索行为
# ============================================================
class TestRetrieval:
    async def test_returns_snippets_with_citations(self) -> None:
        tool = KnowledgeSearchTool(backend=_backend())
        result = await tool.run(SearchKnowledgeParams(query="Kafka"))
        assert result.ok
        assert "Kafka" in result.content  # 夹具只写了 Kafka，没有“消息队列”词项
        # 出处标注是"回答可验证"的前提，必须存在
        assert "出处" in result.content
        assert "[1]" in result.content

    async def test_finds_relevant_content(self) -> None:
        tool = KnowledgeSearchTool(backend=_backend())
        result = await tool.run(SearchKnowledgeParams(query="用了哪些大数据技术"))
        assert result.ok
        assert any(kw in result.content for kw in ("Kafka", "Flink", "ClickHouse"))

    async def test_scope_resume_excludes_jobs(self) -> None:
        tool = KnowledgeSearchTool(backend=_backend())
        result = await tool.run(SearchKnowledgeParams(query="RAG 向量数据库", scope="resume"))
        # RAG 只出现在岗位块里；限定 scope=resume 后不应召回它
        assert "job-001" not in result.content

    async def test_scope_jobs_excludes_resume(self) -> None:
        tool = KnowledgeSearchTool(backend=_backend())
        result = await tool.run(SearchKnowledgeParams(query="Kafka", scope="jobs"))
        assert "resume.md" not in result.content

    async def test_limit_respected(self) -> None:
        tool = KnowledgeSearchTool(backend=_backend())
        result = await tool.run(SearchKnowledgeParams(query="技术", limit=1))
        assert result.ok
        assert "[2]" not in result.content  # 只要 1 条，不该出现第 2 条编号

    @pytest.mark.parametrize("min_score", [0.0, 0.3])
    async def test_no_match_gives_actionable_hint(self, min_score: float) -> None:
        """查不到时要告诉模型**怎么调整**，而不是简单说"无结果"。

        没有任何词项匹配时，闸门关闭也应返回空；弱关联则靠显式闸门过滤。
        """
        from app.core.config import RagSettings

        settings = get_settings().model_copy(update={"rag": RagSettings(min_score=min_score)})
        tool = KnowledgeSearchTool(settings=settings, backend=_backend())
        result = await tool.run(
            SearchKnowledgeParams(
                query="外星语言量子纠缠拓扑绝缘体" if min_score else "zxqvzxqv", scope="resume"
            )
        )
        assert not result.ok
        assert "没有检索到" in result.content
        assert "scope" in result.content  # 给出下一步动作

    async def test_without_gate_weak_overlap_can_return_content(self) -> None:
        """单字等弱关联仍可能有正分；零分过滤不能判定资料足以回答。"""
        tool = KnowledgeSearchTool(backend=_backend())
        result = await tool.run(SearchKnowledgeParams(query="外星语言量子纠缠拓扑绝缘体"))
        assert result.ok  # “量”等单字与夹具匹配，正分不代表有答案
        assert result.content  # 但内容其实无关

    async def test_gate_does_not_drop_real_matches(self) -> None:
        """闸门不能把真正相关的结果也筛掉 —— 设错阈值的代价比召回噪声更大。"""
        from app.core.config import RagSettings

        settings = get_settings().model_copy(update={"rag": RagSettings(min_score=0.05)})
        tool = KnowledgeSearchTool(settings=settings, backend=_backend())
        result = await tool.run(SearchKnowledgeParams(query="Kafka 消息队列"))
        assert result.ok

    async def test_zero_gate_disabled_by_default(self) -> None:
        from app.core.config import RagSettings

        assert RagSettings().min_score == 0.0

    async def test_empty_corpus_gives_setup_instructions(self) -> None:
        """知识库为空时必须引导用户去准备数据，而不是抛内部异常。"""
        from app.rag.embedder import TfidfEmbedder

        emb = TfidfEmbedder()
        emb.fit(["占位内容"])
        tool = KnowledgeSearchTool(backend=LocalKnowledgeBackend(retriever=Retriever([], emb)))
        result = await tool.run(SearchKnowledgeParams(query="任何"))
        assert not result.ok
        assert "知识库为空" in result.content
        # 【断言的是"指引指向当前真正有效的入口"，不是某段具体文案】
        #
        # 原来这里断言包含 "ingest.py"。P6 把知识库默认数据源改成
        # "什么都不加载"之后，`ingest.py` 只负责写 data/resume.md，
        # 而那个文件在通用形态下**根本不会被加载** ——
        # 于是这条测试还在绿着，而它守着的指引已经彻底失效了：
        # 用户照着做、重试、再失败，看不出哪里错。
        #
        # **测试断言具体文案时会跟着文案一起过期。** 所以改成断言
        # "指引指向了真正生效的配置入口"，那才是它要守的东西。
        assert "AGENT_CORPUS_PATHS" in result.content, (
            "空知识库的指引必须指向当前真正生效的数据源配置入口"
        )


# ============================================================
# 与 FunctionTool 的分派配合
# ============================================================
class TestRegistryIntegration:
    async def test_async_tool_dispatched_correctly(self) -> None:
        """KnowledgeSearchTool 的 run 是 async 的，必须走异步分派路径。"""
        from app.tools.base import ToolRegistry

        registry = ToolRegistry()
        registry.register(KnowledgeSearchTool(backend=_backend()))
        result = await registry.execute(_call(query="Kafka"))
        assert result.ok
        assert result.duration_ms >= 0

    async def test_invalid_scope_rejected_by_validation(self) -> None:
        """非法 scope 走参数校验失败路径，错误信息要能回灌给模型自愈。"""
        from app.tools.base import ToolRegistry

        registry = ToolRegistry()
        registry.register(KnowledgeSearchTool(backend=_backend()))
        result = await registry.execute(_call(query="Kafka", scope="不存在的范围"))
        assert not result.ok
        assert "参数校验失败" in result.content


# ============================================================
# 共享实例与配置驱动
# ============================================================
class TestFactory:
    def test_shared_instance_reused(self) -> None:
        first = get_shared_retriever()
        second = get_shared_retriever()
        assert first is second, "共享实例没有被复用，每次调用都重建索引"

    def test_reset_clears_singleton(self) -> None:
        first = get_shared_retriever()
        reset_shared_retriever()
        assert get_shared_retriever() is not first

    def test_configured_retriever_uses_settings(self) -> None:
        """工厂必须把配置真正传下去 —— 否则改了 .env 行为不变，极难排查。"""
        settings = get_settings()
        r = build_configured_retriever(settings)
        stats = r.stats()
        assert stats["mode"] == settings.rag.mode
        assert stats["reranker"] == (
            "none" if settings.rag.reranker == "none" else settings.rag.reranker
        )

    def test_build_configured_is_fresh_each_time(self) -> None:
        """评测需要在一进程里跑不同配置 —— 工厂不能返回缓存实例。"""
        assert build_configured_retriever() is not build_configured_retriever()

    def test_unknown_reranker_raises_clearly(self) -> None:
        from app.core.config import RagSettings
        from app.rag.factory import build_reranker

        with pytest.raises(ValueError, match="未知的 RAG_RERANKER"):
            build_reranker(RagSettings(reranker="不存在的重排器"))

    def test_lexical_reranker_configurable(self) -> None:
        from app.core.config import RagSettings
        from app.rag.factory import build_reranker

        r = build_reranker(RagSettings(reranker="lexical"))
        assert isinstance(r, LexicalReranker)

    def test_none_reranker_returns_none(self) -> None:
        from app.core.config import RagSettings
        from app.rag.factory import build_reranker

        assert build_reranker(RagSettings(reranker="none")) is None
