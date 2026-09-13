"""RRF（Reciprocal Rank Fusion）：把多路检索结果融合成一个排名。

【为什么不是"分数加权和"】
最直觉的做法是 `final = 0.7·dense_score + 0.3·sparse_score`。但这条路在工程上很难走：

1. **分数不可比**。余弦相似度在 [0,1]，BM25 是无上界的实数（可以是 0~30+）。
   两路分数的量纲完全不同，加权和本质上是拿苹果加橘子。
2. **归一化引入新的不稳定**。若各自 min-max 归一化，结果就依赖于
   "这一次查询返回的分数范围"。同一个文档，换一个查询、多一个候选，
   归一化后的分数就变了，排名会莫名其妙地抖动。
3. **权重需要按查询类型调**。精确术语查询该偏 BM25，语义查询该偏向向量。
   固定权重必然在某一类上表现很差。

【RRF 的做法】
只用**排名**，不用分数：

    RRF(d) = Σ_{r ∈ 各路检索} 1 / (k + rank_r(d))

`k` 通常取 60。它的好处：
  - 天然免疫量纲差异 —— 只关心"排第几"
  - 一个文档如果在多路里都靠前，得分叠加，自然被推上去
  - 不需要调权重，也不需要归一化

【k 的作用】
k 越小，头部排名的优势越极端（第 1 名 1/61 vs 第 2 名 1/62 差别很小，但
k=1 时 1/2 vs 1/3 差别很大）。其实 k 越大越"平"，越倾向于奖励
"在多路中都出现"而不是"在某一路中排第一"。60 是原论文的经验值。
"""

from __future__ import annotations

from collections import defaultdict


def reciprocal_rank_fusion(
    rankings: list[list[str]],
    *,
    k: int = 60,
    weights: list[float] | None = None,
) -> list[tuple[str, float]]:
    """融合多路排名。

    Args:
        rankings: 每一路检索的**有序 id 列表**（第 0 个是最相关的）
        k: RRF 平滑常数，默认 60
        weights: 每一路的权重，默认等权。**只有当有评测数据支持时才该用非等权** ——
                 凭直觉设权重是 RRF 最常见的误用。

    Returns:
        [(id, 融合得分), ...]，按得分降序。
    """
    if weights is not None and len(weights) != len(rankings):
        raise ValueError(f"权重数量({len(weights)})与检索路数({len(rankings)})不一致")

    scores: dict[str, float] = defaultdict(float)

    for i, ranking in enumerate(rankings):
        weight = weights[i] if weights else 1.0
        for rank, doc_id in enumerate(ranking, start=1):
            # 用 rank 而不是索引：从 1 开始更符合"第几名"的直觉，
            # 也避免 k=0 时出现除零
            scores[doc_id] += weight / (k + rank)

    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def fuse_rankings(
    rankings: list[list[str]],
    *,
    k: int = 60,
    top_n: int | None = None,
) -> list[str]:
    """只关心融合后的 id 顺序，不关心具体分值的便捷封装。"""
    fused = reciprocal_rank_fusion(rankings, k=k)
    ids = [doc_id for doc_id, _ in fused]
    return ids[:top_n] if top_n is not None else ids
