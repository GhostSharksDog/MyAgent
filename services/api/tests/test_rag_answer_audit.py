"""引用、证据覆盖、语义审核必须有相互不能冒充的反例。"""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.agent.prompts import build_system_prompt
from app.rag.answer_audit import (
    AnswerRecord,
    AnswerReview,
    EvidenceBundle,
    EvidenceQuote,
    audit_answers,
    citation_numbers,
    collect_bundle,
)
from app.rag.chunker import Chunk
from app.rag.evaluate import EvalQuery, EvalSet, GoldCondition, fingerprint
from app.rag.loaders import DocType
from app.rag.retriever import assemble_context
from app.rag.store import SearchHit

ROOT = Path(__file__).resolve().parents[3]


def chunk(key: str, text: str) -> Chunk:
    return Chunk(id=key, doc_id=key + ".md", doc_type=DocType.NOTE, text=text, index=0)


class FixedRetriever:
    def __init__(self, chunks: list[Chunk], ranked: dict[str, list[Chunk]]) -> None:
        self.chunks, self.ranked = chunks, ranked

    async def aretrieve(self, query: str, k: int) -> list[SearchHit]:
        return [
            SearchHit(chunk=c, rank=i, score=0.5) for i, c in enumerate(self.ranked[query][:k], 1)
        ]


@pytest.fixture
async def inputs():  # type: ignore[no-untyped-def]
    date = chunk("date", "周五发布。错误率超过2%时回滚；达到2%无需回滚。")
    approver = chunk("approver", "发布需要值班员确认。")
    noise = chunk("noise", "食堂周五营业。")
    suite = EvalSet(
        name="unit",
        queries=[
            EvalQuery(
                id="pair",
                query="发布何时/谁确认",
                gold=[GoldCondition(doc_id="date.md"), GoldCondition(doc_id="approver.md")],
            ),
            EvalQuery(
                id="negative",
                query="下周五食堂每人收费多少",
                answerable=False,
                category="unanswerable",
            ),
        ],
    )
    corpus = [date, approver, noise]
    retriever = FixedRetriever(
        corpus, {suite.queries[0].query: corpus, suite.queries[1].query: [noise]}
    )
    return await collect_bundle(retriever, suite, k=3), suite, corpus


def record(bundle: EvidenceBundle, **changes: Any) -> AnswerRecord:
    return AnswerRecord.model_validate(
        {
            "query_id": "pair",
            "bundle_id": bundle.bundle_id,
            "origin": "model",
            "answer": "周五发布 [1]，需要值班员确认 [2]。",
            "declared_abstention": False,
            **changes,
        }
    )


def reviewed(
    row: AnswerRecord,
    *,
    verdict: str = "supported",
    complete: bool = True,
    quote: str = "周五发布。",
    **changes: Any,
) -> AnswerRecord:
    row = row.model_copy(deep=True)
    row.review = AnswerReview.model_validate(
        {
            "reviewer": "test-only-human-annotation",
            "origin": "human",
            "query_id": row.query_id,
            "answer_sha256": fingerprint(row.answer),
            "bundle_id": row.bundle_id,
            "complete": complete,
            "answer_correct": verdict == "supported",
            "is_abstention": False,
            "claims": [
                {
                    "start": 0,
                    "end": len(row.answer),
                    "verdict": verdict,
                    "evidence": [{"source": 1, "quote": quote}],
                }
            ],
            **changes,
        }
    )
    return row


async def test_export_contains_only_unlabelled_visible_context(inputs: Any) -> None:
    bundle, suite, corpus = inputs
    payload = bundle.model_dump(mode="json")
    raw = json.dumps(payload, ensure_ascii=False)
    assert "reference_answer" not in raw and '"gold"' not in raw and '"answerable"' not in raw
    for question in bundle.questions:
        messages = json.dumps(
            [m.model_dump() for m in question.generation_messages], ensure_ascii=False
        )
        assert question.id not in messages
        assert '"category"' not in messages and '"gold"' not in messages
    assert (
        bundle.questions[0].context
        == assemble_context(
            [SearchHit(chunk=c, rank=i, score=0.5) for i, c in enumerate(corpus, 1)]
        )[0]
    )
    short = await collect_bundle(
        FixedRetriever(corpus, {q.query: corpus for q in suite.queries}), suite, max_chars=1
    )
    assert len(short.questions[0].sources) == 1  # 保留首块语义
    row = record(short, answer="值班员确认 [2]。")
    report = audit_answers(short, suite, corpus, [row])
    assert report["per_query"][0]["invalid_citations"] == [2]
    assert report["per_query"][0]["visible_evidence_coverage"] == 0.5


