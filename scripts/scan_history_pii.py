"""全历史 PII 扫描：检查**每一个提交的每一个文件**，而不只是当前工作区。

【为什么必须扫历史而不是扫当前文件】

git 是**只追加**的：删掉一个文件不等于它消失，它还在旧提交里。
一旦把仓库推成公开，`git log -p` 任何人都能翻出被"删掉"的内容。

所以推送前的检查对象不是"现在有哪些文件"，而是
**"历史上出现过的所有 blob"**。这两者经常不一致 ——
典型场景就是先提交了敏感文件，后来才加进 .gitignore。

【为什么用 git cat-file --batch 而不是逐提交 checkout】
逐提交 checkout 是 O(提交数 × 工作区大小)，仓库一大就跑不动。
`git rev-list --objects --all` 列出所有可达对象，再用 `--batch`
批量读内容，每个 blob 只读一次 —— 去重后通常只有几十 MB，
秒级完成。**扫描工具自己不能慢到让人不想跑它。**

用法：
    python scripts/scan_history_pii.py            # 扫描
    python scripts/scan_history_pii.py --terms x  # 额外词表
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 通用模式：格式本身不含任何人信息，对任何使用者都适用
GENERIC_PATTERNS: dict[str, str] = {
    "手机号": r"1[3-9]\d{9}",
    "邮箱": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "身份证": r"\b\d{17}[\dXx]\b",
}

# 具体词表（姓名/学校/公司）从 gitignore 覆盖的文件读 ——
# 写在这里等于把要保护的东西和工具一起送出去
TERMS_FILE = ROOT / "data" / "pii-terms.txt"

# 【为什么必须跳过生成文件 —— 这是为了让报告还能被人看】
#
# `pnpm-lock.yaml` 里有 264 处邮箱命中，全部来自**第三方包的作者信息**
# （npm 注册表元数据里就带着），一条都不是本项目作者的。
#
# 危险的不是这 264 条本身，而是它们的后果：一份几百行的报告会让人
# 直接滚到底看结论，于是**真正的命中被淹没在噪音里**。
# 一个"反正总是红的"的检查，等于没有检查 —— 这和 PII 扫描工具本身
# 因编码问题静默崩溃是同一个结局，只是路径不同。
#
# 这些文件由工具生成、不由人编写，所以按文件名整体跳过（而不是按规则跳过：
# 规则跳过会让同一个文件里的别的内容也失去保护）。
_GENERATED_NAMES = {
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "Cargo.lock",
}


def is_generated(path: str) -> bool:
    """是否是"由工具生成、不由人编写"的文件（见 `_GENERATED_NAMES` 的说明）。"""
    name = path.rsplit("/", 1)[-1]
    return name in _GENERATED_NAMES or name.endswith(".min.js") or name.endswith(".min.css")


def load_terms(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        s.strip()
        for s in path.read_text(encoding="utf-8").splitlines()
        if s.strip() and not s.startswith("#")
    ]


def all_blobs() -> list[tuple[str, str]]:
    """返回 [(object_id, path)]，覆盖所有提交里出现过的文件。

    【踩坑记录：`text=True` 会在这里把工具整个搞崩，而且报错指向别处】

    `text=True` 用**本机区域编码**解码（这台机器是 GBK），而 git 输出的是
    UTF-8（仓库里有中文路径，且 `core.quotePath=false` 时路径不会被转义）。
    解码失败发生在 subprocess 的**后台读取线程**里，那个线程直接死掉，
    于是 `proc.stdout` 变成 `None` —— 上面那句 `.stdout` 在下一行
    `out.splitlines()` 处炸成一个 AttributeError：

        AttributeError: 'NoneType' object has no attribute 'splitlines'

    这个报错**完全没提编码**，指向的还是一个语法上没问题的表达式。
    我是靠"扫一次历史看看"才发现自己的 PII 扫描工具本身早就跑不动了 ——
    而跑不动的工具会让整条纪律（提交前必须扫）静默失效。

    所以这里显式指定 UTF-8，并额外把"读取线程死掉"这种情况报成一句人话。
    """
    proc = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        capture_output=True,
        encoding="utf-8",  # git 的输出是 UTF-8，与区域编码无关
        errors="replace",  # 万一有转义/二进制字节，也不要让读取线程炸掉
        cwd=ROOT,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git rev-list 失败（退出码 {proc.returncode}）：{(proc.stderr or '')[:300]}")
    out = proc.stdout
    if out is None:
        # 只有"读取线程死了"会走到这里（见上面那段注释），报成明确的原因
        raise SystemExit("无法读取 git 输出（子进程输出读取线程异常退出，通常是编码问题）")
    if not out.strip():
        raise SystemExit("git rev-list 没有输出 —— 这个仓库还没有任何提交？")

    pairs: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def read_blobs(oids: list[str]) -> dict[str, bytes]:
    """批量读取 blob 内容（每个只读一次）。"""
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        # 必须传 bytes：输出是二进制（blob 可能是任意字节），
        # 用 text=True 会让 git 的二进制内容在解码时炸掉。
        # 而一旦按二进制读，输入侧也必须给 bytes —— 两边要一致。
        input="\n".join(oids).encode("ascii"),
        capture_output=True,
        cwd=ROOT,
        check=True,
    )
    data = proc.stdout
    result: dict[str, bytes] = {}
    pos = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl == -1:
            break
        header = data[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(header) < 3:
            break
        oid, _type, size = header[0], header[1], int(header[2])
        body = data[pos : pos + size]
        pos += size + 1  # 跳过结尾的换行
        if _type == "blob":
            result[oid] = body
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="全历史 PII 扫描")
    ap.add_argument("--terms", default="", help="额外的逗号分隔词表")
    args = ap.parse_args()

    patterns: dict[str, str] = dict(GENERIC_PATTERNS)
    terms = load_terms(TERMS_FILE)
    for i, t in enumerate(terms):
        patterns[f"具体词[{t[:4]}…]"] = re.escape(t)
    for i, t in enumerate(x for x in args.terms.split(",") if x.strip()):
        patterns[f"额外词[{t.strip()}]"] = re.escape(t.strip())

    if not terms:
        print(f"⚠ 未找到 {TERMS_FILE}，具体词表为空 —— 只扫通用格式\n")

    print("=" * 70)
    print("全历史 PII 扫描（所有提交的所有文件）")
    print("=" * 70)

    pairs = all_blobs()
    oids = sorted({oid for oid, _ in pairs})
    print(f"可达文件条目 {len(pairs)} 个，去重后 blob {len(oids)} 个\n")

    contents = read_blobs(oids)
    path_of = {oid: path for oid, path in pairs}

    findings: list[dict[str, str]] = []
    binary_skipped = 0
    generated_skipped: set[str] = set()

    for oid, blob in contents.items():
        path = path_of.get(oid, "?")
        if is_generated(path):
            generated_skipped.add(path)
            continue
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            binary_skipped += 1
            continue
        for label, pat in patterns.items():
            for m in re.finditer(pat, text):
                findings.append(
                    {
                        "path": path,
                        "oid": oid[:10],
                        "label": label,
                        # 【只报告位置，不报告命中内容】——
                        # 否则扫描报告本身就成了新的泄露点
                        "offset": str(m.start()),
                    }
                )

    if binary_skipped:
        print(f"（跳过 {binary_skipped} 个二进制 blob）\n")
    if generated_skipped:
        print(f"（跳过 {len(generated_skipped)} 个生成文件，如 lock 文件 —— 见 is_generated 的说明）\n")

    if not findings:
        print("[OK] 全历史未发现 PII")
        print("=" * 70)
        return 0

    # 按文件聚合
    by_path: dict[str, list[dict[str, str]]] = {}
    for f in findings:
        by_path.setdefault(f["path"], []).append(f)

    print(f"[FAIL] 发现 {len(findings)} 处命中，涉及 {len(by_path)} 个文件：\n")
    for path, items in sorted(by_path.items()):
        labels = sorted({i["label"] for i in items})
        print(f"  {path}")
        print(f"      命中 {len(items)} 处：{', '.join(labels)}")
    print(
        "\n命中内容**不会**被打印 —— 否则这份报告就成了新的泄露点。\n"
        "处理方式：\n"
        "  · 若文件仍在工作区：删除并从 .gitignore 补充规则，然后改写历史\n"
        "    （git filter-repo 或 BFG；注意 force push 后所有协作者需重新克隆）\n"
        "  · 若只存在于历史：同样需要改写历史，删文件是不够的\n"
        "  · **在改写完成之前不要推送** —— 推送后历史即公开，无法撤回"
    )
    print("=" * 70)
    return 1


if __name__ == "__main__":
    sys.exit(main())
