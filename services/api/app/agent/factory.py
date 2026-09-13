"""记忆装配工厂。

放在独立模块的理由与 rag/factory 相同：**服务与 CLI 必须用同一套装配逻辑**。
否则会出现"CLI 里记得住、HTTP 接口里记不住"这类只在某一条路径上复现的怪问题。

记忆默认关闭（`MEMORY_ENABLED=false`）。这不是保守，而是方法论：
记忆会增加每轮的记忆装配与召回开销，**它的价值应该被度量而不是被假设** ——
和检索消融实验一样的道理。开启方式：

    MEMORY_ENABLED=true
"""

from __future__ import annotations

import logging

from app.agent.memory import ConversationMemory, LongTermMemory
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def build_memories(
    settings: Settings | None = None,
    *,
    llm: object | None = None,
) -> tuple[ConversationMemory | None, LongTermMemory | None]:
    """按配置构造 (短期记忆, 长期记忆)。未启用时返回 (None, None)。

    Args:
        llm: 用于摘要压缩的 LLM 客户端。不传则短期记忆退化为"截断"策略
             （会记日志提示，不会静默丢上下文）。
    """
    s = settings or get_settings()

    if not s.memory.enabled:
        logger.info("记忆模块未启用（MEMORY_ENABLED=false）")
        return None, None

    short = ConversationMemory(
        llm=llm,
        max_turns=s.memory.max_turns,
        keep_recent=s.memory.keep_recent,
        max_summary_chars=s.memory.max_summary_chars,
        enable_summary=s.memory.enable_summary,
    )

    long_term = LongTermMemory(path=s.memory.facts_file, max_facts=s.memory.max_facts)
    loaded = long_term.load()
    logger.info(
        "记忆模块已启用：短期窗口 %d 轮（保留最近 %d 轮，摘要%s），长期记忆已加载 %d 条（%s）",
        short.max_turns,
        short.keep_recent,
        "开启" if short.enable_summary else "关闭",
        loaded,
        s.memory.facts_file,
    )

    return short, long_term
