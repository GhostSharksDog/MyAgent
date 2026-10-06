"""在真实三种编排与工具上注入合成 LLM；不创建网络客户端或读取用户配置。

这是独立评测进程的测试适配器。文件工具和 tokenizer 的依赖注入只在本进程
运行期间生效，不应被导入 Web 服务来执行。
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import patch

from pydantic import Field

from app.agent.loop import Agent
from app.agent.multi import SupervisorAgent
from app.agent.planning import PlanAndExecuteAgent
from app.core.config import AgentSettings
from app.evaluation.tasks import (
    Mode,
    RunBundle,
    StrictModel,
    TaskSuite,
    ToolObservation,
    TrialRecord,
)
from app.llm.types import ChatMessage, ChatResponse, Role, StreamDelta, ToolCall, Usage
from app.tools.base import ToolRegistry, ToolResult
from app.tools.builtin import CalculatorParams, _calculator
from app.tools.files import ReadFileTool


class FixtureCall(StrictModel):
    name: Literal["calculator", "read_file"]
    arguments: dict[str, Any]


class FixtureResponse(StrictModel):
    answer: dict[str, Any]
    calls: list[FixtureCall] = Field(default_factory=list)


class ObservedRegistry(ToolRegistry):
    """记录真实工具执行。Plan／Supervisor 的外层 SSE 不转发子 Agent 工具事件。"""

    def __init__(self) -> None:
        super().__init__()
        self.observations: list[ToolObservation] = []

    async def execute(self, call: ToolCall) -> ToolResult:
        result = await super().execute(call)
        self.observations.append(
            ToolObservation(
                tool_name=call.name,
                arguments=call.arguments,
                ok=result.ok,
                content=result.content,
                duration_ms=result.duration_ms,
            )
        )
        return result


class ScriptedLLM:
    """只读已声明 fixture；不读取评分规则，不实现真实推理。"""

    def __init__(self, fixture: FixtureResponse) -> None:
        self.fixture = fixture
        self.calls = 0

    @staticmethod
    def usage() -> Usage:
        return Usage(prompt_tokens=20, completion_tokens=10, total_tokens=30)

    async def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        self.calls += 1
        prompt = messages[-1].content or ""
        if "任务规划器" in prompt:
            value = {
                "reasoning": "合成规划，仅验证链路",
                "steps": [
                    {"id": 1, "description": "读取公开资料与核验约束", "expected": "事实"},
                    {"id": 2, "description": "整理为用户要求的 JSON", "expected": "结构化结论"},
                ],
            }
        elif "任务协调者" in prompt:
            value = {"specialists": ["资料分析员", "结果核验员"]}
        else:
            value = self.fixture.answer
        return ChatResponse(
            message=ChatMessage.assistant(json.dumps(value, ensure_ascii=False)),
            usage=self.usage(),
            model="scripted-fixture-v1",
        )

    async def stream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> AsyncIterator[StreamDelta]:
        self.calls += 1
        if self.fixture.calls and not any(message.role == Role.TOOL for message in messages):
            calls = [
                ToolCall(
                    id=f"fixture_{i}",
                    name=c.name,
                    arguments=c.arguments,
                    raw_arguments=json.dumps(c.arguments),
                )
                for i, c in enumerate(self.fixture.calls)
            ]
            yield StreamDelta(
                tool_call_deltas=[{"index": i, **c.to_wire()} for i, c in enumerate(calls)]
            )
            yield StreamDelta(finish_reason="tool_calls", usage=self.usage())
        else:
            yield StreamDelta(content=json.dumps(self.fixture.answer, ensure_ascii=False))
            yield StreamDelta(finish_reason="stop", usage=self.usage())


async def run_offline(
    suite: TaskSuite,
    fixtures: dict[str, FixtureResponse],
    *,
    modes: list[Mode],
    task_ids: list[str] | None = None,
    repetitions: int = 1,
) -> RunBundle:
    selected = task_ids if task_ids is not None else [task.id for task in suite.tasks]
    tasks = {task.id: task for task in suite.tasks}
    if set(selected) - tasks.keys() or set(selected) - fixtures.keys():
        raise ValueError("任务或合成输出缺失；不会自动从评分答案补齐")
    # Validate selection before creating any directories or invoking an Agent.
    bundle = RunBundle(
        suite_id=suite.id,
        suite_sha256=suite.sha256,
        source="synthetic",
        model="scripted-fixture-v1",
        modes=modes,
        task_ids=selected,
        repetitions=repetitions,
        records=[],
    )
    with tempfile.TemporaryDirectory(prefix="legacy-agent-eval-") as directory:
        for task_id in selected:
            task = tasks[task_id]
            workspace = Path(directory) / task_id
            workspace.mkdir()
            for name, content in task.assets.items():
                path = workspace / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
            # model_construct uses declared defaults without BaseSettings environment sources.
            settings = AgentSettings.model_construct(
                profile="general",
                workspace_root=str(workspace),
                max_steps=4,
                run_timeout=10,
                file_write_enabled=False,
                file_allow_secrets=False,
            )
            with (
                patch("app.tools.files.get_settings", return_value=SimpleNamespace(agent=settings)),
                patch("app.llm.tokens._encoder", return_value=None),
            ):
                for mode in modes:
                    for attempt in range(1, repetitions + 1):
                        llm = ScriptedLLM(fixtures[task_id])
                        tools = ObservedRegistry()
                        tools.register_fn(
                            "calculator", "精确核验算术表达式", CalculatorParams, _calculator
                        )
                        if task.assets:
                            tools.register(ReadFileTool())
                        agent = (
                            Agent(llm, tools, settings)
                            if mode == "react"
                            else PlanAndExecuteAgent(llm, tools, settings, max_steps_per_step=4)
                            if mode == "plan"
                            else SupervisorAgent(llm, tools, settings)
                        )
                        started = time.perf_counter()
                        events = [
                            e.model_dump(mode="json", exclude_none=True)
                            async for e in agent.run_stream(task.prompt)
                        ]
                        bundle.records.append(
                            TrialRecord(
                                task_id=task_id,
                                mode=mode,
                                attempt=attempt,
                                elapsed_seconds=time.perf_counter() - started,
                                llm_calls=llm.calls,
                                events=events,
                                tool_observations=tools.observations,
                            )
                        )
    return bundle
