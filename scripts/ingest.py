"""文档摄取：把 PDF / DOCX / 文本规范化后写入 data/ 目录，供检索链路使用。

为什么需要这一步，而不是让检索直接读原文件？
  1. 解析只做一次，之后每次检索都直接读干净的文本，避免反复付解析成本
  2. 解析质量可以人工检查一次并修正，而不是每次运行时重新踩坑
  3. data/ 目录已被 .gitignore 排除，个人简历等隐私数据不会进版本库

用法::

    python scripts/ingest.py "D:\\path\\简历.pdf"
    python scripts/ingest.py "D:\\path\\简历.pdf" --type resume --out data/resume.md
    python scripts/ingest.py "D:\\path\\岗位.docx" --type jd --out data/target_jd.md

不加参数时运行内置自检（不读任何外部文件）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.rag.loaders import DocType, LoadError, load_document  # noqa: E402


def quality_report(text: str) -> dict[str, object]:
    """对提取结果做质量评估。

    PDF 解析最常见的失败模式是**双栏排版被交错读取**，表现为：
    单行很短、行数很多、且相邻行的语义不连贯。
    这里用可量化的信号做初筛，而不是靠肉眼看几行就下结论。
    """
    lines = text.split("\n")
    non_empty = [ln for ln in lines if ln.strip()]
    lengths = [len(ln) for ln in non_empty]

    avg_len = sum(lengths) / len(lengths) if lengths else 0.0
    # 中文字符占比：低于 20% 且文档本该是中文简历时可疑
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    cjk_ratio = cjk / len(text) if text else 0.0

    suspicious: list[str] = []
    if non_empty and avg_len < 8:
        suspicious.append(
            f"非空行平均长度仅 {avg_len:.1f} 字符，疑似 PDF 按视觉行硬换行或列交错"
        )
    if len(non_empty) > 0 and len(non_empty) / max(len(text), 1) > 0.08:
        suspicious.append("行密度异常高，可能每行都被当成独立段落")
    if cjk_ratio < 0.2:
        suspicious.append(f"中文字符占比仅 {cjk_ratio:.0%}，可能提取到了乱码或英文层")

    return {
        "总字符数": len(text),
        "总行数": len(lines),
        "非空行数": len(non_empty),
        "非空行平均长度": round(avg_len, 1),
        "最长行长度": max(lengths) if lengths else 0,
        "中文字符占比": f"{cjk_ratio:.0%}",
        "疑点": suspicious,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="把文档规范化写入 data/ 目录")
    parser.add_argument("source", nargs="?", help="源文件路径（pdf / docx / md / txt）")
    parser.add_argument("--out", default=None, help="输出路径，默认按类型决定")
    parser.add_argument(
        "--type",
        choices=[t.value for t in DocType],
        default=None,
        help="文档类型，不传则按文件名猜测",
    )
    parser.add_argument("--preview", type=int, default=800, help="预览前 N 个字符")
    parser.add_argument("--quiet", action="store_true", help="只输出统计，不预览内容")
    args = parser.parse_args()

    if not args.source:
        print(__doc__)
        print("[!] 未提供源文件，未执行任何操作。")
        return 0

    doc_type = DocType(args.type) if args.type else None

    print(f"读取：{args.source}")
    try:
        doc = load_document(args.source, doc_type)
    except LoadError as exc:
        print(f"[x] {exc}")
        return 1

    print(f"  类型      : {doc.doc_type}")
    print(f"  页数      : {doc.page_count or '（非 PDF）'}")
    print(f"  字符数    : {doc.char_count}")
    for w in doc.warnings:
        print(f"  [!] 警告  : {w}")

    report = quality_report(doc.text)
    print("\n--- 提取质量评估 ---")
    for k, v in report.items():
        if k == "疑点":
            continue
        print(f"  {k:14s}: {v}")
    issues = report["疑点"]
    if issues:
        print("  疑点:")
        for i in issues:
            print(f"    - {i}")
    else:
        print("  疑点: 无（行结构正常）")

    # 输出路径
    default_out = {
        DocType.RESUME: "data/resume.md",
        DocType.JD: "data/target_jd.md",
        DocType.NOTE: "data/note.md",
    }[doc.doc_type]
    out_path = ROOT / (args.out or default_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header = f"<!-- 来源: {doc.source} | 类型: {doc.doc_type} | 由 scripts/ingest.py 生成 -->\n\n"
    out_path.write_text(header + doc.text + "\n", encoding="utf-8", newline="\n")
    print(f"\n[OK] 已写入 {out_path.relative_to(ROOT)}（{out_path.stat().st_size} 字节）")
    print("     该目录已被 .gitignore 排除，不会进入版本库。")

    if not args.quiet and args.preview > 0:
        print(f"\n--- 内容预览（前 {args.preview} 字符）---")
        print(doc.text[: args.preview])
        if len(doc.text) > args.preview:
            print(f"\n... （共 {len(doc.text)} 字符，其余略）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
