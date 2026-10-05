"""文件浏览接口：给前端的工作区文件树与预览用。

【最重要的设计约束：安全边界必须复用，不能复制】

`app/tools/files.py` 里的 `_resolve()` 是整套路径防护的唯一入口 ——
它做三件必须按顺序做的事：拼路径 → **resolve 展开符号链接与 `..`** → 用
`is_relative_to` 校验仍在工作区内。

给前端加接口时最容易犯的错是"照着写一份类似的校验"：
两份实现迟早漂移，而漂移的那一份就是漏洞。所以这里**直接 import 复用**，
一个字符的校验逻辑都不重写。

【为什么不能只让 Agent 读文件、不给前端接口】

Agent 读文件是把内容塞进**提示词**，用户只能从句子里推断；
而"打开项目文件夹、预览文件"要的是**看得见**的浏览体验 ——
点开目录、看到文件名、点开文件看到内容与语法着色。

两者是不同用途：一个是给模型的上下文，一个是给人的界面。
但**权限边界必须是同一条**，否则界面就成了绕过模型侧限制的后门
（用户完全可以让 Agent 别读 .env，却自己在预览面板里点开它）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.tools.files import (
    FileAccessError,
    _check_secret,
    _rel,
    _resolve,
    workspace_root,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["files"])


class FileEntry(BaseModel):
    name: str
    # 相对工作区根的路径 —— 前端用它继续下钻
    path: str
    is_dir: bool
    size: int = 0


class DirListing(BaseModel):
    path: str
    entries: list[FileEntry] = Field(default_factory=list)
    truncated: bool = False
    # 是否还有上层目录可回退（根目录时 false）
    can_go_up: bool = False


class FileContent(BaseModel):
    path: str
    content: str
    size: int
    truncated: bool
    # 二进制文件不返回内容，只返回这个标记 —— 让前端显示"无法预览"
    is_binary: bool = False


class WorkspaceInfo(BaseModel):
    """工作区状态。前端据此决定显示文件树还是"去设置里配置"的引导。"""

    configured: bool
    root: str = ""
    reason: str = ""


def _require_root() -> Path:
    root = workspace_root()
    if root is None:
        raise HTTPException(
            status_code=409,
            detail="文件功能未启用。请在设置里指定「工作区根目录」后重试。",
        )
    return root


@router.get("/workspace", response_model=WorkspaceInfo, summary="工作区状态")
async def workspace_info() -> WorkspaceInfo:
    root = workspace_root()
    if root is None:
        return WorkspaceInfo(
            configured=False,
            reason="未配置工作区根目录。在设置里填一个目录即可启用文件浏览。",
        )
    return WorkspaceInfo(configured=True, root=str(root))


@router.get("/list", response_model=DirListing, summary="列出目录")
async def list_dir(
    path: str = Query(default=".", description="相对工作区根的目录路径"),
    include_hidden: bool = Query(default=False, description="是否显示隐藏文件"),
) -> DirListing:
    """列出目录内容。

    【为什么默认不显示隐藏文件】
    项目根目录里天然有一堆 `.git`、`.venv`、`.pytest_cache` ——
    它们对用户没有意义，却会把真正关心的源码挤出视野。
    默认隐藏、按需打开，是"默认值让最常见场景零配置可用"的又一例。
    """
    root = _require_root()
    try:
        target = _resolve(path)
    except FileAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"{path!r} 不是目录")

    limit = get_settings().agent.file_max_entries
    try:
        items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"无法读取该目录：{exc}") from exc

    if not include_hidden:
        items = [p for p in items if not p.name.startswith(".")]

    entries: list[FileEntry] = []
    truncated = False
    for p in items:
        if len(entries) >= limit:
            truncated = True
            break
        try:
            is_dir = p.is_dir()
            size = 0 if is_dir else p.stat().st_size
        except OSError:
            is_dir, size = False, 0
        entries.append(FileEntry(name=p.name, path=_rel(p), is_dir=is_dir, size=size))

    return DirListing(
        path=_rel(target),
        entries=entries,
        truncated=truncated,
        can_go_up=target != root,
    )


@router.get("/content", response_model=FileContent, summary="读取文件内容")
async def read_content(
    path: str = Query(description="相对工作区根的文件路径"),
    max_chars: int = Query(default=0, ge=0, description="0 = 用服务端默认上限"),
) -> FileContent:
    """读取文件内容用于预览。

    注意这里**复用了与 `read_file` 工具完全相同的那套校验**（_resolve +
    敏感文件黑名单）。这不是偷懒：如果界面走一套宽松的校验，
    用户就能在预览面板里看到 Agent 侧明确拒绝读取的 `.env` ——
    **同一个系统里两套权限判定，等于没有权限判定。**
    """
    _require_root()
    settings = get_settings()
    try:
        target = _resolve(path)
        _check_secret(target, allow=settings.agent.profile == "jobhunt")
    except FileAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"{path!r} 不是文件")

    cap = max_chars or settings.agent.file_max_chars
    try:
        size = target.stat().st_size
        raw = target.read_bytes()[: cap * 4]
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"无法读取该文件：{exc}") from exc

    # 二进制文件不返回内容：一段乱码对用户没有价值，而且会撑爆响应体
    if b"\x00" in raw[:4096]:
        return FileContent(
            path=_rel(target), content="", size=size, truncated=False, is_binary=True
        )

    text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > cap
    return FileContent(
        path=_rel(target),
        content=text[:cap],
        size=size,
        truncated=truncated,
    )
