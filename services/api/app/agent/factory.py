"""记忆与"模型相关单例"的装配工厂。

放在独立模块的理由与 rag/factory 相同：**服务与 CLI 必须用同一套装配逻辑**。
否则会出现"CLI 里记得住、HTTP 接口里记不住"这类只在某一条路径上复现的怪问题。

记忆默认关闭（`MEMORY_ENABLED=false`）。这不是保守，而是方法论：
记忆会增加每轮的记忆装配与召回开销，**它的价值应该被度量而不是被假设** ——
和检索消融实验一样的道理。开启方式：

    MEMORY_ENABLED=true
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.agent.loop import Agent
from app.agent.memory import ConversationMemory, LongTermMemory
from app.agent.sqlite_memory import SqliteLongTermMemory
from app.core.config import Settings, get_settings
from app.llm.client import LLMClient
from app.tools.base import ToolRegistry
from app.tools.builtin import build_default_registry

logger = logging.getLogger(__name__)


def build_memories(
    settings: Settings | None = None,
    *,
    llm: object | None = None,
    long_term: LongTermMemory | None = None,
) -> tuple[ConversationMemory | None, LongTermMemory | None]:
    """按配置构造 (短期记忆, 长期记忆)。未启用时返回 (None, None)。

    Args:
        llm: 用于摘要压缩的 LLM 客户端。不传则短期记忆退化为"截断"策略
             （会记日志提示，不会静默丢上下文）。
    """
    s = settings or get_settings()

    if not s.memory.enabled and long_term is None:
        logger.info("记忆模块未启用（MEMORY_ENABLED=false）")
        return None, None
    if not s.memory.enabled:
        long_term.enabled = False
        return None, long_term

    short = ConversationMemory(
        llm=llm,
        max_turns=s.memory.max_turns,
        keep_recent=s.memory.keep_recent,
        max_summary_chars=s.memory.max_summary_chars,
        enable_summary=s.memory.enable_summary,
    )

    if long_term is None:
        cls = SqliteLongTermMemory if s.memory.backend == "sql" else LongTermMemory
        long_term = cls(path=s.memory.facts_file, max_facts=s.memory.max_facts)
        long_term.load()
    long_term.enabled = s.memory.enabled
    loaded = len(long_term)
    logger.info(
        "记忆模块已启用：短期窗口 %d 轮（保留最近 %d 轮，摘要%s），长期记忆已加载 %d 条（%s）",
        short.max_turns,
        short.keep_recent,
        "开启" if short.enable_summary else "关闭",
        loaded,
        s.memory.facts_file,
    )

    return short, long_term


# ============================================================
# "跟着模型配置走"的那一组单例
# ============================================================
@dataclass
class AgentStack:
    """一次装配的产物：模型客户端 + 记忆 + 工具表 + Agent。

    【为什么把它们打成一个包】
    这四样东西是**一起变**的：换模型要换 LLMClient，而记忆（摘要用 llm）、
    工具表（记忆工具持有长期记忆）、Agent（持有上面全部）都得跟着换。
    只换其中一部分会得到一个"看起来换了、实际还在用旧配置"的系统 ——
    比如工具表里的记忆工具还指向旧实例。
    """

    llm: LLMClient
    tools: ToolRegistry
    agent: Agent
    memory: ConversationMemory | None
    long_term: LongTermMemory | None


def build_agent_stack(
    settings: Settings | None = None, *, long_term: LongTermMemory | None = None
) -> AgentStack:
    """装配 Agent 全栈。

    【为什么要有这个函数，而不是把这段留在 lifespan 里】
    换模型（`POST /api/models/{id}/activate`）需要**重新跑一遍完全相同的装配**。
    如果 lifespan 里一份、切换接口里再抄一份，两份迟早漂移 ——
    而漂移的表现是"界面切了模型，但某条路径还在用旧的"，
    这类问题极难定位。**装配逻辑只能有一份。**

    sessions / tasks 不在其中：它们不随模型变化，而且持有 Redis 连接，
    每次换模型都重建它们既浪费又会让在途请求受影响。
    """
    s = settings or get_settings()

    llm_client = LLMClient(s.llm)

    # 记忆要先于工具表构造：`remember_fact` 工具需要与 Agent 共享同一个
    # 长期记忆实例，否则工具"记住"的东西 Agent 读不到 —— 这是
    # 依赖注入顺序上最容易踩的坑。
    short_memory, long_term = build_memories(s, llm=llm_client, long_term=long_term)

    # 工具集与提示词都由 profile 决定：general（默认）只加载核心工具，
    # jobhunt 才额外加载简历/岗位。见 build_default_registry 的分层说明。
    #
    # 写工具（T23）**默认不加载**：写文件不可撤销，必须由用户显式开启
    # （`AGENT_FILE_WRITE_ENABLED=true`，或设置界面里的开关）。
    # 没开启时它们根本不在工具表里 —— 模型不会尝试，也不会在回答里
    # 承诺"我已经帮你写好了"。
    tools = build_default_registry(
        long_term_memory=long_term,
        profile=s.agent.profile,
        file_write=s.agent.file_write_enabled,
        terminal=s.agent.terminal_enabled,
    )

    agent = Agent(llm_client, tools, s.agent, memory=short_memory, long_term=long_term)
    return AgentStack(
        llm=llm_client, tools=tools, agent=agent, memory=short_memory, long_term=long_term
    )


def mount_agent_stack(app: Any, stack: AgentStack) -> LLMClient | None:
    """把装配结果挂到 `app.state`，返回**被替换下来的旧 LLM 客户端**。

    返回旧客户端是为了让调用方把它关掉：`LLMClient` 内部持有 httpx 连接池，
    只换引用不关旧的，每切换一次模型就漏一个连接池。
    （调用方在**替换之后**再关，这样在途请求还能用完自己手上那个引用。）
    """
    previous: LLMClient | None = getattr(app.state, "llm", None)
    manager = getattr(app.state, "mcp", None)
    if manager is not None:
        for tool in manager.tools():
            stack.tools.register(tool)
    old_tools = getattr(app.state, "tools", None)
    if old_tools is not None:
        old_tools.replace_tools(stack.tools)
        stack.tools = old_tools
    stack.agent = Agent(
        stack.llm, stack.tools, stack.agent._s, memory=stack.memory, long_term=stack.long_term
    )
    app.state.llm = stack.llm
    app.state.tools = stack.tools
    app.state.agent = stack.agent
    app.state.memory = stack.memory
    app.state.long_term = stack.long_term
    return previous


def refresh_agent_tools(app: Any, settings: Settings) -> None:
    """保存能力设置后刷新工具/提示词，不重建或关闭在途请求的模型连接。"""
    long_term = getattr(app.state, "long_term", None)
    if long_term is not None:
        long_term.enabled = settings.memory.enabled
    elif settings.memory.enabled:
        _, long_term = build_memories(settings, llm=app.state.llm)
        app.state.long_term = long_term
    tools = build_default_registry(
        long_term_memory=getattr(app.state, "long_term", None),
        profile=settings.agent.profile,
        file_write=settings.agent.file_write_enabled,
        terminal=settings.agent.terminal_enabled,
    )
    if manager := getattr(app.state, "mcp", None):
        for tool in manager.tools():
            tools.register(tool)
    app.state.tools.replace_tools(tools)
    app.state.agent = Agent(
        app.state.llm,
        app.state.tools,
        settings.agent,
        memory=getattr(app.state, "memory", None),
        long_term=getattr(app.state, "long_term", None),
    )
