"""固定参数的公开重排实验，严格离线，不读取 .env、私人语料或模型。

python scripts/eval_rag_ranking.py --dataset general --json-out data/rag-ranking/general.json
python scripts/eval_rag_ranking.py --dataset legacy --json-out data/rag-ranking/legacy.json
holdout 仅在策略与参数冻结后独立评估，不能用来继续调参。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

from app.rag.benchmark import load_benchmark
from app.rag.chunker import ChunkStrategy
from app.rag.corpus import build_corpus
from app.rag.coverage import CoverageReranker
from app.rag.evaluate import EvalSet, evaluate, validate_labels
from app.rag.loaders import LoadedDocument
from app.rag.rerank import LexicalReranker
from app.rag.retriever import RetrievalMode, Retriever

SEED = ROOT / "services/api/seed"
EXPERIMENT_VERSION = "coverage-development-ablation-v1"
VARIANTS = {
    "lexical": None,
    "deduplicate": {"novelty_weight": 0.0, "redundancy_weight": 0.0},
    "novelty": {"novelty_weight": 0.25, "redundancy_weight": 0.0},
    "diversity": {"novelty_weight": 0.0, "redundancy_weight": 0.25},
    "coverage": {"novelty_weight": 0.25, "redundancy_weight": 0.25},
}


def load_public(dataset: str) -> tuple[list[LoadedDocument], EvalSet, dict[str, Any]]:
    if dataset == "holdout":
        from app.rag.holdout import load_holdout_benchmark

        return load_holdout_benchmark(
            SEED / "rag_holdout", development_root=SEED / "rag_general"
        )
    if dataset == "general":
        return load_benchmark(SEED / "rag_general")
    if dataset != "legacy":
        raise ValueError("dataset 必须为 general、legacy 或 holdout")
    suite = EvalSet.model_validate_json(
        (SEED / "eval_set.json").read_text(encoding="utf-8")
    )
    docs = build_corpus(
        include_resume=True,
        include_jobs=True,
        use_sample_resume=True,
        include_notes=False,
        extra_paths=[],
    )
    return (
        docs,
        suite,
        {
            "dataset_id": "legacy-public",
            "files": [
                {
                    "file": str(path.relative_to(ROOT)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in (
                    SEED / "resume.sample.md",
                    SEED / "jobs.json",
                    SEED / "eval_set.json",
                )
            ],
        },
    )


def _comparison(before: Any, after: Any) -> dict[str, Any]:
    improved, regressed, changed = [], [], []
    for index, (left, right) in enumerate(
        zip(before.per_query, after.per_query, strict=True)
    ):
        fields = ("gold_coverage", "recall", "rr", "ndcg")
        delta = {key: getattr(right, key) - getattr(left, key) for key in fields}
        entry = {
            "id": left.id or f"query-{index + 1}",
            "query": left.query,
            "category": left.category,
            "delta": delta,
            "before": left.model_dump(),
            "after": right.model_dump(),
        }
        if any(value > 1e-12 for value in delta.values()):
            improved.append(entry)
        if any(value < -1e-12 for value in delta.values()):
            regressed.append(entry)
        if left.retrieved != right.retrieved:
            changed.append(entry["id"])
    return {
        "improved": improved,
        "regressed": regressed,
        "changed_order_or_membership": changed,
    }


async def experiment(dataset: str, ks: list[int]) -> dict[str, Any]:
    docs, suite, provenance = load_public(dataset)
    results, comparisons = [], []
    for k in ks:
        baseline = None
        for label, options in VARIANTS.items():
            reranker = (
                LexicalReranker() if options is None else CoverageReranker(**options)
            )
            retriever = Retriever.from_documents(
                docs,
                strategy=ChunkStrategy.SECTION,
                size=500,
                overlap=80,
                min_size=120,
                mode=RetrievalMode.HYBRID,
                reranker=reranker,
                rrf_k=60,
            )
            problems = validate_labels(suite, retriever.chunks)
            if problems:
                raise ValueError("公开标注无效：" + "; ".join(problems))
            report = await evaluate(
                retriever, suite, k=k, min_score=0, diagnostics=True
            )
            report.parameters.update(
                variant=label,
                chunk_strategy="section",
                size=500,
                overlap=80,
                min_size=120,
                recall_k=max(k * 4, 20),
                rrf_k=60,
                rrf_weights=[1.0, 1.0],
                reranker_parameters=(
                    reranker.parameters()
                    if isinstance(reranker, CoverageReranker)
                    else {
                        "implementation": "lexical-original",
                        "weights": [1.0, 0.6, 0.4],
                    }
                ),
            )
            report.provenance.update(
                source="public-offline-ranking", dataset=provenance
            )
            results.append(report.model_dump())
            if baseline is None:
                baseline = report
            else:
                comparisons.append(
                    {"k": k, "variant": label, **_comparison(baseline, report)}
                )
            multi_metrics = report.by_category.get("multi_evidence")
            multi_display = (
                f"{multi_metrics['complete_evidence_rate']:.3f}"
                if multi_metrics
                else "n/a"
            )
            print(
                f"{dataset} k={k} {label}: {report.summary_line()} "
                f"complete={report.metrics.get('complete_evidence_rate', 0):.3f} "
                f"multi={multi_display}",
                flush=True,
            )
    return {
        "experiment_version": EXPERIMENT_VERSION,
        "strategy_source_sha256": hashlib.sha256(
            (ROOT / "services/api/app/rag/coverage.py").read_bytes()
        ).hexdigest(),
        "dataset": dataset,
        "selection_policy": "five predeclared development variants; holdout not used for tuning",
        "variants": VARIANTS,
        "model_requests": 0,
        "notes": [
            "仍使用固定 k 与相同召回；不改变 gold、语料、默认服务策略或相似度阈值。",
            "结果描述检索，不表示生成答案正确率、引用忠实性或模型拒答率。",
            "improved/regressed 分别纳入任一指标增/减的查询，同一查询可能同时出现。",
        ],
        "results": results,
        "comparisons": comparisons,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", choices=("general", "legacy", "holdout"), default="general"
    )
    parser.add_argument("--k", type=int, choices=(4, 5), help="缺省同时报告 k=4 与 k=5")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    report = asyncio.run(experiment(args.dataset, [args.k] if args.k else [4, 5]))
    target = args.json_out or ROOT / f"data/rag-ranking/{args.dataset}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"report: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
