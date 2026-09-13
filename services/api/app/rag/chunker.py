"""文档切分：把长文本切成适合检索的语义块。

【为什么切分是 RAG 里最被低估的环节】
embedding 模型有输入长度上限，而且**向量是整段文本的语义压缩**：
一段话里塞的主题越多，向量就越"平均"，检索时对任何一个主题的区分度都越差。

切分的目标是一个平衡：
  - 块太小  → 语义不完整，召回了也回答不了问题（"他说要熟练掌握什么？"——块里只有"熟练掌握"）
  - 块太大  → 向量语义被稀释，且浪费上下文预算

【本模块的三种策略】
1. `section`  —— 按章节切（简历、JD 这类结构化文档的默认选择）
   优点：块边界与人的阅读单位一致，语义最完整
   缺点：章节长度不可控，可能远超 embedding 上限

2. `recursive` —— 递归按分隔符切（最通用）
   顺序：段落 \\n\\n → 换行 \\n → 句子 。！？ → 硬切
   好处是**优先在语义边界断开**，而不是机械按字数

3. `fixed` —— 固定长度 + 重叠
   只用作兜底与对照基线。它的价值在于"没有语义假设"，
   可以量化"用语义切分到底带来了多少提升"

【重叠（overlap）的作用】
让相邻块共享一部分文本，避免答案正好被切在边界上而两边都召不全。
经验值：overlap ≈ size 的 10%~20%，再大只是在浪费存储和检索时间。
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from itertools import pairwise

from pydantic import BaseModel, Field

from app.rag.loaders import DocType, LoadedDocument


class ChunkStrategy(StrEnum):
    SECTION = "section"
    RECURSIVE = "recursive"
    FIXED = "fixed"


class Chunk(BaseModel):
    """一个检索单元。

    刻意带上丰富的元数据：检索到之后，回答时要能生成引用（"在简历的
    『专业技能』章节"），元数据还可以用于过滤（只看某个岗位、只检索简历）。
    """

    id: str
    doc_id: str
    doc_type: DocType
    text: str
    index: int
    section: str = ""
    char_start: int = 0
    char_end: int = 0
    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def citation(self) -> str:
        """人类可读的引用标识，用于最终回答里的出处标注。

        【为什么需要紧凑化】
        合并小块之后，一个块可能横跨多个章节，`section` 会变成
        「项目经历 / 专业技能 / 教育经历 / 竞赛荣誉」这样的复合串。
        它是**准确**的（这些内容确实都在这块里），但直接拼进出处标注有两个问题：
          1. 这段文字会进入提示词，冗长的出处是在浪费上下文预算
          2. 界面上过长的出处会挤掉真正有用的信息

        所以超过两节时压缩成「第一节 等 N 节」：
        用户知道**主要来自哪里**，也知道这块还包含别的内容。
        准确与可读之间的取舍点就在这里 —— 不是丢掉信息，而是标注出信息的密度。
        """
        sections = [s.strip() for s in self.section.split(" / ") if s.strip()]
        if not sections:
            return f"{self.doc_id} · 第 {self.index + 1} 块"
        if len(sections) <= 2:
            return f"{self.doc_id} · {' / '.join(sections)}"
        return f"{self.doc_id} · {sections[0]} 等 {len(sections)} 节"


def _make_id(doc_id: str, index: int, text: str) -> str:
    """确定性 ID：同样的输入永远得到同样的 ID，便于评测结果可复现。"""
    digest = hashlib.sha1(f"{doc_id}:{index}:{text[:64]}".encode()).hexdigest()[:10]
    return f"{doc_id}::{index}::{digest}"


# ============================================================
# 章节识别
# ============================================================
# 简历/JD 的章节标题特征：短、独立成行、且多为名词短语
_HEADING_PATTERNS = re.compile(
    r"^(?:[#]{1,4}\s*)?"  # 可选 markdown 井号
    r"(教育经历|教育背景|工作经历|实习经历|工作经验|项目经历|项目经验|个人项目|"
    r"开源项目|实践经历|专业技能|技能清单|专业技能与荣誉|技能特长|"
    r"竞赛荣誉|获奖经历|荣誉奖项|证书|校园经历|个人简介|自我介绍|自我评价|"
    r"求职意向|联系方式|基本信息|"
    r"岗位职责|任职要求|技能要求|岗位要求|加分项|我们希望你|你将获得)"
    r"\s*[:：]?\s*$"
)

# 判定为标题的"弱信号"上限：超过这个长度就不像标题了
_MAX_HEURISTIC_HEADING_LEN = 12


def _is_heading(line: str) -> bool:
    """判断一行是否是章节标题。

    【为什么最终只保留两个判据 —— 一段真实的踩坑史】
    初版是"短行 + 无句末标点 = 标题"，对 Markdown 还行，对 **PDF 提取的文本
    是灾难**：PDF 里每个视觉行都独立成段，于是「软件工程」「年龄：21 岁」
    甚至院校名全被判成标题，文档被切得粉碎。

    第二版加了"只有该行前后有空行时才采纳短行信号"。仍然误判 ——
    因为简历里「某某大学」这类内容行**本来就独立成段**，弱信号救不了。

    最终结论：**短行本身不是标题的充分证据，只有"出现在标题词表里"或
    "显式的 Markdown 井号"才是。**

    这个取舍的方向很重要：漏判标题只是章节粒度变粗（内容仍由递归切分兜底，
    不会丢），而误判标题会**凭空制造边界、切碎语义**。所以宁可漏，不可错。
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 30:
        return False

    if stripped.startswith("#"):
        return True
    return bool(_HEADING_PATTERNS.match(stripped))