def test_numeric_citations_ignore_code_and_links() -> None:
    text = "引用[1][2]，坏引用[0][999]；`[3]`\n```text\n[4]\n```\n链接[5](https://example.test)"
    assert citation_numbers(text) == [1, 2, 0, 999]


@pytest.mark.parametrize(
    "text",
    [
        "~~~text\n[1][2]\n~~~",
        "````text\n[1][2]\n`````",
        "```text\n[1][2]",
        "``[1] and ` [2]``",
        r"\[1] \[2]",
    ],
)
def test_code_fences_and_escaped_text_are_not_citations(text: str) -> None:
    assert citation_numbers(text) == []


def test_citation_followed_by_parenthetical_explanation_is_visible() -> None:
    assert citation_numbers("周五发布 [1] (当前制度)。") == [1]
    assert citation_numbers("[1](https://example.test)") == []


async def test_valid_citation_and_complete_gold_never_prove_semantics(inputs: Any) -> None:
    bundle, suite, corpus = inputs
    row = record(bundle, answer="发布一定不用任何人确认 [1][2]。")
    report = audit_answers(bundle, suite, corpus, [row])
    assert report["structural_metrics"]["citation_validity"] == 1
    assert report["per_query"][0]["complete_cited_evidence"] is True
    assert report["human_metrics"]["accuracy_on_reviewed"] is None
    assert report["counts"]["human_correctness_unknown"] == 2


async def test_multi_evidence_cited_coverage_ignores_uncited_retrieved_hit(inputs: Any) -> None:
    bundle, suite, corpus = inputs
    report = audit_answers(bundle, suite, corpus, [record(bundle, answer="周五发布 [1]。")])
    row = report["per_query"][0]
    assert row["visible_evidence_coverage"] == 1 and row["cited_evidence_coverage"] == 0.5
    assert row["complete_cited_evidence"] is False
    assert report["structural_metrics"]["complete_cited_evidence_rate"] == 0


async def test_missing_empty_and_negative_denominators_are_explicit(inputs: Any) -> None:
    bundle, suite, corpus = inputs
    empty = audit_answers(bundle, suite, corpus, [])
    assert empty["counts"]["missing_records"] == 2
    assert empty["structural_metrics"]["answer_completion_rate"] == 0
    assert empty["structural_metrics"]["citation_validity"] is None
    partial = audit_answers(bundle, suite, corpus, [record(bundle, answer="")])
    assert partial["counts"]["empty_answers"] == partial["counts"]["missing_records"] == 1
    assert partial["counts"]["positive"] == partial["counts"]["negative"] == 1


async def test_no_negative_or_no_citations_are_unknown_not_perfect(inputs: Any) -> None:
    _, suite, corpus = inputs
    suite.queries = suite.queries[:1]
    retriever = FixedRetriever(corpus, {suite.queries[0].query: corpus})
    bundle = await collect_bundle(retriever, suite)
    report = audit_answers(bundle, suite, corpus, [record(bundle, answer="答案没有引用")])
    assert report["structural_metrics"]["citation_validity"] is None
    assert report["structural_metrics"]["negative_declared_abstention_rate"] is None


async def test_human_semantics_distinguish_threshold_error_despite_exact_source(
    inputs: Any,
) -> None:
    bundle, suite, corpus = inputs
    row = reviewed(
        record(bundle, answer="达到2%就应回滚 [1]。"),
        verdict="unsupported",
        quote="错误率超过2%时回滚；达到2%无需回滚。",
    )
    report = audit_answers(bundle, suite, corpus, [row])
    assert report["counts"]["review_errors"] == 0
    assert report["structural_metrics"]["citation_validity"] == 1
    assert report["human_metrics"]["accuracy_on_reviewed"] == 0
    assert report["human_metrics"]["review_coverage"] == 0.5


async def test_declared_abstention_can_still_contain_fabricated_answer(inputs: Any) -> None:
    bundle, suite, corpus = inputs
    row = record(
        bundle,
        query_id="negative",
        answer="资料不足，但每人收费123元 [1]。",
        declared_abstention=True,
    )
    row = reviewed(row, verdict="unsupported", quote="食堂周五营业。", is_abstention=False)
    report = audit_answers(bundle, suite, corpus, [row])
    assert report["structural_metrics"]["negative_declared_abstention_rate"] == 1
    assert report["human_metrics"]["negative_abstention_on_reviewed"] == 0
    assert report["human_metrics"]["accuracy_on_reviewed"] == 0


