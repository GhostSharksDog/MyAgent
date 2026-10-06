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

import hashlib
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

    doc_id: str | None = Field(default=None, description="文档 id 精确匹配，避免相似文件名串标")
    doc_id_contains: str | None = Field(
        default=None, description="chunk 所属文档 id 含此子串（如 'resume' 或 'job-002'）"
    )
    section_contains: str | None = Field(
        default=None, description="chunk 的章节名含此子串（如 '专业技能'）"
    )
    text_contains: str | None = Field(default=None, description="chunk 正文含此子串（如 'Kafka'）")

    def matches(self, chunk: Chunk) -> bool:
        if self.doc_id is not None and self.doc_id != chunk.doc_id:
            return False
        if self.doc_id_contains and self.doc_id_contains.lower() not in chunk.doc_id.lower():
            return False
        if self.section_contains and self.section_contains not in chunk.section:
            return False
        return not (self.text_contains and self.text_contains not in chunk.text)


class EvalQuery(BaseModel):
    """一条评测查询。"""

    id: str = ""
    query: str
    gold: list[GoldCondition] = Field(default_factory=list)
    difficulty: str = "normal"  # easy | normal | hard
    category: str = "normal"
    answerable: bool = True
    reference_answer: str = ""  # 人工核对用，不交给检索器
    note: str = ""


class EvalSet(BaseModel):
    name: str = "default"
    description: str = ""
    queries: list[EvalQuery] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str) -> EvalSet:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)


