"""知识库检索工具：把 RAG 接进 Agent 的 ReAct 循环。

【为什么这一步不可省略】
在此之前，检索链路再完善也只是一个**库** —— Agent 的 4 个内置工具
（calculator / get_current_time / read_resume / search_jobs）里没有任何一个
能用上 RAG。**能力没有被 Agent 调用到，就等于不存在。**

这个工具也顺带解决了 `read_resume` 的一个根本局限：
`read_resume` 把整份简历一次性塞进上下文（几百到几千 token，且随简历变长线性增长），
而 `search_knowledge` 只召回相关片段。对长文档，后者的 token 效率高一个数量级。

【工具描述怎么写 —— 这是本文件最重要的一段】
工具描述是"给模型的 API 文档"，它直接决定调用准确率。要写清四件事：
  1. **什么时候该用**（触发条件）
  2. **什么时候不该用**（避免滥用，比如精确计算该用 calculator）
  3. **参数含义与边界**
  4. **返回什么**（让模型知道能不能从中得到答案）

含糊的描述是 Agent 表现差的第一大原因，比换模型有效得多。
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.rag.factory import get_shared_retriever
from app.rag.loaders import DocType
from app.rag.retriever import Retriever
from app.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

_SCOPE_MAP: dict[str, list[DocType] | None] = {
    "all": None,
    "resume": [DocType.RESUME],
    "jobs": [DocType.JD],
    "notes": [DocType.NOTE],
}


class SearchKnowledgeParams(BaseModel):
    query: str = Field(
        description=(
            "检索查询。**用自然语言描述你想找什么信息**，而不是堆关键词。"
            "例如『用户有没有消息队列相关的经验』比『Kafka』更好，"
            "因为检索器能利用完整的语义。"
        ),
        min_length=1,
    )
    scope: Literal["all", "resume", "jobs", "notes"] = Field(
        default="all",
        description=(
            "检索范围。问简历相关内容用 resume；找岗位用 jobs；"
            "跨文档综合分析（如简历与岗位的匹配度）用 all。"
        ),
    )
    limit: int = Field(default=4, ge=1, le=10, description="返回的片段数量，默认 4。")


class KnowledgeSearchTool(Tool):
    """在知识库里做语义检索。

    与 `search_jobs` 的区别：`search_jobs` 是按结构化字段（关键词/城市）
    精确过滤岗位表；本工具是**跨全部文档的语义检索**，
    适合"用户有没有 XX 经验""简历和这个岗位差在哪"这类问题。
    """

    name = "search_knowledge"
    description = (
        "在知识库（用户简历、岗位库、个人笔记）中做语义检索，返回最相关的片段及其出处。"
        "适用于：需要依据文档原文回答的问题，例如"
        "『用户有没有大数据相关经验』『简历里提到过哪些分布式技术』"
        "『哪些岗位要求向量数据库』『用户的实习经历和这个岗位匹配吗』。"
        "返回结果带出处标注，回答时应引用出处。"
        "注意：精确的算术运算请用 calculator；按城市/关键词筛选岗位请用 search_jobs。"
    )
    params_model = SearchKnowledgeParams

    def __init__(
        self, settings: Settings | None = None, retriever: Retriever | None = None
    ) -> None:
        self._settings = settings
        # 允许注入检索器：测试可以塞一个用合成语料构建的实例，
        # 从而不依赖文件系统、也不受进程内共享单例的影响。
        # 这就是依赖注入在工具层的实际价值。
        self._injected = retriever
        # 检索比普通工具慢（建索引 + 两路召回 + 重排），超时给宽一点
        self.timeout = 60.0

    def _get_retriever(self, settings: Settings) -> Retriever:
        return self._injected or get_shared_retriever(settings)

    async def run(self, params: BaseModel) -> ToolResult:
        p = SearchKnowledgeParams.model_validate(params.model_dump())
        settings = self._settings or get_settings()

        try:
            retriever = self._get_retriever(settings)
        except Exception as exc:
            logger.exception("构建检索索引失败")
            return ToolResult.failure(f"知识库索引构建失败：{type(exc).__name__}: {exc}")

        if len(retriever.chunks) == 0:
            return ToolResult.failure(
                "知识库为空，无法检索。请先准备数据："
                "把简历保存为 data/resume.md，或运行 "
                "python scripts/ingest.py <你的简历.pdf> --type resume"
            )

        doc_types = _SCOPE_MAP[p.scope]
        ctx = await retriever.aretrieve_context(
            p.query,
            k=p.limit,
            doc_types=doc_types,
            min_score=settings.rag.min_score,
            max_chars=settings.rag.max_context_chars,
        )

        if not ctx:
            hint = {
                "resume": "简历中",
                "jobs": "岗位库中",
                "notes": "笔记中",
                "all": "知识库中",
            }[p.scope]
            return ToolResult.failure(
                f"在{hint}没有检索到与 {p.query!r} 相关的内容。"
                f"可以换一种表述，或把 scope 改为 all 扩大范围。"
            )

        return ToolResult.success(ctx)


def build_knowledge_tools(settings: Settings | None = None) -> list[Tool]:
    """返回知识库相关的工具。单独一个函数，便于测试与按需装配。"""
    return [KnowledgeSearchTool(settings)]