async def test_partial_or_unknown_review_not_counted_as_full_accuracy(inputs: Any) -> None:
    bundle, suite, corpus = inputs
    row = reviewed(record(bundle), complete=False)
    report = audit_answers(bundle, suite, corpus, [row])
    assert report["human_metrics"]["accuracy_on_reviewed"] is None
    assert report["human_metrics"]["review_coverage"] == 0
    row.review.complete = True
    row.review.answer_correct = None
    report = audit_answers(bundle, suite, corpus, [row])
    assert report["human_metrics"]["accuracy_on_reviewed"] is None
    assert report["human_metrics"]["review_coverage"] == 0.5


@pytest.mark.parametrize(
    "mutation",
    [
        "answer",
        "bundle",
        "span",
        "overlap",
        "quote",
        "no_evidence",
        "unknown_source",
        "uncited_source",
        "contradiction",
        "fixture_label",
        "empty",
    ],
)
async def test_invalid_review_cannot_count_as_semantic_success(inputs: Any, mutation: str) -> None:
    bundle, suite, corpus = inputs
    row = reviewed(record(bundle))
    review = row.review
    if mutation == "answer":
        row.answer += "附加未经审核的事实"
    elif mutation == "bundle":
        review.bundle_id = "f" * 64
    elif mutation == "span":
        review.claims[0].end = len(row.answer.encode("utf-8"))  # 不是byte偏移
    elif mutation == "overlap":
        review.claims.append(review.claims[0].model_copy(deep=True))
    elif mutation == "quote":
        review.claims[0].evidence[0].quote = "周六发布。"
    elif mutation == "no_evidence":
        review.claims[0].evidence = []
    elif mutation == "unknown_source":
        review.claims[0].evidence[0].source = 999
    elif mutation == "uncited_source":
        review.claims[0].evidence = [EvidenceQuote(source=3, quote="食堂周五营业。")]
    elif mutation == "contradiction":
        review.claims[0].verdict = "unsupported"
    elif mutation == "fixture_label":
        row.origin = "controlled_fixture"
    elif mutation == "empty":
        row.answer = ""
        review.answer_sha256 = fingerprint("")
    report = audit_answers(bundle, suite, corpus, [row])
    assert report["counts"]["review_errors"] == 1
    assert report["human_metrics"]["accuracy_on_reviewed"] is None


async def test_controlled_fixture_never_enters_human_quality_denominator(inputs: Any) -> None:
    bundle, suite, corpus = inputs
    row = reviewed(record(bundle))
    row.origin = row.review.origin = "controlled_fixture"
    report = audit_answers(bundle, suite, corpus, [row])
    assert report["counts"]["controlled_fixture_records"] == 1
    assert report["counts"]["human_review_complete"] == 0
    assert report["human_metrics"]["accuracy_on_reviewed"] is None


async def test_review_cannot_be_moved_to_another_question_with_identical_sources(
    inputs: Any,
) -> None:
    _, suite, corpus = inputs
    suite.queries[1] = EvalQuery(
        id="who", query="由谁确认发布", gold=[GoldCondition(doc_id="approver.md")]
    )
    bundle = await collect_bundle(
        FixedRetriever(corpus, {q.query: corpus for q in suite.queries}), suite
    )
    row = reviewed(record(bundle, answer="周五发布 [1]。"))
    row.query_id = "who"
    report = audit_answers(bundle, suite, corpus, [row])
    assert report["per_query"][1]["cited_evidence_coverage"] == 0
    assert "另一个问题" in report["per_query"][1]["review_errors"][0]
    assert report["human_metrics"]["accuracy_on_reviewed"] is None


@pytest.mark.parametrize(
    "answer,start,end",
    [
        ("`周五发布 [1]。`", 1, -1),
        ("[1](https://example.test)", 0, 3),
        ("~~~text\n周五发布 [1]。\n~~~", 8, -4),
    ],
)
async def test_claim_slicing_cannot_turn_code_or_link_into_citation(
    inputs: Any, answer: str, start: int, end: int
) -> None:
    bundle, suite, corpus = inputs
    row = reviewed(record(bundle, answer=answer))
    row.review.claims[0].start = start
    row.review.claims[0].end = len(answer) + end if end < 0 else end
    report = audit_answers(bundle, suite, corpus, [row])
    assert report["per_query"][0]["citations"] == []
    assert report["counts"]["review_errors"] == 1
    assert report["human_metrics"]["accuracy_on_reviewed"] is None


async def test_mutating_record_types_cannot_bypass_strict_validation(inputs: Any) -> None:
    bundle, suite, corpus = inputs
    row = reviewed(record(bundle), complete=False)
    row.review.complete = "false"
    with pytest.warns(UserWarning, match="Pydantic serializer warnings"), pytest.raises(ValueError):
        audit_answers(bundle, suite, corpus, [row])


