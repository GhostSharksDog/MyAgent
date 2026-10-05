"""检索器：RAG 链路对外的高层入口。

【本模块在本次迭代中的核心变化：从单路检索升级为两段式管线】

    ┌───────────── 召回（recall）─────────────┐   ┌── 精排（rerank）──┐
    │  向量检索（TF-IDF）  ─┐                 │   │                   │
    │                        ├─ RRF 融合 ─────┼──▶│  重排器            │──▶ top-k
    │  BM25 稀疏检索       ─┘                 │   │                   │
    └────────────────────────────────────────┘   └───────────────────┘
        宽（recall_k 个候选）                            窄（k 个结果）

为什么要两段：召回要求**快且全**（所以用可预计算的向量/倒排），
精排要求**准**（所以用能看查询-文档交互的模型）。
这是 RAG 的标准结构，也是本项目实测出来的需求 ——
评测显示 Recall@10=0.816 但 MRR@5 只有 0.412，答案在候选集里但排太后。

【为什么检索器要 async】
纯 numpy 的向量计算没有 I/O，本来不需要异步。但管线里挂了 `LLMReranker`，
它要走网络。**把整条管线统一成异步**，比"一半同步一半异步、调用方得记住
哪一半是哪种"要清晰得多。Agent 本身也是异步的，调用侧零摩擦。
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

import numpy as np

from app.rag.bm25 import BM25
from app.rag.chunker import Chunk, ChunkStrategy, chunk_documents
from app.rag.corpus import build_corpus
from app.rag.embedder import Embedder, TfidfEmbedder
from app.rag.fusion import reciprocal_rank_fusion
from app.rag.loaders import DocType, LoadedDocument
from app.rag.rerank import Reranker
from app.rag.rewrite import QueryRewriter
from app.rag.store import SearchHit, VectorStore

logger = logging.getLogger(__name__)


class RetrievalMode(StrEnum):
    """检索模式。用于消融实验：不做对照就说不清每一路贡献了多少。"""

    DENSE = "dense"  # 纯向量（语义相似度）
    SPARSE = "sparse"  # 纯 BM25（词元匹配）
    HYBRID = "hybrid"  # 两路 RRF 融合


class Retriever:
    """检索器。构造后即可反复查询。"""

    def __init__(
        self,
        chunks: list[Chunk],
        embedder: Embedder,
        *,
        mode: RetrievalMode = RetrievalMode.HYBRID,
        reranker: Reranker | None = None,
        rrf_k: int = 60,
        rrf_weights: list[float] | None = None,
        recall_multiplier: int = 4,
        min_recall_k: int = 20,
        fixed_recall_k: int | None = None,
        bm25_k1: float = 1.5,
        bm25_b: float = 0.75,
        rewriter: QueryRewriter | None = None,
        rewrite_weight: float = 0.6,
    ) -> None:
        self._chunks = chunks
        self._embedder = embedder
        self._mode = mode
        self._reranker = reranker
        self._rrf_k = rrf_k
        # 两路召回的 RRF 权重 [向量, BM25]。默认等权，**只有评测数据支持时才该改**。
        self._rrf_weights = rrf_weights
        self._recall_multiplier = recall_multiplier
        self._min_recall_k = min_recall_k
        # 固定召回宽度，供消融实验覆盖默认策略（默认策略是"k 的若干倍"）
        self._fixed_recall_k = fixed_recall_k

        # Query 改写：为 None 时行为与改写功能引入前**完全一致** ——
        # 新增能力不该改变未启用它时的既有行为，否则消融实验的
        # 基线数字就不可比了（这是个很容易被忽视的实验纪律）。
        self._rewriter = rewriter
        # 改写真相对原查询的权重。**必须小于 1**：
        # 改写只是我们的猜测，原查询才是用户真正想问的。
        # 等权会让三个"可能跑偏的猜测"盖过一个确定的事实。
        self._rewrite_weight = rewrite_weight

        self._store = VectorStore(embedder)
        self._store.rebuild(chunks)

        self._bm25 = BM25(k1=bm25_k1, b=bm25_b)
        if chunks:
            self._bm25.fit([c.text for c in chunks])

        self._index: dict[str, Chunk] = {c.id: c for c in chunks}

    # ---------- 构造入口 ----------

    @classmethod
    def from_documents(
        cls,
        docs: list[LoadedDocument],
        *,
        strategy: ChunkStrategy = ChunkStrategy.SECTION,
        size: int = 500,
        overlap: int = 80,
        min_size: int = 0,
        embedder: Embedder | None = None,
        **kwargs: Any,
    ) -> Retriever:
        chunks = chunk_documents(
            docs, strategy=strategy, size=size, overlap=overlap, min_size=min_size
        )
        return cls(chunks, embedder or TfidfEmbedder(), **kwargs)

    @classmethod
    def from_default_corpus(
        cls,
        *,
        strategy: ChunkStrategy = ChunkStrategy.SECTION,
        size: int = 500,
        overlap: int = 80,
        min_size: int = 0,
        use_sample_resume: bool = False,
        embedder: Embedder | None = None,
        **kwargs: Any,
    ) -> Retriever:
        """从项目默认数据源（简历 + 岗位库 + 笔记）构建。"""
        return cls.from_documents(
            build_corpus(use_sample_resume=use_sample_resume),
            strategy=strategy,
            size=size,
            overlap=overlap,
            min_size=min_size,
            embedder=embedder,
            **kwargs,
        )

    # ---------- 各路的召回 ----------

    def _dense_hits(self, query: str, k: int, doc_types: list[str] | None) -> list[SearchHit]:
        return self._store.search(query, k=k, doc_types=doc_types)

    def _sparse_ids(self, query: str, k: int, doc_types: list[str] | None) -> list[str]:
        """BM25 召回，返回按得分降序的 chunk id。

        注意 BM25 的得分与余弦相似度**量纲不同**，所以这里只输出顺序，
        不输出分数 —— 融合阶段本来就只用排名（见 fusion 模块的说明）。
        """
        scores = self._bm25.scores(query)
        if scores.size == 0:
            return []

        if doc_types:
            allowed = {str(t) for t in doc_types}
            mask = np.array([str(c.doc_type) in allowed for c in self._chunks])
            scores = np.where(mask, scores, -np.inf)

        k = min(k, len(self._chunks))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]

        return [self._chunks[int(i)].id for i in top if np.isfinite(scores[int(i)])]

    # ---------- 对外查询 ----------

    async def aretrieve(
        self,
        query: str,
        k: int = 5,
        *,
        doc_types: list[DocType | str] | None = None,
        min_score: float = 0.0,
        recall_k: int | None = None,
    ) -> list[SearchHit]:
        """检索。

        Args:
            k: 最终返回条数
            recall_k: 召回阶段的候选数。**默认比 k 宽得多**（k×4 且不少于 20），
                因为重排只能重排它拿到的东西 —— 召回太窄，重排再强也白搭。
                但注意召回宽度必须**明显大于 k**：`recall_k < k` 时重排器
                拿不到足够候选，recall 会被候选数直接截断。
            min_score: **相关性闸门**（余弦相似度下限），对全部检索模式生效。
                RRF 与 BM25 只提供相对排名，没有"都不相关"这个概念；
                不设闸门时混合检索永远凑满 k 条，即使全是噪声。
                默认 0（关闭）；生产建议开启，阈值需要在评测集上标定。
        """
        normalized = [str(t) for t in doc_types] if doc_types else None
        rk = (
            recall_k or self._fixed_recall_k or max(k * self._recall_multiplier, self._min_recall_k)
        )

        # ---------- Query 改写 ----------
        # 改写产出 [原查询, ...改写真]，原查询永远在第一位。
        queries = await self._rewrite(query)

        if len(queries) == 1:
            # 无改写：走与引入本功能前**逐字节相同**的路径。
            # 不把单查询也塞进融合逻辑，是为了让消融基线保持可比 ——
            # 「行为没变」这件事本身需要被保证，而不是靠"看起来一样"。
            hits = self._single_query_hits(query, rk, normalized)
        else:
            hits = self._fuse_queries(queries, rk, normalized)

        # ---------- 相关性闸门 ----------
        # 【为什么闸门永远只用原查询，而不用改写】
        # 闸门问的是"**用户问的这件事**，语料里到底有没有相关内容"。
        # 用改写真去算，会把"我猜你可能想问 X"的相关性
        # 当成"你问的这件事"的相关性 —— 于是拒答失效，
        # 用户拿到一堆他根本没问的内容。
        #
        # 闸门与下面的重排都锚在原查询上，是这个功能里最关键的一致性约束。
        if min_score > 0 and hits:
            gate = self._dense_score_map(query, normalized)
            hits = [h for h in hits if gate.get(h.chunk.id, 0.0) >= min_score]

        if self._reranker is not None and hits:
            # 同理：重排也是用原查询对候选打分
            return await self._reranker.rerank(query, hits, k)

        return _renumber(hits[:k])

    async def _rewrite(self, query: str) -> list[str]:
        """产出用于召回的查询列表（原查询恒在第一位）。"""
        if self._rewriter is None:
            return [query]
        return await self._rewriter.rewrite(query)

    def _single_query_hits(
        self, query: str, recall_k: int, doc_types: list[str] | None
    ) -> list[SearchHit]:
        """单查询召回（引入改写功能之前的原路径）。"""
        if self._mode is RetrievalMode.DENSE:
            return self._dense_hits(query, recall_k, doc_types)
        if self._mode is RetrievalMode.SPARSE:
            return self._hits_from_ids(self._sparse_ids(query, recall_k, doc_types))
        return self._hybrid(query, recall_k, doc_types)

    def _fuse_queries(
        self, queries: list[str], recall_k: int, doc_types: list[str] | None
    ) -> list[SearchHit]:
        """多查询召回：每个查询各走一遍召回路径，再把所有排名做 RRF 融合。

        【为什么这一步不需要新算法】
        混合检索是「1 个查询 → 2 路排名 → RRF」，
        多查询是「N 个查询 → 2N 路排名 → RRF」。

        **RRF 本来就只关心"有几路排名"，不关心路是怎么来的** ——
        所以这里直接复用 `reciprocal_rank_fusion`，一行新算法都没写。

        这不是巧合，而是说明当初把融合抽成"接收排名列表"这个接口
        切在了正确的位置：**一个抽象切得对不对，看的就是新需求来时
        能不能不改它。**
        """
        rankings: list[list[str]] = []
        weights: list[float] = []

        # 每一路的基础权重（[向量, BM25]，默认等权）。
        # 用 `self._rrf_weights or [1,1]` 而不是把它和查询权重合并成一个新配置 ——
        # 两个正交的维度分开表达，比揉成一个数好解释也好调。
        path_weights = self._rrf_weights or [1.0, 1.0]

        for i, q in enumerate(queries):
            # 原查询（i=0）拿满权重，改写真按 rewrite_weight 打折
            query_weight = 1.0 if i == 0 else self._rewrite_weight

            dense = self._dense_hits(q, recall_k, doc_types)
            rankings.append([h.chunk.id for h in dense])
            weights.append(query_weight * path_weights[0])

            if self._mode is not RetrievalMode.DENSE:
                rankings.append(self._sparse_ids(q, recall_k, doc_types))
                weights.append(query_weight * path_weights[1])

        fused = reciprocal_rank_fusion(rankings, k=self._rrf_k, weights=weights)

        hits: list[SearchHit] = []
        for rank, (cid, score) in enumerate(fused):
            chunk = self._index.get(cid)
            if chunk is not None:
                hits.append(SearchHit(chunk=chunk, score=score, rank=rank))
        return hits

    def _dense_score_map(self, query: str, doc_types: list[str] | None) -> dict[str, float]:
        """全语料的稠密相似度，为排名式检索提供绝对相关性判据。"""
        return {h.chunk.id: h.score for h in self._dense_hits(query, len(self._chunks), doc_types)}

    def _hits_from_ids(self, ids: list[str]) -> list[SearchHit]:
        out: list[SearchHit] = []
        for i, cid in enumerate(ids):
            chunk = self._index.get(cid)
            if chunk is not None:
                out.append(SearchHit(chunk=chunk, score=0.0, rank=i))
        return out

    def _hybrid(self, query: str, recall_k: int, doc_types: list[str] | None) -> list[SearchHit]:
        """RRF 融合两路召回。

        融合的是**排名**而不是分数 —— 余弦相似度与 BM25 得分量纲完全不同，
        加权求和在工程上不可靠（见 fusion 模块的详细说明）。
        """
        dense = self._dense_hits(query, recall_k, doc_types)
        sparse_ids = self._sparse_ids(query, recall_k, doc_types)

        rankings = [[h.chunk.id for h in dense], sparse_ids]
        fused = reciprocal_rank_fusion(rankings, k=self._rrf_k, weights=self._rrf_weights)

        hits: list[SearchHit] = []
        for rank, (cid, score) in enumerate(fused):
            chunk = self._index.get(cid)
            if chunk is not None:
                hits.append(SearchHit(chunk=chunk, score=score, rank=rank))
        return hits

    # ---------- 上下文组装 ----------

    async def aretrieve_context(
        self,
        query: str,
        k: int = 5,
        *,
        doc_types: list[DocType | str] | None = None,
        min_score: float = 0.0,
        max_chars: int = 4000,
    ) -> str:
        """检索并组装成可直接塞进提示词的上下文。

        【为什么按 max_chars 而不是只靠 k】
        块的长度不均：同样是 5 个块，可能是 800 token，也可能是 6000 token。
        按"块数"控制上下文会让成本极不稳定。按字符数（≈ token 数的粗略代理）
        控制才能真正约束成本。

        每块都带 `[编号] 出处` 前缀，让模型能在回答里标注引用来源 ——
        没有引用标注的 RAG 回答，用户无法验证，也就无法信任。
        """
        hits = await self.aretrieve(query, k=k, doc_types=doc_types, min_score=min_score)
        if not hits:
            return ""

        blocks: list[str] = []
        used = 0
        for i, hit in enumerate(hits, 1):
            block = f"[{i}] 出处：{hit.chunk.citation}\n{hit.chunk.text}"
            if used + len(block) > max_chars and blocks:
                break
            blocks.append(block)
            used += len(block)

        return "\n\n---\n\n".join(blocks)

    # ---------- 自省 ----------

    @property
    def chunks(self) -> list[Chunk]:
        return list(self._chunks)

    def stats(self) -> dict[str, Any]:
        s = self._store.stats()
        s["embedder"] = self._embedder.name
        s["mode"] = str(self._mode)
        s["reranker"] = self._reranker.name if self._reranker else "none"
        s["rrf_weights"] = self._rrf_weights
        s["recall_k_policy"] = f"max(k*{self._recall_multiplier}, {self._min_recall_k})"
        s["bm25"] = {"k1": self._bm25.k1, "b": self._bm25.b, "vocab": self._bm25.vocab_size}
        s["rewriter"] = self._rewriter.name if self._rewriter else "none"
        # Query 改写的运行统计必须暴露出来。
        #
        # 【为什么这不是"锦上添花的可观测"，而是必需的】
        # 改写失败时会静默降级成"只用原查询"。如果没有这个计数，
        # 一个**从未真正执行过**的改写器，在消融实验里的表现与
        # "启用了但没效果"**完全一致** —— 于是你会得出
        # "Query 改写对本项目没有收益"这个看起来权威、实则错误的结论。
        #
        # 这件事在本次开发中真实发生过：调用方法名写成了 `complete`，
        # 而 LLMClient 的实际接口是 `chat`。每次调用都抛 AttributeError、
        # 每次都被降级逻辑吞掉，指标一动不动。
        if self._rewriter is not None:
            s["rewriter_stats"] = self._rewriter.stats()
        # 重排成本必须可观测：LLM 重排的代价就是"每次查询多一次调用"，
        # 不把它量化出来，"值不值得"这个问题就没法回答。
        if self._reranker is not None and hasattr(self._reranker, "total_tokens"):
            s["reranker_tokens"] = self._reranker.total_tokens
        return s


def _renumber(hits: list[SearchHit]) -> list[SearchHit]:
    return [hit.model_copy(update={"rank": i}) for i, hit in enumerate(hits)]
