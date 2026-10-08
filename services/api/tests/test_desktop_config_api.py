"""配置与 CRUD 使用临时文件，不联网、不覆盖开发者 .env。"""

from types import SimpleNamespace

import httpx
import pytest
from app.agent.sqlite_memory import SqliteLongTermMemory
from app.api import storage
from app.core.config import LLMSettings, MemorySettings, Settings
from app.session.store import InMemorySessionStore
from fastapi import FastAPI


@pytest.fixture
async def memory_api(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        llm=LLMSettings(_env_file=None, api_key=""),
        memory=MemorySettings(
            _env_file=None, enabled=True, backend="sql", facts_path=str(tmp_path / "facts.db")
        ),
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.sessions = InMemorySessionStore(ttl_seconds=0)
    app.state.long_term = SqliteLongTermMemory(settings.memory.facts_file)
    app.include_router(storage.router)

    async def current(_request):
        return SimpleNamespace(
            run_history=None,
            memory={
                "active_enabled": app.state.long_term.enabled,
                "active_backend": "sql",
                "enable_summary": True,
            },
        )

    monkeypatch.setattr(storage, "get_settings_view", current)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, app


async def test_memory_crud_disabled_retains_and_requires_clear_confirmation(memory_api):
    client, app = memory_api
    response = await client.post("/api/memory", json={"text": "用户偏好中文", "tags": ["语言"]})
    assert response.status_code == 200
    fact = response.json()
    assert (await client.get("/api/memory")).json()["facts"][0]["id"] == fact["id"]
    assert (
        await client.put(f"/api/memory/{fact['id']}", json={"text": "用户偏好简短中文"})
    ).status_code == 200
    assert SqliteLongTermMemory(app.state.long_term.path).facts[0].text == "用户偏好简短中文"
    app.state.long_term.enabled = False
    assert (await client.post("/api/memory", json={"text": "禁止新增"})).status_code == 409
    assert len((await client.get("/api/memory")).json()["facts"]) == 1
    assert (await client.delete("/api/memory")).status_code == 422
    assert (await client.delete("/api/memory?confirm=true")).status_code == 200
    assert SqliteLongTermMemory(app.state.long_term.path).facts == []
    assert (
        await client.put(f"/api/memory/{fact['id']}", json={"text": "不能复活"})
    ).status_code == 404


async def test_setup_is_unknown_until_read_and_never_tests_model(memory_api, monkeypatch):
    client, app = memory_api
    assert (await client.get("/api/setup")).json()["required"]
    operations = []

    async def update(payload, request):
        operations.append(payload.model_dump())
        request.app.state.settings.llm = LLMSettings(
            _env_file=None, api_key=payload.api_key, base_url=payload.base_url, model=payload.model
        )

    async def rebuild(request):
        operations.append("rebuilt")

    from app.api import models

    monkeypatch.setattr(storage, "update_settings", update)
    monkeypatch.setattr(models, "_rebuild_stack", rebuild)
    before = app.state.long_term
    response = await client.post(
        "/api/setup",
        json={
            "provider": "custom",
            "base_url": "http://127.0.0.1:9900/v1",
            "model": "public-model",
            "api_key": "synthetic-key",
        },
    )
    assert response.status_code == 200
    assert not response.json()["required"]
    assert len(operations) == 2
    assert app.state.long_term is before
    assert "synthetic-key" not in response.text
    assert (
        await client.post(
            "/api/setup", json={"base_url": "https://example.com?key=secret", "model": "x"}
        )
    ).status_code == 422


async def test_memory_duplicate_edit_and_database_error_are_actionable(memory_api, monkeypatch):
    client, app = memory_api
    first = (await client.post("/api/memory", json={"text": "第一条事实"})).json()
    await client.post("/api/memory", json={"text": "第二条事实"})
    assert (
        await client.put(f"/api/memory/{first['id']}", json={"text": "第二条事实"})
    ).status_code == 409

    def fail(*_args):
        raise RuntimeError("数据库不可写，请检查目录权限；不会退回内存存储。")

    monkeypatch.setattr(app.state.long_term, "remember", fail)
    response = await client.post("/api/memory", json={"text": "失败的事实"})
    assert response.status_code == 503 and "检查目录权限" in response.text
    assert len((await client.get("/api/memory")).json()["facts"]) == 2


def test_frozen_config_defaults_are_isolated(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("LLM_", "MEMORY_", "SESSION_", "TASK_", "RUN_HISTORY_", "MCP_"))
    }
    env["LEGACY_DATA_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    code = "import sys;sys.frozen=True;sys._MEIPASS='C:/read-only-resources';from app.core.config import *;from app.rag import corpus;from app.tools import builtin;s=get_settings();assert ENV_PATH.parent==DATA_ROOT;assert s.memory.enabled and s.memory.backend=='sql';assert s.session.backend=='sql' and s.session.ttl_seconds==0;assert s.tasks.backend=='memory';assert s.run_history.backend=='sql';assert not s.mcp.enabled;assert not s.llm.is_configured;assert DATA_ROOT.as_posix() in s.database_url;assert PROJECT_ROOT==RESOURCE_ROOT;assert corpus.DATA_DIR==DATA_ROOT;assert corpus.SEED_DIR==SEED_ROOT==builtin._SEED_DIR;print('isolated')"
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"
