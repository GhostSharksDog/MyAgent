"""集合重排需要有冗余／互补对照，不能用基准 gold 编写在线规则。"""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.rag.chunker import Chunk
from app.rag.coverage import COVERAGE_VERSION, CoverageReranker
from app.rag.loaders import DocType
from app.rag.rerank import LexicalReranker
from app.rag.store import SearchHit

ROOT = Path(__file__).resolve().parents[3]


def hit(key: str, text: str, *, section: str = "", doc: str = "one.md") -> SearchHit:
    return SearchHit(
        chunk=Chunk(id=key, doc_id=doc, doc_type=DocType.NOTE, text=text, section=section, index=0),
        score=0.1,
        rank=8,
    )


def ids(hits: list[SearchHit]) -> list[str]:
    return [item.chunk.id for item in hits]


@pytest.mark.parametrize("top_k", [-1, 0])
async def test_nonpositive_k_does_not_return_python_negative_slice(top_k: int) -> None:
    assert await CoverageReranker().rerank("alpha", [hit("a", "alpha")], top_k) == []


async def test_empty_candidates_are_empty() -> None:
    assert await CoverageReranker().rerank("alpha", [], 5) == []


async def test_exact_duplicates_do_not_consume_the_context_slots() -> None:
    candidates = [hit("a", "alpha report"), hit("b", "alpha report"), hit("c", "alpha detail")]
    deduplicated = await CoverageReranker().rerank("alpha", candidates, 3)
    unchanged = await CoverageReranker(
        deduplicate=False, novelty_weight=0, redundancy_weight=0
    ).rerank("alpha", candidates, 3)
    assert ids(deduplicated) == ["a", "c"]
    assert ids(unchanged) == ["a", "b", "c"]


async def test_duplicate_chunk_id_is_never_emitted_twice() -> None:
    candidates = [hit("a", "alpha report"), hit("a", "alpha detail")]
    assert len(await CoverageReranker(deduplicate=False).rerank("alpha", candidates, 2)) == 1


async def test_equal_text_with_different_headings_keeps_distinct_context() -> None:
    candidates = [hit("a", "alpha", section="policy"), hit("b", "alpha", section="delivery")]
    assert set(ids(await CoverageReranker().rerank("alpha", candidates, 2))) == {"a", "b"}


@pytest.mark.parametrize(
    "distinct", ["TOKEN = 'Secret'", "  token = 'secret'", "token  = 'secret'"]
)
async def test_case_and_whitespace_are_not_erased_when_deduplicating(distinct: str) -> None:
    candidates = [hit("a", "token = 'secret'"), hit("b", distinct)]
    assert len(await CoverageReranker().rerank("token", candidates, 2)) == 2


async def test_novelty_changes_repeated_query_coverage_without_losing_first_hit() -> None:
    candidates = [
        hit("a", "alpha beta policy"),
        hit("b", "alpha beta policy repeated"),
        hit("c", "gamma delivery"),
    ]
    ordinary = await CoverageReranker(novelty_weight=0, redundancy_weight=0).rerank(
        "alpha beta gamma", candidates, 2
    )
    coverage = await CoverageReranker(novelty_weight=1, redundancy_weight=0).rerank(
        "alpha beta gamma", candidates, 2
    )
    assert ids(ordinary) == ["a", "b"]
    assert ids(coverage) == ["a", "c"]


async def test_diversity_penalty_promotes_complementary_content() -> None:
    candidates = [
        hit("a", "alpha report shared facts"),
        hit("b", "alpha report shared facts repeated"),
        hit("c", "alpha separate audit"),
    ]
    ordinary = await CoverageReranker(novelty_weight=0, redundancy_weight=0).rerank(
        "alpha", candidates, 2
    )
    diverse = await CoverageReranker(novelty_weight=0).rerank("alpha", candidates, 2)
    assert ids(ordinary) == ["a", "b"]
    assert ids(diverse) == ["a", "c"]


async def test_same_document_can_supply_several_complementary_facts() -> None:
    candidates = [
        hit("a", "alpha policy"),
        hit("b", "beta delivery"),
        hit("c", "gamma noise", doc="other.md"),
    ]
    result = await CoverageReranker().rerank("alpha beta", candidates, 2)
    assert ids(result) == ["a", "b"]
    assert {item.chunk.doc_id for item in result} == {"one.md"}


async def test_all_disabled_matches_lexical_without_mutating_inputs() -> None:
    candidates = [hit("a", "alpha audit"), hit("b", "alpha beta policy"), hit("c", "beta delivery")]
    before = [item.model_dump() for item in candidates]
    ordinary = await LexicalReranker().rerank("alpha beta", candidates, 3)
    experimental = await CoverageReranker(
        novelty_weight=0, redundancy_weight=0, deduplicate=False
    ).rerank("alpha beta", candidates, 3)
    assert [item.model_dump() for item in ordinary] == [item.model_dump() for item in experimental]
    assert [item.model_dump() for item in candidates] == before
    assert [item.rank for item in experimental] == [0, 1, 2]
    assert {item.chunk.citation for item in experimental} == {
        item.chunk.citation for item in candidates
    }


