"""记忆模块：短期对话记忆与长期事实记忆。

【为什么 Agent 需要记忆，以及两种记忆的分工】

短期记忆（ConversationMemory）解决的是**上下文窗口的物理限制**：

    只把 user/assistant 写入历史（P1 的做法）→ 多轮时模型记不住上一轮查过什么
    把工具消息也留下                          → token 随轮数平方级增长，很快超限

两种朴素策略都不行，本模块用**滑动窗口 + 摘要压缩**：
超出窗口的旧对话不是丢弃，而是压缩成一段累积摘要。
被挤出的信息仍以压缩形式保留，成本却可控。

长期记忆（LongTermMemory）解决的是**跨会话的知识沉淀**：

    用户说"我只考虑北京的机会" → 这条偏好应该被记住，下次不用再问
    用户说"我在准备阿里三面"   → 这是背景信息，对后续所有建议都有影响

它与 RAG 的区别是**写入方不同**：RAG 检索的是静态文档（简历、岗位库），
长期记忆检索的是 Agent 在与用户交互中**自己积累的事实**。
两者可以共用同一套向量检索基础设施，但生命周期与语义完全不同。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from pydantic import BaseModel, Field

from app.llm.types import ChatMessage

logger = logging.getLogger(__name__)


# ============================================================
# 短期记忆
# ============================================================
class Turn(BaseModel):
    """一轮完整对话。"""

    user: str
    assistant: str
    ts: float = Field(default_factory=time.time)


class ConversationMemory:
    """短期记忆：滑动窗口 + 摘要压缩。

    【三档策略的权衡】

    | 策略 | token 成本 | 失忆风险 |
    |---|---|---|
    | 全量保留 | 随轮数线性增长，必然超限 | 无 |
    | 固定窗口 | 恒定 | **高**：被挤出的信息永久丢失 |
    | 窗口 + 摘要（本实现） | 有上界 | 低：旧信息以压缩形式保留 |

    【摘要的成本与触发条件】
    摘要需要一次 LLM 调用，所以不能每轮都做 —— 那会让每轮对话的成本翻倍。
    本实现只在**超出窗口**时才压缩一次，并且压缩后旧的摘要会被合并进新摘要
    （增量摘要），而不是重新摘要全部历史。
    """

    SUMMARY_PROMPT = """\
请把以下求职咨询对话压缩成一段简明的背景摘要，供后续对话参考。

保留：
- 用户的求职目标、意向岗位、意向城市、时间安排
- 简历中的关键事实（技能、经历、学历）
- 已经给出的结论性建议（尤其是用户认可或否决过的）
- 用户明确表达的偏好与限制

丢弃：
- 寒暄、重复表述、被否决的中间方案细节
- 工具的原始返回内容（只保留结论）

