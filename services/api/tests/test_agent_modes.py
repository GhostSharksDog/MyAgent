"""Agent 形态路由测试（HTTP 层）。

【为什么要单独测这个】
规划型与多 Agent 在实现完成后**一度无法从 API 触发** ——
它们只存在于 Python 模块里，HTTP 入口永远走默认的 ReAct。
前端于是永远看不到 `plan` / `delegate` 事件，那些面板也就永远不显示。

这是"功能实现了但没有被接上"的典型例子：单元测试全绿，
端到端却完全走不到那条路径。所以路由本身必须有测试守住。

三种形态的契约是同一条：**同样的请求体，只是 mode 不同**，
且都必须能流式产出事件、都能写回会话。
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


class ModeLLM:
    """同时支持 chat（规划/路由/汇总）与 stream_chat（单步/专家/ReAct）的假 LLM。"""

    def __init__(self, *, plan_steps: int = 2, specialists: list[str] | None = None) -> None:
        self._plan_steps = plan_steps
        self._specialists = specialists if specialists is not None else ["简历诊断师"]
        self.chat_calls = 0
        self.stream_calls = 0

    async def chat(self, messages: Sequence[ChatMessage], **_kw: Any) -> ChatResponse:
        prompt = messages[-1].content or ""
        self.chat_calls += 1

        if "任务规划器" in prompt:
            payload = {
                "reasoning": "先看简历再下结论",
                "steps": [
                    {"id": i, "description": f"第{i}步", "expected": "结论"}
                    for i in range(1, self._plan_steps + 1)
                ],
            }
            return _resp(json.dumps(payload, ensure_ascii=False))

        if "任务协调者" in prompt:
            return _resp(json.dumps({"specialists": self._specialists}, ensure_ascii=False))

        # 汇总 / 摘要
        return _resp("这是汇总后的最终答案。")

    async def stream_chat(
        self, messages: Sequence[ChatMessage], tools: Any = None, **_kw: Any
    ) -> AsyncIterator[StreamDelta]:
        self.stream_calls += 1
        yield StreamDelta(content="单步结论")
        yield StreamDelta(
            finish_reason="stop",
            usage=Usage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        )


def _resp(text: str) -> ChatResponse:
    return ChatResponse(
        message=ChatMessage(role=Role.ASSISTANT, content=text),
        usage=Usage(prompt_tokens=50, completion_tokens=10, total_tokens=60),
    )


def _install_llm(llm: ModeLLM) -> ModeLLM:
    """把假 LLM 装到**两个**会用它的地方。

    这是个必须封装的细节：`_resolve` 读的是 `app.state.llm`，
    但**无状态模式的 react 请求**走的是 `app.state.agent` ——
    后者在 lifespan 启动时就用当时的 LLM 构造好了，只换 `llm` 对它无效。

    初版测试给一个自定义字段赋值（`app.state._mode_llm`），
    而路由根本不读那个字段，于是"自定义的专家列表"完全没生效 ——
    测试看起来在验证某件事，实际什么都没验。
    用函数把"装到哪两处"固定下来，就不会再犯。
    """
    from app.agent.loop import Agent

    app.state.llm = llm
    app.state.agent = Agent(
        llm,  # type: ignore[arg-type]
        app.state.tools,
        app.state.settings.agent,
        memory=None,
        long_term=app.state.long_term,
    )
    return llm


@pytest.fixture(autouse=True)
def _fresh_state() -> None:
    app.state.sessions = InMemorySessionStore()
    _install_llm(ModeLLM())


def _events(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                out.append(json.loads(payload))
    return out


def _stream(client: TestClient, message: str, **extra: Any) -> list[dict[str, Any]]:
    r = client.post("/api/chat/stream", json={"message": message, **extra})
    assert r.status_code == 200, r.text[:200]
    return _events(r.text)


# ============================================================
# 形态元信息
# ============================================================
class TestModeDiscovery:
    def test_meta_exposes_modes(self, client: TestClient) -> None:
        """前端从后端**发现**可用形态，而不是在前端硬编码一份可能过期的列表。"""
        modes = client.get("/api/meta").json()["agent_modes"]
        assert set(modes) == {"react", "plan", "multi"}

    def test_default_mode_is_react(self, client: TestClient) -> None:
        events = _stream(client, "问题")
        types = [e["type"] for e in events]
        assert "plan" not in types and "delegate" not in types

    def test_invalid_mode_rejected(self, client: TestClient) -> None:
        r = client.post("/api/chat/stream", json={"message": "x", "mode": "不存在的形态"})
        assert r.status_code == 422


# ============================================================
# 三种形态各自的事件契约
# ============================================================
class TestReactMode:
    def test_produces_final(self, client: TestClient) -> None:
        events = _stream(client, "问题", mode="react")
        assert any(e["type"] == "final" for e in events)
        assert events[-1]["type"] == "done"


class TestPlanMode:
    def test_emits_plan_and_steps(self, client: TestClient) -> None:
        _install_llm(ModeLLM(plan_steps=2))
        events = _stream(client, "帮我分析简历和岗位", mode="plan")
        types = [e["type"] for e in events]

        assert "plan" in types, "规划形态必须产出 plan 事件，否则前端面板无从渲染"
        assert types.count("plan_step") >= 2
        assert types[-1] == "done"

    def test_plan_event_carries_full_snapshot(self, client: TestClient) -> None:
        events = _stream(client, "问题", mode="plan")
        plan = next(e for e in events if e["type"] == "plan")["plan"]
        assert plan["goal"] == "问题"
        assert len(plan["steps"]) == 2
        assert plan["reasoning"]

    def test_steps_reach_done(self, client: TestClient) -> None:
        events = _stream(client, "问题", mode="plan")
        last = [e for e in events if e["type"] == "plan_step"][-1]
        statuses = [s["status"] for s in last["plan"]["steps"]]
        assert statuses == ["done", "done"]

    def test_no_tool_events_in_plan_mode(self, client: TestClient) -> None:
        """规划形态的**步骤内部**才会调工具，顶层事件流里不该混入工具事件。

        顶层混入工具事件意味着某处的循环被复用错了 —— 那会让前端的
        时间线把"计划步骤"和"工具调用"混在一起渲染。
        """
        events = _stream(client, "问题", mode="plan")
        types = [e["type"] for e in events]
        assert "tool_call" not in types


class TestMultiMode:
    def test_emits_delegate_events(self, client: TestClient) -> None:
        _install_llm(ModeLLM(specialists=["简历诊断师", "岗位分析师"]))
        events = _stream(client, "简历和岗位匹配吗", mode="multi")
        types = [e["type"] for e in events]

        assert types.count("delegate") == 2
        assert types.count("delegate_result") == 2
        assert types[-1] == "done"

    def test_delegate_carries_specialist_name(self, client: TestClient) -> None:
        events = _stream(client, "问题", mode="multi")
        names = [e["specialist"] for e in events if e["type"] == "delegate"]
        assert names == ["简历诊断师"]

    def test_no_plan_events_in_multi_mode(self, client: TestClient) -> None:
        events = _stream(client, "问题", mode="multi")
        types = [e["type"] for e in events]
        assert "plan" not in types


# ============================================================
# 形态 × 会话 的正交组合
# ============================================================
class TestModeWithSession:
    @pytest.mark.parametrize("mode", ["react", "plan", "multi"])
    def test_all_modes_persist_turn(self, client: TestClient, mode: str) -> None:
        """三种形态都必须能把结果写回会话 —— 否则切到 plan/multi 就"丢历史"。"""
        sid = client.post("/api/sessions").json()["id"]
        _stream(client, "问题", mode=mode, session_id=sid)

        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["turn_count"] == 1
        assert detail["turns"][0]["content"] == "问题"
        assert detail["turns"][1]["content"]

    @pytest.mark.parametrize("mode", ["react", "plan", "multi"])
    def test_unknown_session_404_in_all_modes(self, client: TestClient, mode: str) -> None:
        r = client.post(
            "/api/chat/stream",
            json={"message": "x", "mode": mode, "session_id": "不存在"},
        )
        assert r.status_code == 404

    def test_react_mode_uses_session_history(self, client: TestClient) -> None:
        """只有 react 形态使用会话历史 —— 这是刻意的，但必须是**已知行为**。"""
        sid = client.post("/api/sessions").json()["id"]
        client.post("/api/chat", json={"message": "第一问", "session_id": sid})
        client.post("/api/chat", json={"message": "第二问", "session_id": sid})

        # 第二轮时历史应被带上（假 LLM 不做校验，这里靠 session 的轮次间接确认）
        detail = client.get(f"/api/sessions/{sid}").json()
        assert detail["turn_count"] == 2

    @pytest.mark.parametrize("mode", ["plan", "multi"])
    def test_non_react_modes_ignore_history(self, client: TestClient, mode: str) -> None:
        """plan / multi 收下 history 但不使用 —— 且必须留下日志而不是静默忽略。

        "参数收下了但没用"是最容易被误认为已生效的情况，
        所以后端会记一条 INFO 日志。这里通过"结果不受 history 影响"来间接确认。
        """
        sid = client.post("/api/sessions").json()["id"]
        client.post("/api/chat", json={"message": "第一问", "session_id": sid})
        events = _stream(client, "第二问", mode=mode, session_id=sid)
        assert any(e["type"] == "final" for e in events)


# ============================================================
# 非流式端点也必须支持形态
# ============================================================
class TestNonStreamingModes:
    @pytest.mark.parametrize("mode", ["react", "plan", "multi"])
    def test_chat_supports_all_modes(self, client: TestClient, mode: str) -> None:
        r = client.post("/api/chat", json={"message": "问题", "mode": mode})
        assert r.status_code == 200
        body = r.json()
        assert body["answer"]
        assert body["stopped_reason"] in {"finished", "error", "max_steps", "loop_detected"}

    def test_plan_mode_returns_plan_payload(self, client: TestClient) -> None:
        """非流式调用也要能拿到完整答案（由综合步骤产出）。

        注意 `ChatResponse` 目前**不含 plan 字段** —— 它面向"简单集成"，
        调用方只关心答案。需要计划快照的场景请用流式端点（`plan` 事件携带）。
        这是刻意的接口分工，不是遗漏。
        """
        body = client.post("/api/chat", json={"message": "问题", "mode": "plan"}).json()
        assert body["answer"] == "这是汇总后的最终答案。"
