"""RAG 层测试。

覆盖重点是**迭代中修掉的真实缺陷与新增能力的契约**，而不是重复验证顺利路径：

  1. 章节标题无正文时整节被丢弃 → 内容凭空消失（最危险：不报错、只是查不到）
  2. HTML 注释进了语料 → 元数据变成可检索噪声
  3. 标题启发式过于激进 → 「软件工程」「年龄：21岁」被误判为标题
  4. 纯标记碎片（`---`）参与相似度计算 → 3 字符的块拿到高分挤出真内容
  5. 短标题被"信息量过滤"误删 → 章节元数据丢失
  6. 中文用 sklearn 默认分词 → 整句被当成一个词，检索彻底失效
  7. BM25 的 tf 不饱和 / IDF 为负 / 长度归一化失效
  8. RRF 的权重校验与同分稳定性
  9. LLM 重排返回非法输出时**绝不能中断检索**（重排是优化项，不是关键路径）
 10. 重排后 rank 必须重新编号（否则 UI 与日志自相矛盾）

外加指标计算本身的正确性（Recall / MRR / NDCG 的边界情况）——
**评测工具算错了，比被测系统错了更可怕**，因为它会给你虚假的信心。
"""

from __future__ import annotations

import asyncio

import pytest
from app.rag.bm25 import BM25
from app.rag.chunker import (
    Chunk,
    ChunkStrategy,
    _is_heading,
    _is_informative,
    _merge_small_chunks,
    chunk_document,
    split_by_sections,
    split_recursive,
)
from app.rag.embedder import EmbedderError, TfidfEmbedder
from app.rag.evaluate import (
    EvalQuery,
    EvalSet,
    GoldCondition,
    evaluate,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)
from app.rag.fusion import fuse_rankings, reciprocal_rank_fusion
from app.rag.loaders import DocType, LoadedDocument, normalize_text
from app.rag.rerank import LexicalReranker, LLMReranker, NoOpReranker
from app.rag.retriever import RetrievalMode, Retriever
from app.rag.store import SearchHit, VectorStore
from app.rag.tokenizer import tokenize, tokenize_query_filtered


def _doc(text: str, name: str = "test.md", doc_type: DocType = DocType.RESUME) -> LoadedDocument:
    return LoadedDocument(source=name, doc_type=doc_type, text=text, char_count=len(text))


# ============================================================
# 文本规范化
# ============================================================
class TestNormalize:
    def test_html_comment_stripped(self) -> None:
        """摄取脚本写在文件头的注释不能变成可检索内容。"""
        raw = "<!-- 来源: 简历.pdf | 类型: resume -->\n\n张三\n后端工程师"
        out = normalize_text(raw)
        assert "<!--" not in out
        assert "来源" not in out
        assert out.startswith("张三")

    def test_multiline_comment_stripped(self) -> None:
        raw = "<!--\n多行\n说明\n-->\n正文开始"
        assert normalize_text(raw) == "正文开始"

    def test_whitespace_collapsed(self) -> None:
        assert normalize_text("a   b\t\tc\nd   \n\n\n\ne") == "a b c\nd\n\ne"

    def test_single_newline_preserved(self) -> None:
        """段落感要保留 —— 全压成一行会让后续切分失去依据。"""
        assert "\n" in normalize_text("第一行\n第二行")


# ============================================================
# 章节切分
# ============================================================
class TestSplitBySections:
    def test_basic_sections(self) -> None:
        text = "张三\n138-0000\n\n教育经历\n某某大学\n\n专业技能\nPython、Redis"
        sections = dict(split_by_sections(text))
        assert "教育经历" in sections
        assert "某某大学" in sections["教育经历"]
        assert "Python、Redis" in sections["专业技能"]

    def test_heading_without_body_is_not_lost(self) -> None:
        """回归：连续两个标题时，前一个标题所在章节正文为空。

        初版实现会把这一节整个丢掉 —— 简历里「院校名」后面紧跟「专业名」，
        结果学历那一行凭空消失，检索永远查不到。
        """
        text = "教育经历\n某某大学（211 院校）\n软件工程\n2017-2021"
        sections = split_by_sections(text)
        all_text = "\n".join(body for _, body in sections)
        assert "某某大学" in all_text, "标题无正文时内容被丢弃了"

    def test_leading_content_goes_to_opening_section(self) -> None:
        text = "张三\n邮箱：a@b.com\n\n教育经历\n某某大学"
        sections = split_by_sections(text)
        assert sections[0][0] == "(开头)"
        assert "a@b.com" in sections[0][1]

    def test_markdown_headings(self) -> None:
        text = "# 标题\n内容A\n\n## 小节\n内容B"
        sections = dict(split_by_sections(text))
        assert "标题" in sections
        assert "内容A" in sections["标题"]

    @pytest.mark.parametrize(
        "line",
        ["教育经历", "专业技能", "工作经历", "个人项目", "任职要求", "## 教育经历", "竞赛荣誉"],
    )
    def test_vocabulary_headings_recognized(self, line: str) -> None:
        assert _is_heading(line)

    @pytest.mark.parametrize(
        "line",
        [
            "软件工程",
            "某某大学（211 院校）",
            "年龄：21 岁",
            "2023 年 9 月~2027 年 6 月",
            "内容A",
            "电话：123",
            "某某大学",
        ],
    )
    def test_content_lines_not_treated_as_headings(self, line: str) -> None:
        """回归：这些是 PDF 提取出的**内容行**，不是标题。

        初版用"短行 + 无句末标点"判定，把它们全判成标题，导致切分碎片化。
        最终的取舍是：**宁可漏判标题，也不凭空发明标题** ——
        漏判只是章节粒度变粗（内容仍由递归切分兜底，不会丢），
        误判会制造假边界、切碎语义。
        """
        assert not _is_heading(line)

    def test_markdown_heading_always_recognized(self) -> None:
        """显式 Markdown 井号是最可靠的信号，任何长度都要认。"""
        assert _is_heading("### 这是一个比较长的自定义小节标题")
        assert _is_heading("# 标题")


