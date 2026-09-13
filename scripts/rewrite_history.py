"""改写 git 历史：把某个文件在所有提交中替换成当前版本。

【为什么需要自己写，而不是用 filter-branch / filter-repo】

  · `git filter-repo` 本机没装
  · `git filter-branch` 需要 sh，而本机环境禁止创建命名管道
        sh.exe: *** fatal error - couldn't create signal pipe, Win32 error 5

所以这里只用 **git 的底层（plumbing）命令** —— 它们都是原生 git.exe，
不需要经过 shell：
    rev-list / cat-file / ls-tree / mktree / hash-object / update-ref

【为什么这件事必须在推送之前做】

git 是**只追加**的：删掉一个含 PII 的文件，不等于它从历史里消失。
一旦推送到公开仓库，`git log -p` 任何人都能翻出被"删掉"的内容。

而**未推送之前改写是免费的**：没有协作者、没有别人的 clone、没有 PR，
改完提交 hash 变了也没有任何人受影响。这个窗口一错过就再也没有了。

【为什么按"整个文件替换成当前版本"而不是按行替换】

要处理的文件只存在两个版本：含 PII 的初版、和现在的修订版。
所以"在每一个出现该文件的提交里，把它换成修订版"就是精确的语义，
而且**不依赖任何字符串匹配**（按行替换要处理引号、全角标点、编码，
每多一个匹配规则就多一处可能改错的地方）。

【为什么保留"未改动的树返回原 sha"这个性质】

只有真正需要改的提交才应该产生新 hash。如果一个提交的树完全不受影响
却拿到了新 sha，那么**所有后续提交的 hash 都会连锁变化** ——
本来 3 个提交要变，会变成 19 个全变。多出来的 16 次变化没有任何理由，
只会让"到底改了什么"变得难以核对。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*args: str, input_bytes: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["git", *args],
        input=input_bytes,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（{proc.returncode}）："
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def rewrite_tree(
    tree_sha: str,
    target: str,
    new_blob: str | None,
    prefix: str = "",
) -> tuple[str, bool]:
    """递归处理 target 路径。

    `new_blob` 为 None 时**删除**该条目，否则替换成该 blob。

    返回 (新树 sha, 是否有改动)。**没有改动时返回原 sha**，
    这样不受影响的提交 hash 完全不变。
    """
    raw = git("ls-tree", "-z", tree_sha)
    entries: list[tuple[str, str, str, str]] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        meta, name = item.split(b"\t", 1)
        mode, typ, sha = meta.decode().split()
        entries.append((mode, typ, sha, name.decode("utf-8", "surrogateescape")))

    changed_any = False
    new_entries: list[tuple[str, str, str, str]] = []
    for mode, typ, sha, name in entries:
        full = f"{prefix}{name}"
        if typ == "blob" and full == target:
            if new_blob is None:
                # 删除：不放进新树
                changed_any = True
                continue
            if sha != new_blob:
                sha = new_blob
                changed_any = True
            new_entries.append((mode, typ, sha, name))
        elif typ == "tree":
            sub_sha, sub_changed = rewrite_tree(sha, target, new_blob, f"{full}/")
            if sub_changed and sub_sha == "":
                # 子树变空了：连目录一起删掉（否则会留下空目录树）
                changed_any = True
                continue
            new_entries.append((mode, typ, sub_sha, name))
            changed_any = changed_any or sub_changed
        else:
            new_entries.append((mode, typ, sha, name))

    if not changed_any:
        # 关键：返回原树。否则所有后续提交 hash 都会无理由地连锁变化。
        return tree_sha, False

    if not new_entries:
        # 这个目录整个空了 —— 用空字符串表示"删除"
        return "", True

    payload = b"".join(
        f"{mode} {typ} {sha}\t{name}".encode("utf-8", "surrogateescape") + b"\0"
        for mode, typ, sha, name in new_entries
    )
    return git("mktree", "-z", input_bytes=payload).decode().strip(), True


def main() -> int:
    ap_args = sys.argv[1:]
    remove = False
    if ap_args and ap_args[0] == "--remove":
        remove = True
        ap_args = ap_args[1:]

    if not ap_args:
        print(__doc__)
        print("用法：")
        print("  python scripts/rewrite_history.py <路径>            # 替换成当前版本")
        print("  python scripts/rewrite_history.py --remove <路径>   # 从所有历史中删除")
        return 1
    target = ap_args[0].replace("\\", "/")

    if remove:
        new_blob: str | None = None
        print(f"模式：从所有历史中删除 {target}")
    else:
        # 当前版本（必须是已提交的状态）
        if git("status", "--porcelain", "--", target).strip():
            print(f"[x] {target} 有未提交的改动 —— 先提交它，否则替换进去的是不一致的内容")
            return 1
        new_blob = git("hash-object", "-w", "--", target).decode().strip()
        print(f"模式：把 {target} 在所有历史中替换为当前版本")
        print(f"替换 blob：{new_blob[:10]}")

    # 备份引用：改写失败时能一键回到原状
    head = git("rev-parse", "HEAD").decode().strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    backup = f"refs/backup/pre-rewrite-{head[:8]}"
    git("update-ref", backup, head)
    print(f"已建备份引用：{backup} -> {head[:10]}\n")

    commits = git("rev-list", "--reverse", "--topo-order", "HEAD").decode().split()
    print(f"待检查提交：{len(commits)} 个")

    mapping: dict[str, str] = {}
    rewritten = 0

    for sha in commits:
        raw = git("cat-file", "commit", sha).decode("utf-8", "surrogateescape")
        header, _, message = raw.partition("\n\n")
        lines = header.split("\n")

        tree = ""
        parents: list[str] = []
        rest: list[str] = []
        for line in lines:
            if line.startswith("tree "):
                tree = line[5:]
            elif line.startswith("parent "):
                parents.append(line[7:])
            else:
                rest.append(line)

        new_tree, changed = rewrite_tree(tree, target, new_blob)

        new_parents = [mapping.get(p, p) for p in parents]
        parent_changed = any(mapping.get(p, p) != p for p in parents)

        if not changed and not parent_changed:
            mapping[sha] = sha
            continue

        # 逐字节保留 author / committer / 时间戳 / 提交信息，
        # 只换 tree 与 parent —— 尽量少改，改写痕迹才可核对
        new_lines = [f"tree {new_tree}"]
        new_lines += [f"parent {p}" for p in new_parents]
        new_lines += rest
        new_commit = "\n".join(new_lines) + "\n\n" + message

        new_sha = (
            git("hash-object", "-t", "commit", "-w", "--stdin", input_bytes=new_commit.encode())
            .decode()
            .strip()
        )
        mapping[sha] = new_sha
        rewritten += 1
        print(f"  改写 {sha[:8]} -> {new_sha[:8]}  ({raw.splitlines()[0][:0]}tree 变了={changed})")

    new_head = mapping.get(head, head)
    if new_head == head:
        print("\n[OK] 没有任何提交需要改写")
        return 0

    git("update-ref", f"refs/heads/{branch}", new_head, head)
    print(f"\n[OK] {branch}: {head[:10]} -> {new_head[:10]}（改写 {rewritten} 个提交）")
    print(f"\n回滚方式：git update-ref refs/heads/{branch} {head}")
    print(f"          （备份引用 {backup}）")
    print("\n清理旧对象（确认无误后再执行）：")
    print("  git reflog expire --expire=now --all")
    print("  git gc --prune=now --aggressive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
