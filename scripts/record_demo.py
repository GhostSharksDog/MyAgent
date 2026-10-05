r"""录制一次真实对话的事件流，供离线回放。

【为什么录制脚本里必须内置 PII 闸门】

录制下来的是**完整的工具调用结果** —— 而 `search_knowledge` 的返回里
就是简历原文（姓名、电话、学校、公司名全都有）。

这个文件一旦被误提交，PII 就永久进入了 git 历史 —— 删掉文件也删不掉历史，
只能改写历史（force push + 所有协作者重新克隆），代价极大。

所以闸门放在**写入之前**，而且声明式配置（`--pii-patterns` 或环境变量）：
录完先扫，命中就**拒绝写出**并列出命中位置。默认扫描本项目已知的 PII 词表。

【为什么要保留"每条事件之间的延迟"】

回放时要还原"token 逐个出现"的节奏，所以必须记录相对时间。
但它是**回放控制信息**，不该混进业务事件体（会污染内核↔UI 的事件契约），
所以单独放在 meta.delays 里。

用法：
    # 前提：API 服务在跑，且有可用的 LLM 额度
    .\scripts\dev.ps1 serve
    python scripts/record_demo.py --out data/demo/transcript.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent

# ============================================================
# PII 词表：**分成两类，而且这个区分是必需的**
# ============================================================
#
# 【为什么具体的人名/公司名不能硬编码在这里】
# 第一版把"姓名、手机号、学校、公司名"直接写成了这个文件里的常量。
# 结果很讽刺：**PII 检测器自己成了仓库里最大的一处 PII 泄漏** ——
# 要提交这个脚本，就得把用户的手机号和姓名一起提交进 git。
#
# 这不是被 PII 扫描的误报，它就是真泄漏。教训是：
# **检测器的配置本身也是一处数据面，它同样需要按数据来对待。**
#
# 所以分两类：
#   1. 通用模式（正则）：手机号、邮箱、身份证号 —— 它们**不是任何人的
#      具体信息**，只是一个格式，可以硬编码，而且对谁都适用。
#   2. 具体词表（人名/学校/公司）：**因人而异**，所以从 gitignore 的
#      文件里读，或者从环境变量传。
#
# 缺省情况下没有第 2 类词表，脚本依然能用 —— 通用模式已经能拦住
# 电话和邮箱这类最强的标识符。有词表时拦得更全。

# 通用模式：格式本身不含个人信息，对任何使用者都适用
GENERIC_PATTERNS: list[str] = [
    # 中国大陆手机号。刻意不写成 r"1[3-9]\d{9}" 那种"能匹配更多"的形式 ——
    # 扫描器宁可漏掉几个边缘格式，也不能因为误报让人干脆关掉它。
    r"1[3-9]\d{9}",
    # 邮箱
    r"[\w.+-]+@[\w-]+\.[\w.]+",
    # 身份证（18 位，末位可能是 X）
    r"\b\d{17}[\dXx]\b",
]

# 具体词表的位置：**必须在 gitignore 覆盖的目录内**
DEFAULT_TERMS_FILE = "data/pii-terms.txt"

DEMO_QUESTION = (
    "请检索我的简历，告诉我：我掌握哪些大数据与分布式技术？逐条列出并标注出处。"
)


def load_terms(path: str | Path) -> list[str]:
    """从 gitignore 覆盖的文件里读具体词表（姓名、学校、公司名）。

    【为什么词表要放在 gitignore 的目录里，而不是跟脚本放一起】
    因为这些词**本身就是 PII**。把它们写进随代码提交的文件，
    等于把"要保护的东西"和"保护它的工具"打包在一起送出去 ——
    第一版就是这么错的。

    文件格式很简单：一行一个词，`#` 开头是注释。
    不存在时返回空列表（通用正则仍然在起作用，不是静默失效）。
    """
    p = Path(path)
    if not p.exists():
        return []
    terms: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            terms.append(s)
    return terms


def scan_pii(payload: object, patterns: list[str]) -> list[str]:
    """递归扫描整个 JSON 结构里的 PII 命中。

    【为什么扫描整个结构而不是只扫最终答案】
    最终答案可能只是转述，而**原始的工具返回**才是完整原文。
    漏掉任何一层都可能让 PII 溜进去，所以这里不挑字段。
    """
    hits: list[str] = []
    text = json.dumps(payload, ensure_ascii=False)
    for pat in patterns:
        try:
            if re.search(pat, text):
                # 只报告命中了什么模式，**不把命中的上下文打出来** ——
                # 否则日志里就有了 PII，等于把泄露点从文件搬到了日志。
                # 报告时也只显示模式本身（`pat`），不显示匹配到的内容。
                hits.append(pat)
        except re.error:
            # 词表里的普通词（不含正则元字符）也要能工作：
            # 当成字面量再试一次。
            if pat in text:
                hits.append(pat)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="录制演示用的事件流")
    ap.add_argument("--api-url", default="http://127.0.0.1:8000")
    ap.add_argument("--out", default="data/demo/transcript.json")
    ap.add_argument("--question", default=DEMO_QUESTION)
    ap.add_argument("--mode", default="react", choices=["react", "plan", "multi"])
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument(
        "--pii-terms",
        default=DEFAULT_TERMS_FILE,
        help=(
            "具体词表文件（一行一个词：姓名/学校/公司名）。"
            f"默认 {DEFAULT_TERMS_FILE}，它必须位于 gitignore 覆盖的目录内 —— "
            "因为这些词本身就是 PII，写进随代码提交的文件等于把要保护的东西一起送出去。"
        ),
    )
    ap.add_argument(
        "--extra-patterns",
        default="",
        help="额外扫描模式（逗号分隔，支持正则）；用于临时补充",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="跳过 PII 检查（**仅在你确认输出路径已被 gitignore 时使用**）",
    )
    args = ap.parse_args()

    # 通用模式（格式，不含任何人信息）+ 具体词表（因人而异，从 gitignore 的文件读）
    patterns: list[str] = [*GENERIC_PATTERNS, *load_terms(args.pii_terms)]
    if args.extra_patterns:
        patterns.extend(p.strip() for p in args.extra_patterns.split(",") if p.strip())

    # 登录用户名也常出现在路径或内容里，顺手加进词表
    if username := os.environ.get("USERNAME") or os.environ.get("USER"):
        patterns.append(username)

    print("=" * 66)
    print("录制演示事件流")
    print("=" * 66)
    print(f"问题：{args.question}")
    print(f"模式：{args.mode}")
    terms_n = len(load_terms(args.pii_terms))
    print(f"PII 词表：通用模式 {len(GENERIC_PATTERNS)} 条 + 具体词 {terms_n} 条")
    if terms_n == 0:
        print(
            f"  ⚠ 未找到具体词表 {args.pii_terms} —— 姓名/学校/公司名不会被拦截。\n"
            f"    建议创建它（每行一个词），文件已在 gitignore 内，不会进仓库。"
        )

    events: list[dict] = []
    delays: list[float] = []
    meta: dict = {}
    model_name = ""

    try:
        with httpx.Client(timeout=args.timeout) as client:
            # 顺手记下模型名。**不放在事件流里取** —— 事件流的契约里没有
            # "本次用的哪个模型"这个字段，硬塞进去会污染内核与 UI 的契约。
            # 元信息属于元信息该在的地方。
            try:
                model_name = str(client.get(f"{args.api_url}/api/meta").json().get("model", ""))
            except httpx.HTTPError:
                pass

            with client.stream(
                "POST",
                f"{args.api_url}/api/chat/stream",
                json={"message": args.question, "mode": args.mode},
            ) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", "replace")
                    print(f"API 返回 {resp.status_code}：{body[:400]}")
                    return 1

                # ---------- 手写 SSE 分帧 ----------
                # 【为什么不直接用某个库】
                # 这里要的恰好是"和前端一模一样的分帧逻辑" ——
                # 用第三方库的话，它对 `data:` 前缀、多行数据、
                # 空行分隔的处理可能和前端手写实现不一致，
                # 录出来的东西就会和真实消费的形态有偏差。
                last = time.perf_counter()
                event_name = ""
                data_lines: list[str] = []

                for raw_line in resp.iter_lines():
                    line = raw_line.rstrip("\r")
                    if line == "":
                        if data_lines:
                            payload = "\n".join(data_lines)
                            try:
                                parsed = json.loads(payload)
                            except json.JSONDecodeError:
                                data_lines, event_name = [], ""
                                continue
                            now = time.perf_counter()
                            delays.append(round(now - last, 4))
                            last = now
                            events.append(parsed)
                            if parsed.get("type") == "done" and parsed.get("usage"):
                                meta["usage"] = parsed["usage"]
                        data_lines, event_name = [], ""
                        continue
                    if line.startswith(":"):
                        continue  # SSE 注释行（心跳 ping）
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())

                    if len(events) > 5000:
                        print("事件数异常（>5000），疑似死循环，已中止")
                        return 1
    except httpx.HTTPError as exc:
        print(f"无法连接 {args.api_url}：{exc}")
        print("请先启动 API 服务：.\\scripts\\dev.ps1 serve")
        return 1

    if not events:
        print("没有录到任何事件 —— 检查 API 是否正常、LLM 额度是否可用")
        return 1

    types = [e.get("type") for e in events]
    print(f"\n共录到 {len(events)} 个事件")
    print("  事件类型分布：" + ", ".join(f"{t}×{types.count(t)}" for t in sorted(set(types))))
    if meta.get("usage"):
        print(f"  token 用量：{meta['usage']}")

    payload = {
        "meta": {
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "question": args.question,
            "mode": args.mode,
            "model": meta.get("model") or model_name,
            "usage": meta.get("usage", {}),
            # 延迟单独放：它是**回放控制信息**，不是业务事件的一部分。
            # 混进事件体会污染内核与 UI 之间的事件契约。
            "delays": delays,
        },
        "events": [{"event": e} for e in events],
    }

    # ---------- PII 闸门（在写入之前） ----------
    if not args.force:
        hits = scan_pii(payload, patterns)
        if hits:
            print("\n" + "!" * 66)
            print("PII 检查未通过，**拒绝写出文件**")
            print("!" * 66)
            print(f"命中：{hits}")
            print(
                "\n录制内容里包含工具返回的原文（简历正文），属于个人信息。\n"
                "处理方式，二选一：\n"
                "  1. 输出到被 gitignore 的目录（本项目 data/ 已被忽略），"
                "用 --force 跳过检查\n"
                "  2. 换一份不含 PII 的语料再录（例如只问 seed/jobs.json 里的岗位）\n"
                "\n如果命中的是姓名/学校/公司名，请把它们写进 "
                f"{DEFAULT_TERMS_FILE}（一行一个词，该文件在 gitignore 内）。"
            )
            return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    ignored = ""
    try:
        import subprocess

        r = subprocess.run(
            ["git", "check-ignore", str(out)],
            capture_output=True,
            # 显式 UTF-8，理由同 scan_history_pii.py：git 输出 UTF-8，
            # 而 text=True 按本机区域编码解码会杀掉后台读取线程
            encoding="utf-8",
            errors="replace",
            cwd=ROOT,
        )
        ignored = "（已被 .gitignore 排除）" if r.returncode == 0 else "⚠ **未被 gitignore 排除**"
    except Exception:
        pass

    print(f"\n已写入 {out} {ignored}")
    print("\n离线演示：")
    print(f"  $env:DEMO_REPLAY_FILE = '{out}'")
    print("  .\\scripts\\dev.ps1 serve")
    print("  然后确认 /healthz 的 demo_replay 字段不是 off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
