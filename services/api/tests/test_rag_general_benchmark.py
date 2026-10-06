"""通用检索基准：正例、部分证据、无答案与数据隔离都要有反例。"""

from __future__ import annotations

import argparse
import json
import runpy
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.rag.benchmark import BenchmarkDocument, load_benchmark
from app.rag.chunker import Chunk, ChunkStrategy, chunk_documents
from app.rag.evaluate import EvalQuery, EvalSet, GoldCondition, evaluate, validate_labels
from app.rag.loaders import DocType
from app.rag.rerank import LexicalReranker
from app.rag.retriever import RetrievalMode, Retriever
from app.rag.store import SearchHit

ROOT = Path(__file__).resolve().parents[3]
PUBLIC = ROOT / "services/api/seed/rag_general"


def chunk(key: str, text: str, doc: str = "one.md") -> Chunk:
    return Chunk(id=key, doc_id=doc, doc_type=DocType.NOTE, text=text, index=0)


class FixedRetriever:
    def __init__(self, corpus: list[Chunk], ranked: dict[str, list[Chunk]]) -> None:
        self.chunks, self.ranked = corpus, ranked
        self.seen: list[float] = []

    def stats(self) -> dict[str, str]:
        return {"embedder": "fixed"}

    async def aretrieve(self, query: str, k: int, min_score: float = 0.0) -> list[SearchHit]:
        self.seen.append(min_score)
        return [SearchHit(chunk=c, score=0.8, rank=i) for i, c in enumerate(self.ranked[query][:k])]


async def test_partial_evidence_is_not_complete_even_with_first_hit() -> None:
    first, second = chunk("a", "date"), chunk("b", "approval", "two.md")
    queries = EvalSet(
        queries=[
            EvalQuery(
                id="pair",
                query="when/who",
                gold=[
                    GoldCondition(text_contains="date"),
                    GoldCondition(text_contains="approval"),
                ],
            )
        ]
    )
    result = await evaluate(FixedRetriever([first, second], {"when/who": [first]}), queries, k=2)
    assert result.metrics["hit_rate"] == 1
    assert result.metrics["recall"] == 0.5
    assert result.metrics["gold_coverage"] == 0.5
    assert result.metrics["complete_evidence_rate"] == 0
    assert result.failures[0]["reason"] == "仅命中部分证据条件"
    assert result.per_query[0].missing_gold == [{"text_contains": "approval"}]


async def test_two_conditions_in_one_chunk_do_not_require_two_documents() -> None:
    evidence = chunk("a", "date and approval")
    suite = EvalSet(
        queries=[
            EvalQuery(
                query="pair",
                gold=[GoldCondition(text_contains="date"), GoldCondition(text_contains="approval")],
            )
        ]
    )
    report = await evaluate(FixedRetriever([evidence], {"pair": [evidence]}), suite)
    assert report.metrics["recall"] == report.metrics["complete_evidence_rate"] == 1
    assert report.per_query[0].relevant_count == 1


@pytest.mark.parametrize("returns_noise", [False, True])
async def test_no_answer_metrics_do_not_dilute_positive_scores(returns_noise: bool) -> None:
    evidence, noise = chunk("a", "date"), chunk("n", "unrelated")
    suite = EvalSet(
        queries=[
            EvalQuery(query="positive", gold=[GoldCondition(text_contains="date")]),
            EvalQuery(query="negative", answerable=False, category="unanswerable"),
        ]
    )
    report = await evaluate(
        FixedRetriever(
            [evidence, noise],
            {"positive": [evidence], "negative": [noise] if returns_noise else []},
        ),
        suite,
    )
    assert report.metrics["recall"] == report.metrics["mrr"] == 1
    assert report.metrics["count"] == 1
    assert report.abstention_metrics["negative_return_rate"] == float(returns_noise)
    assert report.abstention_metrics["abstention_rate"] == float(not returns_noise)
    assert len(report.failures) == int(returns_noise)
    assert report.per_query[1].abstained is not returns_noise


async def test_no_negative_queries_is_unknown_instead_of_zero_error_rate() -> None:
    evidence = chunk("a", "date")
    suite = EvalSet(queries=[EvalQuery(query="q", gold=[GoldCondition(text_contains="date")])])
    report = await evaluate(FixedRetriever([evidence], {"q": []}), suite)
    assert report.abstention_metrics["count"] == 0
    assert report.abstention_metrics["negative_return_rate"] is None
    assert report.abstention_metrics["answerable_empty_rate"] == 1