def split_by_sections(text: str) -> list[tuple[str, str]]:
    """按章节切分，返回 [(章节名, 正文), ...]。

    首个章节之前的引言部分归入 "(开头)" 章节 —— 简历的姓名、电话、
    邮箱通常在这里，检索"联系方式""姓名"时需要能命中。

    【关键：绝不丢弃任何一行】
    初版实现只在"当前缓冲区非空"时才把章节入列。于是**连续两个标题行**
    会导致前一个标题所在的章节正文为空、整节被丢掉 ——
    简历里「院校名」那一行后面紧跟「专业名」，两者都被判成标题，
    结果院校名凭空消失，学历信息永远检索不到。

    内容在切分阶段被静默丢弃，是 RAG 里最危险的 bug：不报错、不崩溃，
    只是"有些信息永远检索不到"，而且极难定位。

    修正：当某章节正文为空时，把它的标题本身作为内容保留下来。
    """
    lines = text.split("\n")
    sections: list[tuple[str, list[str]]] = []
    current_title = "(开头)"
    current_lines: list[str] = []

    def flush() -> None:
        """结算当前章节。空正文时退化为"标题即内容"，保证不丢信息。"""
        nonlocal current_title, current_lines
        if any(ln.strip() for ln in current_lines):
            sections.append((current_title, current_lines))
        elif current_title != "(开头)":
            sections.append((current_title, [current_title]))
        current_lines = []

    for line in lines:
        if _is_heading(line):
            flush()
            current_title = line.strip().lstrip("#").strip()
        else:
            current_lines.append(line)

    flush()

    return [(title, "\n".join(ls).strip()) for title, ls in sections if any(ls)]


# ============================================================
# 递归切分
# ============================================================
_SEPARATORS = ["\n\n", "\n", "。", "；", "！", "？", ". ", "; ", " "]


