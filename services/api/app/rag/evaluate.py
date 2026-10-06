"""检索质量评测：把"感觉效果变好了"变成可比较的数字。

【为什么必须做这件事】
RAG 有太多可调参数：切分策略、块大小、重叠、embedding 模型、top-k、
要不要重排、要不要混合检索……如果每次调整都靠"随便问几个问题看看"，
你会陷入两个陷阱：
  1. **过拟合到你自己想出来的那几个问题**上，真实查询反而变差
  2. 说不清是哪一步带来了提升，面试时只能答"感觉好一些"

有了评测集和指标，每一步调整都能回答："Recall@5 从 0.62 涨到 0.79。"

【指标的选择：召回层和排序层要分开看】
  - Recall@k   ：该找的找到了没有。**召回层的核心指标**——
                 rerank 再强，也救不回没被召回的内容。所以这一层看 Recall。
  - Precision@k：返回的结果里有多少是相关的。衡量噪声比例。
  - MRR        ：第一个正确结果排在第几位。**排序层的核心指标**，
                 直接决定用户看到的第一条对不对。
  - NDCG@k     ：考虑位置衰减的排序质量，对"相关性有强弱之分"更敏感。

经验：召回层看 Recall@20~50，排序层看 NDCG@5~10。分开看才能定位问题出在哪一层。

【评测集的构造原则】
  - 查询要覆盖**真实的用户意图**，而不是"关键词本身"
    （问"我用过哪些消息队列"比问"Kafka"更能暴露真实检索问题）
  - 正例标注要覆盖不同难度：直接命中、需要同义匹配、需要跨块推理
  - 从真实使用中收集失败查询，补进评测集——这是评测集最有价值的来源
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.rag.chunker import Chunk
from app.rag.retriever import Retriever

logger = logging.getLogger(__name__)


# ============================================================
# 评测集数据结构
# ============================================================
class GoldCondition(BaseModel):
    """一个相关性判定条件。

    **用条件而不是写死 chunk id**，因为 chunk id 会随切分参数变化，
    评测集就成了"改一次参数就得重标一次"的消耗品。
    条件式标注在切分策略变化后依然可用，这是它能长期维护的关键。

    所有填写的条件必须**同时满足**才判定为相关（AND 语义）。
    """

    doc_id_contains: str | None = Field(
        default=None, description="chunk 所属文档 id 含此子串（如 'resume' 或 'job-002'）"
    )
    section_contains: str | None = Field(
        default=None, description="chunk 的章节名含此子串（如 '专业技能'）"
    )
    text_contains: str | None = Field(default=None, description="chunk 正文含此子串（如 'Kafka'）")

    def matches(self, chunk: Chunk) -> bool:
        if self.doc_id_contains and self.doc_id_contains.lower() not in chunk.doc_id.lower():
            return False
        if self.section_contains and self.section_contains not in chunk.section:
            return False
        return not (self.text_contains and self.text_contains not in chunk.text)


class EvalQuery(BaseModel):
    """一条评测查询。"""

    query: str
    gold: list[GoldCondition] = Field(default_factory=list)
    difficulty: str = "normal"  # easy | normal | hard
    note: str = ""


class EvalSet(BaseModel):
    name: str = "default"
    description: str = ""
    queries: list[EvalQuery] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str) -> EvalSet:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)


# ============================================================
# 指标计算
# ============================================================
def _is_relevant(chunk: Chunk, gold: list[GoldCondition]) -> bool:
    """命中任一 gold 条件即算相关（OR 语义：命中多个不同位置都算对）。"""
    return any(cond.matches(chunk) for cond in gold)


def recall_at_k(
    ranked: list[Chunk], gold: list[GoldCondition], k: int, corpus: list[Chunk]
) -> float:
    """Recall@k：相关块中被召回的比例。

    分母是"整个语料里符合条件的块总数"，而不是"标注里列了几个条件"——
    因为一个条件可能命中多个块，分母算错会让指标虚高。
    """
    if not gold:
        return 0.0
    total_relevant = sum(1 for c in corpus if _is_relevant(c, gold))
    if total_relevant == 0:
        return 0.0
    hit = sum(1 for c in ranked[:k] if _is_relevant(c, gold))
    return hit / total_relevant


def precision_at_k(ranked: list[Chunk], gold: list[GoldCondition], k: int) -> float:
    if k == 0:
        return 0.0
    return sum(1 for c in ranked[:k] if _is_relevant(c, gold)) / k


def reciprocal_rank(ranked: list[Chunk], gold: list[GoldCondition]) -> float:
    """RR：第一个相关结果排名的倒数。第 1 位 = 1.0，第 4 位 = 0.25，没找到 = 0。"""
    for i, chunk in enumerate(ranked, start=1):
        if _is_relevant(chunk, gold):
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[Chunk], gold: list[GoldCondition], k: int, corpus: list[Chunk]) -> float:
    """NDCG@k：带位置衰减的排序质量。

    本实现用二元相关性（相关=1，不相关=0），所以 IDCG 是"理想排序下
    前 min(相关块总数, k) 个位置全部命中"的 DCG 值。
    分母必须包含未召回的相关块；只数实际命中会把漏召回误算为满分。
    """
    if k < 0:
        raise ValueError("k 必须大于等于 0")
    dcg = sum(
        1.0 / math.log2(i + 1)
        for i, chunk in enumerate(ranked[:k], start=1)
        if _is_relevant(chunk, gold)
    )
    n_relevant = sum(1 for c in corpus if _is_relevant(c, gold))
    # 理想情况下前面全是相关块
    ideal_hits = min(n_relevant, k)
    if ideal_hits == 0:
        return 0.0
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


# ============================================================
# 评测执行
# ============================================================
class QueryResult(BaseModel):
    query: str
    difficulty: str
    recall: float
    precision: float
    rr: float
    ndcg: float
    hit_rank: int | None = None
    retrieved: list[str] = Field(default_factory=list)


class EvalReport(BaseModel):
    eval_set: str
    retriever: str
    metric_version: str = "ndcg-corpus-v2"
    # 管线配置必须随报告一起留档：否则过几天看到一份 JSON 报告，
    # 根本不知道它是哪套配置跑出来的，消融对比也就无从谈起。
    mode: str = ""
    reranker: str = ""
    k: int
    chunk_count: int
    metrics: dict[str, float] = Field(default_factory=dict)
    by_difficulty: dict[str, dict[str, float]] = Field(default_factory=dict)
    per_query: list[QueryResult] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)

    def summary_line(self) -> str:
        m = self.metrics
        return (
            f"Recall@{self.k}={m.get('recall', 0):.3f}  "
            f"Precision@{self.k}={m.get('precision', 0):.3f}  "
            f"MRR={m.get('mrr', 0):.3f}  "
            f"NDCG@{self.k}={m.get('ndcg', 0):.3f}"
        )

    def pipeline(self) -> str:
        """管线标识，用于消融对比表的行名。"""
        parts = [self.retriever, self.mode]
        if self.reranker and self.reranker != "none":
            parts.append(self.reranker)
        return "+".join(p for p in parts if p)


async def evaluate(
    retriever: Retriever,
    eval_set: EvalSet,
    *,
    k: int = 5,
) -> EvalReport:
    """跑一遍完整评测。

    **异步**是因为检索管线里可能挂着 `LLMReranker`（要走网络）。
    纯本地的向量/BM25 路径本可以同步，但整条管线统一成异步，
    调用方不必记住"哪一半是哪种"，也避免以后换重排器时改一堆调用点。
    """
    corpus = retriever.chunks
    stats = retriever.stats()
    results: list[QueryResult] = []
    failures: list[dict[str, Any]] = []

    for item in eval_set.queries:
        hits = await retriever.aretrieve(item.query, k=k)
        ranked = [h.chunk for h in hits]

        r = recall_at_k(ranked, item.gold, k, corpus)
        p = precision_at_k(ranked, item.gold, k)
        rr = reciprocal_rank(ranked, item.gold)
        ndcg = ndcg_at_k(ranked, item.gold, k, corpus)

        hit_rank = next((i for i, c in enumerate(ranked, 1) if _is_relevant(c, item.gold)), None)

        results.append(
            QueryResult(
                query=item.query,
                difficulty=item.difficulty,
                recall=r,
                precision=p,
                rr=rr,
                ndcg=ndcg,
                hit_rank=hit_rank,
                retrieved=[f"{h.chunk.citation} ({h.score:.3f})" for h in hits],
            )
        )

        # 完全没召回到相关内容的查询单独记录：这是最有价值的改进线索
        if hit_rank is None:
            failures.append(
                {
                    "query": item.query,
                    "difficulty": item.difficulty,
                    "reason": "前 k 个结果里没有任何相关块",
                    "gold_conditions": [c.model_dump(exclude_none=True) for c in item.gold],
                    "actually_retrieved": [f"{h.chunk.citation} ({h.score:.3f})" for h in hits],
                    "note": item.note,
                }
            )

    n = max(len(results), 1)
    metrics = {
        "recall": sum(r.recall for r in results) / n,
        "precision": sum(r.precision for r in results) / n,
        "mrr": sum(r.rr for r in results) / n,
        "ndcg": sum(r.ndcg for r in results) / n,
        "hit_rate": sum(1 for r in results if r.hit_rank is not None) / n,
    }

    # 按难度分层：如果 hard 类查询全挂，说明同义匹配能力不足
    by_diff: dict[str, dict[str, float]] = {}
    for level in {r.difficulty for r in results}:
        subset = [r for r in results if r.difficulty == level]
        m = max(len(subset), 1)
        by_diff[level] = {
            "count": len(subset),
            "recall": sum(r.recall for r in subset) / m,
            "mrr": sum(r.rr for r in subset) / m,
        }

    return EvalReport(
        eval_set=eval_set.name,
        retriever=str(stats.get("embedder", "unknown")),
        mode=str(stats.get("mode", "")),
        reranker=str(stats.get("reranker", "none")),
        k=k,
        chunk_count=len(corpus),
        metrics=metrics,
        by_difficulty=by_diff,
        per_query=results,
        failures=failures,
    )