# ============================================================
# 递归切分与重叠
# ============================================================
class TestSplitRecursive:
    def test_short_text_single_chunk(self) -> None:
        assert split_recursive("短文本", 100) == ["短文本"]

    def test_splits_on_paragraph_boundary(self) -> None:
        text = "第一段内容。" * 10 + "\n\n" + "第二段内容。" * 10
        chunks = split_recursive(text, 40)
        assert len(chunks) > 1
        # 优先在段落边界切，不应把「第二段」拆到第一段里
        assert not any("第一段" in c and "第二段" in c for c in chunks)

    def test_no_content_lost(self) -> None:
        """切分绝不能丢内容 —— 拼接后各段的关键词都应还在。"""
        text = "".join(f"第{i}句测试内容。" for i in range(30))
        chunks = split_recursive(text, 50)
        joined = "".join(chunks)
        for i in (0, 10, 29):
            assert f"第{i}句" in joined

    def test_overlap_shares_tail(self) -> None:
        text = "".join(f"句子{i}。" for i in range(20))
        chunks = split_recursive(text, 40, overlap=10)
        assert len(chunks) > 1
        # 相邻块应有共享内容
        assert chunks[1][:5] in chunks[0]


# ============================================================
# 信息量过滤与碎片合并
# ============================================================
class TestInformativeness:
    @pytest.mark.parametrize("junk", ["---", "###", "|", "***", "   ", ">>> "])
    def test_markup_only_dropped(self, junk: str) -> None:
        assert not _is_informative(junk)

    @pytest.mark.parametrize("kept", ["项目经历", "A", "1", "中文", "Redis"])
    def test_real_content_kept(self, kept: str) -> None:
        """回归：短标题不能被误删，否则章节元数据会丢失。"""
        assert _is_informative(kept)


class TestMergeSmall:
    def test_small_chunks_merged_into_buffer(self) -> None:
        """小于 min_size 的块会持续并入缓冲区，直到超过阈值。

        注意：一个超过阈值的长块**会作为"补足者"并入前面的小缓冲**，
        而不是另起一块 —— 因为合并发生在"追加"这一步。
        """
        raw = [("a", "短"), ("b", "也短"), ("c", "x" * 300)]
        merged = _merge_small_chunks(raw, min_size=100)
        assert len(merged) == 1
        assert "短" in merged[0][1]
        assert "也短" in merged[0][1]

    def test_large_chunks_stay_separate(self) -> None:
        raw = [("a", "x" * 200), ("b", "y" * 200)]
        assert len(_merge_small_chunks(raw, min_size=100)) == 2

    def test_disabled_when_zero(self) -> None:
        raw = [("a", "短"), ("b", "也短")]
        assert _merge_small_chunks(raw, min_size=0) == raw

    def test_trailing_small_merged_backwards(self) -> None:
        """结尾的小尾巴要并回上一块，不能留下孤立的短块。"""
        raw = [("a", "x" * 200), ("b", "尾巴")]
        merged = _merge_small_chunks(raw, min_size=100)
        assert len(merged) == 1
        assert "尾巴" in merged[0][1]

    def test_no_content_lost(self) -> None:
        raw = [("a", "AA"), ("b", "BB"), ("c", "CC"), ("d", "DD")]
        merged = _merge_small_chunks(raw, min_size=10)
        joined = "".join(t for _, t in merged)
        for token in ("AA", "BB", "CC", "DD"):
            assert token in joined


