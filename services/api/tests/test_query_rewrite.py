"""Query 改写（Multi-Query / HyDE）的测试。

【这组测试的第一目标不是"功能对不对"，而是"降级有没有被看见"】

开发这个功能时踩了**连续三个同类错误**，每一个都被降级逻辑吞掉：

    1. 方法名猜错：写了 `llm.complete(...)`，实际接口是 `chat(...)`
    2. 参数类型猜错：传了裸 dict，实际要 `ChatMessage` 对象
    3. 字段名猜错：读了 `resp.content`，实际是 `resp.message.content`

三次的表现**完全一样**：不报错、不崩溃，只是消融实验里
"加了改写"和"不加改写"的指标一模一样。

我当时差点据此写下"Query 改写对本项目没有收益"这个结论 ——
而真相是这个功能**从未执行过一次**。

所以这组测试重点锁定：
  · 失败必须被计数（`failures`），而不是只打一行日志
  · 原查询必须永远在结果里（改写是有损变换，不能替换源数据）
  · 未启用改写时，检索行为必须与功能引入前**完全一致**（消融基线可比）
"""

from __future__ import annotations

from typing import Any

import pytest
from app.rag.rewrite import (
    CachingRewriter,
    HydeRewriter,
    MultiQueryRewriter,
    NoOpRewriter,
    QueryRewriter,
    build_rewriter,
)


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeResponse:
    """模拟 ChatResponse：注意文本在 `.message.content`。

    这个形状是刻意的 —— 上面第 3 个错误就是把 `.message` 漏掉了。
    假对象如果写成 `resp.content`，测试会跟着错误实现一起"通过"，
    那就完全失去了发现问题的能力。
    """

    def __init__(self, text: str) -> None:
        self.message = FakeMessage(text)


class FakeLLM:
    def __init__(self, reply: str = "改写一\n改写二\n改写三", *, fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[Any], **kwargs: Any) -> FakeResponse:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.fail:
            raise RuntimeError("模拟上游失败")
        return FakeResponse(self.reply)


# ============================================================
# 基类契约
# ============================================================
class TestRewriterContract:
    async def test_original_query_always_present(self) -> None:
        """**最重要的一条纪律**：改写是增加召回路径，不是替换原查询。

        模型完全可能吐出一堆跑偏的改写。如果那些改写**替换**了原查询，
        一次坏的生成就会让检索彻底失效 —— 而正确做法是"退化成没启用改写"。
        **绝不能用一个有损变换去替换源数据。**
        """
        llm = FakeLLM("完全无关的东西A\n完全无关的东西B")
        out = await MultiQueryRewriter(llm).rewrite("原问题")
        assert out[0] == "原问题", "原查询必须在第一位"
        assert "完全无关的东西A" in out, "改写也应保留（它们是额外的召回路径）"

    async def test_original_is_first(self) -> None:
        """原查询必须在第一位 —— 融合权重按位置给，第一位权重最高。

        这不是格式要求：改写是我们**猜**用户想问什么，原查询是用户
        **真的**问了什么。让猜测拿到高权重会本末倒置。
        """
        out = await MultiQueryRewriter(FakeLLM()).rewrite("问题")
        assert out[0] == "问题"

    async def test_dedup_and_blank_filtered(self) -> None:
        """改写里出现原查询或空行时必须去掉，否则会重复计票。"""
        llm = FakeLLM("问题\n\n   \n另一条\n另一条")
        out = await MultiQueryRewriter(llm).rewrite("问题")
        assert out == ["问题", "另一条"], f"去重/去空失败：{out}"

    async def test_count_is_capped(self) -> None:
        """多余的改写要截断 —— 模型偶尔会吐十几行，检索成本会失控。"""
        llm = FakeLLM("\n".join(f"改写{i}" for i in range(20)))
        out = await MultiQueryRewriter(llm, count=3).rewrite("问题")
        assert len(out) == 4, f"应为 原查询 + 3 条改写，实际 {len(out)}"


# ============================================================
# 失败必须可见 —— 这是本次开发最大的教训
# ============================================================
class TestFailureIsObservable:
    async def test_failure_degrades_but_does_not_raise(self) -> None:
        """改写是增强件，挂了不该让检索挂掉。"""
        r = MultiQueryRewriter(FakeLLM(fail=True))
        out = await r.rewrite("问题")
        assert out == ["问题"], "失败时应退化为仅用原查询"

    async def test_failure_is_counted(self) -> None:
        """**降级必须留下计数，否则你无法区分"没效果"和"没执行"。**

        这条测试就是那三个连续错误的防线：如果哪天有人把计数去掉、
        或者把异常吞得更彻底，这里会红。
        """
        r = MultiQueryRewriter(FakeLLM(fail=True))
        for _ in range(3):
            await r.rewrite("问题")
        st = r.stats()
        assert st["calls"] == 3
        assert st["failures"] == 3, "失败次数没有被记录 —— 评测会得出错误的'零收益'结论"
        assert st["generated"] == 0

    async def test_success_is_counted(self) -> None:
        r = MultiQueryRewriter(FakeLLM("A\nB\nC"))
        await r.rewrite("问题")
        st = r.stats()
        assert st["failures"] == 0
        assert st["generated"] == 3, "成功产出的改写真条数应被记录"

    async def test_wrong_llm_interface_is_caught_and_counted(self) -> None:
        """方法名/签名不对时必须**记账**，而不是安静地什么都不做。

        这正是真实踩到的坑：如果 LLMClient 改名或换签名，
        我们希望测试立刻红，而不是等消融实验给出一个假结论。
        """

        class WrongLLM:
            pass  # 没有任何方法

        r = MultiQueryRewriter(WrongLLM())
        out = await r.rewrite("问题")
        assert out == ["问题"]
        assert r.stats()["failures"] == 1


