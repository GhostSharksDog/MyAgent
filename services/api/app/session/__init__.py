"""会话层：把"一场对话"变成服务端的一等概念。

     models   Session / SessionSummary
     store    SessionStore 抽象 + 内存实现 + Redis 实现
     factory  按配置装配（auto / memory / fake / redis）

引入会话解决了三个问题：刷新页面丢历史、多标签页无法共享、
以及**服务端的记忆能力用不上**（HTTP 接口此前拿不到 P2 做的记忆，
因为服务端不知道"这是同一场对话"）。
"""

from app.session.factory import build_session_store
from app.session.models import Session, SessionSummary, new_session_id
from app.session.store import (
    DEFAULT_TTL_SECONDS,
    InMemorySessionStore,
    RedisSessionStore,
    SessionStore,
)

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "InMemorySessionStore",
    "RedisSessionStore",
    "Session",
    "SessionStore",
    "SessionSummary",
    "build_session_store",
    "new_session_id",
]