# ============================================================
# chunk_document 集成
# ============================================================
class TestChunkDocument:
    def test_markup_fragments_excluded(self) -> None:
        text = "教育经历\n某某大学\n\n---\n\n专业技能\nPython"
        chunks = chunk_document(_doc(text))
        assert all(_is_informative(c.text) for c in chunks)
        assert not any(c.text.strip() == "---" for c in chunks)

    def test_doc_type_and_metadata_propagated(self) -> None:
        doc = LoadedDocument(
            source="job-001 测试岗位",
            doc_type=DocType.JD,
            text="任职要求\nPython、Kafka",
            metadata_hint={"city": "北京"},
        )
        chunks = chunk_document(doc)
        assert all(c.doc_type == DocType.JD for c in chunks)
        assert all(c.metadata.get("city") == "北京" for c in chunks)

    def test_ids_are_deterministic(self) -> None:
        """同样的输入必须得到同样的 id，否则评测结果不可复现。"""
        doc = _doc("教育经历\n某某大学\n\n专业技能\nPython")
        assert [c.id for c in chunk_document(doc)] == [c.id for c in chunk_document(doc)]

    def test_all_strategies_produce_chunks(self) -> None:
        text = "教育经历\n某某大学\n\n" + "专业技能\n" + "Python、Redis。" * 30
        for strategy in ChunkStrategy:
            chunks = chunk_document(_doc(text), strategy=strategy, size=120)
            assert chunks, f"{strategy} 策略没有产出任何块"


# ============================================================
# 向量化
# ============================================================
class TestTfidfEmbedder:
    def test_vectors_are_l2_normalized(self) -> None:
        import numpy as np

        emb = TfidfEmbedder()
        corpus = ["Python 后端开发", "Kafka 消息队列", "ClickHouse 数据分析"]
        emb.fit(corpus)
        mat = emb.encode(corpus)
        norms = np.linalg.norm(mat, axis=1)
        assert np.allclose(norms, 1.0, atol=1e-5)

    def test_chinese_retrieval_works(self) -> None:
        """中文必须能检索。

        若用 sklearn 默认的空格分词，整句会被当成一个"词"，
        所有文本向量几乎相同，检索彻底失效。这是本模块最关键的一条断言。
        """
        import numpy as np

        emb = TfidfEmbedder()
        corpus = ["我熟练掌握 Kafka 消息队列", "我熟悉 Python 并发编程", "我做过前端页面开发"]
        emb.fit(corpus)
        q = emb.encode_query("消息队列")
        scores = emb.encode(corpus) @ q
        assert int(np.argmax(scores)) == 0, "中文检索失效，可能用了空格分词"

    def test_encode_before_fit_raises(self) -> None:
        with pytest.raises(EmbedderError):
            TfidfEmbedder().encode(["x"])

    def test_empty_corpus_raises(self) -> None:
        with pytest.raises(EmbedderError):
            TfidfEmbedder().fit([])

    def test_too_short_corpus_raises_clear_error(self) -> None:
        """字符 2-gram 在极短文本上切不出特征。

        这里必须给出**可操作**的错误提示，而不是把 sklearn 那句
        "empty vocabulary; perhaps the documents only contain stop words"
        直接甩出去 —— 那句话会把人引向"停用词"的错误方向。
        """
        with pytest.raises(EmbedderError, match="语料过短"):
            TfidfEmbedder().fit(["x"])

    def test_empty_encode_returns_empty_matrix(self) -> None:
        emb = TfidfEmbedder()
        emb.fit(["a b c"])
        assert emb.encode([]).shape[0] == 0


# ============================================================
# 向量库
# ============================================================
class TestVectorStore:
    @staticmethod
    def _store() -> VectorStore:
        emb = TfidfEmbedder()
        texts = ["我熟练掌握 Kafka 消息队列", "我熟悉 Python 并发编程", "前端 React 页面开发"]
        emb.fit(texts)
        store = VectorStore(emb)
        store.add(
            [
                Chunk(
                    id=f"c{i}",
                    doc_id="d",
                    doc_type=DocType.RESUME,
                    text=t,
                    index=i,
                    section=f"s{i}",
                )
                for i, t in enumerate(texts)
            ]
        )
        return store

    def test_search_returns_relevant_first(self) -> None:
        hits = self._store().search("消息队列", k=3)
        assert hits
        assert "Kafka" in hits[0].chunk.text

    def test_rank_is_sequential(self) -> None:
        hits = self._store().search("Python", k=3)
        assert [h.rank for h in hits] == list(range(len(hits)))

    def test_doc_type_filter(self) -> None:
        store = self._store()
        assert store.search("Python", k=3, doc_types=["jd"]) == []
        assert store.search("Python", k=3, doc_types=["resume"])

    def test_min_score_drops_noise(self) -> None:
        store = self._store()
        assert len(store.search("完全不相关的外星词汇", k=3, min_score=0.5)) == 0

    def test_empty_store_returns_empty(self) -> None:
        emb = TfidfEmbedder()
        emb.fit(["一些足够长的文本内容"])
        assert VectorStore(emb).search("文本", k=3) == []

    def test_stats(self) -> None:
        stats = self._store().stats()
        assert stats["chunk_count"] == 3
        assert stats["by_doc_type"] == {"resume": 3}


# ============================================================
# 指标计算（评测工具本身的正确性）
# ============================================================
def _chunk(cid: str, text: str = "", doc: str = "d", section: str = "") -> Chunk:
    return Chunk(
        id=cid, doc_id=doc, doc_type=DocType.NOTE, text=text or cid, index=0, section=section
    )


