r"""把本地仓库推送到 GitHub，且**不让令牌落盘**。

【为什么必须专门处理"令牌不落盘"这件事】

最省事的写法是把令牌拼进远端地址：

    git remote set-url origin https://<token>@github.com/user/repo.git

但那样令牌会被写进 `.git/config`（明文，且这个文件不会被 gitignore ——
它整个在 .git/ 里）。之后任何一次 `git remote -v`、任何一个能读到
你工作目录的脚本、任何一次误备份都会把它带出去。

用 `GIT_ASKPASS` 就不一样：git 每次需要密码时调用一个临时脚本，
那个脚本从**环境变量**里读令牌。令牌只存活在这个进程的环境里，
命令结束后就没了 —— 不进 argv（`ps` 看不到）、不进配置文件。

【为什么用 ASKPASS 而不是 `-c credential.helper=`】
禁用 helper 之后 git 会退回"交互式提示"，而本环境没有终端可以输入。
ASKPASS 是那个"非交互式提供凭据"的标准接口。

【本机实测的环境限制（都已处理）】
1. `git push` 经 SSH 会调用 msys 的 sh.exe，而沙箱禁止创建命名管道 →
   **SSH 推送在本环境不可行**，只能用 HTTPS。
2. 系统级 `credential.helper=manager` 同样会拉起 sh.exe 并崩溃 →
   推送时必须显式禁用（`-c credential.helper=`），由 ASKPASS 接管。

用法：
    # 方式 A：从环境变量读（推荐，令牌不进 shell 历史）
    $env:GITHUB_TOKEN = 'ghp_xxx'
    python scripts/push_github.py

    # 方式 B：交互式输入（不回显、不落盘）
    python scripts/push_github.py

    # 只检查远端与本地状态，不推送
    python scripts/push_github.py --dry-run
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OWNER = "GhostSharksDog"
REPO = "MyAgent"
HTTPS_URL = f"https://github.com/{OWNER}/{REPO}.git"
BRANCH = "master"


def git(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        env=env,
        check=False,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="推送到 GitHub（令牌不落盘）")
    ap.add_argument("--branch", default=BRANCH)
    ap.add_argument("--dry-run", action="store_true", help="只检查状态，不推送")
    args = ap.parse_args()

    print("=" * 66)
    print(f"推送到 https://github.com/{OWNER}/{REPO}")
    print("=" * 66)

    # ---------- 推送前检查 ----------
    status = git("status", "--porcelain").stdout.strip()
    if status:
        print("[!] 工作区有未提交的改动：")
        print(status[:500])
        print("    建议先提交或 stash —— 推送的只是已提交的内容，未提交的不会上去。\n")

    count = git("rev-list", "--count", "HEAD").stdout.strip()
    last = git("log", "--oneline", "-1").stdout.strip()
    print(f"本地：{count} 个提交，最新 {last}")

    remote = git("ls-remote", "--heads", "origin", args.branch)
    if remote.stdout.strip():
        print(f"远端：{args.branch} 已存在 -> {remote.stdout.split()[0][:10]}")
        print("      ⚠ 远端已有内容。若不是你之前推的，先确认再继续。")
    else:
        print(f"远端：{args.branch} 不存在（首次推送）")

    if args.dry_run:
        print("\n[--dry-run] 未执行推送")
        return 0

    # ---------- 取令牌 ----------
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print(
            "\n需要一个 **Personal Access Token**（不能用账号密码："
            "GitHub 从 2021-08-13 起已禁止密码用于 git 操作）。\n"
            "创建：https://github.com/settings/tokens\n"
            "  · 经典令牌：勾选 repo 权限\n"
            "  · 细粒度令牌：选 MyAgent 仓库，Contents 设为 Read and write\n"
        )
        try:
            token = getpass.getpass("粘贴令牌（输入不回显）：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return 1
    if not token:
        print("[x] 没有令牌，退出")
        return 1

    # ---------- 用 ASKPASS 提供凭据 ----------
    #
    # git 需要用户名和密码时各调用一次 askpass 脚本，靠提示词区分：
    # 提示里含 "Username" 就给用户名，否则给令牌。
    # 这个脚本写到临时目录，用完立刻删。
    with tempfile.TemporaryDirectory(prefix="jobpilot-push-") as tmp:
        askpass = Path(tmp) / "askpass.py"
        askpass.write_text(
            "import os, sys\n"
            "prompt = sys.argv[1] if len(sys.argv) > 1 else ''\n"
            "if 'username' in prompt.lower():\n"
            "    sys.stdout.write(os.environ.get('JP_GIT_USER', ''))\n"
            "else:\n"
            "    sys.stdout.write(os.environ.get('JP_GIT_TOKEN', ''))\n",
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["JP_GIT_USER"] = OWNER
        env["JP_GIT_TOKEN"] = token
        env["GIT_ASKPASS"] = f'"{sys.executable}" "{askpass}"'
        env["GIT_TERMINAL_PROMPT"] = "0"

        print("\n推送中……")
        result = git(
            # 必须显式禁用系统级 helper：本机 credential.helper=manager
            # 会拉起 sh.exe 并在沙箱里崩溃，反而挡住推送。
            "-c",
            "credential.helper=",
            "push",
            "-u",
            "origin",
            f"{args.branch}:{args.branch}",
        env=env,
        )

    # 输出可能含令牌吗？不会 —— git 只会回显 URL 与状态。
    out = (result.stdout + result.stderr).strip()
    print(out[-2000:] if out else "(无输出)")

    if result.returncode != 0:
        print(f"\n[x] 推送失败（退出码 {result.returncode}）")
        if "Authentication failed" in out or "403" in out:
            print(
                "    认证失败。常见原因：\n"
                "      · 用的是账号密码而不是 Personal Access Token\n"
                "      · 令牌过期或缺少 repo / Contents 权限"
            )
        elif "Could not resolve host" in out or "Failed to connect" in out:
            print("    网络不可达 —— 检查是否能访问 github.com:443")
        return 1

    print(f"\n[OK] 已推送 {args.branch} 到 https://github.com/{OWNER}/{REPO}")
    print(f"     https://github.com/{OWNER}/{REPO}/commits/{args.branch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
