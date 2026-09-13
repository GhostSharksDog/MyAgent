"""长期记忆工具：让 Agent 主动记住关于用户的事实。

【为什么需要"写入"这个动作，而不是自动记录全部对话】
自动全记有两个问题：噪声大（寒暄也被记下），且成本高（每轮一次嵌入+存储）。
更好的模式是**让模型判断什么值得记** —— 它比关键词规则更懂语境：
用户说"我只考虑北京的机会"值得记，"你好"不值得记。

代价是模型可能漏记或记错。因此工具描述必须把"什么该记"讲得非常具体，
并且要求 Agent 在记录后向用户确认 —— 让用户可以纠正。

【与 RAG 的分工】
`search_knowledge` 检索**静态文档**（简历、岗位库）；
本工具写入的是**交互中产生的事实**（偏好、目标、进展）。
两者共用向量检索基础设施，但写入方与生命周期完全不同。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from app.agent.memory import LongTermMemory
from app.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class RememberFactParams(BaseModel):
    fact: str = Field(
        description=(
            "要记住的事实，写成**完整、自足的陈述句**。"
            "要包含主语，因为这条记录将来会脱离当前对话被单独召回 —— "
            "『只考虑北京』这样的片段在几天后读起来毫无意义，"
            "应写成『用户的求职意向城市是北京』。"
        ),
        min_length=1,
        max_length=500,
    )
    tags: list[str] = Field(
        default_factory=list,
        description="分类标签，用于后续过滤，例如 ['求职意向']、['面试进展']、['简历事实']。",
    )


class RememberFactTool(Tool):
    """把关于用户的重要事实写入长期记忆。"""

    name = "remember_fact"
    description = (
        "把关于用户的重要事实写入长期记忆，使其在**以后的对话中**依然可用。"
        "适用于：求职意向（目标岗位/城市/薪资）、明确的偏好与限制、"
        "重要的个人背景（学历、关键经历、技能）、面试与投递的进展。"
        "**不要记录**：寒暄、一次性的临时问题、可以从简历直接查到而不需要特别记住的细节。"
        "记录后应在回答中顺带向用户确认，以便用户纠正。"
    )
    params_model = RememberFactParams

    def __init__(self, memory: LongTermMemory) -> None:
        self._memory = memory

    def run(self, params: BaseModel) -> ToolResult:
        p = RememberFactParams.model_validate(params.model_dump())

        added = self._memory.remember(p.fact, tags=p.tags)
        if not added:
            # 去重命中不是失败，但必须如实告知 —— 否则模型会以为记下了新东西
            return ToolResult.success(f"这条信息已经在长期记忆中了，无需重复记录：{p.fact}")

        self._memory.save()  # 立即落盘：进程崩溃不该丢掉刚记住的用户偏好
        logger.info("长期记忆新增（tags=%s）：%s", p.tags, p.fact)

        total = len(self._memory)
        return ToolResult.success(
            f"已记住：{p.fact}（当前长期记忆共 {total} 条）。请在回答中向用户确认这条信息是否正确。"
        )