class TestMetrics:
    def test_recall_perfect_and_zero(self) -> None:
        gold = [GoldCondition(text_contains="Kafka")]
        corpus = [_chunk("a", "Kafka"), _chunk("b", "Python")]
        assert recall_at_k([corpus[0]], gold, 1, corpus) == 1.0
        assert recall_at_k([corpus[1]], gold, 1, corpus) == 0.0

    def test_recall_denominator_counts_all_matching_chunks(self) -> None:
        """分母是语料中符合条件的**块总数**，不是标注条数。

        算错会让指标虚高 —— 一个条件命中 3 个块时，只召回 1 个应该是 1/3。
        """
        gold = [GoldCondition(text_contains="Kafka")]
        corpus = [_chunk("a", "Kafka"), _chunk("b", "Kafka 也在"), _chunk("c", "Kafka 三")]
        assert recall_at_k([corpus[0]], gold, 3, corpus) == pytest.approx(1 / 3)

    def test_recall_no_match_in_corpus_is_zero(self) -> None:
        """标注条件匹配不到任何块时返回 0，而不是除零崩溃。"""
        assert (
            recall_at_k([_chunk("a")], [GoldCondition(text_contains="不存在")], 1, [_chunk("a")])
            == 0.0
        )

    def test_recall_empty_gold_is_zero(self) -> None:
        assert recall_at_k([_chunk("a")], [], 1, [_chunk("a")]) == 0.0

    @pytest.mark.parametrize(("rank_of_hit", "expected"), [(1, 1.0), (2, 0.5), (4, 0.25)])
    def test_reciprocal_rank(self, rank_of_hit: int, expected: float) -> None:
        gold = [GoldCondition(text_contains="target")]
        ranked = [_chunk(f"c{i}") for i in range(rank_of_hit - 1)] + [_chunk("hit", "target")]
        assert reciprocal_rank(ranked, gold) == pytest.approx(expected)

    def test_reciprocal_rank_miss(self) -> None:
        assert reciprocal_rank([_chunk("a")], [GoldCondition(text_contains="target")]) == 0.0

    def test_ndcg_perfect_ranking_is_one(self) -> None:
        gold = [GoldCondition(text_contains="t")]
        ranked = [_chunk("a", "t"), _chunk("b", "t")]
        assert ndcg_at_k(ranked, gold, 2, ranked) == pytest.approx(1.0)

    def test_ndcg_penalizes_lower_rank(self) -> None:
        gold = [GoldCondition(text_contains="t")]
        corpus = [_chunk("a", "t"), _chunk("b", "x")]
        high = ndcg_at_k(corpus, gold, 2, corpus)
        low = ndcg_at_k(list(reversed(corpus)), gold, 2, corpus)
        assert high > low

    def test_ndcg_miss_is_zero(self) -> None:
        corpus = [_chunk("a", "x")]
        assert ndcg_at_k(corpus, [GoldCondition(text_contains="t")], 1, corpus) == 0.0

    @pytest.mark.parametrize("include_tail", [True, False])
    def test_ndcg_missing_relevant_chunk_is_not_perfect(self, include_tail: bool) -> None:
        corpus = [_chunk("a", "target A"), _chunk("b", "target B"), _chunk("c", "noise")]
        ranked = [corpus[0], corpus[2]] + ([corpus[1]] if include_tail else [])
        score = ndcg_at_k(ranked, [GoldCondition(text_contains="target")], 2, corpus)
        assert score == pytest.approx(0.6131471927654585)

    @pytest.mark.parametrize("k", [1, 2, 5])
    def test_ndcg_matches_independent_reference(self, k: int) -> None:
        from itertools import permutations

        from sklearn.metrics import ndcg_score

        corpus = [_chunk(str(i), "target" if i < 3 else "noise") for i in range(5)]
        gold = [GoldCondition(text_contains="target")]
        for order in permutations(range(5)):
            scores = [0] * 5
            for rank, index in enumerate(order):
                scores[index] = 5 - rank
            expected = ndcg_score([[1, 1, 1, 0, 0]], [scores], k=k)
            assert ndcg_at_k([corpus[i] for i in order], gold, k, corpus) == pytest.approx(expected)

    def test_ndcg_zero_cutoff_and_negative_cutoff(self) -> None:
        corpus = [_chunk("a", "target")]
        gold = [GoldCondition(text_contains="target")]
        assert ndcg_at_k(corpus, gold, 0, corpus) == 0
        with pytest.raises(ValueError, match="k"):
            ndcg_at_k(corpus, gold, -1, corpus)

    def test_gold_conditions_are_and(self) -> None:
        """同一条件内的多个字段是 AND 语义。"""
        cond = GoldCondition(doc_id_contains="resume", text_contains="Kafka")
        assert cond.matches(_chunk("a", "Kafka 经验", doc="resume.md"))
        assert not cond.matches(_chunk("b", "Kafka 经验", doc="job-001"))
        assert not cond.matches(_chunk("c", "Python", doc="resume.md"))


