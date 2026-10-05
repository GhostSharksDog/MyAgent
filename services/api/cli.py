"""命令行 Agent 客户端。

用途有两个：
1. **开发调试**：比打开浏览器快得多，能看到完整事件流。
2. **验证内核**：不依赖任何前端，证明 Agent 循环本身是通的。

用法::

    python services/api/cli.py                     # 交互式对话
    python services/api/cli.py -q "帮我看看简历"    # 单次提问（脚本/CI 用）
    python services/api/cli.py --show-raw          # 额外打印原始事件（排查问题用）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 允许 `python services/api/cli.py` 直接运行（把 services/api 加入模块搜索路径）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.agent.events import EventType
from app.agent.factory import build_memories
from app.agent.loop import Agent
from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.llm.client import LLMClient
from app.llm.types import ChatMessage
from app.tools.builtin import build_default_registry

# ---------- 终端配色 ----------
DIM = "\033[90m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
BOLD = "\033[1m"
RESET = "\033[0m"

BANNER = f"""{BOLD}{MAGENTA}Legacy{RESET} {DIM}— 求职/招聘 AI Agent（手写 ReAct 内核）{RESET}
{DIM}输入问题开始对话；命令：/tools 查看工具，/clear 清空上下文，/exit 退出{RESET}
"""


class CLI:
    def __init__(self, agent: Agent, settings: Settings, *, show_raw: bool = False) -> None:
        self.agent = agent
        self.settings = settings
        self.show_raw = show_raw
        self.history: list[ChatMessage] = []

    # ---------- 渲染 ----------

    def _render_tool_call(self, name: str, args: dict[str, object] | None) -> None:
        import json

        arg_text = json.dumps(args or {}, ensure_ascii=False)
        if len(arg_text) > 160:
            arg_text = arg_text[:160] + "…"
        print(f"\n  {YELLOW}⚙ 调用工具{RESET} {BOLD}{name}{RESET} {DIM}{arg_text}{RESET}")

    def _render_tool_result(self, ok: bool | None, content: str, duration_ms: int | None) -> None:
        icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        preview = content.replace("\n", " ⏎ ")
        if len(preview) > 200:
            preview = preview[:200] + "…"
        timing = f"{DIM}({duration_ms}ms){RESET}" if duration_ms else ""
        print(f"  {icon} {DIM}{preview}{RESET} {timing}")

    # ---------- 主循环 ----------

    async def ask(self, question: str) -> None:
        print(f"\n{BOLD}{CYAN}你 ›{RESET} {question}")
        print(f"{BOLD}{MAGENTA}Legacy ›{RESET} ", end="", flush=True)

        final_text = ""
        error_text: str | None = None
        printed_tokens = False

        async for event in self.agent.run_stream(question, self.history):
            if self.show_raw:
                print(
                    f"\n{DIM}[raw] {event.model_dump_json(exclude_none=True)[:220]}{RESET}",
                    flush=True,
                )

            match event.type:
                case EventType.TOKEN:
                    # 打字机效果：逐段打印
                    print(event.content, end="", flush=True)
                    printed_tokens = True

                case EventType.TOOL_CALL:
                    # 先换行，避免工具提示挤在回答文字后面
                    if printed_tokens:
                        print()
                        printed_tokens = False
                    self._render_tool_call(event.tool_name or "?", event.tool_args)
                    print(f"{BOLD}{MAGENTA}Legacy ›{RESET} ", end="", flush=True)

                case EventType.TOOL_RESULT:
                    self._render_tool_result(event.tool_ok, event.content, event.duration_ms)

                case EventType.FINAL:
                    final_text = event.content
                    # 流式已经把内容打出来了，这里只在两者不一致时补打（如空回复占位）
                    if not printed_tokens:
                        print(final_text, end="", flush=True)

                case EventType.ERROR:
                    error_text = event.content

                case EventType.DONE:
                    print()
                    if event.usage and event.usage.total_tokens:
                        print(
                            f"{DIM}  ── {event.steps_used} 步 · "
                            f"输入 {event.usage.prompt_tokens} + 输出 {event.usage.completion_tokens} "
                            f"= {event.usage.total_tokens} tokens{RESET}"
                        )

        if error_text:
            print(f"\n{RED}⚠ {error_text}{RESET}")

        # 只把「用户提问 + 最终回答」写入历史。
        # 工具调用过程不保留：它是过程性信息，对后续轮次无价值，留着只会持续烧 token。
        if final_text:
            self.history.append(ChatMessage.user(question))
            self.history.append(ChatMessage.assistant(final_text))

    async def repl(self) -> None:
        print(BANNER)
        while True:
            try:
                # 【易错点】在 async 函数里直接调用 input() 会**阻塞事件循环**。
                # 单用户 CLI 里看不出问题，但在任何有并发任务的场景下就是致命 bug
                # （整个进程卡住，定时器、心跳、其他协程全部停摆）。
                # asyncio.to_thread 把它丢到线程池，事件循环保持可调度。
                line = (await asyncio.to_thread(input, f"{BOLD}{CYAN}你 ›{RESET} ")).strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{DIM}再见。{RESET}")
                return

            if not line:
                continue
            if line in ("/exit", "/quit", "exit", "quit"):
                print(f"{DIM}再见。{RESET}")
                return
            if line == "/clear":
                self.history.clear()
                print(f"{DIM}上下文已清空。{RESET}")
                continue
            if line == "/history":
                if not self.history:
                    print(f"{DIM}（暂无历史）{RESET}")
                for m in self.history:
                    tag = "你" if m.role == "user" else "AI"
                    print(f"{DIM}[{tag}] {str(m.content)[:120]}{RESET}")
                continue
            if line == "/tools":
                for schema in agent_tools_schemas(self.agent):
                    print(
                        f"  {BOLD}{schema['name']}{RESET}\n    {DIM}{schema['description']}{RESET}"
                    )
                continue

            await self.ask(line)


def agent_tools_schemas(agent: Agent) -> list[dict[str, str]]:
    """从 Agent 取出工具描述（用于 /tools 命令）。"""
    tools = agent._tools
    out: list[dict[str, str]] = []
    for schema in tools.schemas():
        fn = schema["function"]
        out.append({"name": fn["name"], "description": fn["description"]})
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description="Legacy 命令行客户端")
    parser.add_argument("-q", "--question", help="单次提问后退出（非交互模式）")
    parser.add_argument("--show-raw", action="store_true", help="打印原始事件，便于排查")
    parser.add_argument(
        "--memory", action="store_true", help="强制启用记忆模块（覆盖 MEMORY_ENABLED 配置）"
    )
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.log_level, colorful=True)

    if not settings.llm.is_configured:
        print(f"{RED}✗ 未配置 LLM_API_KEY{RESET}")
        print(f"{DIM}请复制 .env.example 为 .env，并填入你的密钥。{RESET}")
        return 2

    llm = LLMClient(settings.llm)

    if args.memory and not settings.memory.enabled:
        # 用 model_copy 覆盖而不是改全局配置：命令行开关不该影响进程外的东西
        settings = settings.model_copy(
            update={"memory": settings.memory.model_copy(update={"enabled": True})}
        )

    # 记忆要先于工具表构造：remember_fact 工具必须与 Agent 共享同一个实例
    short_memory, long_term = build_memories(settings, llm=llm)
    tools = build_default_registry(long_term_memory=long_term)
    agent = Agent(llm, tools, settings.agent, memory=short_memory, long_term=long_term)
    cli = CLI(agent, settings, show_raw=args.show_raw)

    try:
        if args.question:
            await cli.ask(args.question)
        else:
            await cli.repl()
    finally:
        if long_term is not None:
            long_term.save()
            print(f"{DIM}长期记忆已保存（{len(long_term)} 条）{RESET}")
        await llm.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
