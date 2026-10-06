"""上下文预算：把要发给模型的消息裁剪到 token 上限之内。

《为什么"每个工具输出限 8000 字"不等于"上下文有上限"》

T09 之前只有前者。两者的差别是一道乘法：

    max_steps=12 × 每个观察最多 8000 字 ≈ 9.6 万字 ≈ 中文 14 万 token

也就是说，一个"每步都调工具、每次都返回大结果"的回合，
可以轻松把请求顶到远超任何模型的上下文上限，而这个过程中**没有任何一处会拦它**。
到最后的结局是 API 报错（400/413），一整轮的推理全部白费 ——
而用户看到的只是一句难懂的报错。

《裁剪顺序：为什么是这个顺序》

    1. 系统提示        永不裁剪（角色设定没了，后面全乱）
    2. 长期记忆        尽量保留（它是"关于用户的稳定事实"，跨会话复用）
    3. 最早的对话轮次   先丢（最旧的信息，且摘要机制已经压缩过一轮）
    4. 中途的工具观察   再丢（它们是过程，价值随时间衰减最快）
    5. 当前这一轮       最后才动（用户正在问的东西，动它等于答非所问）

《为什么必须整组裁剪 —— 这是最容易写错的地方》

OpenAI 兼容协议要求 `role=tool` 的消息**必须**紧跟在请求它的那条 assistant
消息后面，并用 `tool_call_id` 配对。所以：

    丢了 assistant 消息、留下它的 tool 结果   → 400（找不到对应的调用）
    只丢一半的 tool 结果                    → 400（配对数量对不上）

裁剪的单位因此不是"一条消息"，而是**一组**：一条 assistant（可能带 tool_calls）
加上紧随其后的所有 tool 结果。`_group()` 负责这件事，
`test_trim_never_orphans_tool_messages` 负责盯着它别退化。

《裁掉的东西要说出来》

静默裁剪是这个功能最危险的形态：用户发现"它怎么忘了刚才说的"，
却没有任何线索。所以 `TrimReport` 会记录丢了多少条、多少 token，
由调用方打进日志与事件流。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.llm.tokens import count_messages_tokens
from app.llm.types import ChatMessage, Role

logger = logging.getLogger(__name__)


@dataclass
class TrimReport:
    """一次裁剪的结果说明。"""

    before_tokens: int = 0
    after_tokens: int = 0
    dropped_messages: int = 0
    dropped_tokens: int = 0
    # 是否**仍然**超预算（说明连"保护前缀 + 最后一组"都放不下）
    still_over: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def trimmed(self) -> bool:
        return self.dropped_messages > 0

    def describe(self) -> str:
        if not self.trimmed and not self.still_over:
            return f"上下文 {self.before_tokens} token，未超预算"
        parts = [
            f"上下文 {self.before_tokens} → {self.after_tokens} token",
            f"丢弃 {self.dropped_messages} 条消息（{self.dropped_tokens} token）",
        ]
        if self.still_over:
            parts.append("仍在预算之上 —— 请调大 AGENT_CONTEXT_TOKEN_BUDGET 或减小单次工具输出")
        return "；".join(parts)


class ContextBudget:
    """按 token 预算裁剪消息列表。

    【为什么用类而不是一个函数】
    裁剪策略有三个可调项（预算、保护几条前缀、是否保留最后一组），
    而且它需要被日志和事件流解释"发生了什么"。做成对象之后，
    Agent 持有它、测试也能直接构造它，不必把参数在函数签名里传来传去。
    """

    def __init__(self, budget_tokens: int, *, protect_prefix: int = 0) -> None:
        # 0 或负数 = 不限制。**默认必须是"不限制"**：猜一个上限会把
        # "本来就长但正常"的请求裁出内容，而这比超窗报错更难发现。
        self.budget_tokens = budget_tokens
        # 前缀里是系统提示与长期记忆，它们永不裁剪
        self.protect_prefix = max(0, protect_prefix)

    @property
    def enabled(self) -> bool:
        return self.budget_tokens > 0

    def fit(
        self,
        messages: Sequence[ChatMessage],
        *,
        protect_prefix: int | None = None,
    ) -> tuple[list[ChatMessage], TrimReport]:
        """返回 (裁剪后的消息, 报告)。未启用预算时原样返回。

        `protect_prefix` 可以**按次覆盖**：调用方比构造函数更清楚这一次
        前几条是"背景"（系统提示 / 长期记忆 / 摘要）。实测中这一点有意义 ——
        长期记忆没召回任何事实时它那一条根本不存在，构造时按"有长期记忆"
        猜出来的条数会多保护一条真实的对话消息。
        """
        report = TrimReport(before_tokens=count_messages_tokens(messages))
        if not self.enabled:
            report.after_tokens = report.before_tokens
            return list(messages), report

        prefix = self.protect_prefix if protect_prefix is None else max(0, protect_prefix)
        head = list(messages[:prefix])
        rest = list(messages[prefix:])
        groups = _group(rest)

        head_tokens = count_messages_tokens(head)
        group_tokens = [count_messages_tokens(g) for g in groups]
        total = head_tokens + sum(group_tokens)

        if total <= self.budget_tokens:
            report.after_tokens = total
            return list(messages), report

        # 从**最早**的组开始丢，但永远留下最后一组（那是用户当下的问题）
        kept: list[list[ChatMessage]] = []
        dropped = 0
        dropped_tokens = 0
        for index, group in enumerate(groups):
            is_last = index == len(groups) - 1
            if is_last:
                kept.append(group)
                continue
            if head_tokens + sum(group_tokens[index:]) <= self.budget_tokens:
                kept.append(group)
                continue
            dropped += len(group)
            dropped_tokens += group_tokens[index]
            report.notes.append(
                f"丢弃第 {index + 1} 组（{len(group)} 条，{group_tokens[index]} token）"
            )

        result = head + [m for group in kept for m in group]
        report.after_tokens = count_messages_tokens(result)
        report.dropped_messages = dropped
        report.dropped_tokens = dropped_tokens
        # 连"保护前缀 + 最后一组"都放不下时才算仍然超预算 ——
        # 这种情况我们**不**继续裁（再裁就要动用户当前的问题了），
        # 而是让它带着一条明确的警告发出去。
        report.still_over = report.after_tokens > self.budget_tokens

        if report.trimmed:
            logger.info("上下文超预算，已裁剪：%s", report.describe())
        if report.still_over:
            logger.warning(
                "裁剪后上下文仍有 %d token（预算 %d）：保护前缀与最后一组本身就超了。"
                "请调大 AGENT_CONTEXT_TOKEN_BUDGET，或减小单次工具输出上限",
                report.after_tokens,
                self.budget_tokens,
            )
        return result, report


def _group(messages: Sequence[ChatMessage]) -> list[list[ChatMessage]]:
    """把消息切成"不可拆分"的组。

    【规则】
    · 一条 assistant（无论是否带 tool_calls）+ 紧随其后的全部 tool 结果 = 一组
    · 其余消息（user / system / 独立的 assistant）= 各自一组

    【为什么 tool 结果必须跟着它的 assistant 一起走】
    OpenAI 兼容协议要求 `role=tool` 的消息用 `tool_call_id` 与前面那条
    assistant 的 `tool_calls` 配对。拆散它们，服务端会直接 400 ——
    而那个报错通常只说"tool_call_id 找不到对应的调用"，
    完全看不出是我们的裁剪逻辑干的。
    """
    groups: list[list[ChatMessage]] = []
    for message in messages:
        if message.role == Role.TOOL and groups and _is_tool_group(groups[-1]):
            # 追加到上一组（那条 assistant + 它的 tool 结果）
            groups[-1].append(message)
            continue
        groups.append([message])
    return groups


def _is_tool_group(group: list[ChatMessage]) -> bool:
    """这个组是不是"带工具调用的 assistant"（可以继续吸收 tool 结果）。"""
    return bool(group) and group[0].role == Role.ASSISTANT and bool(group[0].tool_calls)


# ============================================================
# 工具使用摘要（技术债 T07）
# ============================================================
def summarize_tools(trace: Sequence[Mapping[str, object]]) -> str:
    """把一次回合的工具调用压成一行，供**后续轮次**参考。

    《为什么不做成"把 tool 消息也存进历史"》

    ADR-006 当初禁止 tool 消息入历史，理由是它们会持续吃 token：
    原始工具输出动辄几千字，留着就等于每轮都重发一遍。那个判断是对的，
    但它留下了一个真实的缺口 —— **模型不记得自己查过什么**，
    于是下一轮会重复调用同一个工具（慢，而且费 token）。

    所以补的不是"原文"，而是**一行摘要**：查了哪些工具、各几次、结果多大。
    实测这行通常 20–60 token，而它挡掉的重复调用一次就是几百到几千 token。

    《为什么摘要里不含工具返回的内容》

    内容已经在上一轮的回答里体现过了（模型据此得出了结论）。
    把结论再摘一遍是重复；而把原文摘进来就退回了"tool 消息入历史"的老问题。
    这行摘要的作用是让模型知道"这个话题我已经查过，别再查一遍"。

    【为什么要按顺序保留第一次出现的次序】
    "先检索、再读文件、最后算了一下"与"先算、再检索"对模型的意义不同：
    它反映的是推理路径。用出现顺序而不是字典序，读起来才是那条路径。
    """
    if not trace:
        return ""

    order: list[str] = []
    counts: dict[str, int] = {}
    failures: dict[str, int] = {}
    sizes: dict[str, int] = {}

    for item in trace:
        name = str(item.get("name") or "未知工具")
        if name not in counts:
            order.append(name)
            counts[name] = 0
            failures[name] = 0
            sizes[name] = 0
        counts[name] += 1
        if item.get("ok") is False:
            failures[name] += 1
        # `chars` 由调用方给出（工具结果的长度）。没有就不显示规模，
        # 而不是猜一个 —— 猜出来的数字会让人以为它有意义。
        chars = item.get("chars")
        if isinstance(chars, int):
            sizes[name] += chars

    parts: list[str] = []
    for name in order:
        piece = f"{name}×{counts[name]}" if counts[name] > 1 else name
        if sizes[name]:
            piece += f"（约 {_human_size(sizes[name])}）"
        if failures[name]:
            piece += f"，其中 {failures[name]} 次失败"
        parts.append(piece)

    # 措辞刻意是"我做过什么"，而不是一段系统提示：
    # 这行会被放进**助手自己那条历史消息**里（见 memory.abuild_context），
    # 用第一人称读起来才像它自己的一条记录，而不是外来指令 ——
    # 外来指令式的措辞（"不要说我没有工具"）容易让模型在回答里复述它。
    return "（本轮我调用过：" + "、".join(parts) + "）"


def _human_size(chars: int) -> str:
    if chars >= 1000:
        return f"{chars / 1000:.1f}k 字"
    return f"{chars} 字"