async def test_explicit_threshold_and_fingerprints_follow_actual_inputs() -> None:
    evidence = chunk("a", "date")
    suite = EvalSet(queries=[EvalQuery(query="q", gold=[GoldCondition(text_contains="date")])])
    retriever = FixedRetriever([evidence], {"q": [evidence]})
    before = await evaluate(retriever, suite, min_score=0.2)
    assert retriever.seen == [0.2]
    assert before.parameters == {"k": 5, "min_score": 0.2}
    after = await evaluate(retriever, suite)
    assert before.eval_set_sha256 == after.eval_set_sha256
    assert before.corpus_sha256 == after.corpus_sha256
    evidence.text += "changed"
    changed = await evaluate(retriever, suite)
    assert before.corpus_sha256 != changed.corpus_sha256
    suite.queries[0].gold[0].text_contains = "changed"
    changed = await evaluate(retriever, suite)
    assert before.eval_set_sha256 != changed.eval_set_sha256


@pytest.mark.parametrize(
    "kwargs",
    [{"k": 0}, {"k": -1}, {"min_score": -0.1}, {"min_score": 1.1}, {"min_score": float("nan")}],
)
async def test_invalid_evaluation_parameters_fail(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        await evaluate(FixedRetriever([], {}), EvalSet(), **kwargs)


def test_exact_doc_id_does_not_accept_similarly_named_document() -> None:
    gold = GoldCondition(doc_id="guide.md", text_contains="date")
    assert gold.matches(chunk("a", "date", "guide.md"))
    assert not gold.matches(chunk("b", "date", "old-guide.md"))


@pytest.mark.parametrize(
    "queries,part",
    [
        (
            [
                EvalQuery(
                    query="q",
                    gold=[
                        GoldCondition(text_contains="date"),
                        GoldCondition(text_contains="absent"),
                    ],
                )
            ],
            "gold 2",
        ),
        ([EvalQuery(query="q", gold=[GoldCondition()])], "没有约束"),
        ([EvalQuery(query="q")], "缺少 gold"),
        (
            [EvalQuery(query="q", answerable=False, gold=[GoldCondition(text_contains="date")])],
            "不能同时",
        ),
        (
            [
                EvalQuery(id="a", query="q", answerable=False),
                EvalQuery(id="a", query="r", answerable=False),
            ],
            "id 重复",
        ),
        ([], "没有查询"),
    ],
)
def test_label_validation_catches_bad_branches(queries: list[EvalQuery], part: str) -> None:
    problems = validate_labels(EvalSet(queries=queries), [chunk("a", "date")])
    assert any(part in problem for problem in problems)


@pytest.mark.parametrize(
    "path",
    [
        "../private.md",
        "documents/../../private.md",
        "D:/secret.md",
        "documents\\secret.md",
        "/documents/a.md",
        "documents/.env",
        "documents/a.txt",
    ],
)
def test_manifest_cannot_select_private_or_unsupported_paths(path: str) -> None:
    with pytest.raises(ValueError):
        BenchmarkDocument(file=path, domain="x", sha256="a" * 64)


@pytest.fixture
def copied_benchmark(tmp_path: Path) -> Path:
    destination = tmp_path / "public"
    shutil.copytree(PUBLIC, destination)
    return destination


@pytest.mark.parametrize("file", ["documents/team-leave.md", "eval_set.json"])
def test_content_drift_is_rejected(copied_benchmark: Path, file: str) -> None:
    path = copied_benchmark / file
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="摘要不符"):
        load_benchmark(copied_benchmark)


def test_missing_document_is_not_silently_skipped(copied_benchmark: Path) -> None:
    (copied_benchmark / "documents/team-leave.md").unlink()
    with pytest.raises(FileNotFoundError):
        load_benchmark(copied_benchmark)


def test_duplicate_manifest_is_rejected(copied_benchmark: Path) -> None:
    path = copied_benchmark / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["documents"].append(manifest["documents"][0])
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="重复路径"):
        load_benchmark(copied_benchmark)


@pytest.mark.parametrize(
    "strategy", [ChunkStrategy.SECTION, ChunkStrategy.RECURSIVE, ChunkStrategy.FIXED]
)
def test_all_evidence_labels_survive_supported_chunking(strategy: ChunkStrategy) -> None:
    docs, suite, metadata = load_benchmark(PUBLIC)
    corpus = chunk_documents(docs, strategy=strategy, size=500, overlap=80, min_size=120)
    assert not validate_labels(suite, corpus)
    assert len(docs) == 16 and len(suite.queries) == 60
    assert Counter(q.category for q in suite.queries) == {
        category: 12
        for category in ("direct", "paraphrase", "distractor", "multi_evidence", "unanswerable")
    }
    assert all(q.id and q.reference_answer for q in suite.queries)
    assert len({q.query for q in suite.queries}) == 60
    assert all(q.gold or not q.answerable for q in suite.queries)
    assert {d.doc_type for d in docs} == {DocType.NOTE}
    assert {d.metadata_hint["domain"] for d in docs} == {"team", "project", "ops", "knowledge"}
    assert len(metadata["documents"]) == 16


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("公开评测读取了用户配置、私人数据或网络")


