"""HTTP 接口层测试。

【为什么必须单独测这一层】
内核测试全绿，不代表接口是好的：路由参数、状态码、响应模型、SSE 事件格式
都是独立的失败面。这个项目一度有 70 个内核测试、却**没有任何一个测试导入
app.api 或 app.main** —— 五个端点零覆盖。而 bug 恰恰就藏在那一层：

    routes.py 用 f-string + repr() 手拼流式错误事件：
        f'{{"type":"error","content":{exc!r}}}'
    → 这不是合法 JSON（Python repr 用单引号且不转义），
      前端 JSON.parse 直接抛异常，用户只看到"连接中断"。

所以本文件的核心任务有两个：覆盖端点的正常路径，以及**守住这类格式契约**。

用 TestClient 走完整 ASGI 栈（含 lifespan），但把 app.state.agent 换成假的，
这样既能测真实的序列化与路由逻辑，又不花一分钱 token。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from app.agent.events import AgentEvent, AgentRunResult, EventType
from app.agent.loop import Agent
from app.llm.types import ChatMessage, Usage
from app.main import app
from fastapi.testclient import TestClient


# ============================================================
# 假 Agent：产出脚本化事件，不调用真实模型
# ============================================================
class FakeAgent:
    """按剧本产出事件的假 Agent。

    只实现路由真正用到的方法（run_stream / run），
    这本身就是"依赖注入让测试变简单"的体现。
    """

    def __init__(self, events: list[AgentEvent] | None = None, *, boom: bool = False) -> None:
        self._events = events or [
            AgentEvent(type=EventType.START, content="hi"),
            AgentEvent(type=EventType.STEP, step=1),
            AgentEvent(
                type=EventType.TOOL_CALL,
                step=1,
                tool_name="calculator",
                tool_args={"expression": "1+1"},
            ),
            AgentEvent(
                type=EventType.TOOL_RESULT,
                step=1,
                tool_name="calculator",
                tool_ok=True,
                content="1+1 = 2",
                duration_ms=3,
                truncated=False,
            ),
            AgentEvent(type=EventType.STEP, step=2),
            AgentEvent(type=EventType.TOKEN, step=2, content="答案是 2"),
            AgentEvent(type=EventType.FINAL, step=2, content="答案是 2"),
            AgentEvent(type=EventType.DONE, step=2, steps_used=2),
        ]
        self._boom = boom
        self.received: list[tuple[str, list[ChatMessage]]] = []

    async def run_stream(
        self, user_input: str, history: Sequence[ChatMessage] | None = None
    ) -> AsyncIterator[AgentEvent]:
        self.received.append((user_input, list(history or [])))
        if self._boom:
            raise RuntimeError('模拟内部故障——含引号 " 和反斜杠 \\ 以验证转义')
        for event in self._events:
            yield event

    async def run(
        self, user_input: str, history: Sequence[ChatMessage] | None = None
    ) -> AgentRunResult:
        """非流式入口。与真 Agent 一样复用 run_stream 的结果 ——
        不重复实现，避免两条路径行为不一致。"""
        answer_parts: list[str] = []
        steps_used = 0
        usage = Usage()
        tool_calls: list[dict[str, Any]] = []
        stopped = "finished"

        async for event in self.run_stream(user_input, history):
            match event.type:
                case EventType.TOKEN:
                    answer_parts.append(event.content)
                case EventType.FINAL:
                    answer_parts = [event.content]
                case EventType.TOOL_CALL:
                    tool_calls.append({"name": event.tool_name, "args": event.tool_args})
                case EventType.DONE:
                    steps_used = event.steps_used
                    usage = event.usage or Usage()
                    stopped = event.stopped_reason

        return AgentRunResult(
            answer="".join(answer_parts),
            steps_used=steps_used,
            usage=usage,
            tool_calls=tool_calls,
            stopped_reason=stopped,
        )


def _collect_from_stream(content: str) -> list[dict[str, Any]]:
    """把 SSE 响应体解析成事件对象列表。

    这个函数本身就是断言工具：**任何一行 data 不是合法 JSON 都会直接抛异常**，
    因此它同时守住了"SSE 必须是合法 JSON"这个格式契约。
    """
    events: list[dict[str, Any]] = []
    for line in content.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        events.append(json.loads(payload))  # 解析失败 = 契约被破坏
    return events


def _install(fake: Any) -> None:
    """把假 Agent 装进组合根，替换掉真实 Agent。"""
    app.state.agent = fake


# ============================================================
# 元信息端点
# ============================================================
class TestMetaEndpoints:
    def test_healthz(self, client: TestClient) -> None:
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "llm_configured" in body
        # 形态必须可见：它决定提示词、工具集与知识库默认数据源，
        # 而配错了不会报错（只会得到另一个形态的助手）。
        assert body["profile"] == "general"
        assert isinstance(body["tools"], list)
        assert "calculator" in body["tools"]

    def test_meta(self, client: TestClient) -> None:
        r = client.get("/api/meta")
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "legacy-api"
        assert body["tool_count"] == 3
        assert body["max_steps"] >= 1

    def test_meta_exposes_default_profile(self, client: TestClient) -> None:
        """元信息里必须能看出当前是哪个形态。

        【为什么断言写死 "general" 而不是"和配置相等"】
        与 `tool_count == 3` 同一条前提：这套用例跑在**默认配置**下，
        而"默认是通用形态"正是本次改动的核心约定（见 AgentSettings.profile）。
        断言成"等于配置里的值"会让 `profile=jobhunt` 的环境也通过，
        于是"默认被改回求职形态"这件事就没人守得住了。
        """
        body = client.get("/api/meta").json()
        assert body["profile"] == "general"

    def test_tools_lists_core_set_with_schemas(self, client: TestClient) -> None:
        r = client.get("/api/tools")
        assert r.status_code == 200
        tools = r.json()
        assert len(tools) == 3
        by_name = {t["name"]: t for t in tools}
        assert set(by_name) == {
            "calculator",
            "get_current_time",
            "search_knowledge",
        }
        # 描述与参数结构必须完整，否则模型无法正确调用工具
        for t in tools:
            assert t["description"]
            assert t["parameters"]["type"] == "object"


# ============================================================
# 非流式对话
# ============================================================
class TestChatEndpoint:
    def test_chat_returns_answer(self, client: TestClient) -> None:
        _install(FakeAgent())
        r = client.post("/api/chat", json={"message": "1+1 等于几"})
        assert r.status_code == 200
        body = r.json()
        assert body["answer"] == "答案是 2"
        assert body["steps_used"] == 2
        assert body["stopped_reason"] == "finished"
        assert [c["name"] for c in body["tool_calls"]] == ["calculator"]

    def test_chat_passes_history_in_order(self, client: TestClient) -> None:
        fake = FakeAgent()
        _install(fake)
        r = client.post(
            "/api/chat",
            json={
                "message": "第二个问题",
                "history": [
                    {"role": "user", "content": "第一个问题"},
                    {"role": "assistant", "content": "第一个回答"},
                ],
            },
        )
        assert r.status_code == 200

        user_input, history = fake.received[0]
        assert user_input == "第二个问题"
        assert [str(m.role) for m in history] == ["user", "assistant"]
        assert history[0].content == "第一个问题"

    @pytest.mark.parametrize(
        "payload",
        [
            {},  # 缺 message
            {"message": ""},  # 空字符串（min_length=1）
            {"message": "x" * 9000},  # 超长（max_length=8000）
            {"message": "ok", "history": [{"role": "tool", "content": "x"}]},  # 非法角色
        ],
    )
    def test_invalid_payload_rejected(self, client: TestClient, payload: dict[str, Any]) -> None:
        _install(FakeAgent())
        assert client.post("/api/chat", json=payload).status_code == 422


# ============================================================
# 流式对话（SSE 契约）
# ============================================================
class TestStreamEndpoint:
    def test_event_sequence(self, client: TestClient) -> None:
        _install(FakeAgent())
        r = client.post("/api/chat/stream", json={"message": "1+1 等于几"})

        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]

        events = _collect_from_stream(r.text)
        assert [e["type"] for e in events] == [
            "start",
            "step",
            "tool_call",
            "tool_result",
            "step",
            "token",
            "final",
            "done",
        ]

    def test_tool_result_carries_observability_fields(self, client: TestClient) -> None:
        """耗时与截断标记必须下发，否则前端 trace 里看不到真实情况。"""
        _install(FakeAgent())
        r = client.post("/api/chat/stream", json={"message": "x"})

        events = _collect_from_stream(r.text)
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["tool_name"] == "calculator"
        assert result["tool_ok"] is True
        assert result["duration_ms"] == 3
        assert result["truncated"] is False

    def test_internal_error_emits_valid_json(self, client: TestClient) -> None:
        """回归测试：错误事件必须是合法 JSON。

        曾经的 bug 用 f-string + repr() 手拼 JSON，异常信息里只要含引号，
        前端 JSON.parse 就会抛异常。这里故意让假 Agent 抛一个**含引号与反斜杠**
        的异常，确保转义正确。
        """
        _install(FakeAgent(boom=True))
        r = client.post("/api/chat/stream", json={"message": "x"})

        assert r.status_code == 200  # 流已开始，无法再改状态码
        events = _collect_from_stream(r.text)  # 解析失败会直接抛异常
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "内部故障" in events[0]["content"]
        assert '"' in events[0]["content"]  # 引号被正确转义后仍可解析

    def test_history_forwarded(self, client: TestClient) -> None:
        fake = FakeAgent()
        _install(fake)
        client.post(
            "/api/chat/stream",
            json={"message": "新问题", "history": [{"role": "user", "content": "旧问题"}]},
        )
        assert fake.received[0][1][0].content == "旧问题"


# ============================================================
# 与真实 Agent 的装配一致性
# ============================================================
class TestWiring:
    def test_lifespan_installs_real_agent(self, client: TestClient) -> None:
        """lifespan 必须把真实的 Agent/工具表装配到 app.state。

        这一条守的是"组合根"：如果装配漏了某个组件，路由要到运行时才炸，
        而这里能在启动阶段就暴露出来。

        注意本用例必须**在替换 agent 之前**断言，所以它用独立的一次装配检查。
        """
        names = app.state.tools.names()
        # 默认（general）profile 只装核心三件套 —— 求职技能包不在默认能力集里
        assert len(names) == 3
        assert set(names) == {"calculator", "get_current_time", "search_knowledge"}
        assert app.state.settings is not None
        # app.state.agent 在本模块中可能已被其他用例替换，故只校验类型来源
        assert isinstance(app.state.agent, Agent | FakeAgent)

    def test_cors_allows_local_dev_origin(self, client: TestClient) -> None:
        """P3 的前端跑在 Vite 端口上，属于跨域，必须放行。"""
        r = client.options(
            "/api/chat",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
