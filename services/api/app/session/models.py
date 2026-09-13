"""会话模型。

【为什么"会话"应该是一个显式的一等概念，而不是客户端自己攒历史】

P1/P2 阶段历史是**客户端传上来的**（`ChatRequest.history`）。这在单机 CLI 场景
没问题，但一旦有了 Web 前端就会暴露三个问题：

  1. **刷新页面就丢历史**。历史只活在浏览器的内存里。
  2. **多标签页/多设备无法共享**。同一用户开两个窗口就是两条独立对话。
  3. **服务端的记忆能力用不上**。P2 做了短期记忆（窗口+摘要）、长期记忆，
     但 HTTP 接口拿不到它们 —— 因为服务端根本不知道"这是同一场对话"。

引入会话之后，历史与记忆都归属服务端，客户端只持有一个 `session_id`。
这也让"多进程部署"成为可能（会话存 Redis，任何实例都能服务同一场对话）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.agent.memory import Turn


def new_session_id() -> str:
    """生成会话 id。

    用 uuid4 而不是自增或时间戳：会话 id 会暴露在 URL 与前端存储里，
    可枚举的 id 意味着任何人改一个数字就能读到别人的对话。
    这在有鉴权之后仍然是坏习惯 —— 鉴权会失效、会有漏配的路由。
    """
    return uuid.uuid4().hex


def _default_title(message: str, max_len: int = 24) -> str:
    """用首轮用户输入自动生成会话标题。

    为什么不用 LLM 生成：那会让"创建会话"这个本该零成本的操作
    变成一次网络调用与 token 消耗。截断首句足够实用，也足够快。
    """
    text = message.strip().replace("\n", " ")
    return text[:max_len] + ("…" if len(text) > max_len else "")


class Session(BaseModel):
    """一场完整对话。"""

    id: str = Field(default_factory=new_session_id)
    title: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    turns: list[Turn] = Field(default_factory=list)
    # 累计用量：成本可观测性的最小单位。放在会话维度而不是全局，
    # 才能回答"这场对话花了多少"这种真实问题。
    total_tokens: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)

    def append_turn(self, user: str, assistant: str, *, tokens: int = 0) -> None:
        self.turns.append(Turn(user=user, assistant=assistant))
        self.updated_at = time.time()
        self.total_tokens += tokens
        if not self.title:
            self.title = _default_title(user)

    @property
    def turn_count(self) -> int:
        return len(self.turns)


class SessionSummary(BaseModel):
    """列表视图用的轻量结构。

    刻意**不含 turns** —— 列出 20 个会话时把全部对话内容读出来，
    在 Redis 场景下就是 20 次大 value 读取。列表只需要元信息。
    """

    id: str
    title: str
    created_at: float
    updated_at: float
    turn_count: int
    total_tokens: int
