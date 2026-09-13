"""检索器：RAG 链路对外的高层入口。

职责边界很清晰——
    loaders → chunker → embedder → store 都只做一件事，
    retriever 负责把它们串成"给一个查询，还一组证据"的能力。

【为什么把 Retriever 与 VectorStore 分开】
VectorStore 只管"向量进、相似度出"，不关心业务；
Retriever 承担业务语义：要不要元数据过滤、要不要混合检索、
要不要丢弃低分结果、上下文怎么组装。分开之后：
  - 换向量库（内存 → pgvector → Milvus）不影响业务逻辑
  - 检索策略（纯向量 → 混合 → 加重排）可以独立迭代与 A/B 对比
"""

from __future__ import annotations

import logging
from typing import Any

from app.rag.chunker import Chunk, ChunkStrategy, chunk_documents
from app.rag.corpus import build_corpus
from app.rag.embedder import Embedder, TfidfEmbedder
from app.rag.loaders import DocType, LoadedDocument
from app.rag.store import SearchHit, VectorStore

logger = logging.getLogger(__name__)


class Retriever:
    """检索器。构造后即可反复查询。"""

    def __init__(self, chunks: list[Chunk], embedder: Embedder) -> None:
        self._chunks = chunks
        self._embedder = embedder
        self._store = VectorStore(embedder)
        self._store.rebuild(chunks)

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
    ) -> Retriever:
        chunks = chunk_documents(
            docs, strategy=strategy, size=size, overlap=overlap, min_size=min_size
        )
        return cls(chunks, embedder or TfidfEmbedder())

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
    ) -> Retriever:
        """从项目默认数据源（简历 + 岗位库 + 笔记）构建。"""
        return cls.from_documents(
            build_corpus(use_sample_resume=use_sample_resume),
            strategy=strategy,
            size=size,
            overlap=overlap,
            min_size=min_size,
            embedder=embedder,
        )

    # ---------- 查询 ----------

    def retrieve(
        self,
        query: str,
        k: int = 5,
        *,
        doc_types: list[DocType | str] | None = None,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """检索最相关的 k 个块。"""
        normalized = [str(t) for t in doc_types] if doc_types else None
        return self._store.search(query, k=k, doc_types=normalized, min_score=min_score)

    def retrieve_context(
        self,
        query: str,
        k: int = 5,
        *,
        doc_types: list[DocType | str] | None = None,
        min_score: float = 0.0,
        max_chars: int = 4000,
    ) -> str:
        """检索并组装成可直接塞进提示词的上下文。

        【为什么需要 max_chars 而不是只靠 k】
        块的长度不均：同样是 5 个块，可能是 800 token，也可能是 6000 token。
        按"块数"控制上下文会让成本极不稳定。按字符数（≈ token 数的粗略代理）
        控制才能真正约束成本，这也是工程上更可靠的做法。

        每块都带 `[编号] 出处` 前缀，让模型能在回答里标注引用来源——
        没有引用标注的 RAG 回答，用户无法验证，也就无法信任。
        """
        hits = self.retrieve(query, k=k, doc_types=doc_types, min_score=min_score)
        if not hits:
            return ""

        blocks: list[str] = []
        used = 0
        for i, hit in enumerate(hits, 1):
            block = f"[{i}] 出处：{hit.chunk.citation}（相关度 {hit.score:.3f}）\n{hit.chunk.text}"
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
        return s
