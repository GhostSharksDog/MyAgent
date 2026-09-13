"""多 Agent 协作（主管-工人）测试。

最重要的一条是 `TestConcurrency`：**验证专家确实并发执行**。

这不是实现细节，而是本模块与 Plan-and-Execute 的核心差别所在：
Plan-and-Execute 的步骤**有依赖**（第 2 步要用第 1 步的结论）所以必须串行；
职责不同的专家之间**没有依赖**，所以应该并发。

搞反的代价很具体：
  - 把无依赖的专家串行化 → 总耗时是所有专家之和（3 位专家 × 每次 5 秒 = 15 秒）
  - 把有依赖的步骤并发化 → 后面的步骤拿到空的前置结果

所以"是否并发"必须被测试守住，而不是靠读代码相信它。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from app.agent.events import AgentRunResult, EventType
from app.agent.multi import DEFAULT_SPECIALISTS, Specialist, SupervisorAgent
from app.core.config import AgentSettings
from app.llm.types import ChatMessage, ChatResponse, Role, StreamDelta, Usage
from app.tools.base import ToolRegistry


# ============================================================
# 假 LLM
# ============================================================
class SupervisorLLM:
    """按提示词内容路由：路由调用 / 专家执行 / 汇总调用。"""

    def __init__(
        self,
        *,
        selected: list[str] | None = None,
        route_raw: str | None = None,
        expert_answer: str = "专家结论：用户适合这个岗位。",
        synthesis: str = "综合结论：建议优先补强 RAG 经验。",
        expert_delay: float = 0.0,
        route_raises: bool = False,
        synthesis_raises: bool = False,
        fail_experts: set[str] | None = None,
    ) -> None:
        self._selected = selected if selected is not None else ["简历诊断师", "岗位分析师"]
        self._route_raw = route_raw
        self._expert_answer = expert_answer
        self._synthesis = synthesis
        self._expert_delay = expert_delay
        self._route_raises = route_raises
        self._synthesis_raises = synthesis_raises
        self._fail_experts = fail_experts or set()

        self.routed = 0
        self.expert_calls = 0
        self.synthesis_calls = 0
        self.expert_started_at: list[float] = []

    async def chat(self, messages: Sequence[ChatMessage], **_kw: Any) -> ChatResponse:
        prompt = messages[-1].content or ""

        if "任务协调者" in prompt:
            self.routed += 1
            if self._route_raises:
                raise RuntimeError("路由服务不可用")
            raw = self._route_raw or json.dumps(
                {"specialists": self._selected, "reasoning": "因为跨了多个领域"},
                ensure_ascii=False,
            )
            return _resp(raw)

        if "你是 JobPilot 的协调者" in prompt:
            self.synthesis_calls += 1
            if self._synthesis_raises:
                raise RuntimeError("汇总服务不可用")
            return _resp(self._synthesis)

        return _resp("（未识别的调用）")

    async def stream_chat(
        self, messages: Sequence[ChatMessage], tools: Any = None, **_kw: Any
    ) -> AsyncIterator[StreamDelta]:
        prompt = messages[0].content or ""
        self.expert_calls += 1
        self.expert_started_at.append(time.perf_counter())

        # 用 system prompt 里的特征词判断是哪位专家（模拟不同专家耗时不同）。
        #
        # 【注意匹配的是什么】这里匹配的是 system prompt 的文本，不是专家名 ——
        # 因为专家名（如"岗位分析师"）并不出现在它自己的 system prompt 里
        # （prompt 写的是"你是招聘市场分析师"）。用名字匹配会静默地匹配不上，
        # 导致"本该失败的专家没有失败"，测试于是测了个寂寞。
        if any(frag in prompt for frag in self._fail_experts):
            raise RuntimeError("这位专家内部出错了")

        if self._expert_delay:
            await asyncio.sleep(self._expert_delay)

        for delta in [
            StreamDelta(content=self._expert_answer),
            StreamDelta(
                finish_reason="stop",
                usage=Usage(prompt_tokens=50, completion_tokens=30, total_tokens=80),
            ),
        ]:
            yield delta


def _resp(text: str) -> ChatResponse:
    return ChatResponse(
        message=ChatMessage(role=Role.ASSISTANT, content=text),
        usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )


def _supervisor(llm: Any, **kw: Any) -> SupervisorAgent:
    return SupervisorAgent(
        llm,
        kw.pop("tools", ToolRegistry()),
        AgentSettings(max_steps=3),
        **kw,
    )


# ============================================================
# 路由
# ============================================================
class TestRouting:
    async def test_selects_named_specialists(self) -> None:
        llm = SupervisorLLM(selected=["简历诊断师"])
        events = [e async for e in _supervisor(llm).run_stream("看看我的简历")]
        delegates = [e.specialist for e in events if e.type is EventType.DELEGATE]
        assert delegates == ["简历诊断师"]

    @pytest.mark.parametrize(
        "raw",
        [
            "完全不是 JSON",
            "{ 坏掉的 JSON",
            '{"specialists": "不是数组"}',
            '{"specialists": []}',
            "{}",
        ],
    )
    async def test_bad_route_output_falls_back_to_first_specialist(self, raw: str) -> None:
        """路由没选出人时**不能空转**，也不能回一句"我不知道该找谁"。

        退回默认专家（第一位）仍然能给出有价值的回答。
        """
        llm = SupervisorLLM(route_raw=raw)
        events = [e async for e in _supervisor(llm).run_stream("问题")]
        delegates = [e.specialist for e in events if e.type is EventType.DELEGATE]
        assert delegates == [DEFAULT_SPECIALISTS[0].name]

    async def test_unknown_names_ignored(self) -> None:
        """主管偶尔会拼错专家名或编一个不存在的名字。

        静默忽略比让整个请求失败合理得多 —— 剩下的专家通常仍能完成任务。
        """
        llm = SupervisorLLM(route_raw='{"specialists": ["不存在的专家", "岗位分析师"]}')
        events = [e async for e in _supervisor(llm).run_stream("问题")]
        delegates = [e.specialist for e in events if e.type is EventType.DELEGATE]
        assert delegates == ["岗位分析师"]

    async def test_max_delegates_enforced(self) -> None:
        """多选一位就多一次完整的 Agent 运行，代价是真实的。

        上限必须在代码里强制，而不是靠提示词"请选 1~3 位"——
        提示词是请求，代码是保证。
        """
        llm = SupervisorLLM(selected=[s.name for s in DEFAULT_SPECIALISTS])
        events = [e async for e in _supervisor(llm, max_delegates=2).run_stream("问题")]
        delegates = [e.specialist for e in events if e.type is EventType.DELEGATE]
        assert len(delegates) == 2

    async def test_duplicate_names_deduplicated(self) -> None:
        llm = SupervisorLLM(route_raw='{"specialists": ["岗位分析师", "岗位分析师"]}')
        events = [e async for e in _supervisor(llm).run_stream("问题")]
        delegates = [e.specialist for e in events if e.type is EventType.DELEGATE]
        assert delegates == ["岗位分析师"]

    async def test_route_failure_terminates_gracefully(self) -> None:
        llm = SupervisorLLM(route_raises=True)
        events = [e async for e in _supervisor(llm).run_stream("问题")]
        assert any(e.type is EventType.ERROR for e in events)
        assert events[-1].type is EventType.DONE
        assert events[-1].stopped_reason == "error"


# ============================================================
# 事件序列
# ============================================================
class TestEventSequence:
    async def test_full_sequence(self) -> None:
        llm = SupervisorLLM(selected=["简历诊断师", "岗位分析师"])
        events = [e async for e in _supervisor(llm).run_stream("问题")]
        types = [e.type for e in events]

        assert types[0] is EventType.START
        assert types[-1] is EventType.DONE
        assert types[-2] is EventType.FINAL
        assert types.count(EventType.DELEGATE) == 2
        assert types.count(EventType.DELEGATE_RESULT) == 2

    async def test_delegate_result_carries_status(self) -> None:
        llm = SupervisorLLM(selected=["简历诊断师"])
        events = [e async for e in _supervisor(llm).run_stream("问题")]
        result = next(e for e in events if e.type is EventType.DELEGATE_RESULT)
        assert result.specialist == "简历诊断师"
        assert result.tool_ok is True
        assert result.content

    async def test_failed_specialist_marked_not_ok(self) -> None:
        llm = SupervisorLLM(selected=["简历诊断师"], fail_experts={"资深简历诊断师"})
        events = [e async for e in _supervisor(llm).run_stream("问题")]
        result = next(e for e in events if e.type is EventType.DELEGATE_RESULT)
        assert result.tool_ok is False
        # 【必须保留真正的失败原因】
        # `Agent` 把故障转成事件而不抛异常，失败时 answer 是空的，
        # 真正的原因在 error 里。初版只拼了 stopped_reason，
        # 失败信息退化成"未正常完成（error）："后面什么都没有。
        assert "出错了" in result.content
        assert result.content.strip().endswith("）") is False


# ============================================================
# 并发（本模块最核心的验证）
# ============================================================
class TestConcurrency:
    async def test_specialists_run_in_parallel(self) -> None:
        """3 位专家各耗时 0.25 秒，总耗时应约 0.25 秒而不是 0.75 秒。

        这是主管-工人模式相对流水线的核心优势，也是与 Plan-and-Execute
        （步骤有依赖、必须串行）的关键实现差别。
        """
        llm = SupervisorLLM(
            selected=["简历诊断师", "岗位分析师", "面试教练"],
            expert_delay=0.25,
        )
        started = time.perf_counter()
        await _supervisor(llm, max_concurrency=3).run("问题")
        elapsed = time.perf_counter() - started

        assert llm.expert_calls == 3
        assert elapsed < 0.6, f"专家似乎被串行执行了，总耗时 {elapsed:.2f}s（期望约 0.25s）"

    async def test_started_timestamps_overlap(self) -> None:
        """更直接的证据：三位专家的启动时间应该几乎相同。"""
        llm = SupervisorLLM(
            selected=["简历诊断师", "岗位分析师", "面试教练"],
            expert_delay=0.2,
        )
        await _supervisor(llm, max_concurrency=3).run("问题")

        assert len(llm.expert_started_at) == 3
        spread = max(llm.expert_started_at) - min(llm.expert_started_at)
        assert spread < 0.1, f"专家启动时间跨度 {spread:.3f}s，说明没有真正并发"

    async def test_concurrency_limit_respected(self) -> None:
        """5 位专家同时发起 = 5 路并发 LLM 调用，很容易触发限流。

        信号量把并发限制在 2，总耗时应约为 3 批 × 0.15s。
        """
        many = [
            Specialist(name=f"专家{i}", description="d", system_prompt=f"专家{i}的提示")
            for i in range(5)
        ]
        llm = SupervisorLLM(selected=[s.name for s in many], expert_delay=0.15)
        started = time.perf_counter()
        await _supervisor(llm, specialists=many, max_delegates=5, max_concurrency=2).run("q")
        elapsed = time.perf_counter() - started

        # 2 并发 × 0.15s：5 位需要 3 批 ≈ 0.45s（远小于串行的 0.75s，远大于全并发的 0.15s）
        assert 0.3 < elapsed < 0.75, f"并发限制似乎没生效，耗时 {elapsed:.2f}s"


# ============================================================
# 故障隔离
# ============================================================
class TestFailureIsolation:
    async def test_one_failure_does_not_stop_others(self) -> None:
        llm = SupervisorLLM(
            selected=["简历诊断师", "岗位分析师"],
            fail_experts={"资深简历诊断师"},
        )
        result = await _supervisor(llm).run("问题")

        by_name = {d["name"]: d for d in result.tool_calls}
        assert by_name["简历诊断师"]["ok"] is False
        assert by_name["岗位分析师"]["ok"] is True
        assert result.stopped_reason == "finished"

    async def test_all_failed_marks_run_as_error(self) -> None:
        llm = SupervisorLLM(
            selected=["简历诊断师", "岗位分析师"],
            fail_experts={"资深简历诊断师", "招聘市场分析师"},
        )
        result = await _supervisor(llm).run("问题")
        assert result.stopped_reason == "error"

    async def test_synthesis_failure_falls_back_to_expert_texts(self) -> None:
        """汇总失败时退回专家原文。

        比返回"出错了"有用得多 —— 用户至少能看到几位专家的完整分析。
        """
        llm = SupervisorLLM(selected=["简历诊断师"], synthesis_raises=True)
        events = [e async for e in _supervisor(llm).run_stream("问题")]
        final = next(e for e in events if e.type is EventType.FINAL)
        assert "专家分析结果" in final.content
        assert "汇总生成失败" in final.content
        assert llm._expert_answer in final.content


# ============================================================
# 用量与结果
# ============================================================
class TestAccounting:
    async def test_all_three_phases_counted(self) -> None:
        """路由 + 专家 + 汇总，三段成本都要计入。

        漏掉任何一段，成本统计就会系统性偏低 ——
        而多 Agent 的成本恰恰是它最需要被监督的地方。
        """
        llm = SupervisorLLM(selected=["简历诊断师", "岗位分析师"])
        result = await _supervisor(llm).run("问题")
        # 路由 120 + 2 位专家 × 80 + 汇总 120 = 400
        assert result.usage.total_tokens == 400

    async def test_run_result_shape(self) -> None:
        llm = SupervisorLLM(selected=["简历诊断师"])
        result = await _supervisor(llm).run("问题")
        assert isinstance(result, AgentRunResult)
        assert result.answer == llm._synthesis
        assert result.steps_used == 1  # 成功的专家数
        assert len(result.tool_calls) == 1

    async def test_router_called_once(self) -> None:
        llm = SupervisorLLM(selected=["简历诊断师", "岗位分析师"])
        await _supervisor(llm).run("问题")
        assert llm.routed == 1
        assert llm.synthesis_calls == 1


# ============================================================
# 专家定义
# ============================================================
class TestSpecialists:
    def test_default_specialists_are_distinct(self) -> None:
        names = [s.name for s in DEFAULT_SPECIALISTS]
        assert len(names) == len(set(names)), "专家名重复会让路由指向不确定"

    def test_descriptions_state_when_to_use(self) -> None:
        """描述是**路由依据**，必须写"什么时候该找它"，而不是"它擅长什么"。

        前者能直接支撑判断，后者要靠主管自己脑补。
        """
        for s in DEFAULT_SPECIALISTS:
            assert "当" in s.description or "找它" in s.description, (
                f"专家「{s.name}」的描述没有说明何时使用：{s.description}"
            )

    def test_system_prompts_are_non_trivial(self) -> None:
        """系统提示词太短等于没有 —— 专家就没法和普通对话区分开。"""
        for s in DEFAULT_SPECIALISTS:
            assert len(s.system_prompt) > 100, f"专家「{s.name}」的提示词过短"

    async def test_custom_specialists(self) -> None:
        custom = [
            Specialist(
                name="唯一专家",
                description="当需要测试时找它",
                system_prompt="你负责测试。" * 20,
            )
        ]
        llm = SupervisorLLM(route_raw='{"specialists": ["唯一专家"]}')
        events = [e async for e in _supervisor(llm, specialists=custom).run_stream("q")]
        assert [e.specialist for e in events if e.type is EventType.DELEGATE] == ["唯一专家"]
