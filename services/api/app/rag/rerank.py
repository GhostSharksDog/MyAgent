"""重排（Reranking）：在召回结果上做精排。

【为什么需要它 —— 本项目是实测出来的需求，不是照搬最佳实践】
P2 的评测给出了一组很有说服力的数字：

    Recall@5  = 0.611  →  Recall@10 = 0.816   (+0.205)
    MRR@5     = 0.412  →  MRR@10    = 0.437   (+0.025，几乎不动)

**正确答案就在候选集里，只是排得太靠后。** 这就是"需要重排"的典型信号：
召回层没问题（Recall 够高），瓶颈在排序层（MRR 不涨）。

【重排为什么能work —— 双塔与交叉编码器的本质差别】
- **召回用的双塔模型/BM25**：查询和文档**各自独立**编码，最后只做一次向量点积。
  好处是可以离线预计算所有文档向量，检索是 O(1) 次点积；
  代价是查询与文档之间**没有任何词级交互** —— 模型没法知道
  "查询里的'消息队列'恰好对应文档里的'Kafka'"。
- **交叉编码器（cross-encoder）**：把 `[查询, 文档]` **拼在一起**送进模型，
  让模型在内部做完整的注意力交互。精度显著更高，
  但它**必须对每个候选都跑一次前向**，无法预计算 —— 所以只能用在
  几十个候选的小集合上。这就是 RAG 的经典两段式结构：
  **召回求快求全 → 重排求准**。

【本模块提供两种实现】
1. `LexicalReranker`：零成本、零延迟的特征式重排。用查询词覆盖度、
   短语命中、章节名匹配等信号。虽然不如神经交叉编码器，但它证明了
   "重排这件事有收益"，并且可以作为离线基线。
2. `LLMReranker`：listwise 重排，把候选编号后交给大模型一次性排序。
   这是**精度最高也最贵**的方案：每次查询多一次 LLM 调用。
   在候选只有 10~20 个时，一次调用的成本完全可以接受。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from app.rag.store import SearchHit
from app.rag.tokenizer import tokenize, tokenize_query_filtered

logger = logging.getLogger(__name__)


class Reranker(ABC):
    """重排器接口。"""

    name: str = "base"

    @abstractmethod
    async def rerank(self, query: str, hits: list[SearchHit], top_k: int) -> list[SearchHit]:
        """对候选重排并返回前 top_k 个。"""
        raise NotImplementedError


class NoOpReranker(Reranker):
    """不重排。用于消融实验的对照组 —— 没有对照组就说不清重排贡献了多少。"""

    name = "none"

    async def rerank(self, query: str, hits: list[SearchHit], top_k: int) -> list[SearchHit]:
        return _renumber(hits[:top_k])


class LexicalReranker(Reranker):
    """特征式重排（零依赖、离线可跑）。

    三个信号，都是"双塔检索看不到"的：

    1. **查询词覆盖度**：候选里命中了查询中多少比例的词元。
       与 BM25 的区别是这里用**比例**而不是加权和，
       所以长文档不会因为"词多"而占便宜，短文档也不会被长度惩罚 ——
       恰好补上检索阶段的偏差。
    2. **短语命中**：查询中最长的连续 CJK 串是否原样出现在候选里。
       `"我的实习经历"` 里的 `"实习经历"` 原样出现，比零散命中几个字强得多。
    3. **章节名匹配**：候选所属章节名命中查询词。章节名是**人工构造的强信号**
       （"专业技能"、"任职要求"），却完全不参与向量/BM25 打分，
       属于被白白浪费的信息。

    权重是刻意保持简单的 —— 只有三个信号、量级都在 [0,1]。
    在只有十几块的语料上调一堆权重，只会过拟合评测集。
    """

    name = "lexical"

    def __init__(
        self,
        *,
        coverage_weight: float = 1.0,
        phrase_weight: float = 0.6,
        section_weight: float = 0.4,
    ) -> None:
        self.coverage_weight = coverage_weight
        self.phrase_weight = phrase_weight
        self.section_weight = section_weight

    @staticmethod
    def _longest_cjk_phrase(query: str) -> str:
        """取查询中最长的连续 CJK 串作为"关键短语"。

        中文查询没有空格，无法靠分词得到短语，但连续汉字段往往就是
        用户真正想找的东西（"实习经历"、"向量数据库"）。
        """
        runs = re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]{2,}", query)
        return max(runs, key=len) if runs else ""

    def score(self, query: str, hit: SearchHit) -> float:
        terms = tokenize_query_filtered(query)
        text_terms = set(tokenize(hit.chunk.text))
        section_terms = set(tokenize(hit.chunk.section))

        coverage = sum(1 for t in set(terms) if t in text_terms) / len(set(terms)) if terms else 0.0

        phrase = self._longest_cjk_phrase(query)
        phrase_bonus = 1.0 if phrase and phrase in hit.chunk.text else 0.0

        section_bonus = (
            sum(1 for t in set(terms) if t in section_terms) / len(set(terms)) if terms else 0.0
        )

        return (
            self.coverage_weight * coverage
            + self.phrase_weight * phrase_bonus
            + self.section_weight * section_bonus
        )

    async def rerank(self, query: str, hits: list[SearchHit], top_k: int) -> list[SearchHit]:
        if not hits:
            return []
        scored = [(self.score(query, hit), hit) for hit in hits]
        # 按重排分降序；同分时保留原有相对顺序（稳定排序），
        # 避免重排把本来正确的顺序打乱
        scored.sort(key=lambda pair: -pair[0])
        reordered = [
            hit.model_copy(update={"score": score, "rank": i})
            for i, (score, hit) in enumerate(scored[:top_k])
        ]
        return reordered


class LLMReranker(Reranker):
    """LLM listwise 重排。

    【为什么用 listwise 而不是 pointwise】
    pointwise 是"逐个候选问模型：这个相关吗？"—— 20 个候选就是 20 次调用，
    又贵又慢，而且模型看不到其他候选，无法做相对判断。
    listwise 把**全部候选一次给出**，让模型直接输出排序：
    一次调用解决，而且相对排序正是重排真正需要的东西。

    【必须防的三个坑】
    1. **模型可能返回越界或重复的编号** → 严格校验，丢弃非法项
    2. **模型可能返回非 JSON** → 解析失败时**保留原顺序**，绝不抛异常中断检索
       （重排是优化项，不是关键路径，它不该有能力搞垮整个检索）
    3. **候选文本可能很长** → 必须截断，否则一次重排就能吃掉几千 token
    """

    name = "llm"

    PROMPT = """\