要求：用第三人称陈述，不超过 300 字，只输出摘要正文，不要任何前缀。"""

    def __init__(
        self,
        *,
        llm: object | None = None,
        max_turns: int = 8,
        keep_recent: int = 6,
        max_summary_chars: int = 1200,
        enable_summary: bool = True,
    ) -> None:
        self._llm = llm
        self.max_turns = max_turns
        # 压缩时保留最近 N 轮原文 —— 摘要是有损的，最近的上下文最需要保真
        self.keep_recent = min(keep_recent, max_turns)
        self.max_summary_chars = max_summary_chars
        self.enable_summary = enable_summary and llm is not None

        self._turns: list[Turn] = []
        self._summary: str = ""

    # ---------- 写入 ----------

    def add_turn(self, user: str, assistant: str) -> None:
        self._turns.append(Turn(user=user, assistant=assistant))

    def clear(self) -> None:
        self._turns.clear()
        self._summary = ""

    # ---------- 组装上下文 ----------

    async def abuild_context(self) -> list[ChatMessage]:
        """把记忆组装成消息列表，供 Agent 拼进请求。

        这是记忆模块**唯一**对外的读取接口 —— Agent 不需要知道内部是
        窗口还是摘要，未来换成 Redis 存储或换成别的压缩策略都不影响调用方。
        """
        await self._maybe_compress()

        messages: list[ChatMessage] = []

        if self._summary:
            # 用 system 角色承载摘要：它在语义上是"背景设定"而不是某一轮对话。
            # 若用 user/assistant，模型可能把它当成一条真实的历史消息来回应。
            messages.append(
                ChatMessage.system(f"【前情摘要（较早的对话已压缩）】\n{self._summary}")
            )

        # 【注意这里是全部剩余轮次，而不是 _turns[-keep_recent:]】
        # 初版写成切片，结果是：窗口内只放 4 轮（max_turns=5、keep_recent=3）时，
        # 第 1 轮会被**静默丢弃且没有摘要补偿** —— 信息凭空消失，不报错，
        # 只是模型偶尔"不记得"。这正是记忆模块最该避免的失败模式。
        #
        # 现在的分工是清晰的：`_maybe_compress` 负责裁剪（并在裁剪时生成摘要），
        # `abuild_context` 只负责把剩下的全部渲染出来。
        for turn in self._turns:
            messages.append(ChatMessage.user(turn.user))
            messages.append(ChatMessage.assistant(turn.assistant))

        return messages

    # ---------- 压缩 ----------

    async def _maybe_compress(self) -> None:
        if len(self._turns) <= self.max_turns:
            return

        overflow = self._turns[: -self.keep_recent]
        self._turns = self._turns[-self.keep_recent :]

        if not self.enable_summary:
            # 没有 LLM 时退化为"截断"，但要留下痕迹 ——
            # 静默丢弃上下文会让模型表现异常却无从解释。
            logger.warning(
                "对话已达 %d 轮上限，且未配置摘要 LLM，最早的 %d 轮被丢弃",
                self.max_turns,
                len(overflow),
            )
            self._summary = (
                f"（更早的 {len(overflow)} 轮对话因未配置摘要模型已被丢弃，"
                f"如需完整上下文请告知用户重新提供关键信息）"
            )
            return

        rendered = "\n".join(f"用户：{t.user}\n助手：{t.assistant}" for t in overflow)
        prompt = self.SUMMARY_PROMPT
        if self._summary:
            prompt += (
                f"\n\n已有的摘要（请与新材料合并，不要遗漏其中仍然有效的信息）：\n{self._summary}"
            )

        try:
            from app.llm.types import ChatMessage as Msg

            response = await self._llm.chat(  # type: ignore[attr-defined]
                [Msg.user(f"{prompt}\n\n需要压缩的对话：\n{rendered}")],
                temperature=0.2,
            )
            new_summary = (response.message.content or "").strip()
        except Exception as exc:
            # 摘要失败绝不能中断对话：退化为只丢弃最早的若干轮
            logger.warning("对话摘要失败，退化为截断：%s", exc)
            new_summary = ""

        if new_summary:
            self._summary = new_summary[: self.max_summary_chars]
        elif not self._summary:
            self._summary = f"（{len(overflow)} 轮较早的对话未能成功摘要）"

    # ---------- 自省 ----------

    @property
    def turns(self) -> list[Turn]:
        return list(self._turns)

    @property
    def summary(self) -> str:
        return self._summary

    def stats(self) -> dict[str, object]:
        return {
            "turns": len(self._turns),
            "max_turns": self.max_turns,
            "keep_recent": self.keep_recent,
            "has_summary": bool(self._summary),
            "summary_chars": len(self._summary),
            "summary_enabled": self.enable_summary,
        }


# ============================================================
# 长期记忆
# ============================================================
class Fact(BaseModel):
    """一条长期记忆。"""

    text: str
    tags: list[str] = Field(default_factory=list)
    ts: float = Field(default_factory=time.time)


class LongTermMemory:
    """长期记忆：跨会话的事实与偏好，按语义检索召回。

    实现上复用 RAG 的向量基础设施（TF-IDF + 内存向量库），
    因此它同样支持"同义不同词"的检索（"意向城市" ≈ "想去哪"）。

    【为什么用向量检索而不是简单的全量拼接】
    长期记忆会持续增长。全量拼进上下文有两个问题：token 成本线性增长，
    且无关记忆会干扰当前话题。按相关性召回只取最相关的几条，
    成本恒定、干扰最小。

    【持久化】
    当前落地为 JSON 文件。它的不足很明确：单进程、无并发控制、
    不适合多副本部署。P3 接 Redis/SQL 时替换 `save`/`load` 即可，
    检索接口不变 —— 这也是把存储与检索分开的理由。
    """

    def __init__(self, path: Path | None = None, max_facts: int = 200) -> None:
        self.path = path
        self.max_facts = max_facts
        self._facts: list[Fact] = []
        self._store: object | None = None  # 懒构建的向量库

    # ---------- 写入 ----------

    def remember(self, text: str, tags: list[str] | None = None) -> bool:
        """记一条事实。返回是否真的新增（去重后）。

        去重是必需的：Agent 可能反复被告知同一件事（"我在北京"），
        重复存储会挤占召回名额，让真正有用的记忆排不进来。
        """
        text = text.strip()
        if not text:
            return False

        normalized = text.lower()
        for fact in self._facts:
            if fact.text.lower() == normalized:
                return False

        self._facts.append(Fact(text=text, tags=tags or []))
        self._store = None  # 失效缓存，下次检索时重建

        if len(self._facts) > self.max_facts:
            # 简单的容量控制：丢弃最早的。生产环境应改为"按最近访问时间淘汰"
            # 或"让模型判断重要性"，但前者需要记录访问、后者需要额外调用。
            dropped = len(self._facts) - self.max_facts
            self._facts = self._facts[dropped:]
            logger.info("长期记忆超出上限，淘汰最早的 %d 条", dropped)

        return True

    # ---------- 检索 ----------

    def _ensure_store(self) -> None:
        """懒构建向量索引。

        为什么懒：记忆可能很少（几条）或为空，为几条数据建索引纯属浪费。
        为什么缓存：每次 recall 都重建索引会让每轮对话多出一次全量拟合。
        """
        if self._store is not None or not self._facts:
            return

        from app.rag.chunker import Chunk
        from app.rag.embedder import TfidfEmbedder
        from app.rag.loaders import DocType
        from app.rag.store import VectorStore

        embedder = TfidfEmbedder()
        store = VectorStore(embedder)
        store.rebuild(
            [
                Chunk(
                    id=f"fact-{i}",
                    doc_id="long_term_memory",
                    doc_type=DocType.NOTE,
                    text=f.text,
                    index=i,
                    section=",".join(f.tags),
                )
                for i, f in enumerate(self._facts)
            ]
        )
        self._store = store

    def recall(self, query: str, k: int = 3, *, min_score: float = 0.0) -> list[Fact]:
        """按相关性召回记忆。"""
        if not self._facts:
            return []
        self._ensure_store()

        hits = self._store.search(query, k=k, min_score=min_score)  # type: ignore[attr-defined]
        by_id = {f"fact-{i}": f for i, f in enumerate(self._facts)}
        out: list[Fact] = []
        for hit in hits:
            if fact := by_id.get(hit.chunk.id):
                out.append(fact)
        return out

    def as_context(self, query: str, k: int = 3) -> str:
        """召回并渲染成可直接拼进提示词的文本。"""
        facts = self.recall(query, k=k)
        if not facts:
            return ""
        return "\n".join(f"- {f.text}" for f in facts)

    # ---------- 持久化 ----------

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [f.model_dump() for f in self._facts]
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )

    def load(self) -> int:
        if self.path is None or not self.path.exists():
            return 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._facts = [Fact.model_validate(item) for item in raw]
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("长期记忆加载失败，从空开始：%s", exc)
            self._facts = []
        self._store = None
        return len(self._facts)

    # ---------- 自省 ----------

    @property
    def facts(self) -> list[Fact]:
        return list(self._facts)

    def __len__(self) -> int:
        return len(self._facts)

    def stats(self) -> dict[str, object]:
        return {"facts": len(self._facts), "max_facts": self.max_facts, "path": str(self.path)}
