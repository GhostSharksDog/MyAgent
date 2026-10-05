"""模型管理接口：保存多个供应商配置、随时切换当前使用的那一个。

《为什么需要它，而不是"把 base_url 填在设置里"就够》

设置界面原来有四个字段（地址 / 模型名 / 密钥 / 温度），一次只能配一个。
而真实使用是**多目标**的：日常用便宜的、难的问题切到强的、
公司内网走自建网关、断网时切本地 Ollama。每换一次都要重抄地址与模型名，
密钥还要重新粘贴。

所以这里做的是"清单 + 激活"：
  · 清单（`data/models.json`）由 `app/llm/library.py` 管；
  · **激活**指的是把选中的那份写进 `.env`（与设置界面同一套写入逻辑），
    然后**重建 Agent 全栈** —— 否则会出现最糟的一种情况：
    界面显示"当前使用 X"，而进程里还在用 Y。

《为什么激活必须重建，而不能只清配置缓存》

`LLMClient` 在构造时就把配置**快照**进了 `self._s`，并把密钥写进了 httpx 的
`Authorization` 头。所以清 `get_settings` 缓存对它毫无影响 ——
进程里那个客户端仍然拿着旧地址、旧模型、旧密钥。
本项目的设置面板一直宣称"改完立即生效"（不需要重启），
而模型这一项在此之前**并没有真正做到**：改完要重启才生效。
这个接口顺带把它补上了（见 `_activate` 里的重建与旧客户端关闭）。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.agent.factory import build_agent_stack, mount_agent_stack
from app.api.settings import _apply, _mask, _write_env
from app.core.config import get_settings
from app.llm.library import ModelLibrary, SavedModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/models", tags=["models"])


def _library() -> ModelLibrary:
    """取模型库。

    【为什么做成函数而不是模块级常量】
    测试要能把它指到临时文件上；模块级常量一旦在导入时定死路径，
    测试就只能去动真实的 `data/models.json` —— 那是用户的文件。
    """
    return ModelLibrary()


# ============================================================
# 视图模型
# ============================================================
class ModelView(BaseModel):
    id: str
    label: str
    provider: str
    base_url: str
    model: str
    # 只回掩码，理由与 /api/settings 相同：界面需要知道"配没配"，不需要原文
    api_key_masked: str = ""
    api_key_set: bool = False
    temperature: float | None = None
    # 当前进程正在用的是不是这一条（按地址+模型名+密钥比对，不是按 label）
    active: bool = False


class ModelListResponse(BaseModel):
    models: list[ModelView]
    # 当前 `.env` 里的配置**没有**对应任何一条保存的模型时，为 True。
    # 界面据此显示"当前配置未保存为模型"并提供一键保存 ——
    # 而不是让用户看着一个不在清单里的配置发懵。
    current_unsaved: bool = False
    current: ModelView


class ModelSaveRequest(BaseModel):
    """新增/更新一条模型配置。

    全部字段可选：带 `id` 是更新，不带是新增。
    `extra="forbid"` 让字段名写错时当场 422，而不是被静默忽略
    （本项目在前后端字段名漂移上踩过坑，这条是那次留下的纪律）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    label: str = Field(min_length=1, max_length=60)
    provider: str = "custom"
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    # 更新时留空 = 不改动（与设置界面的密钥语义一致）
    api_key: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class ImportCurrentRequest(BaseModel):
    """ "把当前配置存为模型"的请求体：**只需要一个名字**。

    其余字段由服务端从 `.env` 读 —— 这正是这个接口存在的理由：
    密钥在界面上只有掩码，用户抄不出来。如果复用 `ModelSaveRequest`，
    用户就得把地址/模型名/密钥再填一遍，那正是它想省掉的事。
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=60)
    provider: str = "custom"


# ============================================================
# 当前配置 ↔ 清单
# ============================================================
def _current_as_model() -> SavedModel:
    """把 `.env` 里当前的 LLM 配置表示成一条（未保存的）模型。"""
    s = get_settings()
    return SavedModel(
        id="__current__",
        label="当前配置",
        base_url=s.llm.base_url,
        model=s.llm.model,
        api_key=s.llm.api_key.get_secret_value(),
        provider="custom",
        temperature=s.llm.temperature,
    )


def _to_view(saved: SavedModel, current: SavedModel) -> ModelView:
    return ModelView(
        id=saved.id,
        label=saved.label,
        provider=saved.provider,
        base_url=saved.base_url,
        model=saved.model,
        api_key_masked=_mask(saved.api_key),
        api_key_set=bool(saved.api_key),
        temperature=saved.temperature,
        active=saved.same_target_as(current),
    )


def _list_response() -> ModelListResponse:
    current = _current_as_model()
    saved = _library().load()
    return ModelListResponse(
        models=[_to_view(m, current) for m in saved],
        current_unsaved=bool(current.base_url)
        and not any(m.same_target_as(current) for m in saved),
        current=ModelView(
            id=current.id,
            label=current.label,
            provider=current.provider,
            base_url=current.base_url,
            model=current.model,
            api_key_masked=_mask(current.api_key),
            api_key_set=bool(current.api_key),
            temperature=current.temperature,
            active=True,
        ),
    )


# ============================================================
# 接口
# ============================================================
@router.get("", response_model=ModelListResponse, summary="列出已保存的模型")
async def list_models() -> ModelListResponse:
    """已保存的模型清单 + 当前实际生效的配置。

    `active` 是**算出来的**（比对地址、模型名、密钥），而不是存在文件里的一个
    `active_id`。理由：用户可以随时手改 `.env`，而存一个 id 就会在那种情况下
    说谎 —— 显示"当前使用 A"，实际用的是手改的 B。
    算出来的东西不会与事实不一致。
    """
    return _list_response()


@router.post("", response_model=ModelListResponse, summary="新增或更新一个模型")
async def save_model(payload: ModelSaveRequest) -> ModelListResponse:
    library = _library()
    existing = {m.id: m for m in library.load()}.get(payload.id or "")

    # 更新时 api_key 留空 = 不改动（否则用户改个名字就把密钥清掉了）
    api_key = payload.api_key if payload.api_key else (existing.api_key if existing else "")
    saved = SavedModel(
        id=payload.id or uuid.uuid4().hex[:12],
        label=payload.label.strip(),
        provider=payload.provider,
        base_url=payload.base_url.strip(),
        model=payload.model.strip(),
        api_key=api_key or "",
        temperature=payload.temperature,
    )
    library.upsert(saved)
    logger.info("模型已保存：%s（%s / %s）", saved.label, saved.base_url, saved.model)
    return _list_response()


@router.delete("/{model_id}", response_model=ModelListResponse, summary="删除一个模型")
async def delete_model(model_id: str) -> ModelListResponse:
    library = _library()
    if not any(m.id == model_id for m in library.load()):
        raise HTTPException(status_code=404, detail=f"没有 id 为 {model_id} 的模型")
    library.delete(model_id)
    return _list_response()


@router.post("/{model_id}/activate", response_model=ModelListResponse, summary="切换到某个模型")
async def activate_model(model_id: str, request: Request) -> ModelListResponse:
    """把选中的模型写进 `.env` 并**立即生效**（重建 Agent 全栈）。

    【为什么这一步必须重建而不是清缓存】
    `LLMClient` 构造时把配置快照进了自己（含 httpx 的 Authorization 头），
    清 `get_settings` 缓存对它没有任何影响。只清缓存的话，
    界面会说"当前使用 X"，而进程里还在用 Y —— 这正是最该避免的那种
    "配置看起来生效了"的假象。
    """
    model = next((m for m in _library().load() if m.id == model_id), None)
    if model is None:
        raise HTTPException(status_code=404, detail=f"没有 id 为 {model_id} 的模型")

    updates = {
        "LLM_BASE_URL": model.base_url.strip(),
        "LLM_MODEL": model.model.strip(),
    }
    if model.api_key:
        updates["LLM_API_KEY"] = model.api_key
    if model.temperature is not None:
        updates["LLM_TEMPERATURE"] = str(model.temperature)
    _write_env(updates)
    _apply()
    await _rebuild_stack(request)
    logger.info("已切换到模型：%s（%s / %s）", model.label, model.base_url, model.model)
    return _list_response()


async def _rebuild_stack(request: Request) -> None:
    """按新的配置重建 Agent 全栈，并关掉被替换下来的那个 LLM 客户端。

    【为什么要关旧的】
    `LLMClient` 内部持有 httpx 连接池。只换引用不关旧的，每切换一次模型
    就漏一个连接池 —— 切十次就是十个，而症状（句柄耗尽）要到很久以后才出现。

    【为什么是"先替换再关"】
    在途请求手上还拿着旧客户端的引用，先替换引用再关，能让它把这一个请求用完；
    反过来（先关再换）会让正在流式输出的对话当场断掉。
    仍然可能与新请求有一个极短的窗口重叠，但那是配置切换本身固有的，
    **总比"切了模型但进程还在用旧的"要好**。
    """
    old = mount_agent_stack(request.app, build_agent_stack(get_settings()))
    if old is not None:
        try:
            await old.aclose()
        except Exception:  # pragma: no cover - 关闭失败不该让切换失败
            logger.warning("关闭旧的 LLM 客户端失败（不影响切换）", exc_info=True)


@router.post("/import-current", response_model=ModelListResponse, summary="把当前配置存为模型")
async def import_current(payload: ImportCurrentRequest | None = None) -> ModelListResponse:
    """把 `.env` 里正在生效的配置保存进清单（给它起个名字）。

    【为什么需要它】
    用户可能先手改了 `.env`（或用了很久的默认 DeepSeek 配置），
    然后在界面上看到"当前配置未保存为模型"。没有这个入口，
    他只能把地址和密钥重新抄一遍 —— 而密钥在界面上**根本看不到**（只有掩码），
    等于抄不了。所以"保存当前"必须由服务端代劳。
    """
    current = _current_as_model()
    library = _library()

    # 已经存过就不要重复添加（判据同样是地址+模型名+密钥）
    if any(m.same_target_as(current) for m in library.load()):
        return _list_response()

    label = (payload.label if payload and payload.label else "") or current.model
    library.upsert(
        SavedModel(
            id=uuid.uuid4().hex[:12],
            label=label,
            provider=payload.provider if payload else "custom",
            base_url=current.base_url,
            model=current.model,
            api_key=current.api_key,
            temperature=current.temperature,
        )
    )
    return _list_response()