def split_recursive(text: str, size: int, overlap: int = 0) -> list[str]:
    """递归按语义分隔符切分。

    策略：先尝试用最"强"的分隔符（段落）切；如果切出来的一段仍然超长，
    就用更弱的分隔符（换行 → 句号 → 空格）继续切。这样能保证：
    **只要有可能，切点就落在语义边界上，而不是句子中间。**
    """
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []

    for sep in _SEPARATORS:
        if sep not in text:
            continue
        parts = text.split(sep)
        # 分隔符要拼回片段尾部，否则会丢掉句号等内容
        rejoined = [p + sep for p in parts[:-1]] + [parts[-1]]

        chunks: list[str] = []
        buffer = ""
        for piece in rejoined:
            if len(piece) > size:
                # 这一段自己就超长 → 用更弱的分隔符继续切
                if buffer:
                    chunks.append(buffer)
                    buffer = ""
                chunks.extend(split_recursive(piece, size, overlap))
                continue
            if len(buffer) + len(piece) <= size:
                buffer += piece
            else:
                if buffer:
                    chunks.append(buffer)
                buffer = piece
        if buffer:
            chunks.append(buffer)

        if chunks:
            return _apply_overlap([c.strip() for c in chunks if c.strip()], overlap)

    # 所有分隔符都切不动（如无空格的长串）→ 硬切兜底
    return _apply_overlap([text[i : i + size] for i in range(0, len(text), size)], overlap)


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    """让相邻块共享 overlap 个字符。

    实现方式是把上一块的尾部**前置**到当前块。这不是为了内容重复，
    而是防止答案恰好跨越切点时两边都召不全。
    """
    if overlap <= 0 or len(chunks) <= 1:
        return chunks

    result = [chunks[0]]
    for prev, cur in pairwise(chunks):
        tail = prev[-overlap:] if len(prev) > overlap else prev
        result.append((tail + cur).strip())
    return result


# ============================================================
# 对外入口
# ============================================================
def _is_informative(text: str) -> bool:
    """判断一块内容是否值得入索引。

    【为什么需要这个过滤器】
    Markdown 里的 `---` 分隔线、单独的 `###`、只有符号的片段，切分后会变成
    "内容极短但存在"的块。它们的危害不是占空间，而是**参与相似度计算**：
    TF-IDF 在 L2 归一化下，一个只有 3 个字符的块一旦命中就拿到极高分数，
    把真正有用的内容挤下去 —— 与"岗位模板头部"是同一类问题。

    【判据必须精确，否则会误伤】
    第一版用"剥掉标点后剩余字符数 >= 5"作为判据，结果**把 4 个字的短标题
    「项目经历」也删了**，导致章节元数据丢失、按章节检索失效。

    更精确的规则是：只要含**至少一个实义字符**（中日韩文字、字母或数字）就保留。
    这样 `---`、`###`、`|`、`**` 会被丢掉，而任何真实的短标题都会留下。
    """
    return any(ch.isalnum() for ch in text)


def _merge_small_chunks(chunks: list[tuple[str, str]], min_size: int) -> list[tuple[str, str]]:
    """把过小的块合并进相邻块。

    【为什么需要这一步 —— 一个真实测出来的问题】
    评测中发现"我在哪家公司实习过"这类**关于简历的查询，却把岗位块排在最前**。
    根因是岗位块的开头是一个模板化头部：

        岗位名称：XXX  公司：XXX  城市：XXX  薪资：XXX

    它只有 170 字左右，却塞满了「公司」「岗位」这类高频泛化词。TF-IDF 是
    按向量长度归一化的，**短且命中的块得分天然偏高**，于是它成了所有提到
    "公司/岗位/职位"的查询的"高频吸引子"，把真正的答案挤到后面。

    合并小块能同时缓解两个问题：
      1. 消除碎片化 —— 7 字的「年龄：21 岁」单独成块毫无意义
      2. 稀释模板化头部 —— 合并后泛化词的相对权重被正文冲淡

    【章节名必须一起合并 —— 一个真实踩过的坑】
    初版合并时只保留**第一个**块的 section，后续块的章节名直接丢弃。
    后果不只是评测标注匹配不上，更严重的是**用户可见的引用标注会出错**：

        chunk.citation → f"{doc_id} · {section}"
        用户在答案里看到"出处：resume.md · 张三"，
        而那段内容其实来自「竞赛荣誉」和「教育经历」。

    引用错了比没有引用更糟 —— 它给了用户一个可核对却核对不上来源，
    会直接摧毁对整个系统的信任。所以合并时用 `_join_sections()` 把所有
    章节名拼起来，引用标注因此变成「张三 / 教育经历 / 竞赛荣誉」这样的
    复合来源，虽然长一点但准确。

    代价：章节名变长会让引用标注不够简洁。所以 min_size 不宜过大
    （经验值 100~200 字），合并层数越少，来源标注越精确。
    """
    if min_size <= 0:
        return chunks

    merged: list[tuple[str, str]] = []
    buf_sections: list[str] = []
    buf_text = ""

    for section, text in chunks:
        if not buf_text:
            buf_sections, buf_text = [section], text
            continue

        if len(buf_text) < min_size:
            # 当前缓冲还不够大 → 把这一块并进来（章节名也要并）
            buf_sections.append(section)
            buf_text = f"{buf_text}\n{text}"
        else:
            merged.append((_join_sections(buf_sections), buf_text))
            buf_sections, buf_text = [section], text

    if buf_text:
        # 最后一个小尾巴并回上一块，避免产生一个孤立的短块
        if merged and len(buf_text) < min_size:
            prev_section, prev_text = merged[-1]
            merged[-1] = (
                _join_sections([*prev_section.split(" / "), *buf_sections]),
                f"{prev_text}\n{buf_text}",
            )
        else:
            merged.append((_join_sections(buf_sections), buf_text))

    return merged


