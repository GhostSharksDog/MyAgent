"""付费验证入口先离线验参数、输出和请求数，避免用真请求调试脚本。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from app.core.config import LLMSettings
from app.llm.client import LLMBadRequestError
from app.llm.types import ChatMessage

_spec = importlib.util.spec_from_file_location(
    "verify_agent_modes", Path(__file__).resolve().parents[3] / "scripts" / "verify_agent_modes.py"
)
assert _spec and _spec.loader
verification = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verification)


async def test_live_counter_rejects_extra_requests_before_transport() -> None:
    counter = verification.RequestCounter(1)
    transported = []

    async def handler(request: httpx.Request) -> httpx.Response:
        transported.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), event_hooks={"request": [counter.before_request]}
    ) as client:
        await client.post("https://example.invalid", json={"max_tokens": 512})
        with pytest.raises(RuntimeError, match="额度"):
            await client.post("https://example.invalid", json={"max_tokens": 512})
    assert len(transported) == len(counter.requests) == 1


async def test_live_client_disables_400_fallback_and_caps_output() -> None:
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "unsupported response_format"})

    async with httpx.AsyncClient(
        base_url="https://example.invalid/v1", transport=httpx.MockTransport(handler)
    ) as http:
        client = verification.VerificationClient(
            LLMSettings(_env_file=None, api_key="public-test", max_retries=0, max_tokens=512),
            client=http,
        )
        with pytest.raises(LLMBadRequestError):
            await client.chat(
                [ChatMessage.user("public")],
                response_format={"type": "json_object"},
                max_tokens=4096,
            )
    assert len(requests) == 1
    assert requests[0]["max_tokens"] == 512
    assert "response_format" not in requests[0]


async def test_all_live_cases_can_be_constructed_and_reported_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    from tests.test_agent_runtime import RuntimeLLM

    class FakeClient(RuntimeLLM):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(plan=0.1, route=0.1, synthesis=0.1, expert=0.1)

    configured = get_settings().model_copy(
        update={"llm": LLMSettings(_env_file=None, api_key="public-test")}
    )
    monkeypatch.setattr(verification, "VerificationClient", FakeClient)
    monkeypatch.setattr(verification, "get_settings", get_settings)
    monkeypatch.setenv("LLM_API_KEY", "public-test")
    get_settings.cache_clear()
    try:
        report = await verification.verify(30)
    finally:
        get_settings.cache_clear()
    assert configured.llm.is_configured
    assert len(report["cases"]) == 9
    assert not report["unverified"]
    assert all(case.get("passed", True) for case in report["cases"])
    assert report["env_unchanged"]
    assert report["request_count"] == 0