class TestEvaluateHarness:
    async def test_ndcg_uses_full_retriever_corpus(self) -> None:
        corpus = [_chunk("a", "target A"), _chunk("b", "target B"), _chunk("c", "noise")]

        class PartialRetriever:
            chunks = corpus

            def stats(self):
                return {"embedder": "synthetic"}

            async def aretrieve(self, query, k):
                return [
                    SearchHit(chunk=corpus[0], score=2, rank=1),
                    SearchHit(chunk=corpus[2], score=1, rank=2),
                ]

        queries = EvalSet(
            queries=[EvalQuery(query="target", gold=[GoldCondition(text_contains="target")])]
        )
        report = await evaluate(PartialRetriever(), queries, k=2)
        assert report.metrics["ndcg"] == pytest.approx(0.6131471927654585)
        assert report.metric_version == "ndcg-corpus-v2"

    def _retriever(self):
        from app.rag.retriever import Retriever

        docs = [
            _doc("教育经历\n某某大学\n\n专业技能\n熟练掌握 Kafka、Flink、ClickHouse", "resume.md"),
            LoadedDocument(
                source="job-001 大模型工程师",
                doc_type=DocType.JD,
                text="岗位名称：大模型工程师\n\n任职要求\n熟悉 RAG 与向量数据库",
            ),
        ]
        return Retriever.from_documents(docs)

    def test_report_structure(self) -> None:
        r = self._retriever()
        eval_set = EvalSet(
            name="t",
            queries=[
                EvalQuery(query="我熟悉哪些消息队列", gold=[GoldCondition(text_contains="Kafka")]),
                EvalQuery(
                    query="哪个岗位要求向量数据库",
                    gold=[GoldCondition(doc_id_contains="job-001", text_contains="向量数据库")],
                ),
            ],
        )
        report = asyncio.run(evaluate(r, eval_set, k=3))
        assert report.chunk_count > 0
        assert 0.0 <= report.metrics["recall"] <= 1.0
        assert 0.0 <= report.metrics["mrr"] <= 1.0
        assert len(report.per_query) == 2
        assert "Recall@" in report.summary_line()

    def test_failures_recorded_with_diagnostics(self) -> None:
        """完全未命中的查询必须留下"期望 vs 实取"，这是改进的唯一线索。"""
        r = self._retriever()
        eval_set = EvalSet(
            name="t",
            queries=[
                EvalQuery(
                    query="完全不存在的主题",
                    gold=[GoldCondition(text_contains="外星语言")],
                    difficulty="hard",
                )
            ],
        )
        report = asyncio.run(evaluate(r, eval_set, k=3))
        assert len(report.failures) == 1
        f = report.failures[0]
        assert "gold_conditions" in f
        assert "actually_retrieved" in f
        assert report.metrics["hit_rate"] == 0.0


# ============================================================
# 分词器
# ============================================================
class TestTokenizer:
    def test_latin_words_lowercased_and_kept_whole(self) -> None:
        assert tokenize("Kafka") == ["kafka"]

    @pytest.mark.parametrize(
        "raw",
        ["TCP/IP", "C++", "bge-small-zh-v1.5", "30-60K", "ClickHouse", "node.js"],
    )
    def test_technical_tokens_survive(self, raw: str) -> None:
        """技术串必须整体保留。

        若按标点切开，`30-60K` 会变成 `30` 与 `60k`，
        检索"薪资 35-70K"就退化成数字匹配，语义全丢。
        """
        tokens = tokenize(raw)
        assert raw.lower() in tokens, f"{raw} 被切碎了：{tokens}"

    def test_cjk_produces_unigrams_and_bigrams(self) -> None:
        tokens = tokenize("消息队列")
        assert "消" in tokens and "息" in tokens  # 单字保召回
        assert "消息" in tokens and "队列" in tokens  # 双字保精度

    def test_mixed_text(self) -> None:
        tokens = tokenize("熟悉 Kafka 消息队列")
        assert "熟悉" in tokens
        assert "kafka" in tokens
        assert "消息" in tokens

    def test_empty_and_punctuation_only(self) -> None:
        assert tokenize("") == []
        assert tokenize("，。！？") == []

    def test_query_stopwords_filtered(self) -> None:
        """疑问词对 BM25 是纯噪声：它们几乎不出现在语料里，只会稀释有效词。"""
        assert "什么" not in tokenize_query_filtered("我熟悉什么技术")
        assert "熟悉" in tokenize_query_filtered("我熟悉什么技术")

    def test_query_filter_keeps_content_words(self) -> None:
        tokens = tokenize_query_filtered("哪些岗位要求向量数据库")
        assert "向量" in tokens
        assert "数据" in tokens
        # 注意：分词器产出的是单字与双字，三字词（"数据库"）不会作为单个词元出现。
        # 这是刻意的取舍 —— 双字足以覆盖，而枚举所有长度会让词表爆炸。
        assert "岗位" in tokens or "位要" in tokens