def fingerprint(value: Any) -> str:
    """对实际输入内容留摘要，参数或语料变化后不能冒充同一基线。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_labels(eval_set: EvalSet, corpus: list[Chunk]) -> list[str]:
    """逐个证据条件校验；OR 总命中不能掩盖另一条失效的 gold。"""
    problems: list[str] = []
    ids = [q.id for q in eval_set.queries if q.id]
    if len(ids) != len(set(ids)):
        problems.append("查询 id 重复")
    if not eval_set.queries:
        problems.append("评测集没有查询")
    for index, item in enumerate(eval_set.queries, 1):
        label = item.id or str(index)
        if not item.answerable:
            if item.gold:
                problems.append(f"{label}: 无答案查询不能同时标注正例")
            continue
        if not item.gold:
            problems.append(f"{label}: 有答案查询缺少 gold")
        for position, condition in enumerate(item.gold, 1):
            if not any(condition.model_dump(exclude_none=True).values()):
                problems.append(f"{label}: gold {position} 没有约束，会匹配全部语料")
            elif not any(condition.matches(chunk) for chunk in corpus):
                problems.append(f"{label}: gold {position} 匹配不到任何块")
    return problems


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
    id: str = ""
    query: str
    difficulty: str
    category: str = "normal"
    answerable: bool = True
    recall: float
    precision: float
    rr: float
    ndcg: float
    hit_rank: int | None = None
    retrieved: list[str] = Field(default_factory=list)
    relevant_count: int = 0
    gold_coverage: float = 0.0
    complete_evidence: bool = False
    abstained: bool = False
    missing_gold: list[dict[str, Any]] = Field(default_factory=list)


class EvalReport(BaseModel):
    eval_set: str
    retriever: str
    metric_version: str = "ndcg-corpus-v2"
    eval_set_sha256: str = ""
    corpus_sha256: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    # 管线配置必须随报告一起留档：否则过几天看到一份 JSON 报告，
    # 根本不知道它是哪套配置跑出来的，消融对比也就无从谈起。
    mode: str = ""
    reranker: str = ""
    k: int
    chunk_count: int
    metrics: dict[str, float] = Field(default_factory=dict)
    by_difficulty: dict[str, dict[str, float]] = Field(default_factory=dict)
    by_category: dict[str, dict[str, float]] = Field(default_factory=dict)
    abstention_metrics: dict[str, float | None] = Field(default_factory=dict)
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
    min_score: float = 0.0,
) -> EvalReport:
    """跑一遍完整评测。

    **异步**是因为检索管线里可能挂着 `LLMReranker`（要走网络）。
    纯本地的向量/BM25 路径本可以同步，但整条管线统一成异步，
    调用方不必记住"哪一半是哪种"，也避免以后换重排器时改一堆调用点。
    """
    if k <= 0:
        raise ValueError("评测 k 必须大于 0")
    if not math.isfinite(min_score) or not 0 <= min_score <= 1:
        raise ValueError("min_score 必须在 0 到 1 之间")
    corpus = retriever.chunks
    stats = retriever.stats()
    results: list[QueryResult] = []
    failures: list[dict[str, Any]] = []

    for item in eval_set.queries:
        # 旧调用形式保持兼容；只有显式开启闸门时传参数。
        hits = await retriever.aretrieve(
            item.query, k=k, **({"min_score": min_score} if min_score else {})
        )
        ranked = [h.chunk for h in hits]

        r = recall_at_k(ranked, item.gold, k, corpus)
        p = precision_at_k(ranked, item.gold, k)
        rr = reciprocal_rank(ranked, item.gold)
        ndcg = ndcg_at_k(ranked, item.gold, k, corpus)

        hit_rank = next((i for i, c in enumerate(ranked, 1) if _is_relevant(c, item.gold)), None)
        missing_gold = [
            c.model_dump(exclude_none=True)
            for c in item.gold
            if not any(c.matches(chunk) for chunk in ranked)
        ]
        coverage = (len(item.gold) - len(missing_gold)) / len(item.gold) if item.gold else 0.0

        results.append(
            QueryResult(
                id=item.id,
                query=item.query,
                difficulty=item.difficulty,
                category=item.category,
                answerable=item.answerable,
                recall=r,
                precision=p,
                rr=rr,
                ndcg=ndcg,
                hit_rank=hit_rank,
                retrieved=[f"{h.chunk.citation} ({h.score:.3f})" for h in hits],
                relevant_count=sum(_is_relevant(c, item.gold) for c in corpus),
                gold_coverage=coverage,
                complete_evidence=bool(item.gold) and not missing_gold,
                abstained=not hits,
                missing_gold=missing_gold,
            )
        )

        # 完全没召回到相关内容的查询单独记录：这是最有价值的改进线索
        if (item.answerable and coverage < 1) or (not item.answerable and hits):
            failures.append(
                {
                    "id": item.id,
                    "query": item.query,
                    "difficulty": item.difficulty,
                    "category": item.category,
                    "reason": (
                        "无答案查询仍返回了内容"
                        if not item.answerable
                        else "前 k 个结果里没有任何相关块"
                        if hit_rank is None
                        else "仅命中部分证据条件"
                    ),
                    "gold_conditions": [c.model_dump(exclude_none=True) for c in item.gold],
                    "actually_retrieved": [f"{h.chunk.citation} ({h.score:.3f})" for h in hits],
                    "note": item.note,
                    "missing_gold": missing_gold,
                }
            )

    positives = [r for r in results if r.answerable]
    metrics = _aggregate(positives)
    negatives = [r for r in results if not r.answerable]
    abstention = {
        "count": float(len(negatives)),
        "negative_return_rate": sum(not r.abstained for r in negatives) / len(negatives)
        if negatives
        else None,
        "abstention_rate": sum(r.abstained for r in negatives) / len(negatives)
        if negatives
        else None,
        "answerable_empty_rate": sum(r.abstained for r in positives) / len(positives)
        if positives
        else None,
    }

    # 按难度分层：如果 hard 类查询全挂，说明同义匹配能力不足
    by_diff: dict[str, dict[str, float]] = {}
    for level in sorted({r.difficulty for r in positives}):
        by_diff[level] = _aggregate([r for r in positives if r.difficulty == level])
    by_category = {
        category: _aggregate([r for r in positives if r.category == category])
        for category in sorted({r.category for r in positives})
    }

    return EvalReport(
        eval_set=eval_set.name,
        eval_set_sha256=fingerprint(eval_set.model_dump()),
        corpus_sha256=fingerprint([c.model_dump() for c in corpus]),
        parameters={"k": k, "min_score": min_score},
        retriever=str(stats.get("embedder", "unknown")),
        mode=str(stats.get("mode", "")),
        reranker=str(stats.get("reranker", "none")),
        k=k,
        chunk_count=len(corpus),
        metrics=metrics,
        by_difficulty=by_diff,
        by_category=by_category,
        abstention_metrics=abstention,
        per_query=results,
        failures=failures,
    )


def _aggregate(results: list[QueryResult]) -> dict[str, float]:
    n = max(len(results), 1)
    return {
        "count": float(len(results)),
        "recall": sum(r.recall for r in results) / n,
        "precision": sum(r.precision for r in results) / n,
        "mrr": sum(r.rr for r in results) / n,
        "ndcg": sum(r.ndcg for r in results) / n,
        "hit_rate": sum(r.hit_rank is not None for r in results) / n,
        "gold_coverage": sum(r.gold_coverage for r in results) / n,
        "complete_evidence_rate": sum(r.complete_evidence for r in results) / n,
    }