# ============================================================
# 具体实现
# ============================================================
class TestImplementations:
    async def test_multi_query_passes_chat_message(self) -> None:
        """必须传 ChatMessage 对象而非裸 dict。

        `LLMClient.chat()` 会调 `msg.to_wire()` 序列化，传 dict 会得到
        `AttributeError: 'dict' object has no attribute 'to_wire'` ——
        这是开发中真实踩到的第 2 个错误。
        """
        llm = FakeLLM()
        await MultiQueryRewriter(llm).rewrite("问题")
        msg = llm.calls[0]["messages"][0]
        assert hasattr(msg, "to_wire"), f"应当传 ChatMessage 对象，实际是 {type(msg)}"

    async def test_hyde_uses_lower_temperature(self) -> None:
        """HyDE 要的是"像文档"，不是"有创意"，温度应低于 Multi-Query。

        Multi-Query 需要**差异性**（否则三条改写召回的还是同一批东西）；
        HyDE 需要**贴近语料措辞**。同一个参数在两个实现里该取不同的值。
        """
        llm = FakeLLM("假想文档片段")
        await HydeRewriter(llm).rewrite("问题")
        hyde_temp = llm.calls[0]["kwargs"].get("temperature")

        llm2 = FakeLLM("A\nB")
        await MultiQueryRewriter(llm2).rewrite("问题")
        multi_temp = llm2.calls[0]["kwargs"].get("temperature")

        assert hyde_temp is not None and multi_temp is not None
        assert hyde_temp < multi_temp, f"HyDE({hyde_temp}) 应低于 Multi-Query({multi_temp})"

    async def test_hyde_truncates(self) -> None:
        """假想文档太长会把检索成本推上去，必须截断。"""
        llm = FakeLLM("字" * 5000)
        out = await HydeRewriter(llm, max_chars=200).rewrite("问题")
        assert len(out) == 2
        assert len(out[1]) == 200

    async def test_noop_returns_only_original(self) -> None:
        r = NoOpRewriter()
        assert await r.rewrite("问题") == ["问题"]
        assert r.stats()["generated"] == 0


# ============================================================
# 缓存
# ============================================================
class TestCaching:
    async def test_second_call_hits_cache(self) -> None:
        """同一查询只调一次模型。

        【为什么这不是"性能优化"而是必需】
        评测会用同一批查询反复跑（消融阶梯 5~8 条管线 × 14 条查询）。
        不缓存的话光跑一次消融就要几十次额外调用 ——
        结果是"这个功能因为太贵所以没人跑评测，
        于是也没人知道它有没有用"。**无法被度量的优化等于没有。**
        """
        llm = FakeLLM("A\nB")
        r = CachingRewriter(MultiQueryRewriter(llm))
        await r.rewrite("同一个问题")
        await r.rewrite("同一个问题")
        assert len(llm.calls) == 1, "第二次应命中缓存"
        assert r.stats()["cache_hits"] == 1
        assert r.stats()["cache_misses"] == 1

    async def test_cache_key_includes_rewriter_name(self) -> None:
        """不同改写器不能共用缓存。

        否则切换配置后（Multi-Query → HyDE）会拿到上一组的改写结果 ——
        那会让消融实验的数字完全不可信（A 配置的数字里混着 B 的缓存）。
        **错误的缓存比没有缓存更糟，因为它静默地污染实验结论。**
        """
        llm_a = FakeLLM("multi的结果")
        llm_b = FakeLLM("hyde的结果")
        multi = CachingRewriter(MultiQueryRewriter(llm_a), max_size=8)
        hyde = CachingRewriter(HydeRewriter(llm_b), max_size=8)
        # 名字不同 → key 不同 → 两次都会真的调用
        await multi.rewrite("问题")
        await hyde.rewrite("问题")
        assert len(llm_a.calls) == 1 and len(llm_b.calls) == 1

    async def test_inner_stats_are_visible_from_outside(self) -> None:
        """外层要能读到内层的失败次数。

        **"读不到"和"没有失败"看起来是一样的** ——
        如果缓存包装层不透传统计，一个每次都失败的内层改写器
        在外层看起来会完全正常。
        """
        r = CachingRewriter(MultiQueryRewriter(FakeLLM(fail=True)))
        await r.rewrite("问题")
        st = r.stats()
        assert st["inner_failures"] == 1, f"内层失败次数没有透出来：{st}"

    async def test_cache_is_bounded(self) -> None:
        r = CachingRewriter(MultiQueryRewriter(FakeLLM("A")), max_size=3)
        for i in range(6):
            await r.rewrite(f"问题{i}")
        assert len(r._cache) <= 3, "缓存必须有界，否则长跑会吃光内存"


