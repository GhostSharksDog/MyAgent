"""向量存储与相似度检索。

【当前实现：内存暴力检索】
把所有向量存成一个 (n, dim) 的 numpy 矩阵，查询时做一次矩阵乘法算全部相似度。

为什么这样够用、以及什么时候不够用：
  - 暴力检索的复杂度是 O(n × dim)。10 万个 512 维向量，一次查询约 5000 万次
    浮点乘加，现代 CPU 上是**几毫秒**量级。所以"向量库"在小规模下是伪需求。
  - 真正的瓶颈出现在百万级以上，或者需要持久化、多副本、实时增删的时候。
    那时才需要 HNSW（近似最近邻，用图索引把复杂度降到 O(log n)）。

这也是 P4 才引入 pgvector 的理由：**先让规模成为问题，再引入解决方案。**
现在用 FAISS 或 Milvus 只会增加部署与调试成本，学不到任何东西。

【相似度为什么用点积就够了】
余弦相似度 = dot(a, b) / (|a| × |b|)。上游 Embedder 已经把向量 L2 归一化，
所以 |a| = |b| = 1，余弦相似度直接等于点积，省掉 n 次开方与除法。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from pydantic import BaseModel

from app.rag.chunker import Chunk
from app.rag.embedder import Embedder
from app.rag.ranking import positive_top_k

logger = logging.getLogger(__name__)


class SearchHit(BaseModel):
    """一条检索结果。"""

    chunk: Chunk
    score: float
    rank: int = 0


class VectorStore:
    """内存向量库。"""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._chunks: list[Chunk] = []
        self._matrix: np.ndarray | None = None

    # ---------- 写入 ----------

    def add(self, chunks: list[Chunk]) -> int:
        """切块入向量化并入库。返回入库数量。"""
        if not chunks:
            return 0

        vectors = self._embedder.encode([c.index_text for c in chunks])
        self._matrix = vectors if self._matrix is None else np.vstack([self._matrix, vectors])
        self._chunks.extend(chunks)

        logger.info(
            "入库 %d 块，当前共 %d 块，向量维度 %d",
            len(chunks),
            len(self._chunks),
            vectors.shape[1],
        )
        return len(chunks)

    def rebuild(self, chunks: list[Chunk]) -> int:
        """清空重建。语料更新后必须重建——TF-IDF 的 IDF 权重依赖全语料。

        空语料必须优雅处理：上层（评测脚本、服务启动）依赖"先构造再检查
        块数是否为 0"来决定要不要提示用户去准备数据。如果这里直接抛异常，
        那条友好提示永远走不到，用户只会看到一个 tf-idf 的内部报错。
        """
        self._chunks = []
        self._matrix = None
        if not chunks:
            logger.warning("语料为空，向量库已置空")
            return 0
        self._embedder.fit([c.index_text for c in chunks])
        return self.add(chunks)

    # ---------- 检索 ----------

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        doc_types: list[str] | None = None,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """相似度检索。

        Args:
            query: 查询文本
            k: 返回条数
            doc_types: 只在这些文档类型里检索（如只查简历、只查岗位）。
                       这叫元数据过滤，是 RAG 提效最廉价的手段之一——
                       用户问"我简历里写了什么"时，检索岗位库纯属浪费。
            min_score: 相似度下限。低于它直接丢弃，宁可不召回也不要塞噪声。
                       检索到的无关内容比没检索到更糟：模型会拿它硬编答案。
        """
        if self._matrix is None or not self._chunks or k <= 0 or not query.strip():
            return []

        qvec = self._embedder.encode_query(query)
        scores = self._matrix @ qvec  # (n,)

        # 元数据过滤：先把候选缩到目标范围
        if doc_types:
            allowed = {str(t) for t in doc_types}
            mask = np.array([str(c.doc_type) in allowed for c in self._chunks])
            scores = np.where(mask, scores, -np.inf)

        # 无匹配时不能靠零分凑满结果；同分的候选与截断均按语料顺序稳定处理。
        top_idx = positive_top_k(scores, k)

        hits: list[SearchHit] = []
        for idx in top_idx:
            score = float(scores[idx])
            if score < min_score or not np.isfinite(score):
                continue
            hits.append(SearchHit(chunk=self._chunks[int(idx)], score=score, rank=len(hits)))
        return hits

    # ---------- 自省 ----------

    def __len__(self) -> int:
        return len(self._chunks)

    @property
    def chunks(self) -> list[Chunk]:
        return list(self._chunks)

    def stats(self) -> dict[str, Any]:
        from collections import Counter

        by_type = Counter(str(c.doc_type) for c in self._chunks)
        return {
            "chunk_count": len(self._chunks),
            "dim": int(self._matrix.shape[1]) if self._matrix is not None else 0,
            "by_doc_type": dict(by_type),
            "total_chars": sum(len(c.text) for c in self._chunks),
        }