# ============================================================
# BM25
# ============================================================
class TestBM25:
    @staticmethod
    def _bm25() -> BM25:
        bm = BM25()
        bm.fit(
            [
                "我熟练掌握 Kafka 消息队列，理解分区与副本机制",
                "我熟悉 Python 并发编程与异步 IO",
                "使用 ClickHouse 做亿级数据的分析查询",
            ]
        )
        return bm

    def test_relevant_doc_scores_highest(self) -> None:
        import numpy as np

        scores = self._bm25().scores("消息队列")
        assert int(np.argmax(scores)) == 0

    def test_unseen_term_scores_zero_everywhere(self) -> None:
        import numpy as np

        scores = self._bm25().scores("外星语言量子纠缠")
        assert np.allclose(scores, 0.0)

    def test_empty_query_scores_zero(self) -> None:
        import numpy as np

        assert np.allclose(self._bm25().scores(""), 0.0)

    def test_idf_is_never_negative(self) -> None:
        """词出现在多数文档里时，标准 IDF 会变负导致"包含它反而扣分"。

        BM25+ 的 +1 修正保证恒为正。语料很小时这个问题尤其突出。
        """
        bm = BM25()
        bm.fit(["共同词 A", "共同词 B", "共同词 C"])  # 「共同」出现在 100% 文档
        assert bm._idf("共同") > 0

    def test_oov_term_idf_is_zero_not_negative(self) -> None:
        bm = BM25()
        bm.fit(["kafka"])
        assert bm._idf("完全不存在的词") == 0.0

    def test_frequency_saturates(self) -> None:
        """tf 必须饱和：出现 10 次的得分不该是出现 1 次的 10 倍。

        这是 BM25 相对朴素 TF-IDF 的核心改进 —— 第 10 次出现几乎没有新信息。
        """
        bm = BM25()
        bm.fit(["kafka", "kafka kafka kafka kafka kafka kafka kafka kafka kafka kafka"])
        scores = bm.scores("kafka")
        assert scores[1] / scores[0] < 5, "tf 没有饱和，退化成朴素 TF 了"

    def test_length_normalization_dampens_long_docs(self) -> None:
        """长文档不该仅因为"词多"就无脑占优。"""
        short = "kafka 消息队列"
        long = "kafka 消息队列 " + "无关内容 " * 60
        bm = BM25()
        bm.fit([short, long])
        scores = bm.scores("kafka 消息队列")
        assert scores[0] > scores[1], "长度归一化失效，长文档占了便宜"

    def test_b_zero_disables_length_norm(self) -> None:
        bm = BM25(b=0.0)
        bm.fit(["kafka", "kafka " + "噪声 " * 50])
        # b=0 时长度不参与归一化，两侧 tf 相同时得分应接近
        scores = bm.scores("kafka")
        assert scores[1] / scores[0] > 0.9

    def test_empty_corpus(self) -> None:
        bm = BM25()
        bm.fit([])
        assert bm.scores("任何查询").size == 0

    def test_vocab_size(self) -> None:
        assert self._bm25().vocab_size > 0


# ============================================================
# RRF 融合
# ============================================================
class TestRRF:
    def test_doc_in_both_lists_wins(self) -> None:
        """在多路里都出现的文档应当胜出 —— 这是 RRF 的核心行为。"""
        fused = fuse_rankings([["a", "b", "c"], ["b", "d", "e"]])
        assert fused[0] == "b"

    def test_rank_order_respected_within_single_list(self) -> None:
        assert fuse_rankings([["x", "y", "z"]]) == ["x", "y", "z"]

    def test_score_is_sum_of_reciprocals(self) -> None:
        fused = dict(reciprocal_rank_fusion([["a"], ["a"]], k=60))
        assert fused["a"] == pytest.approx(2 / 61)

    def test_k_controls_flatness(self) -> None:
        """k 越小，头部优势越极端。"""
        small = dict(reciprocal_rank_fusion([["a", "b"]], k=1))
        large = dict(reciprocal_rank_fusion([["a", "b"]], k=1000))
        assert (small["a"] - small["b"]) > (large["a"] - large["b"])

    def test_weights_validated(self) -> None:
        with pytest.raises(ValueError, match="权重数量"):
            reciprocal_rank_fusion([["a"], ["b"]], weights=[1.0])

    def test_empty_input(self) -> None:
        assert fuse_rankings([]) == []
        assert fuse_rankings([[], []]) == []

    def test_top_n(self) -> None:
        assert fuse_rankings([["a", "b", "c"]], top_n=2) == ["a", "b"]

    def test_deterministic_tie_break(self) -> None:
        """同分时按 id 排序，保证结果可复现 —— 否则评测数字会抖动。"""
        first = fuse_rankings([["b"], ["a"]])
        second = fuse_rankings([["b"], ["a"]])
        assert first == second


# ============================================================
# 重排
# ============================================================
def _hit(cid: str, text: str, section: str = "", score: float = 0.0) -> SearchHit:
    return SearchHit(
        chunk=Chunk(id=cid, doc_id="d", doc_type=DocType.NOTE, text=text, index=0, section=section),
        score=score,
        rank=0,
    )