async def test_ties_keep_original_relative_order() -> None:
    candidates = [hit("z", "alpha one"), hit("a", "alpha two"), hit("m", "alpha three")]
    baseline = ids(await CoverageReranker().rerank("alpha", candidates, 3))
    assert baseline == ["z", "a", "m"]
    for _ in range(3):
        assert ids(await CoverageReranker().rerank("alpha", candidates, 3)) == baseline


@pytest.mark.parametrize("name", ["novelty_weight", "redundancy_weight"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_weights_fail_explicitly(name: str, value: float) -> None:
    with pytest.raises(ValueError, match=name):
        CoverageReranker(**{name: value})


def test_experimental_parameter_record_describes_actual_algorithm() -> None:
    reranker = CoverageReranker(novelty_weight=0.1, redundancy_weight=0.2, deduplicate=False)
    parameters = reranker.parameters()
    assert parameters["implementation"] == COVERAGE_VERSION
    assert parameters["novelty_weight"] == 0.1
    assert parameters["redundancy_weight"] == 0.2
    assert parameters["deduplicate"] is False


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("排序实验读取了配置、私人语料或网络")


@pytest.mark.parametrize("dataset", ["general", "legacy"])
def test_public_experiment_never_reads_configuration_private_data_or_network(
    monkeypatch: pytest.MonkeyPatch, dataset: str
) -> None:
    from app.core import config
    from app.rag import corpus

    monkeypatch.setattr(config, "get_settings", forbidden)
    monkeypatch.setattr(corpus, "_load_notes", forbidden)
    monkeypatch.setattr(corpus, "_load_user_path", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    module = runpy.run_path(str(ROOT / "scripts/eval_rag_ranking.py"))
    docs, suite, provenance = module["load_public"](dataset)
    assert docs and suite.queries and provenance
    assert len(docs) == (16 if dataset == "general" else 7)


def test_holdout_entry_cannot_bypass_freeze_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.rag import holdout

    module = runpy.run_path(str(ROOT / "scripts/eval_rag_ranking.py"))
    calls = []

    def refuse(root: Path, *, development_root: Path) -> Any:
        calls.append((root, development_root))
        raise ValueError("冻结校验失败")

    monkeypatch.setattr(holdout, "load_holdout_benchmark", refuse)
    monkeypatch.setitem(module["load_public"].__globals__, "load_benchmark", forbidden)
    with pytest.raises(ValueError, match="冻结校验失败"):
        module["load_public"]("holdout")
    assert calls == [
        (ROOT / "services/api/seed/rag_holdout", ROOT / "services/api/seed/rag_general")
    ]


def test_ranking_cli_records_actual_source_hash_without_configuration_or_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hashlib
    import json

    from app.core import config
    from app.rag import corpus

    monkeypatch.setattr(config, "get_settings", forbidden)
    monkeypatch.setattr(corpus, "_load_notes", forbidden)
    monkeypatch.setattr(corpus, "_load_user_path", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    module = runpy.run_path(str(ROOT / "scripts/eval_rag_ranking.py"))
    output = tmp_path / "ranking.json"
    assert module["main"](["--dataset", "legacy", "--k", "4", "--json-out", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    expected_hash = hashlib.sha256(
        (ROOT / "services/api/app/rag/coverage.py").read_bytes()
    ).hexdigest()
    assert report["strategy_source_sha256"] == expected_hash
    assert report["model_requests"] == 0
    assert len(report["results"]) == 5
    assert all(len(result["per_query"]) == 14 for result in report["results"])
    assert all(result["k"] == 4 for result in report["results"])


def test_experiment_comparison_keeps_both_improvement_and_regression() -> None:
    from app.rag.evaluate import EvalReport, QueryResult

    module = runpy.run_path(str(ROOT / "scripts/eval_rag_ranking.py"))
    left = QueryResult(
        query="synthetic", difficulty="normal", recall=0.5, precision=0.5, rr=1, ndcg=0.5
    )
    right = left.model_copy(update={"recall": 1, "rr": 0.5})
    before = EvalReport(
        eval_set="synthetic", retriever="fixed", k=5, chunk_count=1, per_query=[left]
    )
    after = before.model_copy(update={"per_query": [right]})
    compared = module["_comparison"](before, after)
    assert len(compared["improved"]) == len(compared["regressed"]) == 1
    assert compared["improved"][0]["delta"]["recall"] == 0.5
    assert compared["regressed"][0]["delta"]["rr"] == -0.5
