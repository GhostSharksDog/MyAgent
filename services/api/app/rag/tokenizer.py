"""中英混合分词器。

【为什么单独抽出来】
TF-IDF 基线用的是字符 n-gram（不需要分词），但 BM25 需要**词元**：
它的 tf 饱和与文档长度归一化都建立在"词在文档中出现几次"之上。
两者共用同一套 token 定义，才能让混合检索的融合结果可信 ——
如果两路检索建立在不同的切词口径上，融合出的排名就没法解释。

【中文分词的三条路线】
1. **词典分词**（jieba 等）：效果最好，但要引入依赖与词典，且对
   技术术语（`Flink CDC`、`Redisson`、`ClickHouse`）经常切错，需要维护自定义词典。
2. **字符 n-gram**（本项目 TF-IDF 基线用的）：零依赖，召回稳，但丢掉词边界，
   无法体现"某个词出现了 3 次"这种信息。
3. **CJK 单字 + 双字 + 拉丁整词**（本模块）：兼顾两者 ——
   单字保召回，双字保精度，拉丁词与技术符号整体保留。

选 3 的理由：这是一个**零依赖且在技术文本上表现稳定**的方案。
技术简历里大量出现 `Kafka`、`ClickHouse`、`TCP/IP`、`30-60K` 这类
词典分词容易切错的串，按"拉丁连续段整体保留"处理反而更准。

【一个必须处理的细节】
`30-60K`、`bge-small-zh-v1.5`、`TCP/IP` 这类含连字符/斜杠/点的技术串
必须整体保留。若按标点切开，`30-60K` 会变成 `30` 和 `60K`，
检索"薪资 35-70K"时就会退化成对数字的匹配，语义全丢。
"""

from __future__ import annotations

import re

# 中日韩统一表意文字（含扩展 A 区常用部分）
_CJK = r"\u4e00-\u9fff\u3400-\u4dbf"

# 拉丁/数字技术串：首字符必须是字母或数字，后续允许 _ + # . / - ，
# 这样 `TCP/IP`、`C++`、`bge-small-zh-v1.5`、`30-60K` 都能整体保留
_LATIN_TOKEN = r"[A-Za-z0-9][A-Za-z0-9_+#./\-]*"

_TOKEN_PATTERN = re.compile(rf"{_LATIN_TOKEN}|[{_CJK}]")


def tokenize(text: str) -> list[str]:
    """把文本切成词元列表。

    - 拉丁/数字串 → 小写整体保留（`Kafka` → `kafka`）
    - CJK 连续段   → 单字 + 相邻双字（`消息队列` → 消 息 队 列 消息 息队 队列）

    单字与双字同时保留是刻意的：单字提高召回（查到"队"也能命中），
    双字提高精度（"队列"比"队"更有区分度）。BM25 的 IDF 会自动
    把高频单字（"的""了"）的权重压下去，不需要额外的停用词表。
    """
    tokens: list[str] = []
    cjk_run: list[str] = []

    def flush_cjk() -> None:
        if not cjk_run:
            return
        tokens.extend(cjk_run)  # 单字
        tokens.extend("".join(cjk_run[i : i + 2]) for i in range(len(cjk_run) - 1))  # 双字
        cjk_run.clear()

    for match in _TOKEN_PATTERN.finditer(text):
        piece = match.group()
        if len(piece) == 1 and ("\u4e00" <= piece <= "\u9fff" or "\u3400" <= piece <= "\u4dbf"):
            cjk_run.append(piece)
        else:
            flush_cjk()
            tokens.append(piece.lower())

    flush_cjk()
    return tokens


def tokenize_query(text: str) -> list[str]:
    """查询分词。

    当前与 `tokenize` 一致，单独留一个入口是为了将来能对查询做特殊处理
    （如去掉疑问词"什么""哪些"）。**疑问词对 BM25 是纯噪声**：
    它们在几乎所有文档里都不出现，却会抬高查询长度、稀释真正有用的词。
    """
    return tokenize(text)


# 查询侧的疑问词/停用词。它们几乎不携带检索信息，却会干扰 BM25 的评分。
_QUERY_STOPWORDS = frozenset(
    {
        "我",
        "的",
        "是",
        "有",
        "在",
        "和",
        "与",
        "或",
        "了",
        "吗",
        "呢",
        "什么",
        "哪些",
        "哪个",
        "怎么",
        "如何",
        "多少",
        "几年",
        "介绍",
        "一下",
        "请问",
        "帮我",
        "看看",
    }
)


def tokenize_query_filtered(text: str) -> list[str]:
    """去掉疑问词与第一人称后的查询词元。

    用于 BM25 这类对词频敏感的评分器。注意**不能用于向量检索** ——
    向量模型理解完整语义，砍掉词反而会破坏语义。
    这也说明了两路检索本就该有不同的预处理，融合的是排名而不是中间结果。
    """
    return [t for t in tokenize(text) if t not in _QUERY_STOPWORDS]