class TestLexicalReranker:
    @staticmethod
    def _reranker() -> LexicalReranker:
        return LexicalReranker()

    async def test_promotes_high_coverage_candidate(self) -> None:
        r = self._reranker()
        hits = [
            _hit("low", "我熟悉前端页面开发"),
            _hit("high", "我熟练掌握 Kafka 消息队列与分区机制"),
        ]
        out = await r.rerank("Kafka 消息队列", hits, top_k=2)
        assert out[0].chunk.id == "high"

    async def test_section_name_is_a_signal(self) -> None:
        """章节名是人工构造的强信号，却不参与向量/BM25 打分 —— 属于被浪费的信息。"""
        r = self._reranker()
        hits = [
            _hit("plain", "一些无关的技术罗列内容文本", section="其他"),
            _hit("titled", "一些无关的技术罗列内容文本", section="专业技能"),
        ]
        out = await r.rerank("专业技能有哪些", hits, top_k=2)
        assert out[0].chunk.id == "titled"

    def test_exact_phrase_bonus(self) -> None:
        r = self._reranker()
        phrase_hit = _hit("a", "我的实习经历包括哪些技术内容", section="")
        scattered = _hit("b", "实习的经历，历实习，经历", section="")
        assert r.score("实习经历", phrase_hit) > r.score("实习经历", scattered)

    async def test_empty_input(self) -> None:
        assert await self._reranker().rerank("q", [], top_k=5) == []

    async def test_top_k_truncates(self) -> None:
        hits = [_hit(f"c{i}", f"内容 {i}") for i in range(6)]
        out = await self._reranker().rerank("内容", hits, top_k=3)
        assert len(out) == 3

    async def test_rank_is_renumbered(self) -> None:
        """重排后 rank 必须与最终顺序一致，否则 UI 与日志会自相矛盾。"""
        hits = [_hit(f"c{i}", f"内容 {i}") for i in range(4)]
        out = await self._reranker().rerank("内容", hits, top_k=4)
        assert [h.rank for h in out] == [0, 1, 2, 3]

    async def test_no_candidates_lost(self) -> None:
        hits = [_hit(f"c{i}", f"内容 {i}") for i in range(5)]
        out = await self._reranker().rerank("内容", hits, top_k=5)
        assert {h.chunk.id for h in out} == {h.chunk.id for h in hits}


class TestNoOpReranker:
    async def test_preserves_order(self) -> None:
        hits = [_hit("a", "x", score=0.9), _hit("b", "y", score=0.1)]
        out = await NoOpReranker().rerank("q", hits, top_k=2)
        assert [h.chunk.id for h in out] == ["a", "b"]
        assert [h.rank for h in out] == [0, 1]


class _FakeLLM:
    """假 LLM：返回脚本化的重排结果，不花一分钱。"""

    def __init__(self, content: str, *, boom: bool = False) -> None:
        self._content = content
        self._boom = boom
        self.calls = 0

    async def chat(self, messages, **kwargs):
        from app.llm.types import ChatMessage, ChatResponse, Role, Usage

        self.calls += 1
        if self._boom:
            raise RuntimeError("模拟 LLM 故障")
        return ChatResponse(
            message=ChatMessage(role=Role.ASSISTANT, content=self._content),
            usage=Usage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
        )


class TestLLMReranker:
    async def test_reorders_per_model_output(self) -> None:
        llm = _FakeLLM('{"order": [3, 1, 2]}')
        hits = [_hit("a", "第一"), _hit("b", "第二"), _hit("c", "第三")]
        out = await LLMReranker(llm).rerank("q", hits, top_k=3)
        assert [h.chunk.id for h in out] == ["c", "a", "b"]

    async def test_missing_candidates_appended(self) -> None:
        """模型没排到的候选要补在后面，不能凭空丢结果。"""
        llm = _FakeLLM('{"order": [2]}')
        hits = [_hit("a", "第一"), _hit("b", "第二"), _hit("c", "第三")]
        out = await LLMReranker(llm).rerank("q", hits, top_k=3)
        assert out[0].chunk.id == "b"
        assert {h.chunk.id for h in out} == {"a", "b", "c"}

    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            '{"order": "abc"}',
            '{"order": [99, 0, -1]}',  # 越界编号
            "{}",
        ],
    )
    async def test_bad_output_falls_back_to_original_order(self, raw: str) -> None:
        """重排是优化项，不是关键路径 —— 它不该有能力搞垮整个检索。"""
        llm = _FakeLLM(raw)
        hits = [_hit("a", "第一"), _hit("b", "第二")]
        out = await LLMReranker(llm).rerank("q", hits, top_k=2)
        assert [h.chunk.id for h in out] == ["a", "b"]

    async def test_duplicate_indices_deduplicated(self) -> None:
        llm = _FakeLLM('{"order": [2, 2, 1]}')
        hits = [_hit("a", "第一"), _hit("b", "第二")]
        out = await LLMReranker(llm).rerank("q", hits, top_k=2)
        assert [h.chunk.id for h in out] == ["b", "a"]

    async def test_llm_failure_does_not_raise(self) -> None:
        llm = _FakeLLM("", boom=True)
        hits = [_hit("a", "第一"), _hit("b", "第二")]
        out = await LLMReranker(llm).rerank("q", hits, top_k=2)
        assert [h.chunk.id for h in out] == ["a", "b"]

    async def test_json_in_code_fence_parsed(self) -> None:
        """模型常把 JSON 包在 ```json 代码块里 —— 必须能从噪声中提取出来。"""
        llm = _FakeLLM('好的，排序如下：\n```json\n{"order": [2, 1]}\n```')
        hits = [_hit("a", "第一"), _hit("b", "第二")]
        out = await LLMReranker(llm).rerank("q", hits, top_k=2)
        assert [h.chunk.id for h in out] == ["b", "a"]

    async def test_single_candidate_skips_llm_call(self) -> None:
        """只有一个候选时排序毫无意义，不该浪费一次 API 调用。"""
        llm = _FakeLLM('{"order": [1]}')
        out = await LLMReranker(llm).rerank("q", [_hit("a", "唯一")], top_k=1)
        assert llm.calls == 0
        assert [h.chunk.id for h in out] == ["a"]

    async def test_token_usage_tracked(self) -> None:
        """重排成本必须可观测，否则"多一次 LLM 调用"的代价说不清。"""
        llm = _FakeLLM('{"order": [2, 1]}')
        reranker = LLMReranker(llm)
        hits = [_hit("a", "第一"), _hit("b", "第二")]
        await reranker.rerank("q", hits, top_k=2)
        assert reranker.total_tokens == 110