# ============================================================
# 工厂
# ============================================================
class TestFactory:
    def test_none_returns_noop(self) -> None:
        assert isinstance(build_rewriter("none"), NoOpRewriter)
        assert isinstance(build_rewriter(""), NoOpRewriter)

    def test_missing_llm_degrades_not_raises(self) -> None:
        """配了改写但没有模型可用时，应降级而不是让服务起不来。

        **没有模型不等于失败** —— 只是这个增强不可用，检索照常工作。
        """
        assert isinstance(build_rewriter("multi_query", None), NoOpRewriter)

    def test_kinds(self) -> None:
        llm = FakeLLM()
        assert isinstance(build_rewriter("multi_query", llm), CachingRewriter)
        assert isinstance(build_rewriter("hyde", llm), CachingRewriter)

    def test_unknown_kind_raises_clearly(self) -> None:
        """拼错配置要**立刻**报错，不能静默退化成 noop。

        静默退化会让人以为"功能开了但没效果"，而实际是配置名写错了。
        """
        with pytest.raises(ValueError, match="未知的 RAG_QUERY_REWRITE"):
            build_rewriter("hydee", FakeLLM())


# ============================================================
# 与检索器的集成
# ============================================================
class TestRetrieverIntegration:
    def _retriever(self, rewriter: QueryRewriter | None = None):  # type: ignore[no-untyped-def]
        from app.rag.chunker import ChunkStrategy
        from app.rag.loaders import DocType, LoadedDocument
        from app.rag.retriever import RetrievalMode, Retriever

        docs = [
            LoadedDocument(
                source="resume.md",
                doc_type=DocType.RESUME,
                text="专业技能\n熟悉 Kafka、Flink、ClickHouse\n\n实习经历\n负责缓存一致性方案",
            ),
            LoadedDocument(
                source="job-001 后端工程师",
                doc_type=DocType.JD,
                text="岗位名称：后端工程师\n\n任职要求\n熟悉消息队列与分布式缓存",
            ),
        ]
        return Retriever.from_documents(
            docs,
            strategy=ChunkStrategy.SECTION,
            min_size=0,
            mode=RetrievalMode.HYBRID,
            rewriter=rewriter,
        )

    async def test_no_rewriter_is_unchanged_path(self) -> None:
        """未传改写器时，结果必须与功能引入前一致。

        这条保障的是**消融实验的可比性**：基线不能因为新增了一个
        默认关闭的功能就悄悄变化，否则"加了改写提升多少"这个数字
        是拿两个不同基线比出来的。
        """
        r = self._retriever(None)
        hits = await r.aretrieve("Kafka", k=3)
        assert hits, "检索应有结果"
        assert r.stats()["rewriter"] == "none"

    async def test_rewriter_is_invoked(self) -> None:
        """启用改写后，每个查询都要经过改写器。"""
        llm = FakeLLM("消息队列经验\n分布式缓存 实习")
        r = self._retriever(CachingRewriter(MultiQueryRewriter(llm)))
        hits = await r.aretrieve("我有什么技术", k=3)
        assert hits
        st = r.stats()["rewriter_stats"]
        assert st["inner_calls"] >= 1, "改写器没有被调用"
        assert st["inner_failures"] == 0, "改写器调用了但失败了"

    async def test_rewriter_failure_does_not_break_retrieval(self) -> None:
        """**降级路径的端到端验证**：改写全挂，检索仍须正常返回。"""
        r = self._retriever(MultiQueryRewriter(FakeLLM(fail=True)))
        hits = await r.aretrieve("Kafka 消息队列", k=3)
        assert hits, "改写失败不应导致检索无结果"
        assert r.stats()["rewriter_stats"]["failures"] >= 1

    async def test_rewrites_expand_matching_candidates(self) -> None:
        """改写应引入原查询未匹配的候选；禁用改写是同一语料上的对照。

        零匹配不再凑满 k 条，改写前后集合大小可以不同，不能要求一致。
        """
        query = "我有什么技术"
        full = 10  # 取到全部块

        base = await self._retriever(None).aretrieve(query, k=full)
        base_order = [h.chunk.id for h in base]

        # 改写真里带上了语料里的实际词汇（这正是改写该起的作用：
        # 把用户问题里缺失的、语料里存在的词补进来）
        llm = FakeLLM("Kafka Flink ClickHouse 专业技能\n消息队列 分布式缓存 任职要求")
        r = self._retriever(CachingRewriter(MultiQueryRewriter(llm, count=2)))
        rewritten = await r.aretrieve(query, k=full)
        rewritten_order = [h.chunk.id for h in rewritten]

        assert base_order, "标题中的技能应被原查询召回"
        assert set(base_order) < set(rewritten_order), "改写应增加真实匹配，不能只凑零分块"
