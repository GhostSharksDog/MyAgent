"""实验性集合重排：用新增查询词覆盖与文本重复程度分配有限的 top-k。

只读取原查询和召回候选，不使用标注、查询 ID、来源文件名或业务词表。
它没有判断资料能否作答的能力，默认 lexical 管线保持不变。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from app.rag.rerank import LexicalReranker, Reranker
from app.rag.store import SearchHit
from app.rag.tokenizer import tokenize, tokenize_query_filtered

COVERAGE_VERSION = "query-coverage-diversity-v1"


def _features(text: str) -> set[str]:
    """优先双字与技术词，避免通用单字让两段不同的资料看起来重复。"""
    terms = set(tokenize(text))
    specific = {term for term in terms if len(term) > 1}
    return specific or terms


def _similarity(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


@dataclass(frozen=True)
class _Candidate:
    hit: SearchHit
    lexical_score: float
    terms: set[str]
    matches: set[str]


class CoverageReranker(Reranker):
    """保留最相关首项，再贪心平衡相关性、新词覆盖与候选内容的多样性。

    新覆盖使用候选内 IDF，减弱高频查询词反复占位；重复惩罚使用词元
    Jaccard。完全相同的索引正文可去重，但不同章节或来源无需固定配额。
    同文档中的互补事实仍可同时返回。同分保留原有 lexical 相对顺序。

    参数是公开开发集实验之前固定的启发值，没有经过生产流量标定；
    首项保护与低相关候选的副作用需要在独立留出集上验证。
    """

    name = "coverage"
    implementation = COVERAGE_VERSION

    def __init__(
        self,
        *,
        novelty_weight: float = 0.25,
        redundancy_weight: float = 0.25,
        deduplicate: bool = True,
    ) -> None:
        for name, value in (
            ("novelty_weight", novelty_weight),
            ("redundancy_weight", redundancy_weight),
        ):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} 必须为 [0, 1] 内的有限值")
        self.novelty_weight = novelty_weight
        self.redundancy_weight = redundancy_weight
        self.deduplicate = deduplicate
        self._lexical = LexicalReranker()

    def parameters(self) -> dict[str, object]:
        return {
            "implementation": self.implementation,
            "novelty_weight": self.novelty_weight,
            "redundancy_weight": self.redundancy_weight,
            "deduplicate": self.deduplicate,
            "duplicate_policy": "exact-index-text",
            "protect_first": True,
            "lexical_normalization": 2.0,
            "similarity": "index-token-jaccard",
            "coverage": "unseen-query-token-candidate-idf",
        }

    async def rerank(self, query: str, hits: list[SearchHit], top_k: int) -> list[SearchHit]:
        if top_k <= 0 or not hits:
            return []
        query_terms = set(tokenize_query_filtered(query))
        specific = {term for term in query_terms if len(term) > 1}
        query_terms = specific or query_terms
        candidates: list[_Candidate] = []
        seen_ids: set[str] = set()
        seen_text: set[str] = set()
        ordered = sorted(hits, key=lambda hit: -self._lexical.score(query, hit))
        for hit in ordered:
            # 大小写与空白可能承载代码／标识符语义，不用分词后的等价替代原文相等。
            signature = hit.chunk.index_text
            if hit.chunk.id in seen_ids or (self.deduplicate and signature in seen_text):
                continue
            seen_ids.add(hit.chunk.id)
            seen_text.add(signature)
            terms = _features(hit.chunk.index_text)
            candidates.append(
                _Candidate(hit, self._lexical.score(query, hit), terms, terms & query_terms)
            )
        if not candidates:
            return []
        frequencies = Counter(term for item in candidates for term in item.matches)
        weights = {
            term: math.log((len(candidates) + 1) / (frequencies[term] + 1)) + 1
            for term in query_terms
        }
        total_weight = math.fsum(weights.values()) or 1.0
        chosen = [candidates.pop(0)]
        covered = set(chosen[0].matches)
        while candidates and len(chosen) < top_k:

            def utility(item: _Candidate) -> float:
                novelty = math.fsum(weights[term] for term in item.matches - covered) / total_weight
                redundancy = max(_similarity(item.terms, other.terms) for other in chosen)
                return (
                    item.lexical_score / 2
                    + self.novelty_weight * novelty
                    - self.redundancy_weight * redundancy
                )

            best = max(range(len(candidates)), key=lambda index: utility(candidates[index]))
            selected = candidates.pop(best)
            chosen.append(selected)
            covered.update(selected.matches)
        # 分数仍是 lexical 相关性，不能把集合边际增益冒充独立相关性或概率。
        return [
            item.hit.model_copy(update={"rank": rank, "score": item.lexical_score})
            for rank, item in enumerate(chosen)
        ]
