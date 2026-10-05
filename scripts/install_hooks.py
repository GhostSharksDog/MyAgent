"""把版本化的 git hooks 安装到 .git/hooks。

【为什么不直接把脚本写进 .git/hooks】

`.git/` 目录**不进版本控制** —— 直接写进去的话，换一台机器克隆下来就没有 hook 了，
而且 hook 本身的改动也无法被 review。

所以做法是：hook 的真实内容放在 `scripts/hooks/`（随代码提交），
`.git/hooks/` 里只放一份**安装产物**。这样 hook 的每一次修改都能被 diff 看到。

【为什么不用 pre-commit 框架】

`pre-commit` 框架很好用，但它要求所有协作者都装它、并且它的配置在
`.pre-commit-config.yaml` 里另有一套抽象。这里只有一个 hook、
一件事要做，**直接装一个 shell 脚本更简单也更好排查**。

用法：
    python scripts/install_hooks.py          # 安装
    python scripts/install_hooks.py --list   # 查看当前状态
"""

from __future__ import annotations

import argparse
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "scripts" / "hooks"


def git_hooks_dir() -> Path:
    """用 git 自己解析 hooks 路径（尊重 core.hooksPath 配置）。"""
    out = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        capture_output=True,
        # 显式 UTF-8：`text=True` 默认按本机区域编码解码（中文 Windows 是 GBK），
        # 而 git 输出 UTF-8。解码失败会**在后台读取线程里**抛异常并把 stdout
        # 变成 None —— 报错指向"NoneType 没有 strip"，跟编码毫无关系。
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"不是 git 仓库或 git 不可用：{(out.stderr or '').strip()}")
    p = Path(out.stdout.strip())
    return p if p.is_absolute() else (ROOT / p)


def smoke_test(hook: Path) -> tuple[bool, str]:
    """真正执行一次 hook，确认它在本机跑得起来。

    【为什么安装器必须自检 —— 这是由一次真实事故换来的】

    我装好 hook 之后发现：**所有 git commit 都被阻断了**。
    原因是本机沙箱禁止创建命名管道，而 git 经 shebang 启动的解释器
    （`/usr/bin/env` → msys）需要它：

        env.exe: *** fatal error - couldn't create signal pipe, Win32 error 5

    hook 一失败，git 就中止提交。于是"加了一层保护"的直接后果是
    "谁也提交不了代码"。

    一个**不验证安装结果是否可用**的安装器，等于把"配置了一个跑不起来
    的东西"这件事延迟到别人真正用它的时候才暴露 —— 而那时它已经
    阻塞了对方的工作。

    这和健康检查里"只报配置不探活下游"是同一个道理的两面：
      · 探活下游 → 会导致级联故障，所以不做
      · 但**装到本机的东西**必须验证它能跑，因为失败的代价由本机承担，
        而且失败方式是"阻断提交"这种最烦人的一种

    跑不起来的 hook 应当**不安装**并明确说明原因，而不是装上去阻断提交。
    """
    try:
        proc = subprocess.run(
            [str(hook)],
            capture_output=True,
            encoding="utf-8",  # 见 git_hooks_dir 的注释（区域编码 ≠ git 的 UTF-8）
            errors="replace",
            cwd=ROOT,
            timeout=30,
            # 用 shell=False 直接执行，好让 Windows 按 shebang / PATHEXT 解析 ——
            # 这正是 git 调用 hook 的方式，也只有这样测出来的结果才算数。
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, f"退出码 {proc.returncode}；{detail[0] if detail else '无输出'}"
    return True, "ok"


def install(dst_dir: Path, *, skip_smoke: bool = False) -> int:
    dst_dir.mkdir(parents=True, exist_ok=True)
    installed = 0
    skipped = 0

    for src in sorted(SRC.iterdir()):
        if src.is_dir() or src.name.startswith("."):
            continue
        dst = dst_dir / src.name

        # 先按源文件内容做一次冒烟测试
        if not skip_smoke:
            ok, detail = smoke_test(src)
            if not ok:
                print(
                    f"[!] 跳过 {src.name}：它在本机跑不起来（{detail}）\n"
                    f"    已**不安装** —— 装一个会失败的 hook 会阻断所有 git commit，\n"
                    f"    那比没有 hook 严重得多。\n"
                    f"    本机的替代保护：\n"
                    f"      · tests/test_service_split.py::TestPowerShellEncoding（CI 门禁）\n"
                    f"      · scripts\\fix-bom.cmd（dev.ps1 已损坏时的恢复入口）"
                )
                # 如果之前装过一个坏掉的版本，把它清掉，否则提交会一直被阻断
                if dst.exists():
                    dst.unlink()
                    print(f"    已移除 %s 里此前安装的 {src.name}" % dst_dir)
                skipped += 1
                continue

        # 已经装了同样的内容就不动它 —— 保留 mtime，避免每次 setup 都触发文件变更
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            continue
        shutil.copy2(src, dst)
        # git 只执行有可执行位的 hook。Windows 上这个位没有实际意义，
        # 但仓库可能被 WSL / CI 的 Linux 环境使用，所以照样设上。
        dst.chmod(dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        print(f"[OK] 已安装 {src.name} -> {dst}")
        installed += 1

    if installed == 0 and skipped == 0:
        print("[OK] hooks 已是最新，无需改动")
    return 0


def list_status(dst_dir: Path) -> int:
    print(f"源目录：{SRC}")
    print(f"安装目录：{dst_dir}\n")
    for src in sorted(SRC.iterdir()):
        if src.is_dir() or src.name.startswith("."):
            continue
        dst = dst_dir / src.name
        if not dst.exists():
            state = "未安装"
        elif dst.read_bytes() == src.read_bytes():
            state = "已安装且一致"
        else:
            state = "**已安装但内容不同（需要重装）**"
        print(f"  {src.name}: {state}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="安装版本化的 git hooks")
    ap.add_argument("--list", action="store_true", help="只查看状态，不安装")
    ap.add_argument(
        "--skip-smoke-test",
        action="store_true",
        help="跳过可用性自检（**不推荐**：装一个跑不起来的 hook 会阻断所有提交）",
    )
    args = ap.parse_args()

    if not SRC.exists():
        print(f"找不到 hook 源目录：{SRC}")
        return 1

    dst = git_hooks_dir()
    if args.list:
        return list_status(dst)
    return install(dst, skip_smoke=args.skip_smoke_test)


if __name__ == "__main__":
    sys.exit(main())
