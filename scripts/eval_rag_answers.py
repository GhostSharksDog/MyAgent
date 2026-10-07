"""公开单次检索的答案/出处核验：导出输入、离线评分、自检；从不调用模型。

python scripts/eval_rag_answers.py --export --dataset general --bundle-out data/rag-answers/bundle.json --template-out data/rag-answers/records.json
python scripts/eval_rag_answers.py --score --bundle data/rag-answers/bundle.json --records data/rag-answers/records.json --json-out data/rag-answers/report.json
python scripts/eval_rag_answers.py --self-test --dataset holdout --json-out data/rag-answers/self-test.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from app.rag.answer_audit import (
    AnswerRecord,
    AnswerReview,
    ClaimReview,
    EvidenceBundle,
    EvidenceQuote,
    audit_answers,
    collect_bundle,
)
from app.rag.benchmark import load_benchmark
from app.rag.chunker import ChunkStrategy
from app.rag.evaluate import fingerprint
from app.rag.rerank import LexicalReranker
from app.rag.retriever import RetrievalMode, Retriever

GENERAL = ROOT / "services/api/seed/rag_general"
HOLDOUT = ROOT / "services/api/seed/rag_holdout"


def public_inputs(dataset: str, rerank: str):  # type: ignore[no-untyped-def]
    if dataset == "general":
        docs, suite, metadata = load_benchmark(GENERAL)
    elif dataset == "holdout":
        from app.rag.holdout import load_holdout_benchmark

        docs, suite, metadata = load_holdout_benchmark(
            HOLDOUT, development_root=GENERAL
        )
    else:
        raise ValueError("仅接受公开 general 或 holdout 基准")
    if rerank == "lexical":
        reranker = LexicalReranker()
    elif rerank == "coverage":
        from app.rag.coverage import CoverageReranker

        reranker = CoverageReranker()
    else:
        raise ValueError("答案核验只接受离线 lexical/coverage 重排")
    retriever = Retriever.from_documents(
        docs,
        strategy=ChunkStrategy.SECTION,
        size=500,
        overlap=80,
        min_size=120,
        mode=RetrievalMode.HYBRID,
        reranker=reranker,
        rrf_k=60,
        fixed_recall_k=20,
    )
    return retriever, suite, metadata


async def export_bundle(dataset: str, rerank: str, k: int, max_chars: int):  # type: ignore[no-untyped-def]
    retriever, suite, metadata = public_inputs(dataset, rerank)
    params = {
        "dataset": dataset,
        "mode": "hybrid",
        "reranker": rerank,
        "reranker_name": retriever.stats()["reranker"],
        "strategy": "section",
        "size": 500,
        "overlap": 80,
        "min_size": 120,
        "rrf_k": 60,
        "recall_k": 20,
    }
    if rerank == "coverage":
        from app.rag.coverage import CoverageReranker

        params["reranker_parameters"] = CoverageReranker().parameters()
    else:
        params["reranker_parameters"] = {
            "implementation": "lexical-original",
            "weights": [1.0, 0.6, 0.4],
        }
    bundle = await collect_bundle(
        retriever,
        suite,
        k=k,
        max_chars=max_chars,
        parameters=params,
        provenance=metadata,
    )
    return bundle, suite, retriever.chunks


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def controlled_records(bundle: EvidenceBundle, suite: Any) -> list[AnswerRecord]:
    """用标签制造评分对照，仅供自检，禁止计入真实模型/人工质量指标。"""
    queries = {q.id: q for q in suite.queries}
    records: list[AnswerRecord] = []
    for question in bundle.questions:
        q = queries[question.id]
        selected = [
            s for s in question.sources if any(g.matches(s.chunk) for g in q.gold)
        ]
        claims: list[ClaimReview] = []
        answer = ""
        for source in selected:
            start = len(answer)
            answer += f"{source.chunk.text} [{source.number}]\n"
            claims.append(
                ClaimReview(
                    start=start,
                    end=len(answer),
                    verdict="supported",
                    evidence=[
                        EvidenceQuote(source=source.number, quote=source.chunk.text)
                    ],
                )
            )
        abstained = not selected
        if abstained:
            answer = "资料不足，无法依据本次片段确认所问事实。"
        correct = (
            not q.answerable
            if abstained
            else all(any(g.matches(s.chunk) for s in selected) for g in q.gold)
        )
        records.append(
            AnswerRecord(
                query_id=q.id,
                bundle_id=bundle.bundle_id,
                origin="controlled_fixture",
                answer=answer,
                declared_abstention=abstained,
                review=AnswerReview(
                    reviewer="synthetic-counterexample",
                    origin="controlled_fixture",
                    query_id=q.id,
                    bundle_id=bundle.bundle_id,
                    answer_sha256=fingerprint(answer),
                    complete=True,
                    answer_correct=correct,
                    is_abstention=abstained,
                    claims=claims,
                ),
            )
        )
    return records


async def self_test(args: argparse.Namespace) -> dict[str, Any]:
    bundle, suite, corpus = await export_bundle(
        args.dataset, args.rerank, args.k, args.max_chars
    )
    baseline = controlled_records(bundle, suite)
    good = audit_answers(bundle, suite, corpus, baseline)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool) -> None:
        checks.append({"name": name, "passed": passed})
        if not passed:
            raise ValueError(f"核验自检失败：{name}")

    check(
        "受控夹具不产生真实语义正确率",
        good["human_metrics"]["accuracy_on_reviewed"] is None,
    )
    check("夹具审核无结构错误", good["counts"]["review_errors"] == 0)
    check(
        "缺答保留总分母",
        audit_answers(bundle, suite, corpus, [])["counts"]["missing_records"]
        == len(suite.queries),
    )
    selected = next((r for r in baseline if r.review and r.review.claims), None)
    if selected is None:
        raise ValueError("自检需要至少一个有可见证据的正例")
    invalid = selected.model_copy(deep=True)
    invalid.answer += " 不存在的出处 [999999]"
    invalid.review = None
    bad_citation = audit_answers(bundle, suite, corpus, [invalid])
    check(
        "伪造编号不能通过引用检查",
        bad_citation["counts"]["invalid_citation_mentions"] == 1,
    )
    stale = selected.model_copy(deep=True)
    stale.answer += " 额外事实"
    stale_report = audit_answers(bundle, suite, corpus, [stale])
    check("改答案后旧审核失效", stale_report["counts"]["review_errors"] == 1)
    bad_quote = selected.model_copy(deep=True)
    bad_quote.review.claims[0].evidence[0].quote = "这是一条资料中不存在的完整引文"
    quote_report = audit_answers(bundle, suite, corpus, [bad_quote])
    check("捏造引文使审核无效", quote_report["counts"]["review_errors"] == 1)
    uncited = selected.model_copy(deep=True)
    uncited.answer = "存在答案，但没有提供出处。"
    uncited.review = None
    uncited_report = audit_answers(bundle, suite, corpus, [uncited])
    row = next(r for r in uncited_report["per_query"] if r["id"] == selected.query_id)
    check(
        "检索到证据不等于引用完整",
        row["visible_evidence_coverage"] > 0 and row["cited_evidence_coverage"] == 0,
    )
    # 出处可以有效，而结论是否支持仍未知，不能自动计为答对。
    misleading = selected.model_copy(deep=True)
    misleading.answer = f"资料不足，但真实预算确定为987654321元。 [{selected.review.claims[0].evidence[0].source}]"
    misleading.declared_abstention = True
    misleading.review = None
    misleading_report = audit_answers(bundle, suite, corpus, [misleading])
    check(
        "拒答声明和有效出处不能证明答案正确",
        misleading_report["structural_metrics"]["citation_validity"] == 1
        and misleading_report["human_metrics"]["accuracy_on_reviewed"] is None,
    )
    return {
        "origin": "controlled_fixture_only",
        "model_requests": 0,
        "checks": checks,
        "audit": good,
    }


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.self_test:
        return await self_test(args)
    if args.export:
        bundle, _, _ = await export_bundle(
            args.dataset, args.rerank, args.k, args.max_chars
        )
        if args.template_out:
            write_json(
                args.template_out,
                [
                    {
                        "query_id": q.id,
                        "bundle_id": bundle.bundle_id,
                        "origin": "model",
                        "answer": "",
                        "declared_abstention": False,
                        "review": None,
                    }
                    for q in bundle.questions
                ],
            )
        return bundle.model_dump(mode="json")
    bundle = EvidenceBundle.model_validate_json(args.bundle.read_bytes())
    params = bundle.parameters
    k, max_chars = params.get("k"), params.get("max_chars")
    if (
        type(k) is not int
        or not 1 <= k <= 10
        or type(max_chars) is not int
        or max_chars <= 0
    ):
        raise ValueError("bundle 参数无效")
    rebuilt, suite, corpus = await export_bundle(
        params.get("dataset"), params.get("reranker"), k, max_chars
    )
    if rebuilt.bundle_id != bundle.bundle_id:
        raise ValueError("bundle 与公开语料、冻结版本或实际管线不一致；请重新导出")
    raw = json.loads(args.records.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("答案记录必须为 JSON 数组")
    return audit_answers(
        bundle, suite, corpus, [AnswerRecord.model_validate(r) for r in raw]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="严格离线的单次检索答案与引用核验（无模型执行入口）"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--export", action="store_true")
    mode.add_argument("--score", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--dataset",
        choices=["general", "holdout"],
        help="导出/自检数据集，默认 general",
    )
    parser.add_argument(
        "--rerank", choices=["lexical", "coverage"], help="导出/自检重排，默认 lexical"
    )
    parser.add_argument(
        "--k", type=int, choices=range(1, 11), help="导出/自检检索数量，默认5"
    )
    parser.add_argument("--max-chars", type=int, help="导出/自检上下文预算，默认4000")
    parser.add_argument("--bundle-out", type=Path)
    parser.add_argument("--template-out", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--records", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    if args.export and (args.bundle or args.records or args.json_out):
        parser.error(
            "--export 只使用 --bundle-out/--template-out，不接受评分输入或 --json-out"
        )
    if not args.export and (args.bundle_out or args.template_out):
        parser.error("--bundle-out/--template-out 只适用于 --export")
    if args.self_test and (args.bundle or args.records):
        parser.error("--self-test 不读取答案文件；评分请用 --score")
    if args.score and any(
        v is not None for v in (args.dataset, args.rerank, args.k, args.max_chars)
    ):
        parser.error(
            "评分的管线参数由 --bundle 绑定，不能通过 --dataset/--rerank/--k/--max-chars 覆盖"
        )
    args.dataset = args.dataset or "general"
    args.rerank = args.rerank or "lexical"
    args.k = args.k or 5
    args.max_chars = args.max_chars if args.max_chars is not None else 4000
    if args.max_chars <= 0:
        parser.error("--max-chars 必须大于0")
    if args.export and not args.bundle_out:
        parser.error("--export 必须提供 --bundle-out")
    if args.score and (not args.bundle or not args.records):
        parser.error("--score 必须同时提供 --bundle 和 --records")
    inputs = [p.resolve() for p in (args.bundle, args.records) if p]
    outputs = [
        p.resolve() for p in (args.bundle_out, args.template_out, args.json_out) if p
    ]
    if len(outputs) != len(set(outputs)) or set(inputs) & set(outputs):
        parser.error("输出路径不能互相覆盖或覆盖评分输入")
    try:
        payload = asyncio.run(execute(args))
        output = args.bundle_out if args.export else args.json_out
        if output:
            write_json(output, payload)
        if args.export:
            print(
                f"已导出{len(payload['questions'])}个公开问题；bundle={payload['bundle_id']}，不含gold/参考答案；未调用模型。"
            )
        elif args.self_test:
            print(
                f"受控核验自检 {len(payload['checks'])} 项通过；真实模型请求0，语义正确率未知。"
            )
        else:
            print(
                json.dumps(
                    {
                        "counts": payload["counts"],
                        "structural_metrics": payload["structural_metrics"],
                        "human_metrics": payload["human_metrics"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    except (ValueError, TypeError, OSError) as exc:
        parser.error(
            f"答案核验无法完成：{exc}；请核对公开数据、bundle版本和记录/审核结构"
        )


if __name__ == "__main__":
    raise SystemExit(main())
