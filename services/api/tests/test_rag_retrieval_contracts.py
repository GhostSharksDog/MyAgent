"""检索的真实反例：零匹配、标题漏召回、同分漂移、改写串路与证据阶段。"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest
from app.rag.chunker import Chunk
from app.rag.embedder import TfidfEmbedder
from app.rag.evaluate import EvalQuery, EvalSet, GoldCondition, evaluate
from app.rag.loaders import DocType
from app.rag.ranking import positive_top_k
from app.rag.rerank import LexicalReranker, NoOpReranker
from app.rag.retriever import RetrievalMode, RetrievalTrace, Retriever
from app.rag.store import SearchHit, VectorStore
from app.rag.tokenizer import tokenize


def chunk(key: str, text: str, *, section: str = "", kind: DocType = DocType.NOTE) -> Chunk:
    return Chunk(id=key, doc_id=key + ".md", doc_type=kind, text=text, index=0, section=section)


def corpus() -> list[Chunk]:
    return [chunk("a", "alpha"), chunk("b", "bravo"), chunk("c", "cider")]


class Rewriter:
    name = "test"

    def __init__(self) -> None:
        self.calls = 0

    def stats(self) -> dict[str, int]:
        return {"calls": self.calls}

    async def rewrite(self, query: str) -> list[str]:
        self.calls += 1
        await asyncio.sleep(0)
        return [query, "bravo"]


@pytest.mark.parametrize("mode", list(RetrievalMode))
@pytest.mark.parametrize("query", ["qzxwvunknown", "", " \n\t"])
@pytest.mark.parametrize("rerank", [False, True])
async def test_no_matching_content_returns_empty(
    mode: RetrievalMode, query: str, rerank: bool
) -> None:
    r = Retriever(
        corpus(), TfidfEmbedder(), mode=mode, reranker=LexicalReranker() if rerank else None
    )
    assert await r.aretrieve(query, k=5) == []
    assert await r.aretrieve_context(query, k=5) == ""
    # 已知有匹配的对照，拒绝全部返回不能通过。
    assert [h.chunk.id for h in await r.aretrieve("alpha", k=5)] == ["a"]


@pytest.mark.parametrize("mode", list(RetrievalMode))
@pytest.mark.parametrize("k", [0, -1])
async def test_empty_request_does_not_start_rewrite(mode: RetrievalMode, k: int) -> None:
    rewrite = Rewriter()
    r = Retriever(corpus(), TfidfEmbedder(), mode=mode, rewriter=rewrite)
    assert await r.aretrieve("alpha", k=k) == []
    assert await r.aretrieve(" \t", k=3) == []
    assert rewrite.calls == 0


@pytest.mark.parametrize("mode", list(RetrievalMode))
async def test_section_only_term_is_recalled(mode: RetrievalMode) -> None:
    target = chunk("backup", "runs every Sunday", section="backup cadence")
    r = Retriever([target, chunk("other", "codename quartz")], TfidfEmbedder(), mode=mode)
    result = await r.aretrieve("backup", k=5)
    assert [h.chunk.id for h in result] == ["backup"]
    assert result[0].chunk.text == "runs every Sunday"
    assert result[0].chunk.id == target.id
    assert "index_text" not in result[0].chunk.model_dump()


def test_store_drops_zero_and_compacts_ranks() -> None:
    store = VectorStore(TfidfEmbedder())
    store.rebuild(corpus())
    assert store.search("qzxwvunknown", k=3) == []
    assert store.search("alpha", k=0) == []
    assert [(h.chunk.id, h.rank) for h in store.search("alpha", k=3)] == [("a", 0)]
    assert store.search("alpha", doc_types=["jd"]) == []


@pytest.mark.parametrize("k", [0, -1, 1, 3, 6, 100])
def test_positive_top_k_is_stable_at_tied_boundary(k: int) -> None:
    scores = np.array([2.0, np.nan, 4.0, 2.0, np.inf, 0.0, -1.0, 2.0, 4.0])
    original = scores.copy()
    expected = [2, 8, 0, 3, 7][: max(0, k)]
    assert positive_top_k(scores, k).tolist() == expected
    np.testing.assert_equal(scores, original)


def test_top_k_matches_full_sort_with_many_boundary_ties() -> None:
    rng = np.random.default_rng(812)
    for _ in range(20):
        scores = rng.integers(-2, 5, size=80).astype(float)
        expected = sorted(
            (i for i, score in enumerate(scores) if score > 0), key=lambda i: (-scores[i], i)
        )
        for k in (1, 5, 20, 200):
            assert positive_top_k(scores, k).tolist() == expected[:k]
    assert positive_top_k(np.array([0.0, -1.0, np.nan]), 5).tolist() == []


@pytest.mark.parametrize("mode", list(RetrievalMode))
async def test_rewrite_uses_only_selected_recall_routes(
    mode: RetrievalMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Retriever(corpus(), TfidfEmbedder(), mode=mode, rewriter=Rewriter())
    seen: dict[str, list[str]] = {"dense": [], "sparse": []}
    dense, sparse = r._dense_hits, r._sparse_ids

    def dense_call(query: str, k: int, types: list[str] | None) -> list[SearchHit]:
        assert mode != RetrievalMode.SPARSE, "纯 BM25 不得偷偷走向量召回"
        seen["dense"].append(query)
        return dense(query, k, types)

    def sparse_call(query: str, k: int, types: list[str] | None) -> list[str]:
        assert mode != RetrievalMode.DENSE
        seen["sparse"].append(query)
        return sparse(query, k, types)

    monkeypatch.setattr(r, "_dense_hits", dense_call)
    monkeypatch.setattr(r, "_sparse_ids", sparse_call)
    assert {h.chunk.id for h in await r.aretrieve("alpha", k=3)} == {"a", "b"}
    assert seen["dense"] == ([] if mode == RetrievalMode.SPARSE else ["alpha", "bravo"])
    assert seen["sparse"] == ([] if mode == RetrievalMode.DENSE else ["alpha", "bravo"])


@pytest.mark.parametrize("separator", ["，", "。", " ", "\n", "\t", "🙂", "—"])
def test_cjk_bigrams_do_not_cross_separators(separator: str) -> None:
    assert tokenize("验收" + separator + "发布") == ["验", "收", "验收", "发", "布", "发布"]
    assert "收发" in tokenize("验收发布")  # 连续文本对照


async def test_evidence_diagnostics_distinguish_each_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        chunk(key, key + " evidence") for key in ("returned", "filtered", "outside", "absent")
    ]
    r = Retriever(chunks, TfidfEmbedder(), reranker=NoOpReranker())
    calls = 0

    def candidates(*_: Any) -> list[SearchHit]:
        nonlocal calls
        calls += 1
        return [SearchHit(chunk=c, score=1, rank=i) for i, c in enumerate(chunks[:3])]

    monkeypatch.setattr(r, "_single_query_hits", candidates)
    monkeypatch.setattr(
        r, "_dense_score_map", lambda *_: {"returned": 0.8, "filtered": 0.1, "outside": 0.7}
    )
    suite = EvalSet(
        queries=[EvalQuery(query="evidence", gold=[GoldCondition(doc_id=c.doc_id) for c in chunks])]
    )
    report = await evaluate(r, suite, k=1, min_score=0.2, diagnostics=True)
    result = report.per_query[0]
    assert result.candidate_count == 3 and result.gated_count == 2
    assert [d["stage"] for d in result.evidence_diagnostics] == [
        "returned",
        "filtered_by_gate",
        "outside_top_k",
        "not_recalled",
    ]
    assert [d["candidate_rank"] for d in result.evidence_diagnostics] == [1, 2, 3, None]
    assert [d["returned_rank"] for d in result.evidence_diagnostics] == [1, None, None, None]
    assert result.gold_coverage == 0.25
    assert calls == 1, "诊断不得为了找原因重跑查询"
    assert report.failures[0]["evidence_diagnostics"] == result.evidence_diagnostics
    assert report.retrieval_policy == {
        "ranking_version": "positive-stable-v1",
        "tokenizer_version": "cjk-boundary-v2",
        "index_text_policy": "section-and-body-v1",
    }


async def test_original_query_gate_cannot_be_overridden_by_rewrite() -> None:
    r = Retriever(corpus(), TfidfEmbedder(), rewriter=Rewriter())
    trace = RetrievalTrace()
    assert await r.aretrieve("qzxwvunknown", k=3, min_score=0.1, trace=trace) == []
    assert trace.candidate_ids == ["b"]
    assert trace.gated_ids == trace.returned_ids == []


async def test_concurrent_traces_do_not_leak_between_queries() -> None:
    r = Retriever(corpus(), TfidfEmbedder(), rewriter=Rewriter())
    one, two = RetrievalTrace(), RetrievalTrace()
    await asyncio.gather(r.aretrieve("alpha", trace=one), r.aretrieve("cider", trace=two))
    assert set(one.returned_ids) == {"a", "b"}
    assert set(two.returned_ids) == {"b", "c"}
    await r.aretrieve("", trace=one)
    assert one.candidate_ids == one.gated_ids == one.returned_ids == []
    assert set(two.returned_ids) == {"b", "c"}


async def test_diagnostics_do_not_change_scores_or_repeat_rewrite() -> None:
    rewrite = Rewriter()
    r = Retriever(corpus(), TfidfEmbedder(), rewriter=rewrite, reranker=LexicalReranker())
    suite = EvalSet(queries=[EvalQuery(query="alpha", gold=[GoldCondition(doc_id="a.md")])])
    before = await evaluate(r, suite, k=2)
    after = await evaluate(r, suite, k=2, diagnostics=True)
    assert rewrite.calls == 2, "每次评测各调用一次，轨迹不能触发额外改写"
    assert before.metrics == after.metrics
    assert before.per_query[0].retrieved == after.per_query[0].retrieved
    assert before.per_query[0].candidate_count is None
    assert after.per_query[0].candidate_count == 2
