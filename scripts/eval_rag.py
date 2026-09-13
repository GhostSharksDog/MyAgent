"""RAG 检索质量评测脚本。

三种用法：

    # 1. 看清语料被切成了什么（写评测标注前必须先看这个）
    python scripts/eval_rag.py --inspect

    # 2. 校验评测集的标注是否合法（有没有标了却匹配不到任何块的条件）
    python scripts/eval_rag.py --validate

    # 3. 跑评测，输出指标
    python scripts/eval_rag.py --run
    python scripts/eval_rag.py --run --k 8 --strategy fixed

为什么要先 --inspect 再写标注：评测标注必须与真实的切分结果对齐。
凭想象写 gold 条件，很容易出现"标了一个语料里根本不存在的章节"，
导致 recall 分母为 0、指标全 0，然后误判成检索系统坏了。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.rag.chunker import ChunkStrategy  # noqa: E402
from app.rag.evaluate import EvalSet, _is_relevant, evaluate  # noqa: E402
from app.rag.retriever import Retriever  # noqa: E402

EVAL_SET_PUBLIC = ROOT / "services" / "api" / "seed" / "eval_set.json"
EVAL_SET_LOCAL = ROOT / "data" / "eval_set.local.json"


def resolve_eval_set(use_sample: bool) -> Path:
    """决定用哪份评测集。

    【为什么评测集要分两份】
    可提交的那份（`seed/eval_set.json`）必须**与可提交的示例简历对齐**——
    否则别人 clone 之后标注因对不上语料而全部失效。它同时也不能包含
    任何取自真实简历的锚点（学校名、公司名等）。

    用户自己的那份（`data/eval_set.local.json`）锚定真实简历的内容，
    被 .gitignore 排除、只留在本地。检索真实简历时自动优先使用它。
    """
    if not use_sample and EVAL_SET_LOCAL.exists():
        return EVAL_SET_LOCAL
    return EVAL_SET_PUBLIC


def cmd_inspect(retriever: Retriever) -> int:
    stats = retriever.stats()
    print("=== 语料概况 ===")
    print(f"  块数        : {stats['chunk_count']}")
    print(f"  向量维度    : {stats['dim']}")
    print(f"  总字符数    : {stats['total_chars']}")
    print(f"  向量化器    : {stats['embedder']}")
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
        print(f"  #{chunk.index:<2} [{chunk.section or '—':<8}] {len(chunk.text):>4}字  {preview}")

        # 带出元数据，便于写元数据过滤类的评测用例
        if chunk.metadata:
            print(f"       元数据: {chunk.metadata}")
    return 0


def cmd_validate(retriever: Retriever, eval_set: EvalSet) -> int:
    corpus = retriever.chunks
    print(f"评测集：{eval_set.name}（{len(eval_set.queries)} 条查询）")
    print(f"语料：{len(corpus)} 个块\n")

    problems = 0
    for i, item in enumerate(eval_set.queries, 1):
        matched = sum(1 for c in corpus if _is_relevant(c, item.gold))
        conds = " AND ".join(
            json.dumps(c.model_dump(exclude_none=True), ensure_ascii=False) for c in item.gold
        )
        status = "OK " if matched else "空!"
        if not matched:
            problems += 1
        print(f"  {status} [{i:>2}] 匹配 {matched:>2} 块 | {item.query}")
        print(f"          条件: {conds}")

    print()
    if problems:
        print(f"[x] 有 {problems} 条查询的标注匹配不到任何块。")
        print("    这类查询的 recall 分母为 0，无法评估。请先修正标注（见 --inspect 输出）。")
        return 1

    print("[OK] 所有标注都能匹配到至少一个块，评测集可用。")
    return 0


def cmd_run(retriever: Retriever, eval_set: EvalSet, k: int, json_out: Path | None) -> int:
    report = evaluate(retriever, eval_set, k=k)

    print("=" * 72)
    print(f"检索评测报告  |  评测集: {report.eval_set}  |  向量化器: {report.retriever}")
    print(f"语料: {report.chunk_count} 块  |  k = {report.k}")
    print("=" * 72)
    print()
    print(f"  {report.summary_line()}")
    print()

    print("--- 分难度 ---")
    for level, m in sorted(report.by_difficulty.items()):
        print(
            f"  {level:<8} n={int(m['count']):>2}  "
            f"Recall@{k}={m['recall']:.3f}  MRR={m['mrr']:.3f}"
        )
    print()

    print("--- 逐条结果 ---")
    for r in report.per_query:
        mark = "✓" if r.hit_rank else "✗"
        rank = f"第{r.hit_rank}位" if r.hit_rank else "未命中"
        print(f"  {mark} R={r.recall:.2f} P={r.precision:.2f} MRR={r.rr:.2f} NDCG={r.ndcg:.2f} ({rank})  {r.query}")

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
        json_out.write_text(
            report.model_dump_json(indent=2), encoding="utf-8", newline="\n"
        )
        print(f"\n[OK] 报告已写入 {json_out}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 检索质量评测")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true", help="打印语料切块结构")
    mode.add_argument("--validate", action="store_true", help="校验评测集标注")
    mode.add_argument("--run", action="store_true", help="跑评测并输出指标")
    parser.add_argument("--k", type=int, default=5, help="检索条数，默认 5")
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
        default=0,
        help="小于此长度的块会被合并进相邻块（用于消除碎片化与模板头部吸引子）",
    )
    parser.add_argument("--json-out", type=Path, default=None, help="把报告写成 JSON")
    parser.add_argument(
        "--sample",
        action="store_true",
        help="用可提交的示例简历与公开评测集（CI / 他人 clone 后应使用这个）",
    )
    args = parser.parse_args()

    strategy = ChunkStrategy(args.strategy)
    retriever = Retriever.from_default_corpus(
        strategy=strategy,
        size=args.size,
        overlap=args.overlap,
        min_size=args.min_size,
        use_sample_resume=args.sample,
    )

    if len(retriever.chunks) == 0:
        print("[x] 语料为空。请先准备数据：")
        print("    python scripts/ingest.py <简历.pdf> --type resume")
        return 1

    if args.inspect:
        return cmd_inspect(retriever)

    eval_set_path = resolve_eval_set(args.sample)
    if not eval_set_path.exists():
        print(f"[x] 找不到评测集：{eval_set_path}")
        return 1

    eval_set = EvalSet.load(eval_set_path)
    print(f"评测集来源：{eval_set_path.relative_to(ROOT)}\n")

    if args.validate:
        return cmd_validate(retriever, eval_set)

    return cmd_run(retriever, eval_set, args.k, args.json_out)


if __name__ == "__main__":
    raise SystemExit(main())
