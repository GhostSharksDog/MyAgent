"""Query 改写：Multi-Query 与 HyDE。

【为什么需要它 —— 用我自己的失败案例说话】

评测集里有一条一直失败的查询：

    查询「我适合投递哪些岗位」→ 期望覆盖简历的多个侧面
    实际只召回了 5 个岗位块，简历侧一块都没进前 5

原因不是检索算法不够好，而是**一个查询只有一个向量**。
"我适合投递哪些岗位"这句话与"Kafka 使用经验""ClickHouse 位图索引"
这些具体内容之间没有词汇重叠，也没有足够的语义桥梁 ——
它在向量空间里落在一个很泛的位置，谁也召不回来。

**这是「单一查询」的固有局限，加多少召回路数都救不了**，
必须从查询侧解决。两条经典路线：

    Multi-Query：把一个查询改写成 N 个不同角度的查询，各自检索后融合
    HyDE：先生成一段"假想的答案"，再用它去检索

【Multi-Query 为什么能奏效，以及它和"多路召回"的关系】

现有的混合检索是「**一个查询** → 稠密 + 稀疏两路 → RRF 融合」。
Multi-Query 是「**N 个查询** → 每个各两路 → RRF 融合」。

**结构上是同一件事**：都是"多路排名 → 按排名融合"。
所以这里能直接复用 `reciprocal_rank_fusion`，一行新算法都不用写 ——
这不是巧合，而是那个抽象本来就不关心"路"是怎么产生的。
**能复用说明当初的抽象切对了地方。**

【HyDE 为什么在词法检索上反而可能更有效】

HyDE 的直觉是"假设的答案和真实答案在向量空间里更接近"，
这本来是给**稠密神经 embedding** 设计的。

但用 TF-IDF / BM25 时它有一个**更直接**的作用：
用户的问题里往往**没有语料里的词**。
「我适合投什么岗位」里没有 "Kafka"、"Flink"、"后端开发工程师"，
而这些词恰恰是简历和 JD 里的实际用词。

假想答案会把它们**补进来** —— 于是 HyDE 在这里做的其实是
**词汇注入（vocabulary injection）**，正好治的是词法检索的
"查询-文档词不匹配"病。

**这一点值得讲**：同一个技术在不同检索后端上生效的机制可以完全不同。
面试时如果说"HyDE 是为了让向量更接近答案向量"，会被追问
"你用的是 TF-IDF，哪来的语义向量" —— 而正确答案是它在词法链路上
起的是别的作用。

【三条设计纪律】

1. **原查询必须永远在列表里。**改写是**增加**召回路径，不是替换。
   如果模型的改写全是废话，至少原始查询还在 ——
   **绝不能用一个有损变换去替换源数据**。

2. **LLM 失败不能拖垮检索。**改写是增强，增强件坏了必须降级到
   "只用原查询"，而不是让整个检索失败。
   **一个能破坏基础能力的"增强"是负价值。**

3. **改写结果要缓存。**每次检索多一次 LLM 调用（约几百 token），
   而相同查询在评测里会被反复跑。不缓存的话消融实验跑不起。
"""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class QueryRewriter(ABC):
    """把一个问题改写成若干个"检索用查询"。

    返回的列表**必须包含原查询**（见模块文档的第 1 条纪律）。
    为了不让每个实现都记着这件事，基类提供了 `_with_original` 兜底。
    """

    #: 用于日志与 /healthz 展示
    name: str = "base"

    def __init__(self) -> None:
        # 【为什么必须统计成功/失败次数 —— 这是被一次真实事故逼出来的】
        #
        # 我第一版把 LLM 调用写成了 `llm.complete(...)`，而 LLMClient 的
        # 方法其实叫 `chat(...)`。于是每次改写都抛 AttributeError，
        # 被下面那个宽泛的 except 吞掉，静默降级成"只用原查询"。
        #
        # 后果是：**我跑了一次消融实验，看到"加了改写"和"不加改写"的指标
        # 一模一样，差点得出"Query 改写对本项目没有收益"的结论。**
        # 而真相是这个功能压根没执行过。
        #
        # 教训分两层：
        #   · 生产上：降级是对的（增强件不该拖垮基础能力）
        #   · 评测上：降级必须**可观测**，否则你测的是"降级后的行为"
        #     却以为在测"功能本身"—— 这比不测更糟，因为它给出的是
        #     **一个看起来权威的负数结论**。
        self.calls = 0
        self.failures = 0
        self.generated = 0  # 累计产出的改写真条数（不含原查询）

    @abstractmethod
    async def _generate(self, query: str) -> list[str]:
        """生成改写（可以不含原查询，基类会补上）。"""
        ...

    async def rewrite(self, query: str) -> list[str]:
        self.calls += 1
        try:
            generated = await self._generate(query)
        except Exception as exc:
            # 【这里刻意捕获宽泛的异常】
            # 改写是**增强件**：它挂了不该让检索挂掉。
            # 而模型调用可能抛出的异常类型五花八门（HTTP 错误、
            # 超时、解析失败、上游 5xx 被包装成各种形态），
            # 逐一定点捕获既不现实也会漏 —— 漏掉一个就是线上检索整体不可用。
            #
            # 但**降级必须留下痕迹**：failures 计数会被评测脚本与
            # /healthz 读出来。静默降级是本模块最危险的东西。
            self.failures += 1
            logger.warning(
                "Query 改写失败，降级为仅用原查询（累计失败 %d/%d 次）：%s: %s",
                self.failures,
                self.calls,
                type(exc).__name__,
                exc,
            )
            return [query]
        out = self._with_original(query, generated)
        self.generated += len(out) - 1
        return out

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "calls": self.calls,
            "failures": self.failures,
            "generated": self.generated,
        }

    @staticmethod
    def _with_original(query: str, generated: list[str]) -> list[str]:
        """保证原查询在第一位，并去掉重复/空白。

        顺序有意义：融合权重按位置给，第一位拿最高权重。
        原查询拿最高权重是刻意的 —— 它是唯一**用户真的想问**的东西，
        改写只是我们的猜测。
        """
        out = [query.strip()]
        seen = {out[0]}
        for item in generated:
            text = (item or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return out


class NoOpRewriter(QueryRewriter):
    """不改写，只返回原查询。用于消融实验的对照组。"""

    name = "none"

    async def _generate(self, query: str) -> list[str]:
        return []


class MultiQueryRewriter(QueryRewriter):
    """让模型从 N 个不同角度重写同一个问题。

    【为什么提示词里要明确"不同角度"而不是"改写得更清楚"】
    "改写得更清楚"会让模型产出 3 个语义几乎相同的句子 ——
    对召回毫无帮助（三个几乎相同的向量召回的还是同一批东西），
    却照样付 3 倍的检索成本。

    所以要显式要求它换视角：换用词、换粒度、换提问对象。
    **改写的价值来自差异性，不是来自"写得更好"。**
    """

    name = "multi_query"

    def __init__(self, llm: Any, count: int = 3) -> None:
        super().__init__()
        self._llm = llm
        self._count = count

    def _build_prompt(self, query: str) -> str:
        return (
            f"用户的问题是：{query}\n\n"
            f"请从 {self._count} 个**不同角度**把它改写成 {self._count} 条独立的检索查询，"
            f"用于在一个包含「个人简历」与「岗位 JD」的知识库里做检索。\n\n"
            f"要求：\n"
            f"1. 每条查询换一个视角或粒度，不要只是换同义词。"
            f"例如：一条偏技能关键词，一条偏经历场景，一条偏岗位要求。\n"
            f"2. 每条查询必须能独立使用，不依赖其它查询的上下文。\n"
            f"3. 直接输出 {self._count} 行，每行一条，不要编号、不要解释、不要空行。"
        )

    async def _generate(self, query: str) -> list[str]:
        # 用 `llm.chat(...)` —— 这是 LLMClient 的真实接口。
        # 第一版写成了 `llm.complete(...)`（凭印象猜的方法名），
        # 于是每次都抛 AttributeError 被静默吞掉，功能从未真正执行，
        # 而消融实验给出了"零收益"的假结论。详见 QueryRewriter.__init__ 的说明。
        # ChatMessage 而不是裸 dict：chat() 要调 msg.to_wire() 序列化，
        # 传 dict 会得到 AttributeError: 'dict' object has no attribute 'to_wire'。
        # 惰性 import 是为了不让 rag 层与 llm 层在模块级互相依赖。
        from app.llm.types import ChatMessage

        resp = await self._llm.chat(
            [ChatMessage.user(self._build_prompt(query))],
            temperature=0.7,  # 改写要多样性，比生成答案的温度高
        )
        lines = [ln.strip(" -•\t") for ln in str(resp.message.content or "").splitlines()]
        # 只要前 count 条：模型偶尔会多吐几行，多出来的会让检索成本失控
        return [ln for ln in lines if ln][: self._count]


class HydeRewriter(QueryRewriter):
    """HyDE：先生成一段假想答案，再用它检索。

    【为什么提示词要求"用知识库里可能的措辞写"】
    假想答案的作用是**把语料的词汇引进来**。如果模型用一堆
    与自己知识库无关的泛泛之词写，那它除了多消耗一次调用之外毫无作用。

    所以要显式要求它像"文档片段"那样写，而不是像"给用户的回答"那样写。
    """

    name = "hyde"

    def __init__(self, llm: Any, max_chars: int = 300) -> None:
        super().__init__()
        self._llm = llm
        self._max_chars = max_chars

    def _build_prompt(self, query: str) -> str:
        return (
            f"问题：{query}\n\n"
            f"请写一段**假设性的资料片段**（就像它在某人的简历或岗位 JD 里那样），"
            f"用来回答这个问题。\n\n"
            f"要求：\n"
            f"1. 用文档的口吻写，包含具体的技术名词、技能、职责措辞，"
            f"不要写成对话或建议。\n"
            f"2. 不需要真实准确 —— 这只是一段用于检索的假想文本。\n"
            f"3. 控制在 {self._max_chars} 字以内，直接输出正文，不要标题、不要解释。"
        )

    async def _generate(self, query: str) -> list[str]:
        # ChatMessage 而不是裸 dict：chat() 要调 msg.to_wire() 序列化，
        # 传 dict 会得到 AttributeError: 'dict' object has no attribute 'to_wire'。
        # 惰性 import 是为了不让 rag 层与 llm 层在模块级互相依赖。
        from app.llm.types import ChatMessage

        resp = await self._llm.chat(
            [ChatMessage.user(self._build_prompt(query))],
            temperature=0.3,
        )
        text = str(resp.message.content or "").strip()[: self._max_chars]
        return [text] if text else []


class CachingRewriter(QueryRewriter):
    """给任意改写器加一层内存缓存。

    【为什么缓存是必需的，而不是优化】
    每次改写多一次 LLM 调用。而**评测时会用同一批查询反复跑**
    （消融阶梯 5 条管线 × 14 条查询 = 70 次检索，其中大量重复）。

    不缓存的话，光跑一次消融就要 70 次额外调用 ——
    结果是"这个功能因为太慢太贵所以没人跑评测，
    于是也没人知道它到底有没有用"。
    **一个无法被度量的优化，最终会被当作没有价值。**

    key 用 `(改写器名字, 查询)` 的哈希：不同改写器不能共用缓存，
    否则切换配置后会拿到上一组的改写结果 —— 那会让消融实验的数字
    完全不可信（A 配置的数字里混着 B 配置的缓存）。
    """

    def __init__(self, inner: QueryRewriter, max_size: int = 512) -> None:
        super().__init__()
        self._inner = inner
        self._cache: dict[str, list[str]] = {}
        self._max_size = max_size
        self.name = f"{inner.name}+cache"
        self.hits = 0
        self.misses = 0

    def stats(self) -> dict[str, Any]:
        # 把内层的统计一起透出去 —— 否则从外层读不到真实的失败次数，
        # 而"读不到"和"没有失败"看起来是一样的。
        return {
            **super().stats(),
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            **{f"inner_{k}": v for k, v in self._inner.stats().items() if k != "name"},
        }

    def _key(self, query: str) -> str:
        raw = f"{self._inner.name}::{query}".encode()
        return hashlib.sha1(raw).hexdigest()

    async def _generate(self, query: str) -> list[str]:
        return []  # 不使用：改写逻辑在 rewrite 里

    async def rewrite(self, query: str) -> list[str]:
        key = self._key(query)
        if key in self._cache:
            self.hits += 1
            return list(self._cache[key])
        self.misses += 1
        result = await self._inner.rewrite(query)
        # 简单 FIFO 淘汰：缓存条数有界即可，
        # 用 LRU 在这里是过度设计（查询集本身就是有限的）
        if len(self._cache) >= self._max_size:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = list(result)
        return result


def build_rewriter(
    kind: str, llm: Any | None = None, *, count: int = 3, cache_size: int = 512
) -> QueryRewriter:
    """按配置构造改写器。

    `kind="none"` 或没有 LLM 时返回 `NoOpRewriter` ——
    **没有模型不等于失败**，只是这个增强不可用，检索照常工作。
    """
    k = (kind or "none").strip().lower()
    if k in ("none", "", "off"):
        return NoOpRewriter()
    if llm is None:
        logger.warning("配置了 RAG_QUERY_REWRITE=%s 但没有可用的 LLM，改写已禁用", kind)
        return NoOpRewriter()
    if k in ("multi_query", "multiquery", "multi"):
        return CachingRewriter(MultiQueryRewriter(llm, count=count), max_size=cache_size)
    if k == "hyde":
        return CachingRewriter(HydeRewriter(llm), max_size=cache_size)
    raise ValueError(f"未知的 RAG_QUERY_REWRITE：{kind!r}（可选 none / multi_query / hyde）")