def _join_sections(sections: list[str]) -> str:
    """把多个章节名合成一个来源标注。

    去重且保持出现顺序：`["教育经历", "教育经历", "竞赛荣誉"]` →
    `"教育经历 / 竞赛荣誉"`。重复的章节名（同一章节被切成多块后又合并回来）
    会让标注冗长而无信息量。
    """
    seen: list[str] = []
    for section in sections:
        name = section.strip()
        if name and name not in seen:
            seen.append(name)
    return " / ".join(seen)


def chunk_document(
    doc: LoadedDocument,
    *,
    strategy: ChunkStrategy = ChunkStrategy.SECTION,
    size: int = 500,
    overlap: int = 80,
    min_size: int = 0,
) -> list[Chunk]:
    """把一个文档切成块。

    默认用 section 策略：先按章节切，章节内若超长再用递归切。
    这是简历/JD 的最佳组合——既保住章节语义完整性，又不让单块超限。
    """
    doc_id = doc.source
    raw_chunks: list[tuple[str, str]] = []  # (section, text)

    if strategy is ChunkStrategy.FIXED:
        raw_chunks = [
            ("", c)
            for c in _apply_overlap(
                [doc.text[i : i + size] for i in range(0, len(doc.text), size)], overlap
            )
        ]

    elif strategy is ChunkStrategy.RECURSIVE:
        raw_chunks = [("", c) for c in split_recursive(doc.text, size, overlap)]

    else:  # SECTION
        for section, body in split_by_sections(doc.text):
            if not body:
                continue
            if len(body) <= size:
                raw_chunks.append((section, body))
            else:
                # 章节过长：章节名保留在每个子块的元数据里，便于引用
                for piece in split_recursive(body, size, overlap):
                    raw_chunks.append((section, piece))

    # 噪声过滤：丢掉纯符号/标记碎片（如 `---`、单独的 `###`）。
    # 必须排在碎片合并之前 —— 否则一个 3 字符的分隔线会被当成"小碎片"
    # 而把它的前后两段本不相干的内容粘在一起。
    raw_chunks = [(s, t) for s, t in raw_chunks if _is_informative(t)]

    # 碎片合并（在分配 id 之前做，保证 id 与最终文本一致）
    raw_chunks = _merge_small_chunks(raw_chunks, min_size)

    chunks: list[Chunk] = []
    cursor = 0
    for i, (section, text) in enumerate(raw_chunks):
        text = text.strip()
        if not text:
            continue
        start = doc.text.find(text[:40], cursor) if len(text) >= 40 else -1
        if start >= 0:
            cursor = start
        chunks.append(
            Chunk(
                id=_make_id(doc_id, i, text),
                doc_id=doc_id,
                doc_type=doc.doc_type,
                text=text,
                index=i,
                section=section,
                char_start=start if start >= 0 else 0,
                char_end=(start + len(text)) if start >= 0 else len(text),
                metadata=dict(doc.metadata_hint),
            )
        )

    return chunks


def chunk_documents(
    docs: list[LoadedDocument],
    *,
    strategy: ChunkStrategy = ChunkStrategy.SECTION,
    size: int = 500,
    overlap: int = 80,
    min_size: int = 0,
) -> list[Chunk]:
    out: list[Chunk] = []
    for doc in docs:
        out.extend(
            chunk_document(doc, strategy=strategy, size=size, overlap=overlap, min_size=min_size)
        )
    return out