你是一个检索结果重排器。给定用户的查询和若干候选文档片段，请按**与查询的相关性**
从高到低排序。

评判标准：
- 片段是否直接回答了查询的问题
- 片段是否包含查询中的关键信息（术语、实体、概念）
- 只考虑内容相关性，不考虑片段长短

严格只输出一个 JSON 对象，格式为：{"order": [编号, 编号, ...]}
order 必须包含所有候选的编号，且每个编号只出现一次。不要输出任何其他文字。
"""

    def __init__(
        self,
        llm: Any,
        *,
        snippet_chars: int = 260,
        max_candidates: int = 20,
        temperature: float = 0.0,
    ) -> None:
        self._llm = llm
        self.snippet_chars = snippet_chars
        self.max_candidates = max_candidates
        self.temperature = temperature
        # 便于统计成本：每次重排消耗的 token 会计入这里
        self.total_tokens = 0

    def _build_prompt(self, query: str, hits: list[SearchHit]) -> str:
        lines = [self.PROMPT, "", f"查询：{query}", "", "候选片段："]
        for i, hit in enumerate(hits, start=1):
            snippet = hit.chunk.text[: self.snippet_chars].replace("\n", " ")
            section = f"（章节：{hit.chunk.section}）" if hit.chunk.section else ""
            lines.append(f"[{i}] 出处：{hit.chunk.citation}{section}\n{snippet}")
        return "\n".join(lines)

    @staticmethod
    def _parse_order(raw: str, n: int) -> list[int]:
        """解析模型返回的 order，并做严格校验。

        容错策略：从返回文本里**提取第一个 JSON 对象**（模型常会在 JSON
        前后加说明文字或 ```json 代码块），然后校验编号集合。
        任一步失败就返回空列表，由调用方回退到原顺序。
        """
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return []

        order = data.get("order")
        if not isinstance(order, list):
            return []

        # 只保留合法且不重复的编号（1-based）
        seen: set[int] = set()
        cleaned: list[int] = []
        for item in order:
            if isinstance(item, int) and 1 <= item <= n and item not in seen:
                seen.add(item)
                cleaned.append(item)

        return cleaned

    async def rerank(self, query: str, hits: list[SearchHit], top_k: int) -> list[SearchHit]:
        if len(hits) <= 1:
            return _renumber(hits[:top_k])

        candidates = hits[: self.max_candidates]
        prompt = self._build_prompt(query, candidates)

        from app.llm.types import ChatMessage

        try:
            response = await self._llm.chat(
                [ChatMessage.user(prompt)],
                temperature=self.temperature,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            # 重排失败绝不能中断检索：退回原顺序，只记日志
            logger.warning("LLM 重排失败，退回原顺序：%s", exc)
            return _renumber(hits[:top_k])

        self.total_tokens += response.usage.total_tokens
        order = self._parse_order(response.message.content or "", len(candidates))
        if not order:
            logger.warning("LLM 重排返回无法解析的顺序，退回原顺序")
            return _renumber(hits[:top_k])

        reordered = [candidates[i - 1] for i in order]
        # 模型没排到的候选补在后面，保证不丢结果
        reordered.extend(candidates[i] for i in range(len(candidates)) if (i + 1) not in order)
        return _renumber(reordered[:top_k])


def _renumber(hits: list[SearchHit]) -> list[SearchHit]:
    """重排后重新编号 rank，保证 rank 与最终顺序一致。

    不做这一步会留下隐患：UI 按 rank 展示，日志按 rank 归因，
    一旦 rank 与实际顺序不一致，排查问题时会看到自相矛盾的数据。
    """
    return [hit.model_copy(update={"rank": i}) for i, hit in enumerate(hits)]
