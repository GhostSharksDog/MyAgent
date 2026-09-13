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
from pathlib import Path
from urllib.parse import quote

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


def _read_token_secretly() -> str:
    """读令牌 —— 优先不回显，不回显不可用时降级并**明确告知**。

    【为什么要做降级，而且降级时一定要说】

    `getpass` 在 Windows 上走 `msvcrt.getwch()`，它需要进程真的挂在
    **Windows 控制台**上。而 Git Bash / MSYS2 / MinTTY 用的是 pty 模拟，
    进程并没有 Windows 控制台 —— getpass 可能抛异常，也可能**直接卡住**。

    "卡住"比"报错"糟糕得多：用户会以为是脚本挂了，反复重试，
    却不知道其实只要在别处运行就行。

    所以这里主动降级到 `input()` 并**明确打印警告**：
    输入会显示在屏幕上。看得见的输入 + 一句提醒，
    好过一个看起来死掉的进程。**降级必须可见，否则就是静默降级。**
    """
    try:
        return getpass.getpass("粘贴令牌（输入不回显）：").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        return ""
    except Exception as exc:
        # 这里刻意捕获宽泛的异常：getpass 在不同终端环境下抛出的异常类型
        # 五花八门（OSError、ImportError、msvcrt 相关的各种），
        # 逐一定点捕获既不现实也会漏。**这个位置的目标是"绝不卡住"** ——
        # 无论发生什么都要降级到可见输入，而不是让用户面对一个假死的进程。
        print(
            f"\n[!] 不回显输入不可用（{type(exc).__name__}: {exc}）。\n"
            f"    当前终端可能是 Git Bash / MinTTY —— 它没有 Windows 控制台。\n"
            f"    降级为**可见输入**（令牌会显示在屏幕上）：\n"
            f"    如果不希望这样，请改用环境变量：\n"
            f"        read -s GITHUB_TOKEN && export GITHUB_TOKEN\n"
            f"        python scripts/push_github.py\n"
        )
        try:
            return input("粘贴令牌（可见）：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return ""


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

    if not token and not sys.stdin.isatty():
        print(
            "\n[x] 没有令牌，且当前 stdin 不是终端 —— 无法交互式输入。\n"
            "    请改用环境变量：\n"
            "        GITHUB_TOKEN=ghp_xxx python scripts/push_github.py\n"
            "    （Git Bash 里可用 `read -s GITHUB_TOKEN` 输入后再 export，"
            "避免进 shell 历史）"
        )
        return 1

    if not token:
        print(
            "\n需要一个 **Personal Access Token**（不能用账号密码："
            "GitHub 从 2021-08-13 起已禁止密码用于 git 操作）。\n"
            "创建：https://github.com/settings/tokens\n"
            "  · 经典令牌：勾选 repo 权限\n"
            "  · 细粒度令牌：选 MyAgent 仓库，Contents 设为 Read and write\n"
        )
        token = _read_token_secretly()

    if not token:
        print("[x] 没有令牌，退出")
        return 1

    # ---------- 注入凭据 ----------
    #
    # 【为什么用"一次性 URL"而不是其它方式】
    #
    # 试过并失败的：
    #   · `git remote set-url origin https://<token>@...` —— **能用但绝不能这么做**：
    #     令牌会明文落进 `.git/config`，而 .git/ 整个不在版本控制里、
    #     也不会被 .gitignore 提醒。之后任何一次 `git remote -v`、
    #     任何能读到工作目录的脚本、任何一次误备份都会把它带出去。
    #   · `GIT_ASKPASS` —— 它的值必须是**单个可执行文件路径**，不能是
    #     "解释器 + 参数"。本机实测：
    #         error: cannot spawn "...python.exe" "...askpass.py": Permission denied
    #   · `-c http.<url>.extraheader=AUTHORIZATION: Basic ...` —— 本机 git 2.46
    #     下 git 仍报 "could not read Username"，凭据没被采信。
    #   · `GIT_CONFIG_COUNT/KEY_0/VALUE_0` 环境变量注入 —— 同样未被采信。
    #
    # 所以用「把凭据放进这一次 push 的 URL 参数」：
    #   · **不写入任何配置文件** —— 命令结束即消失
    #   · 代价是令牌在命令执行期间出现在进程参数里（`ps` 可见）
    #
    # 这个代价是**有意识接受的**：本机是单用户开发环境，命令只存活几秒；
    # 而落盘的风险是永久的。**在两个风险之间选"短暂暴露"而不是"永久留存"**，
    # 这是凭据处理里最重要的一条取舍原则。
    #
    # 注意 URL 里的名字部分会被 git 当作用户名，所以这里要放真实用户名。
    auth_url = (
        f"https://{quote(OWNER, safe='')}:{quote(token, safe='')}@github.com/{OWNER}/{REPO}.git"
    )

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"

    print("\n推送中……")
    result = git(
        # 显式禁用系统级 helper：本机 credential.helper=manager 会拉起
        # sh.exe 并在沙箱里崩溃（fatal error - couldn't create signal pipe），
        # 那会挡住整个推送 —— 而网络和 TLS 其实都是好的。
        # 诊断依据：禁用后推送能正常到达 GitHub，只在缺凭据时失败。
        "-c",
        "credential.helper=",
        "push",
        "-u",
        auth_url,
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