@pytest.mark.parametrize(
    "mutation",
    [
        "bundle_hash",
        "query_text",
        "source_text",
        "label_version",
        "corpus_version",
        "duplicate_records",
        "unknown_id",
        "wrong_bundle",
    ],
)
async def test_drift_and_duplicate_input_is_rejected(inputs: Any, mutation: str) -> None:
    bundle, suite, corpus = inputs
    records = [record(bundle)]
    if mutation == "bundle_hash":
        bundle.bundle_id = "a" * 64
    elif mutation in ("query_text", "source_text"):
        if mutation == "query_text":
            bundle.questions[0].query = "另一个问题"
        else:
            bundle.questions[0].sources[0].chunk.text = "伪造证据"
            bundle.questions[0].context = "\n\n---\n\n".join(
                f"[{s.number}] 出处：{s.chunk.citation}\n{s.chunk.text}"
                for s in bundle.questions[0].sources
            )
        bundle.bundle_id = fingerprint(bundle.model_dump(mode="json", exclude={"bundle_id"}))
    elif mutation == "label_version":
        suite.description += "漂移"
    elif mutation == "corpus_version":
        corpus = [c.model_copy(deep=True) for c in corpus]
        corpus[0].text += "漂移"
    elif mutation == "duplicate_records":
        records.append(records[0])
    elif mutation == "unknown_id":
        records[0].query_id = "not-present"
    else:
        records[0].bundle_id = "0" * 64
    with pytest.raises(ValueError):
        audit_answers(bundle, suite, corpus, records)


@pytest.mark.parametrize("profile", ["general", "jobhunt"])
def test_prompt_sufficiency_rules_inherit_and_disappear_with_tool(profile: str) -> None:
    prompt = build_system_prompt(profile, {"search_knowledge", "read_resume", "search_jobs"})
    assert "相关片段不代表资料足够" in prompt and "多次检索同时注明文档名和章节" in prompt
    absent = build_system_prompt(profile, set())
    assert "相关片段不代表资料足够" not in absent


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("答案核验不得读取配置/私人语料/网络")


def test_cli_export_score_and_self_test_are_strictly_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.core import config
    from app.rag import corpus

    module = runpy.run_path(str(ROOT / "scripts/eval_rag_answers.py"))
    monkeypatch.setattr(config, "get_settings", forbidden)
    monkeypatch.setattr(corpus, "build_corpus", forbidden)
    monkeypatch.setattr(corpus, "_load_notes", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    bundle, records, report = (
        tmp_path / name for name in ("bundle.json", "records.json", "report.json")
    )
    assert (
        module["main"](["--export", "--bundle-out", str(bundle), "--template-out", str(records)])
        == 0
    )
    assert (
        module["main"](
            [
                "--score",
                "--bundle",
                str(bundle),
                "--records",
                str(records),
                "--json-out",
                str(report),
            ]
        )
        == 0
    )
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["counts"]["expected"] == result["counts"]["empty_answers"] == 60
    assert result["human_metrics"]["accuracy_on_reviewed"] is None
    assert module["main"](["--self-test", "--json-out", str(report)]) == 0
    assert all(c["passed"] for c in json.loads(report.read_text(encoding="utf-8"))["checks"])
    # 自洽但伪造的导出不能绕过真实公开管线重建校验。
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    payload["parameters"]["recall_k"] = 2
    payload["bundle_id"] = fingerprint({k: v for k, v in payload.items() if k != "bundle_id"})
    bundle.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        module["main"](["--score", "--bundle", str(bundle), "--records", str(records)])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["--export"],
        ["--score"],
        ["--self-test", "--max-chars", "0"],
        ["--self-test", "--rerank", "llm"],
        ["--self-test", "--dataset", "private"],
        ["--export", "--bundle-out", "same.json", "--template-out", "same.json"],
        ["--score", "--bundle", "same.json", "--records", "other.json", "--json-out", "same.json"],
        ["--score", "--bundle", "bundle.json", "--records", "answers.json", "--dataset", "holdout"],
        ["--score", "--bundle", "bundle.json", "--records", "answers.json", "--rerank", "coverage"],
        ["--score", "--bundle", "bundle.json", "--records", "answers.json", "--k", "4"],
        ["--score", "--bundle", "bundle.json", "--records", "answers.json", "--max-chars", "3000"],
    ],
)
def test_cli_invalid_inputs_fail_before_running(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = runpy.run_path(str(ROOT / "scripts/eval_rag_answers.py"))
    monkeypatch.setitem(module["main"].__globals__, "execute", forbidden)
    with pytest.raises(SystemExit) as exc:
        module["main"](args)
    assert exc.value.code == 2
