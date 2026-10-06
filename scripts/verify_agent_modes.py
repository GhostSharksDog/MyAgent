"""有硬请求数上限的真实编排验证；不写 .env，不读取私人语料。

python scripts/verify_agent_modes.py --live --output docs/evidence/agent-live.json
只在显式 --live 下联网。全部请求共用计数器，上限 30，输出上限 512，重试 0。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

import httpx  # noqa: E402
from app.agent.events import EventType  # noqa: E402
from app.agent.loop import Agent  # noqa: E402
from app.agent.multi import SupervisorAgent  # noqa: E402
from app.agent.planning import PlanAndExecuteAgent, Planner  # noqa: E402
from app.agent.runtime import RunContext  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.llm.types import ChatMessage, ChatResponse  # noqa: E402
from app.tools.base import ToolRegistry  # noqa: E402
from app.tools.builtin import CalculatorParams, _calculator  # noqa: E402
from app.tools.files import build_file_tools  # noqa: E402


class RequestCounter:
    def __init__(self, limit: int = 30) -> None:
        if not 1 <= limit <= 30:
            raise ValueError("request limit must be 1..30")
        self.limit = limit
        self.requests: list[dict[str, Any]] = []
        self.failed = False

    async def before_request(self, request: httpx.Request) -> None:
        if len(self.requests) >= self.limit:
            raise RuntimeError("真实验证累计请求额度已用完，不再发起请求")
        payload = json.loads(request.content)
        if payload.get("max_tokens", 513) > 512:
            raise RuntimeError("验证请求输出上限必须 <= 512")
        self.requests.append(
            {
                "number": len(self.requests) + 1,
                "stream": payload.get("stream", False),
                "max_tokens": payload["max_tokens"],
            }
        )

    async def after_response(self, response: httpx.Response) -> None:
        if response.is_error:
            self.failed = True


class VerificationClient(LLMClient):
    async def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        # JSON 指令仍在提示词中；去掉兼容性自动 fallback，避免 400 后额外重发。
        kwargs.pop("response_format", None)
        kwargs["max_tokens"] = 512
        return await super().chat(messages, **kwargs)


def env_hash() -> str:
    path = ROOT / ".env"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "absent"


async def verify(limit: int, selected_case: str | None = None) -> dict[str, Any]:
    initial_hash = env_hash()
    configured = get_settings()
    if not configured.llm.is_configured:
        raise RuntimeError("请先配置当前模型；此脚本不会修改 .env")
    llm_settings = configured.llm.model_copy(
        update={"max_tokens": 512, "max_retries": 0, "timeout": 30}
    )
    counter = RequestCounter(limit)
    report: dict[str, Any] = {
        "date": datetime.now(UTC).isoformat(),
        "model": llm_settings.model,
        "request_limit": limit,
        "max_output_tokens": 512,
        "automatic_retries": 0,
        "source": "synthetic public note in temporary workspace",
        "cases": [],
        "unverified": [],
    }
    cases = [
        ("react", "normal"),
        ("plan", "normal"),
        ("multi", "normal"),
        ("plan", "token_budget"),
        ("multi", "token_budget"),
        ("react", "timeout"),
        ("plan", "timeout"),
        ("multi", "timeout"),
        ("multi", "cancel"),
    ]
    if selected_case:
        cases = [(m, c) for m, c in cases if f"{m}/{c}" == selected_case]
        if not cases:
            raise ValueError("unknown verification case")
    with tempfile.TemporaryDirectory(prefix="agent-public-verify-") as workspace:
        await asyncio.to_thread(
            Path(workspace, "sample.md").write_text,
            "Public sample: batches contain 128 and 64 units.\n",
            encoding="utf-8",
        )
        # 仅本验证进程的环境；文件工具始终通过唯一配置入口读取临时工作区。
        overrides = {
            "AGENT_WORKSPACE_ROOT": workspace,
            "AGENT_CORPUS_PATHS": "",
            "AGENT_CORPUS_INCLUDE_SEED": "false",
            "AGENT_PROFILE": "general",
            "AGENT_FILE_WRITE_ENABLED": "false",
            "AGENT_FILE_ALLOW_SECRETS": "false",
        }
        previous = {key: os.environ.get(key) for key in overrides}
        os.environ.update(overrides)
        get_settings.cache_clear()
        try:
            tools = ToolRegistry()
            tools.register_fn("calculator", "精确核验算式", CalculatorParams, _calculator)
            for tool in build_file_tools():
                tools.register(tool)
            headers = {"Authorization": f"Bearer {llm_settings.api_key.get_secret_value()}"}
            async with httpx.AsyncClient(
                base_url=llm_settings.base_url,
                headers=headers,
                timeout=httpx.Timeout(30, connect=10),
                follow_redirects=False,
                event_hooks={
                    "request": [counter.before_request],
                    "response": [counter.after_response],
                },
            ) as http:
                client = VerificationClient(llm_settings, client=http)
                for index, (mode, case) in enumerate(cases):
                    if counter.failed or len(counter.requests) >= limit:
                        report["unverified"] = [f"{m}/{c}" for m, c in cases[index:]]
                        break
                    settings = get_settings().agent.model_copy(
                        update={
                            "max_steps": 2,
                            "run_timeout": 0.05 if case == "timeout" else 60,
                            "plan_max_total_tokens": 1 if case == "token_budget" else 60000,
                            "multi_max_total_tokens": 1 if case == "token_budget" else 80000,
                        }
                    )
                    agent = (
                        Agent(client, tools, settings)
                        if mode == "react"
                        else PlanAndExecuteAgent(
                            client,
                            tools,
                            settings,
                            planner=Planner(client, max_steps=2),
                            max_steps_per_step=2,
                            enable_replan=False,
                        )
                        if mode == "plan"
                        else SupervisorAgent(client, tools, settings, max_delegates=2)
                    )
                    count_before = len(counter.requests)
                    started = time.perf_counter()
                    prompt = "读取 sample.md 并用 calculator 核验 (128+64)*3，用中文给出简短结论。"
                    if case == "cancel":
                        context = RunContext.create(
                            settings, token_limit=settings.multi_max_total_tokens
                        )
                        source = agent.run_stream(prompt, run_context=context)
                        routed = asyncio.Event()

                        async def consume(events: Any, routed_event: asyncio.Event) -> None:
                            async for event in events:
                                if event.type is EventType.DELEGATE:
                                    routed_event.set()

                        consumer = asyncio.create_task(consume(source, routed))
                        try:
                            await asyncio.wait_for(routed.wait(), timeout=30)
                            # 路由结束后让消费任务继续一拍，专家已开始模型调用再取消。
                            await asyncio.sleep(0.05)
                        finally:
                            consumer.cancel()
                            await asyncio.gather(consumer, return_exceptions=True)
                            await source.aclose()
                        remaining = [
                            task
                            for task in asyncio.all_tasks()
                            if not task.done() and "run_one" in task.get_coro().__qualname__
                        ]
                        result = {
                            "stopped_reason": "cancelled",
                            "usage": context.usage.model_dump(),
                            "usage_complete": context.usage_complete,
                            "remaining_expert_tasks": len(remaining),
                            "passed": not remaining,
                            "note": "路由后等待 50ms 以启动专家，然后取消并等待清理",
                        }
                    else:
                        outcome = await agent.run(prompt)
                        result = {
                            "stopped_reason": outcome.stopped_reason,
                            "usage": outcome.usage.model_dump(),
                            "usage_complete": outcome.usage_complete,
                            "answer": outcome.answer[:1200],
                            "expected_reason": "finished" if case == "normal" else case,
                            "passed": outcome.stopped_reason
                            == ("finished" if case == "normal" else case),
                        }
                        if outcome.error and outcome.stopped_reason == "error":
                            # 不保存厂商返回的原始错误（可能包含敏感请求信息）。
                            result["error"] = "模型调用失败；详细原因见本机日志，后续验证停止"
                            counter.failed = True
                    result.update(
                        mode=mode,
                        case=case,
                        requests=len(counter.requests) - count_before,
                        seconds=round(time.perf_counter() - started, 3),
                    )
                    report["cases"].append(result)
                    print(
                        f"{mode}/{case}: {result['stopped_reason']}, requests={result['requests']}",
                        flush=True,
                    )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            get_settings.cache_clear()
    report.update(
        requests=counter.requests,
        request_count=len(counter.requests),
        env_unchanged=env_hash() == initial_hash,
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="显式授权真实模型调用")
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--case", help="只跑单个场景，如 multi/cancel；累计额度由操作者管理")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data" / "agent-live-verification.json"
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("真实验证必须显式传 --live；离线验证使用 pytest")
    if not 1 <= args.max_requests <= 30:
        parser.error("--max-requests 范围为 1..30")
    report = asyncio.run(verify(args.max_requests, args.case))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"actual requests: {report['request_count']}; env unchanged: {report['env_unchanged']}")
    return (
        0
        if not report["unverified"]
        and all(c.get("passed", True) for c in report["cases"])
        and report["env_unchanged"]
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
