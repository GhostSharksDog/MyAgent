"""RAG 层测试。

覆盖重点是**本次迭代修掉的真实缺陷**，而不是重复验证顺利路径：

  1. 章节标题无正文时整节被丢弃 → 内容凭空消失（最危险：不报错、只是查不到）
  2. HTML 注释进了语料 → 元数据变成可检索噪声
  3. 标题启发式过于激进 → 「软件工程」「年龄：21岁」被误判为标题
  4. 纯标记碎片（`---`）参与相似度计算 → 3 字符的块拿到高分挤出真内容
  5. 短标题被"信息量过滤"误删 → 章节元数据丢失
  6. 中文用 sklearn 默认分词 → 整句被当成一个词，检索彻底失效

外加指标计算本身的正确性（Recall / MRR / NDCG 的边界情况）——
**评测工具算错了，比被测系统错了更可怕**，因为它会给你虚假的信心。
"""

from __future__ import annotations

import pytest
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
from app.rag.loaders import DocType, LoadedDocument, normalize_text
from app.rag.store import VectorStore


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
        assert ndcg_at_k(ranked, gold, 2) == pytest.approx(1.0)

    def test_ndcg_penalizes_lower_rank(self) -> None:
        gold = [GoldCondition(text_contains="t")]
        high = ndcg_at_k([_chunk("a", "t"), _chunk("b", "x")], gold, 2)
        low = ndcg_at_k([_chunk("b", "x"), _chunk("a", "t")], gold, 2)
        assert high > low

    def test_ndcg_miss_is_zero(self) -> None:
        assert ndcg_at_k([_chunk("a", "x")], [GoldCondition(text_contains="t")], 1) == 0.0

    def test_gold_conditions_are_and(self) -> None:
        """同一条件内的多个字段是 AND 语义。"""
        cond = GoldCondition(doc_id_contains="resume", text_contains="Kafka")
        assert cond.matches(_chunk("a", "Kafka 经验", doc="resume.md"))
        assert not cond.matches(_chunk("b", "Kafka 经验", doc="job-001"))
        assert not cond.matches(_chunk("c", "Python", doc="resume.md"))


class TestEvaluateHarness:
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
        report = evaluate(r, eval_set, k=3)
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
        report = evaluate(r, eval_set, k=3)
        assert len(report.failures) == 1
        f = report.failures[0]
        assert "gold_conditions" in f
        assert "actually_retrieved" in f
        assert report.metrics["hit_rate"] == 0.0