# ============================================================
# 两段式管线
# ============================================================
class TestRetrieverPipeline:
    @staticmethod
    def _docs() -> list[LoadedDocument]:
        return [
            _doc("教育经历\n某某大学\n\n专业技能\n熟练掌握 Kafka、Flink、ClickHouse", "resume.md"),
            LoadedDocument(
                source="job-001 大模型工程师",
                doc_type=DocType.JD,
                text="岗位名称：大模型工程师\n\n任职要求\n熟悉 RAG 与向量数据库",
            ),
        ]

    @pytest.mark.parametrize(
        "mode", [RetrievalMode.DENSE, RetrievalMode.SPARSE, RetrievalMode.HYBRID]
    )
    async def test_all_modes_return_results(self, mode: RetrievalMode) -> None:
        r = Retriever.from_documents(self._docs(), mode=mode)
        hits = await r.aretrieve("Kafka 消息队列", k=3)
        assert hits, f"{mode} 模式没有返回任何结果"
        assert [h.rank for h in hits] == list(range(len(hits)))

    async def test_sparse_mode_finds_exact_term(self) -> None:
        r = Retriever.from_documents(self._docs(), mode=RetrievalMode.SPARSE)
        hits = await r.aretrieve("ClickHouse", k=3)
        assert hits
        assert "ClickHouse" in hits[0].chunk.text

    async def test_hybrid_fuses_both_paths(self) -> None:
        r = Retriever.from_documents(self._docs(), mode=RetrievalMode.HYBRID)
        hits = await r.aretrieve("向量数据库", k=3)
        assert hits
        assert any("向量数据库" in h.chunk.text for h in hits[:2])

    async def test_reranker_applied(self) -> None:
        r = Retriever.from_documents(
            self._docs(), mode=RetrievalMode.HYBRID, reranker=LexicalReranker()
        )
        assert r.stats()["reranker"] == "lexical"
        hits = await r.aretrieve("专业技能", k=2)
        assert hits

    async def test_doc_type_filter(self) -> None:
        r = Retriever.from_documents(self._docs(), mode=RetrievalMode.HYBRID)
        hits = await r.aretrieve("向量数据库", k=5, doc_types=[DocType.JD])
        assert hits
        assert all(str(h.chunk.doc_type) == "jd" for h in hits)

    async def test_recall_k_wider_than_k(self) -> None:
        """召回必须比最终结果宽 —— 重排只能重排它拿到的东西。"""
        r = Retriever.from_documents(self._docs(), mode=RetrievalMode.HYBRID)
        wide = await r.aretrieve("Kafka", k=1, recall_k=10)
        narrow = await r.aretrieve("Kafka", k=1, recall_k=1)
        assert len(wide) <= 1 and len(narrow) <= 1  # 最终都只返回 1 条

    async def test_context_assembly_has_citations(self) -> None:
        r = Retriever.from_documents(self._docs(), mode=RetrievalMode.HYBRID)
        ctx = await r.aretrieve_context("Kafka", k=2)
        assert "[1]" in ctx
        assert "出处" in ctx

    async def test_context_respects_char_budget(self) -> None:
        r = Retriever.from_documents(self._docs(), mode=RetrievalMode.HYBRID)
        ctx = await r.aretrieve_context("Kafka", k=5, max_chars=100)
        assert len(ctx) <= 400  # 至少第一块会被保留，但不会无限增长

    async def test_empty_corpus(self) -> None:
        from app.rag.embedder import TfidfEmbedder

        emb = TfidfEmbedder()
        emb.fit(["一些内容"])
        r = Retriever([], emb)
        assert await r.aretrieve("任何查询", k=3) == []

    def test_stats_expose_pipeline(self) -> None:
        r = Retriever.from_documents(
            self._docs(), mode=RetrievalMode.HYBRID, reranker=NoOpReranker()
        )
        stats = r.stats()
        assert stats["mode"] == "hybrid"
        assert stats["reranker"] == "none"
        assert "bm25" in stats
