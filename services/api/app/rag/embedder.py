"""向量化：把文本块变成可计算相似度的向量。

【本模块的核心设计：先建基线，再谈提升】
当前实现是 **TF-IDF 基线**，不是真正的语义 embedding。这不是偷懒，是方法论：

  如果你一上来就用 embedding，效果不好时你无法回答"是 embedding 不行，
  还是切分不行，还是查询改写不行"。先有一个零依赖、确定性强、成本为零的
  基线，之后每一步优化才能量化出**它到底贡献了多少**。
  这就是消融实验（ablation）的基础，也是 P4 评测体系的前置条件。

【中文检索的坑：不要用 sklearn 的默认分词】
sklearn 的 TfidfVectorizer 默认按空格分词，而中文句子没有空格——
整句话会被当成一个"词"，导致所有文本块的向量几乎完全一样，检索彻底失效。

三种解法：
  1. **字符 n-gram**（本模块采用）：把"软件工程"拆成"软件""件工""工程"等
     2-gram。无需分词器、零依赖，对中文效果出乎意料地好，是标准的基线做法。
  2. jieba 等分词器 + 词级 TF-IDF：效果略好，但引入依赖与词典问题。
  3. 真正的语义 embedding：能处理"同义不同词"（"并发" ≈ "多线程"），
     这是 n-gram 做不到的——也正是 P2 下一步要验证的提升点。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import numpy as np

logger = logging.getLogger(__name__)


class EmbedderError(RuntimeError):
    """向量化失败。"""


class Embedder(ABC):
    """向量化器接口。

    约定：**输出的向量必须已做 L2 归一化**。
    这样余弦相似度就退化为点积，可以用矩阵乘法一次算出所有相似度，
    既快又省去反复算模长的开销。
    """

    name: str = "base"
    dim: int = 0

    @abstractmethod
    def fit(self, corpus: list[str]) -> None:
        """在语料上拟合（学习词表/IDF 权重）。语义 embedding 模型不需要拟合，实现为空即可。"""

    @abstractmethod
    def encode(self, texts: list[str]) -> np.ndarray:
        """把一批文本编码成 (n, dim) 的 L2 归一化矩阵。"""

    def encode_query(self, text: str) -> np.ndarray:
        """编码单条查询，返回 (dim,) 向量。

        单独留一个方法是因为检索时的查询处理和建库时的文档处理
        可能不同（例如 bge 模型要求查询加 "为这个句子生成表示：" 前缀，
        而文档不加）。接口上预留这个区别，避免以后改架构。
        """
        return self.encode([text])[0]

    @staticmethod
    def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        # 零向量（如全是停用词）会导致除零，用 1 兜底，保持该行为零向量
        return matrix / np.maximum(norms, 1e-12)


class TfidfEmbedder(Embedder):
    """TF-IDF 基线向量化器（字符 n-gram）。

    参数选择说明：
      - `analyzer="char"`：按字符切，而不是按空格。中文必须这样。
      - `ngram_range=(2, 3)`：同时用 2-gram 和 3-gram。
        纯 1-gram 会把"的""了"这类高频字也当特征，噪声大；
        只用 3-gram 则对短查询覆盖不足（"Kafka"只有 5 个字符，切不出几个 3-gram）。
      - `sublinear_tf=True`：用 1+log(tf) 替代原始词频，抑制长文档里
        某个词反复出现带来的权重虚高。这是 BM25 里 tf 饱和思想的简化版。
      - `min_df=1`：语料很小（几十个块），过滤低频词会把关键技能词也滤掉。
        语料上万块时才应该调大这个值。
    """

    name = "tfidf"

    def __init__(self, ngram_range: tuple[int, int] = (2, 3), min_df: int = 1) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer

        self._vectorizer = TfidfVectorizer(
            analyzer="char",
            ngram_range=ngram_range,
            min_df=min_df,
            sublinear_tf=True,
            # 字符 n-gram 下不需要去停用词（"的"参与构成 2-gram 时是有信息量的）
            lowercase=True,
        )
        self.dim = 0
        self._fitted = False

    def fit(self, corpus: list[str]) -> None:
        if not corpus:
            raise EmbedderError("语料为空，无法拟合向量化器")

        # 字符 n-gram 的边界情况：若所有文本都短于 ngram 的最小长度，
        # 切不出任何特征，sklearn 会抛一个很难懂的
        # "empty vocabulary; perhaps the documents only contain stop words"。
        # 这里提前给出可操作的提示，而不是把底层报错直接甩给使用者。
        total_chars = sum(len(t.strip()) for t in corpus)
        if total_chars < 2:
            raise EmbedderError(
                f"语料过短（总字符数 {total_chars}），无法提取字符 n-gram 特征。"
                f"请确认切分后的块不是空白或只有符号。"
            )

        self._vectorizer.fit(corpus)
        self.dim = len(self._vectorizer.get_feature_names_out())
        self._fitted = True
        logger.info("TF-IDF 拟合完成：%d 个字符 n-gram 特征", self.dim)

    def encode(self, texts: list[str]) -> np.ndarray:
        if not self._fitted:
            raise EmbedderError("请先调用 fit()")
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        from scipy.sparse import issparse

        matrix = self._vectorizer.transform(texts)
        if issparse(matrix):
            matrix = matrix.toarray()
        return self._l2_normalize(np.asarray(matrix, dtype=np.float32))

    def vocab_size(self) -> int:
        return self.dim
