"""RAG 检索质量评测脚本。

四种用法：

    # 1. 看清语料被切成了什么（写评测标注前必须先看这个）
    python scripts/eval_rag.py --inspect

    # 2. 校验评测集的标注是否合法（有没有标了却匹配不到任何块的条件）
    python scripts/eval_rag.py --validate

    # 3. 跑一次评测
    python scripts/eval_rag.py --run --mode hybrid --rerank lexical

    # 4. 跑完整消融阶梯，自动输出对比表（推荐）
    python scripts/eval_rag.py --compare
    python scripts/eval_rag.py --compare --with-llm      # 含 LLM 重排（会计费）

为什么要先 --inspect 再写标注：评测标注必须与真实的切分结果对齐。
凭想象写 gold 条件，很容易出现"标了一个语料里根本不存在的章节"，
导致 recall 分母为 0、指标全 0，然后误判成检索系统坏了。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.core.config import get_settings
from app.rag.benchmark import load_benchmark
from app.rag.chunker import ChunkStrategy
from app.rag.corpus import EMPTY_CORPUS_HINT, build_corpus
from app.rag.evaluate import (
    EvalReport,
    EvalSet,
    _is_relevant,
    evaluate,
    validate_labels,
)
from app.rag.rerank import LexicalReranker, LLMReranker, Reranker
from app.rag.retriever import RetrievalMode, Retriever

EVAL_SET_PUBLIC = ROOT / "services" / "api" / "seed" / "eval_set.json"
EVAL_SET_LOCAL = ROOT / "data" / "eval_set.local.json"
GENERAL_BENCHMARK = ROOT / "services" / "api" / "seed" / "rag_general"


def resolve_eval_set(use_sample: bool, dataset: str | None = None) -> Path:
    """决定用哪份评测集。

    【为什么评测集要分两份】
    可提交的那份（`seed/eval_set.json`）必须**与可提交的示例简历对齐**——
    否则别人 clone 之后标注因对不上语料而全部失效。它同时也不能包含
    任何取自真实简历的锚点（学校名、公司名等）。

    用户自己的那份（`data/eval_set.local.json`）锚定真实简历的内容，
    被 .gitignore 排除、只留在本地。检索真实简历时自动优先使用它。
    """
    if dataset == "general":
        return GENERAL_BENCHMARK / "eval_set.json"
    if not use_sample and EVAL_SET_LOCAL.exists():
        return EVAL_SET_LOCAL
    return EVAL_SET_PUBLIC


# ============================================================
# 语料自省
# ============================================================
def cmd_inspect(retriever: Retriever) -> int:
    stats = retriever.stats()
    print("=== 语料概况 ===")
    print(f"  块数        : {stats['chunk_count']}")
    print(f"  向量维度    : {stats['dim']}")
    print(f"  总字符数    : {stats['total_chars']}")
    print(f"  向量化器    : {stats['embedder']}")
    print(f"  检索模式    : {stats['mode']}   重排器：{stats['reranker']}")
    print(f"  BM25        : {stats['bm25']}")
    print(f"  按类型分布  : {stats['by_doc_type']}")
    print()

    current_doc = None
    for chunk in retriever.chunks:
        if chunk.doc_id != current_doc:
            current_doc = chunk.doc_id
            print(f"\n【文档】{current_doc}  ({chunk.doc_type})")
        preview = chunk.text.replace("\n", " ⏎ ")
        if len(preview) > 110:
            preview = preview[:110] + "…"
        print(
            f"  #{chunk.index:<2} [{chunk.section or '—':<8}] {len(chunk.text):>4}字  {preview}"
        )
        if chunk.metadata:
            print(f"       元数据: {chunk.metadata}")
    return 0


# ============================================================
# 标注校验
# ============================================================
def cmd_validate(retriever: Retriever, eval_set: EvalSet) -> int:
    corpus = retriever.chunks
    print(f"评测集：{eval_set.name}（{len(eval_set.queries)} 条查询）")
    print(f"语料：{len(corpus)} 个块\n")

    problems = validate_labels(eval_set, corpus)
    for i, item in enumerate(eval_set.queries, 1):
        matched = sum(1 for c in corpus if _is_relevant(c, item.gold))
        conds = " OR ".join(
            json.dumps(c.model_dump(exclude_none=True), ensure_ascii=False)
            for c in item.gold
        )
        status = "无答案" if not item.answerable else "OK " if matched else "空!"
        print(f"  {status} [{i:>2}] 匹配 {matched:>2} 块 | {item.query}")
        print(f"          条件: {conds}")

    print()
    if problems:
        print(f"[x] 发现 {len(problems)} 个标注问题：")
        for problem in problems:
            print(f"    {problem}")
        print("    标注条件失效或矛盾会扭曲指标，请先修正标注（见 --inspect 输出）。")
        return 1

    print("[OK] 每条正例证据都能匹配，无答案查询未标正例。语义仍需人工核对。")
    return 0


# ============================================================
# 单次评测
# ============================================================
async def cmd_run(
    eval_set: EvalSet,
    *,
    args: argparse.Namespace,
    json_out: Path | None = None,
    quiet: bool = False,
) -> EvalReport:
    retriever = build_retriever(args)
    report = await evaluate(
        retriever, eval_set, k=args.k, min_score=getattr(args, "min_score", 0.0)
    )
    report.parameters.update(
        {
            key: getattr(args, key, None)
            for key in (
                "strategy",
                "size",
                "overlap",
                "min_size",
                "mode",
                "rerank",
                "rewrite",
                "rrf_k",
                "rrf_weights",
                "recall_k",
            )
        }
    )
    report.parameters["retriever_stats"] = retriever.stats()
    report.provenance = {
        "source": "general-public"
        if getattr(args, "dataset", None) == "general"
        else "jobhunt-public"
        if args.sample
        else "declared-local"
    }
    if getattr(args, "dataset", None) == "general":
        _, _, metadata = load_benchmark(GENERAL_BENCHMARK)
        report.provenance.update(metadata)

    if quiet:
        return report

    print("=" * 76)
    print(f"检索评测报告  |  评测集: {report.eval_set}  |  管线: {report.pipeline()}")
    print(f"语料: {report.chunk_count} 块  |  k = {report.k}")
    print("=" * 76)
    print()
    print(f"  {report.summary_line()}")
    print(
        f"  证据条件覆盖={report.metrics['gold_coverage']:.3f}  完整证据率={report.metrics['complete_evidence_rate']:.3f}"
    )
    if report.abstention_metrics["count"]:
        print(
            f"  无答案非空返回率={report.abstention_metrics['negative_return_rate']:.3f}（仅检索层）"
        )

    # 重排成本：LLM 重排的代价是每查询一次额外调用，必须量化出来
    if "reranker_tokens" in retriever.stats():
        tokens = retriever.stats()["reranker_tokens"]
        n = max(len(eval_set.queries), 1)
        print(
            f"  重排成本：{tokens} tokens / {n} 条查询 = {tokens / n:.0f} tokens/查询"
        )
    print()

    print("--- 分查询类型（有答案）---")
    for category, metrics in report.by_category.items():
        print(
            f"  {category:<18} n={int(metrics['count']):>2} Recall={metrics['recall']:.3f} 完整证据={metrics['complete_evidence_rate']:.3f}"
        )

    print("--- 分难度 ---")
    for level, m in sorted(report.by_difficulty.items()):
        print(
            f"  {level:<8} n={int(m['count']):>2}  "
            f"Recall@{args.k}={m['recall']:.3f}  MRR={m['mrr']:.3f}"
        )
    print()

    print("--- 逐条结果 ---")
    for r in report.per_query:
        mark = "✓" if (r.hit_rank if r.answerable else r.abstained) else "✗"
        rank = (
            (f"第{r.hit_rank}位" if r.hit_rank else "未命中")
            if r.answerable
            else "无答案：空返回"
            if r.abstained
            else "无答案：非空返回"
        )
        print(
            f"  {mark} R={r.recall:.2f} P={r.precision:.2f} "
            f"MRR={r.rr:.2f} NDCG={r.ndcg:.2f} ({rank})  {r.query}"
        )

    if report.failures:
        print()
        print(f"--- 失败案例（{len(report.failures)} 条，这是最有价值的改进线索）---")
        for f in report.failures:
            print(f"\n  查询: {f['query']}  [{f['difficulty']}]")
            print(f"  期望: {f['gold_conditions']}")
            print(f"  实取: {f['actually_retrieved']}")
            if f.get("note"):
                print(f"  备注: {f['note']}")

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            json_out.write_text,
            report.model_dump_json(indent=2),
            encoding="utf-8",
            newline="\n",
        )
        print(f"\n[OK] 报告已写入 {json_out}")

    return report


# ============================================================
# 消融阶梯对比
# ============================================================
@dataclass
class LadderStep:
    label: str
    mode: RetrievalMode
    reranker: str
    rewrite: str = "none"


# 阶梯的顺序是刻意设计的：**先固定召回看重排的收益，再固定重排看召回的收益**。
# 这样每一步的 Δ 都能干净地归因到单一组件上。
#
#   ① → ②  换召回器（向量 vs 稀疏）
#   ① → ④  加重排（召回固定为向量）
#   ④ → ⑤  在重排之上再换混合召回（隔离"混合"的边际贡献）
#
# 如果只报①和⑤，你无法回答"提升是哪一步带来的" —— 这是消融实验最常见的错误。
_LADDER: list[LadderStep] = [
    LadderStep("① 纯向量（基线）", RetrievalMode.DENSE, "none"),
    LadderStep("② 纯 BM25", RetrievalMode.SPARSE, "none"),
    LadderStep("③ 混合 RRF", RetrievalMode.HYBRID, "none"),
    LadderStep("④ 向量 + 特征重排", RetrievalMode.DENSE, "lexical"),
    LadderStep("⑤ 混合 + 特征重排", RetrievalMode.HYBRID, "lexical"),
]

# Query 改写的对照阶梯。**必须单独成组，不能混进上面的主阶梯**：
# 上面每一步是"改召回/改重排"，这里是"改查询"，属于不同维度。
# 混在一起的话，⑥ 相对 ⑤ 的 Δ 里会同时含有"加了改写"和"用了不同召回配置"
# 两个变化 —— 那就又回到了"无法归因"的老问题。
#
# 所以设计上让"改写 vs 不改写"在**完全相同**的召回与重排配置下对比。
#
# R5/R6 是刻意加的：用来观察改写与重排的**交互**。
# 重排是用**原查询**对候选重新打分的，所以它有可能吃掉改写带来的排序收益 ——
# 这种事只有把两种组合都跑一遍才能发现，靠推理想不到。
_REWRITE_LADDER: list[LadderStep] = [
    LadderStep("R1 混合（= ③ 对照组）", RetrievalMode.HYBRID, "none", "none"),
    LadderStep("R2 混合 + Multi-Query", RetrievalMode.HYBRID, "none", "multi_query"),
    LadderStep("R3 混合 + HyDE", RetrievalMode.HYBRID, "none", "hyde"),
    LadderStep("R4 混合+重排（= ⑤ 对照组）", RetrievalMode.HYBRID, "lexical", "none"),
    LadderStep(
        "R5 混合+重排 + Multi-Query", RetrievalMode.HYBRID, "lexical", "multi_query"
    ),
    LadderStep("R6 混合+重排 + HyDE", RetrievalMode.HYBRID, "lexical", "hyde"),
]


async def cmd_compare(args: argparse.Namespace, eval_set: EvalSet) -> int:
    """跑完整消融阶梯，输出对比表。

    【为什么必须做阶梯而不是只测"最终方案"】
    只报最终方案的指标，你无法回答面试官最常追问的一句：
    "**这个提升是哪一步带来的？**"
    阶梯式消融让每一步的增量都可见，也避免把多步收益
    错误归因到某一个组件上。
    """
    steps = list(_LADDER)
    if args.with_llm:
        steps.append(LadderStep("⑥ 混合 + LLM 重排", RetrievalMode.HYBRID, "llm"))

    settings = (
        get_settings()
        if args.with_llm or getattr(args, "with_rewrite", False)
        else None
    )
    if args.with_llm and settings and not settings.llm.is_configured:
        print("[!] 未配置 LLM_API_KEY，跳过 LLM 重排步骤。")
        steps = [s for s in steps if s.reranker != "llm"]

    # 改写阶梯需要真实模型（改写本身要调 LLM），没配密钥就跳过
    if getattr(args, "with_rewrite", False):
        if settings and not settings.llm.is_configured:
            print("[!] 未配置 LLM_API_KEY，跳过 Query 改写阶梯。")
        else:
            steps.extend(_REWRITE_LADDER)

    print(f"评测集：{eval_set.name}（{len(eval_set.queries)} 条查询）")
    print(f"k = {args.k}，min_size = {args.min_size}，strategy = {args.strategy}")
    print()

    rows: list[tuple[str, EvalReport]] = []
    for step in steps:
        step_args = argparse.Namespace(**vars(args))
        step_args.mode = step.mode.value
        step_args.rerank = step.reranker
        step_args.rewrite = step.rewrite
        report = await cmd_run(eval_set, args=step_args, quiet=True)
        rows.append((step.label, report))
        print(f"  完成 {step.label}: {report.summary_line()}")
        print(
            f"    完整证据率={report.metrics['complete_evidence_rate']:.3f}  无答案非空返回率={report.abstention_metrics['negative_return_rate']}"
        )

    print()
    print("=" * 88)
    print("消融对比（同一评测集、同一语料、同一 k）")
    print("=" * 88)
    header = (
        f"{'管线':<22} {'Recall@k':>9} {'Δ':>7} {'MRR':>8} {'Δ':>7} "
        f"{'NDCG@k':>9} {'Δ':>7} {'命中率':>8}"
    )
    print(header)
    print("-" * 88)

    base = rows[0][1].metrics
    for label, report in rows:
        m = report.metrics
        d_recall = m["recall"] - base["recall"]
        d_mrr = m["mrr"] - base["mrr"]
        d_ndcg = m["ndcg"] - base["ndcg"]
        print(
            f"{label:<22} {m['recall']:>9.3f} {d_recall:>+7.3f} "
            f"{m['mrr']:>8.3f} {d_mrr:>+7.3f} "
            f"{m['ndcg']:>9.3f} {d_ndcg:>+7.3f} {m['hit_rate']:>8.3f}"
        )

    print()
    print("读表提示：")
    print("  - 看 Δ 列判断每一步的**增量贡献**，而不是只看最后一行有多高")
    print("  - Recall 高但 MRR 低 => 答案在候选集里但排序差，该做重排")
    print("  - Recall 本身就低 => 召回层问题，重排救不回来，该查切分与查询改写")

    if args.json_out:
        payload = [
            {
                "label": label,
                "pipeline": r.pipeline(),
                **r.model_dump(),
            }
            for label, r in rows
        ]
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        print(f"\n[OK] 对比结果已写入 {args.json_out}")

    return 0


# ============================================================
# 组装
# ============================================================
def build_reranker(kind: str) -> Reranker | None:
    if kind == "none":
        return None
    if kind == "lexical":
        return LexicalReranker()
    if kind == "llm":
        from app.llm.client import LLMClient

        return LLMReranker(LLMClient(get_settings().llm))
    raise ValueError(f"未知重排器：{kind}")


def build_rewriter(kind: str):  # type: ignore[no-untyped-def]
    """按 kind 构造改写器。none 返回 None（= 功能不启用）。"""
    from app.rag.rewrite import NoOpRewriter
    from app.rag.rewrite import build_rewriter as _build

    if (kind or "none").lower() in ("none", "", "off"):
        return None
    r = _build(kind, llm=_llm())
    # NoOpRewriter 传给 Retriever 与传 None 在行为上等价，
    # 但**语义不同**：None 表示"这个功能不存在"，NoOp 表示"启用了但没改动"。
    # 评测里统一返回 None，让基线路径与引入改写前逐字节一致。
    return None if isinstance(r, NoOpRewriter) else r


def _llm():  # type: ignore[no-untyped-def]
    from app.llm.client import LLMClient

    return LLMClient(get_settings().llm)


def parse_rrf_weights(raw: str | None) -> list[float] | None:
    if raw is None:
        return None
    parts = raw.split(",")
    if len(parts) != 2:
        raise ValueError("--rrf-weights 需要两个逗号分隔的数字，例如 '1.0,0.3'")
    weights = [float(part) for part in parts]
    if (
        any(not math.isfinite(weight) or weight < 0 for weight in weights)
        or sum(weights) <= 0
    ):
        raise ValueError("--rrf-weights 必须是有限非负数，且至少一路大于0")
    return weights


def build_retriever(args: argparse.Namespace) -> Retriever:
    weights = parse_rrf_weights(getattr(args, "rrf_weights", None))

    # --sample 是完整的数据源声明，不只是替换简历文件名。
    # 公开基准必须排除私人笔记及 .env 中声明的私人路径。
    if getattr(args, "dataset", None) == "general":
        if args.rerank == "llm" or getattr(args, "rewrite", "none") != "none":
            raise ValueError("通用公开基准只允许离线管线，不能使用模型重排或改写")
        docs, _, _ = load_benchmark(GENERAL_BENCHMARK)
    elif args.sample:
        docs = build_corpus(
            include_resume=True,
            include_jobs=True,
            use_sample_resume=True,
            include_notes=False,
            extra_paths=[],
        )
    else:
        agent = get_settings().agent
        include_seed = agent.profile == "jobhunt" or agent.corpus_include_seed
        docs = build_corpus(
            include_resume=include_seed,
            include_jobs=include_seed,
            include_notes=True,
            extra_paths=agent.corpus_path_list,
        )
    return Retriever.from_documents(
        docs,
        strategy=ChunkStrategy(args.strategy),
        size=args.size,
        overlap=args.overlap,
        min_size=args.min_size,
        mode=RetrievalMode(args.mode),
        reranker=build_reranker(args.rerank),
        rrf_k=args.rrf_k,
        rrf_weights=weights,
        rewriter=build_rewriter(getattr(args, "rewrite", "none")),
        **(
            {"fixed_recall_k": args.recall_k} if getattr(args, "recall_k", None) else {}
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAG 检索质量评测")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true", help="打印语料切块结构")
    mode.add_argument("--validate", action="store_true", help="校验评测集标注")
    mode.add_argument("--run", action="store_true", help="跑一次评测并输出指标")
    mode.add_argument("--compare", action="store_true", help="跑消融阶梯并输出对比表")

    parser.add_argument("--k", type=int, default=5, help="检索条数，默认 5")
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.0,
        help="原查询的 TF-IDF 相似度闸门，0关闭；仅供对照，不改变服务配置",
    )
    parser.add_argument(
        "--strategy",
        choices=[s.value for s in ChunkStrategy],
        default=ChunkStrategy.SECTION.value,
        help="切分策略，默认 section",
    )
    parser.add_argument("--size", type=int, default=500, help="块大小上限")
    parser.add_argument("--overlap", type=int, default=80, help="块间重叠字符数")
    parser.add_argument(
        "--min-size",
        type=int,
        default=120,
        help="小于此长度的块会被合并进相邻块（默认 120，消融证明的最优值）",
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in RetrievalMode],
        default=RetrievalMode.HYBRID.value,
        help="检索模式，默认 hybrid",
    )
    parser.add_argument(
        "--rerank",
        choices=["none", "lexical", "llm"],
        default="lexical",
        help="重排器。默认 lexical —— 它是消融实验里唯一稳定带来收益且零成本的选项",
    )
    parser.add_argument(
        "--rewrite",
        choices=["none", "multi_query", "hyde"],
        default="none",
        help=(
            "Query 改写方式，默认 none。会调用真实模型（每次查询多一次 LLM 调用），"
            "结果有缓存，所以消融阶梯里同一查询只付一次"
        ),
    )
    parser.add_argument("--rrf-k", type=int, default=60, help="RRF 平滑常数，默认 60")
    parser.add_argument(
        "--recall-k",
        type=int,
        default=None,
        help="召回阶段的候选数。默认 max(k*4, 20)；语料比它小时两路都会返回全量，"
        "融合会退化成‘用更噪的信号重排’",
    )
    parser.add_argument(
        "--rrf-weights",
        type=str,
        default=None,
        help="两路 RRF 权重，格式 '向量,BM25'（如 '1.0,0.3'）。默认等权",
    )
    parser.add_argument("--json-out", type=Path, default=None, help="把报告写成 JSON")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--sample",
        action="store_true",
        help="用可提交的示例简历与公开评测集（CI / 他人 clone 后应使用这个）",
    )
    source.add_argument(
        "--dataset",
        choices=["general"],
        help="60查询的通用公开基准，只加载清单文件，严格离线",
    )
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="--compare 时额外包含 LLM 重排步骤（真实调用 API，会计费）",
    )
    parser.add_argument(
        "--with-rewrite",
        action="store_true",
        help=(
            "--compare 时额外包含 Query 改写阶梯（Multi-Query / HyDE）。"
            "真实调用 API，会计费；改写结果有缓存，同一查询在整轮阶梯里只付一次"
        ),
    )
    args = parser.parse_args(argv)
    if (
        args.k <= 0
        or args.size <= 0
        or not 0 <= args.overlap < args.size
        or args.min_size < 0
    ):
        parser.error("k、size 必须大于0，overlap 必须在[0,size)内，min-size不能为负")
    if not math.isfinite(args.min_score) or not 0 <= args.min_score <= 1:
        parser.error("--min-score 必须在0到1之间")
    if args.rrf_k <= 0 or (args.recall_k is not None and args.recall_k <= 0):
        parser.error("--rrf-k 和 --recall-k 必须大于0")
    if args.dataset == "general" and (
        args.with_llm
        or args.with_rewrite
        or args.rerank == "llm"
        or args.rewrite != "none"
    ):
        parser.error("--dataset general 是离线基准，不接受模型重排或改写")
    try:
        parse_rrf_weights(args.rrf_weights)
        return execute(args)
    except (ValueError, OSError) as exc:
        parser.error(f"评测无法完成：{exc}；请核对数据清单、标注或 --json-out 路径")


def execute(args: argparse.Namespace) -> int:

    retriever = build_retriever(args)

    if len(retriever.chunks) == 0:
        print(EMPTY_CORPUS_HINT)
        print("    公开离线评测可用 --dataset general 或 --sample")
        return 1

    if args.inspect:
        return cmd_inspect(retriever)

    eval_set_path = resolve_eval_set(args.sample, args.dataset)
    if not eval_set_path.exists():
        print(f"[x] 找不到评测集：{eval_set_path}")
        return 1

    eval_set = EvalSet.load(eval_set_path)
    source = (
        eval_set_path.relative_to(ROOT)
        if eval_set_path.is_relative_to(ROOT)
        else eval_set_path
    )
    print(f"评测集来源：{source}\n")

    if args.validate:
        return cmd_validate(retriever, eval_set)

    problems = validate_labels(eval_set, retriever.chunks)
    if problems:
        raise ValueError(
            "标注无效：" + "; ".join(problems) + "；先运行 --inspect / --validate"
        )

    if args.compare:
        return asyncio.run(cmd_compare(args, eval_set))

    report = asyncio.run(cmd_run(eval_set, args=args, json_out=args.json_out))
    return 0 if report else 1


if __name__ == "__main__":
    raise SystemExit(main())
