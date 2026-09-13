"""按配置组装检索器，并提供进程内共享实例。

【为什么要单独一个工厂 + 共享实例】
1. **配置与代码解耦**：`Retriever` 的构造参数有十来个（切分、检索模式、
   重排器、RRF 参数…），散在调用点会到处不一致。集中在一处，
   评测脚本与线上服务就用的是**同一套装配逻辑** —— 这是
   "评测结果能代表线上表现"的前提。
2. **避免重复建索引**：TF-IDF 拟合与 BM25 建索引要遍历全语料。
   Agent 每调用一次检索工具就重建一次索引，是典型的性能事故。
   共享实例把成本压到进程内一次。
3. **懒加载**：不在 import 或启动时就读盘建索引 ——
   服务启动不该被数据准备情况拖慢，而且语料缺失时要能优雅降级
   （工具返回可操作的提示，而不是启动失败）。
"""

from __future__ import annotations

import logging

from app.core.config import RagSettings, Settings, get_settings
from app.rag.chunker import ChunkStrategy
from app.rag.rerank import LexicalReranker, LLMReranker, Reranker
from app.rag.retriever import RetrievalMode, Retriever

logger = logging.getLogger(__name__)

# 进程内共享实例。键是影响装配结果的配置，配置变了就重建。
_cache: Retriever | None = None
_cache_key: tuple[object, ...] | None = None


def build_reranker(settings: RagSettings) -> Reranker | None:
    """按配置构造重排器。

    注意 `llm` 分支需要 LLMClient —— 这里**延迟 import**，
    避免 rag 层与 llm 层形成循环依赖，也让只用离线检索的场景
    不必加载 httpx 客户端。
    """
    kind = settings.reranker.lower()
    if kind == "none":
        return None
    if kind == "lexical":
        return LexicalReranker()
    if kind == "llm":
        from app.core.config import get_settings as _get
        from app.llm.client import LLMClient

        return LLMReranker(LLMClient(_get().llm))
    raise ValueError(f"未知的 RAG_RERANKER：{settings.reranker!r}（可选 none / lexical / llm）")


def build_configured_retriever(
    settings: Settings | None = None,
    *,
    use_sample_resume: bool = False,
) -> Retriever:
    """按当前配置构造一个全新的检索器（不写缓存）。

    供评测脚本与测试使用 —— 它们需要在同一进程里跑不同配置做对比，
    共享单例会互相污染。
    """
    s = settings or get_settings()
    rag = s.rag

    return Retriever.from_default_corpus(
        strategy=ChunkStrategy(rag.strategy),
        size=rag.chunk_size,
        overlap=rag.chunk_overlap,
        min_size=rag.min_chunk_size,
        use_sample_resume=use_sample_resume,
        mode=RetrievalMode(rag.mode),
        reranker=build_reranker(rag),
        rrf_k=rag.rrf_k,
    )


def get_shared_retriever(settings: Settings | None = None) -> Retriever:
    """取进程内共享的检索器，首次调用时懒加载。

    配置变化会自动触发重建：否则改了 `.env` 却拿到旧索引，
    会得到"配置明明改了但行为没变"这种极难排查的现象。
    """
    global _cache, _cache_key

    s = settings or get_settings()
    rag = s.rag
    key = (
        rag.mode,
        rag.reranker,
        rag.strategy,
        rag.chunk_size,
        rag.chunk_overlap,
        rag.min_chunk_size,
        rag.rrf_k,
    )

    if _cache is not None and _cache_key == key:
        return _cache

    logger.info(
        "构建检索索引（mode=%s, reranker=%s, min_size=%d）",
        rag.mode,
        rag.reranker,
        rag.min_chunk_size,
    )
    _cache = build_configured_retriever(s)
    _cache_key = key
    logger.info("检索索引就绪：%s", _cache.stats())
    return _cache


def reset_shared_retriever() -> None:
    """清空共享实例。测试与"语料更新后热重建"用。"""
    global _cache, _cache_key
    _cache = None
    _cache_key = None