def test_general_cli_runs_without_configuration_private_data_or_network(
    monkeypatch: pytest.MonkeyPatch, copied_benchmark: Path, tmp_path: Path
) -> None:
    from app.rag import corpus

    module = runpy.run_path(str(ROOT / "scripts/eval_rag.py"))
    globals_ = module["main"].__globals__
    monkeypatch.setitem(globals_, "GENERAL_BENCHMARK", copied_benchmark)
    monkeypatch.setitem(globals_, "get_settings", forbidden)
    monkeypatch.setitem(globals_, "build_corpus", forbidden)
    monkeypatch.setattr(corpus, "_load_notes", forbidden)
    monkeypatch.setattr(corpus, "_load_user_path", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    unlisted = copied_benchmark / "documents/private-must-not-be-read.md"
    unlisted.write_text("PRIVATE SENTINEL", encoding="utf-8")
    retriever = module["build_retriever"](
        argparse.Namespace(
            dataset="general",
            sample=False,
            strategy="section",
            size=500,
            overlap=80,
            min_size=120,
            mode="hybrid",
            rerank="lexical",
            rrf_k=60,
        )
    )
    assert all("PRIVATE SENTINEL" not in c.text for c in retriever.chunks)
    output = tmp_path / "new/report.json"
    assert (
        module["main"](
            ["--compare", "--dataset", "general", "--diagnostics", "--json-out", str(output)]
        )
        == 0
    )
    reports = json.loads(output.read_text(encoding="utf-8"))
    assert len(reports) == 5
    assert all(len(r["per_query"]) == 60 and r["parameters"]["min_score"] == 0 for r in reports)
    assert all(r["parameters"]["diagnostics"] is True for r in reports)
    assert all(q["candidate_count"] is not None for r in reports for q in r["per_query"])
    assert all(
        r["metrics"]["count"] == 48 and r["abstention_metrics"]["count"] == 12 for r in reports
    )
    assert all(r["provenance"]["source"] == "general-public" for r in reports)


@pytest.mark.parametrize(
    "extra",
    [
        ["--with-llm"],
        ["--with-rewrite"],
        ["--rerank", "llm"],
        ["--rewrite", "hyde"],
        ["--sample"],
        ["--k", "0"],
        ["--min-score", "nan"],
        ["--rrf-weights", "nan,1"],
        ["--rrf-weights", "0,0"],
        ["--rrf-weights", "-1,1"],
    ],
)
def test_general_cli_rejects_paid_and_invalid_parameters_before_loading(
    monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    module = runpy.run_path(str(ROOT / "scripts/eval_rag.py"))
    monkeypatch.setitem(module["main"].__globals__, "build_retriever", forbidden)
    with pytest.raises(SystemExit) as exc:
        module["main"](["--dataset", "general", *extra])
    assert exc.value.code == 2


def test_legacy_regression_cannot_read_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.rag import corpus

    from tests.test_rag_regression import public_retriever

    monkeypatch.setattr(corpus, "_load_notes", forbidden)
    assert {str(c.doc_type) for c in public_retriever.__wrapped__().chunks} == {"resume", "jd"}


async def test_general_quality_floor_and_negative_control() -> None:
    docs, suite, _ = load_benchmark(PUBLIC)
    retriever = Retriever.from_documents(
        docs,
        strategy=ChunkStrategy.SECTION,
        min_size=120,
        mode=RetrievalMode.HYBRID,
        reranker=LexicalReranker(),
    )
    report = await evaluate(retriever, suite)
    floors = {"recall": 0.83, "mrr": 0.80, "ndcg": 0.79, "complete_evidence_rate": 0.78}
    assert all(report.metrics[key] >= floor for key, floor in floors.items())
    # 无答案门槛默认关闭：如实暴露12/12均返回内容，不把它写成模型拒答率。
    assert report.abstention_metrics["negative_return_rate"] == 1
    empty = FixedRetriever(retriever.chunks, {q.query: [] for q in suite.queries})
    broken = await evaluate(empty, suite)
    assert all(broken.metrics[key] < floor for key, floor in floors.items())
    assert broken.abstention_metrics["abstention_rate"] == 1
    assert broken.abstention_metrics["answerable_empty_rate"] == 1
