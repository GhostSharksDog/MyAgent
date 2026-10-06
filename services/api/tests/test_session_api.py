"""会话 HTTP 接口测试。

与 test_session.py 的分工：
  test_session.py      存储层的行为契约（内存 / Redis / 工厂）
  本文件               HTTP 层的端到端行为（路由、状态码、会话模式的读写）

这里**用真实 Agent + 假 LLM**，而不是替换整个 Agent：
会话模式的关键逻辑在 `_resolve`（从会话恢复记忆、新建绑定记忆的 Agent）
与 `_persist`（成功则写回）里，替换 Agent 就绕过了它们 —— 那等于没测。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from app.llm.types import ChatMessage, ChatResponse, Role, StreamDelta, Usage
from app.main import app
from app.session.store import InMemorySessionStore
from fastapi.testclient import TestClient


class ScriptedLLM:
    """脚本化的假 LLM，被真实的 Agent 使用。

    同时提供 `stream_chat`（Agent 用）与 `chat`（记忆摘要用），
    因为会话模式下记忆是从会话恢复的，可能触发摘要压缩。
    """

    def __init__(self, turns: list[list[StreamDelta]] | None = None) -> None:
        self._turns = turns or [[StreamDelta(content="收到"), StreamDelta(finish_reason="stop")]]
        self._i = 0
        self.received: list[list[ChatMessage]] = []

    async def stream_chat(
        self, messages: Sequence[ChatMessage], tools: Any = None, **_kw: Any
    ) -> AsyncIterator[StreamDelta]:
        self.received.append(list(messages))
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for delta in turn:
            yield delta

    async def chat(self, messages: Sequence[ChatMessage], **_kw: Any) -> ChatResponse:
        return ChatResponse(
            message=ChatMessage(role=Role.ASSISTANT, content="（摘要）"),
            usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )


def text_turn(text: str, tokens: int = 30) -> list[StreamDelta]:
    return [
        StreamDelta(content=text),
        StreamDelta(
            finish_reason="stop",
            usage=Usage(prompt_tokens=tokens, completion_tokens=tokens, total_tokens=tokens * 2),
        ),
    ]


def tool_turn(name: str, args: dict[str, Any]) -> list[StreamDelta]:
    return [
        StreamDelta(
            tool_call_deltas=[
                {
                    "index": 0,
                    "id": "c1",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }
            ]
        ),
        StreamDelta(finish_reason="tool_calls"),
    ]


@pytest.fixture(autouse=True)
def _fresh_store() -> None:
    """每个用例一个新会话存储，避免用例之间互相看到对方的会话。

    注意 `client` 是 session 级的（见 conftest.py），所以这里只换存储，
    不动客户端 —— 换客户端会引入第二个事件循环，
    而 sse-starlette 的全局 AppStatus 事件不支持跨循环。
    """
    app.state.sessions = InMemorySessionStore()


def _install_llm(turns: list[list[StreamDelta]] | None = None) -> ScriptedLLM:
    """替换 LLM **并重建共享 Agent**。

    【为什么必须同时重建 Agent】
    无状态模式的请求走的是 `app.state.agent` —— 它在 lifespan 启动时
    就用**当时的** LLM 客户端构造好了。只替换 `app.state.llm` 对它无效，
    假 LLM 根本不会被调用（测试表现为 `llm.received` 为空）。

    会话模式则是每次请求新建 Agent（绑定该会话的记忆），
    所以只替换 `app.state.llm` 就够 —— 两条路径的差异必须在这里抹平，
    否则测试会因为"走的哪条路"而表现不一致。
    """
    from app.agent.loop import Agent

    llm = ScriptedLLM(turns)
    app.state.llm = llm
    app.state.agent = Agent(
        llm,  # type: ignore[arg-type]
        app.state.tools,
        app.state.settings.agent,
        memory=None,
        long_term=app.state.long_term,
    )
    return llm


def _stream_events(response_text: str) -> list[dict[str, Any]]:
    events = []
    for line in response_text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                events.append(json.loads(payload))
    return events


# ============================================================
# 会话 CRUD
# ============================================================
class TestSessionCrud:
    def test_create(self, client: TestClient) -> None:
        r = client.post("/api/sessions")
        assert r.status_code == 200
        body = r.json()
        assert body["id"]
        assert body["turn_count"] == 0
        assert body["title"] == "（未命名会话）"

    def test_list_empty(self, client: TestClient) -> None:
        r = client.get("/api/sessions")
        assert r.status_code == 200
        body = r.json()
        assert body["sessions"] == []
        assert body["backend"] == "memory"

    def test_list_after_create(self, client: TestClient) -> None:
        """非空列表必须能序列化。

        回归：初版把存储层的 `list[SessionSummary]` 直接塞进声明为
        `list[SessionSummaryModel]` 的响应模型，pydantic v2 不做跨模型
        鸭子类型转换 → 抛 ValidationError。
        **会话为空时测试是通过的**（没有元素需要校验），
        只有创建过会话再列列表才暴露 —— 这就是"边界用例要覆盖非空"的教训。
        """
        for _ in range(3):
            client.post("/api/sessions")
        r = client.get("/api/sessions")
        assert r.status_code == 200
        body = r.json()
        assert len(body["sessions"]) == 3
        assert all(s["id"] and s["title"] for s in body["sessions"])

    def test_detail_empty_turns(self, client: TestClient) -> None:
        sid = client.post("/api/sessions").json()["id"]
        r = client.get(f"/api/sessions/{sid}")
        assert r.status_code == 200
        assert r.json()["turns"] == []

    def test_detail_unknown_returns_404(self, client: TestClient) -> None:
        """404 而不是 200+空会话：让"会话不存在"显式暴露给客户端。"""
        r = client.get("/api/sessions/not-a-real-id")
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"]

    def test_delete(self, client: TestClient) -> None:
        sid = client.post("/api/sessions").json()["id"]
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert client.get(f"/api/sessions/{sid}").status_code == 404

    def test_delete_unknown_returns_404(self, client: TestClient) -> None:
        assert client.delete("/api/sessions/not-a-real-id").status_code == 404

    def test_limit_is_clamped(self, client: TestClient) -> None:
        """limit 必须被夹到合理范围内，否则 limit=100000 就是一次 DoS。"""
        assert client.get("/api/sessions?limit=100000").status_code == 200
        assert client.get("/api/sessions?limit=0").status_code == 200


# ============================================================
# 会话模式的对话
# ============================================================
class TestChatWithSession:
    def test_stream_persists_turn(self, client: TestClient) -> None:
        _install_llm([text_turn("这是回答")])
        sid = client.post("/api/sessions").json()["id"]

        r = client.post("/api/chat/stream", json={"message": "这是问题", "session_id": sid})
        assert r.status_code == 200
        events = _stream_events(r.text)
        assert events[-1]["type"] == "done"

        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["turns"] == [
            {"role": "user", "content": "这是问题"},
            {"role": "assistant", "content": "这是回答"},
        ]

    def test_tokens_recorded_on_session(self, client: TestClient) -> None:
        """用量按会话累计 —— 才能回答"这场对话花了多少"这种真实问题。"""
        _install_llm([text_turn("答", tokens=50)])
        sid = client.post("/api/sessions").json()["id"]
        client.post("/api/chat/stream", json={"message": "问", "session_id": sid})

        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["total_tokens"] == 100

    def test_title_generated_from_first_message(self, client: TestClient) -> None:
        _install_llm([text_turn("答")])
        sid = client.post("/api/sessions").json()["id"]
        client.post("/api/chat/stream", json={"message": "帮我分析简历", "session_id": sid})
        assert client.get(f"/api/sessions/{sid}").json()["title"] == "帮我分析简历"

    def test_history_loaded_from_session(self, client: TestClient) -> None:
        """第二轮必须带上第一轮的历史 —— 这是会话存在的意义。"""
        llm = _install_llm([text_turn("第一轮回答"), text_turn("第二轮回答")])
        sid = client.post("/api/sessions").json()["id"]

        client.post("/api/chat/stream", json={"message": "第一轮问题", "session_id": sid})
        client.post("/api/chat/stream", json={"message": "第二轮问题", "session_id": sid})

        # 第二次请求发给模型的消息里应含第一轮的问答
        second_call = llm.received[1]
        contents = [m.content for m in second_call]
        assert "第一轮问题" in contents
        assert "第一轮回答" in contents
        assert contents[-1] == "第二轮问题"

    def test_client_history_ignored_in_session_mode(self, client: TestClient) -> None:
        """两套历史同时生效必然导致重复或错序。

        会话模式下以服务端历史为准，客户端传的 history 被忽略。
        """
        llm = _install_llm([text_turn("答")])
        sid = client.post("/api/sessions").json()["id"]

        client.post(
            "/api/chat/stream",
            json={
                "message": "新问题",
                "session_id": sid,
                "history": [{"role": "user", "content": "客户端伪造的历史"}],
            },
        )
        contents = [m.content for m in llm.received[0]]
        assert "客户端伪造的历史" not in contents

    def test_unknown_session_returns_404(self, client: TestClient) -> None:
        """会话不存在时在**开始流式之前**返回 404。

        这是刻意的：一旦开始发 SSE 就无法再改 HTTP 状态码，
        客户端只能从事件流里猜测出了什么问题。
        """
        _install_llm()
        r = client.post("/api/chat/stream", json={"message": "问", "session_id": "不存在"})
        assert r.status_code == 404

    def test_failed_turn_not_persisted(self, client: TestClient) -> None:
        """被预算掐断的轮次不写入会话。

        它不是有效上下文，写进去会让后续对话基于半成品推理。
        这与 Agent 内部写短期记忆的判据必须一致。
        """
        _install_llm([tool_turn("calculator", {"expression": f"{i}+1"}) for i in range(20)])
        sid = client.post("/api/sessions").json()["id"]

        r = client.post("/api/chat/stream", json={"message": "无限循环", "session_id": sid})
        events = _stream_events(r.text)
        assert events[-1]["stopped_reason"] == "max_steps"

        assert client.get(f"/api/sessions/{sid}").json()["turns"] == []

    def test_stateless_mode_still_works(self, client: TestClient) -> None:
        """不带 session_id 时保持 P1 的无状态行为，且**不创建会话**。"""
        llm = _install_llm([text_turn("答")])
        r = client.post(
            "/api/chat/stream",
            json={"message": "新问题", "history": [{"role": "user", "content": "外部历史"}]},
        )
        assert r.status_code == 200
        contents = [m.content for m in llm.received[0]]
        assert "外部历史" in contents  # 无状态模式下客户端历史生效
        assert client.get("/api/sessions").json()["sessions"] == []

    def test_non_streaming_chat_persists(self, client: TestClient) -> None:
        _install_llm([text_turn("非流式回答")])
        sid = client.post("/api/sessions").json()["id"]

        r = client.post("/api/chat", json={"message": "问题", "session_id": sid})
        assert r.status_code == 200
        assert r.json()["answer"] == "非流式回答"

        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["turns"][-1]["content"] == "非流式回答"


# ============================================================
# 元信息暴露会话后端
# ============================================================
class TestBackendVisibility:
    def test_healthz_exposes_session_backend(self, client: TestClient) -> None:
        """会话后端必须可见。

        `memory` 意味着多副本部署下会丢会话 —— 这是运维最需要一眼看到的信息，
        而不是等到用户投诉"历史不见了"才发现。
        """
        body = client.get("/healthz").json()
        assert body["session_backend"] in {"memory", "redis", "fake"}

    def test_meta_exposes_session_backend(self, client: TestClient) -> None:
        assert client.get("/api/meta").json()["session_backend"] in {"memory", "redis", "fake"}


@pytest.mark.parametrize("mode", ["plan", "multi"])
@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/chat/stream"])
def test_budget_partial_answer_is_never_saved_to_session(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str, endpoint: str
) -> None:
    from tests.test_agent_runtime import RuntimeLLM

    monkeypatch.setattr(app.state, "llm", RuntimeLLM())
    current = app.state.settings
    monkeypatch.setattr(
        app.state,
        "settings",
        current.model_copy(
            update={"agent": current.agent.model_copy(update={f"{mode}_max_total_tokens": 1})}
        ),
    )
    sid = client.post("/api/sessions").json()["id"]
    response = client.post(
        endpoint, json={"message": "核验公开资料", "session_id": sid, "mode": mode}
    )
    assert response.status_code == 200
    assert "token_budget" in response.text
    assert not client.get(f"/api/sessions/{sid}").json()["turns"]
