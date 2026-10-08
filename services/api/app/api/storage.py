"""首次配置和本机记忆管理。保存不发送模型测试请求。"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.agent.factory import build_memories
from app.agent.memory import Fact
from app.api.settings import SettingsUpdate, get_settings_view, update_settings
from app.core.config import DATA_ROOT, DESKTOP

router = APIRouter(prefix="/api", tags=["storage"])


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = "custom"
    base_url: str = Field(min_length=1, max_length=2000)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(default="", max_length=4000)


class FactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=20)


def _memory(request: Request):
    memory = getattr(request.app.state, "long_term", None)
    if memory is None:
        settings = request.app.state.settings
        enabled = settings.model_copy(
            update={"memory": settings.memory.model_copy(update={"enabled": True})}
        )
        try:
            _, memory = build_memories(enabled, llm=request.app.state.llm)
        except (RuntimeError, OSError) as exc:
            raise HTTPException(503, str(exc)) from exc
        memory.enabled = settings.memory.enabled
        request.app.state.long_term = memory
    return memory


async def _change_memory(operation, *args):
    try:
        return await asyncio.to_thread(operation, *args)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/setup")
async def setup_status(request: Request):
    settings = request.app.state.settings
    return {
        "required": not settings.llm.is_configured,
        "desktop": DESKTOP,
        "data_directory": str(DATA_ROOT),
        "stores_locally": True,
        "session_persistent": request.app.state.sessions.backend
        in {"sqlite", "postgresql", "redis"},
    }


@router.post("/setup")
async def save_setup(payload: SetupRequest, request: Request):
    from urllib.parse import urlsplit

    from app.api.models import _rebuild_stack

    url = urlsplit(payload.base_url.strip())
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise HTTPException(422, "API 地址需要完整 http/https 地址，不能包含密钥、查询参数或账号。")
    if not payload.model.strip():
        raise HTTPException(422, "请填写模型名。")
    if not payload.api_key.strip() and not request.app.state.settings.llm.is_configured:
        raise HTTPException(422, "请填写 API Key；本地模型可填写其服务要求的占位密钥。")
    await update_settings(
        SettingsUpdate(base_url=payload.base_url, model=payload.model, api_key=payload.api_key),
        request,
    )
    await _rebuild_stack(request)
    return await setup_status(request)


@router.get("/storage")
async def storage_status(request: Request):
    settings = request.app.state.settings
    view = await get_settings_view(request)
    return {
        "desktop": DESKTOP,
        "data_directory": str(DATA_ROOT),
        "sessions": {
            "backend": request.app.state.sessions.backend,
            "persistent": request.app.state.sessions.backend in {"sqlite", "postgresql", "redis"},
            "ttl_seconds": settings.session.ttl_seconds,
        },
        "runs": view.run_history.model_dump() if view.run_history else None,
        "memory": view.memory,
    }


@router.get("/memory")
async def list_memory(request: Request):
    memory = _memory(request)
    return {
        "enabled": memory.enabled,
        "facts": [f.model_dump() for f in memory.facts],
        "max_facts": memory.max_facts,
        "backend": request.app.state.settings.memory.backend,
    }


@router.post("/memory", response_model=Fact)
async def add_memory(payload: FactInput, request: Request):
    memory = _memory(request)
    if not memory.enabled:
        raise HTTPException(409, "长期记忆已关闭，请先开启；已有数据仍保留。")
    if not payload.text.strip():
        raise HTTPException(422, "记忆内容不能为空。")

    def save():
        with memory._lock:
            memory.remember(payload.text, payload.tags)
            memory.save()
            return next(
                f for f in memory.facts if f.text.casefold() == payload.text.strip().casefold()
            )

    return await _change_memory(save)


@router.put("/memory/{fact_id}", response_model=Fact)
async def edit_memory(fact_id: str, payload: FactInput, request: Request):
    if not payload.text.strip():
        raise HTTPException(422, "记忆内容不能为空。")
    memory = _memory(request)
    try:
        changed = await _change_memory(memory.update_fact, fact_id, payload.text, payload.tags)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not changed:
        raise HTTPException(404, "这条记忆已删除，请刷新列表。")
    return next(f for f in memory.facts if f.id == fact_id)


@router.delete("/memory/{fact_id}")
async def delete_memory(fact_id: str, request: Request):
    if not await _change_memory(_memory(request).delete_fact, fact_id):
        raise HTTPException(404, "这条记忆已删除，请刷新列表。")
    return {"deleted": True}


@router.delete("/memory")
async def clear_memory(request: Request, confirm: bool = False):
    if not confirm:
        raise HTTPException(422, "清空记忆需要明确确认 confirm=true。")
    await _change_memory(_memory(request).clear_facts)
    return {"cleared": True}
