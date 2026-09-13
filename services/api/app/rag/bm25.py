"""Okapi BM25：稀疏检索的经典评分函数。

【它解决了 TF-IDF 的什么问题】
朴素 TF-IDF 有两个毛病，在真实语料上都很致命：

1. **词频线性增长**
   一个词出现 10 次的文档，得分就是出现 1 次的 10 倍。但语义上，
   "提到 Kafka 十遍"并不比"提到一遍"更相关 —— 第 10 次出现几乎没有新信息。
   BM25 用 `tf / (tf + k1·...)` 让词频**饱和**：增长曲线前陡后平。

2. **没有文档长度归一化**
   长文档天然含更多词，也就更容易命中查询词，于是无脑占优。
   BM25 用 `|D| / avgdl` 做归一化，把"长"这个无关因素扣掉。

【公式】

    score(D, Q) = Σ_{q∈Q} IDF(q) · [ f(q,D)·(k1+1) ] / [ f(q,D) + k1·(1 - b + b·|D|/avgdl) ]

    IDF(q) = ln( (N - n(q) + 0.5) / (n(q) + 0.5) + 1 )

【两个参数怎么调】
- `k1 ∈ [1.2, 2.0]`：控制 tf 饱和速度。越大越"不饱和"（更看重重复出现）。
  默认 1.5 是绝大多数语料上的安全值。
- `b ∈ [0, 1]`：控制长度归一化强度。`b=0` 完全归一化，`b=1` 完全不归一化。
  默认 0.75，是 TREC 系列评测里长期验证的经验值。

**这两个参数必须靠评测集调，不能凭感觉。** 本项目的评测集正好干这个用。

【IDF 里的那个 +1 不是笔误】
标准 IDF 在词出现在超过半数文档时会变成负数，导致"包含这个词反而扣分"。
BM25+ 的写法在最后加 1，保证 IDF 恒为正。语料很小时（本项目只有十几块）
这个修正尤其重要 —— 常用词可能出现在多数块里。
"""

from __future__ import annotations

import logging
import math
from collections import Counter

import numpy as np

from app.rag.tokenizer import tokenize, tokenize_query_filtered

logger = logging.getLogger(__name__)


class BM25:
    """纯 Python + numpy 实现的 Okapi BM25。

    语料是十几到几万个块时，内存里的倒排表足够快；百万级才需要
    Lucene/Elasticsearch 那样的磁盘索引结构。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b

        self._doc_tokens: list[Counter[str]] = []
        self._doc_lengths: np.ndarray = np.zeros(0, dtype=np.float32)
        self._doc_freq: Counter[str] = Counter()
        self._avgdl: float = 0.0
        self._n_docs: int = 0

    # ---------- 建索引 ----------

    def fit(self, corpus: list[str]) -> None:
        self._doc_tokens = [Counter(tokenize(text)) for text in corpus]
        self._doc_lengths = np.array([sum(c.values()) for c in self._doc_tokens], dtype=np.float32)
        self._n_docs = len(corpus)
        self._avgdl = float(self._doc_lengths.mean()) if self._n_docs else 0.0

        self._doc_freq = Counter()
        for counter in self._doc_tokens:
            self._doc_freq.update(counter.keys())

        logger.info(
            "BM25 索引完成：%d 个文档，平均长度 %.1f 词元，词表 %d",
            self._n_docs,
            self._avgdl,
            len(self._doc_freq),
        )

    # ---------- 打分 ----------

    def _idf(self, term: str) -> float:
        """IDF，含 +1 修正保证恒为正（见模块文档）。"""
        n_q = self._doc_freq.get(term, 0)
        if n_q == 0:
            return 0.0  # 词表里没有的词直接跳过，返回 0 比返回负值安全
        return math.log((self._n_docs - n_q + 0.5) / (n_q + 0.5) + 1.0)

    def scores(self, query: str) -> np.ndarray:
        """返回查询对**全部**文档的 BM25 得分向量。

        一次性算全部而不是逐个文档算，是因为 numpy 向量化后
        "词频数组逐项运算"比 Python 循环快一到两个数量级。

        注意查询侧用 `tokenize_query_filtered`：疑问词（"什么""哪些"）
        在语料里几乎不出现，却会稀释真正有用的词，对 BM25 是纯噪声。
        """
        if self._n_docs == 0:
            return np.zeros(0, dtype=np.float32)

        query_terms = tokenize_query_filtered(query)
        if not query_terms:
            return np.zeros(self._n_docs, dtype=np.float32)

        scores = np.zeros(self._n_docs, dtype=np.float32)

        # 归一化因子：分母中与词频无关的那部分，可以预先整体算好
        length_norm = 1.0 - self.b + self.b * (self._doc_lengths / (self._avgdl or 1.0))

        for term in set(query_terms):
            idf = self._idf(term)
            if idf == 0.0:
                continue

            tf = np.array([counter.get(term, 0) for counter in self._doc_tokens], dtype=np.float32)
            numerator = tf * (self.k1 + 1.0)
            denominator = tf + self.k1 * length_norm
            # tf=0 时 numerator=0、denominator=k1*length_norm>0，结果为 0，无需额外屏蔽
            scores += idf * (numerator / denominator)

        return scores

    @property
    def vocab_size(self) -> int:
        return len(self._doc_freq)
