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


# ============================================================
# 目录选择器 —— 一条**与文件读取不同**的权限
# ============================================================
class BrowseEntry(BaseModel):
    name: str
    path: str
    # 子项数量。用来帮用户确认"就是这一层"，而**不暴露文件名**
    child_count: int = 0


class BrowseListing(BaseModel):
    """目录选择器的返回。

    【为什么需要它，以及它为什么是一条独立的权限】

    浏览器里**没有服务端的文件夹对话框** —— `<input type="file" webkitdirectory>`
    只拿到文件、拿不到路径（这是浏览器的安全设计，改不了）。
    所以"打开文件夹作为工作区"这件事，只能由**服务端列出目录**来支持。

    但这里有个绕不开的矛盾：要选工作区，就必须能浏览工作区**之外**的目录 ——
    否则用户永远只能选当前工作区里面的文件夹，那是循环依赖。

    所以这一条是**刻意放宽的、且刻意收窄的**：

        放宽：可以列出任意绝对路径下的**目录结构**
        收窄：**只列目录，不列文件、不读内容**

    即"选择工作区需要看到目录名，但不需要看到文件内容"。
    这条界可以一句话说清，而一句话说不清的权限迟早会被误用。

    【为什么连文件名都不给】
    给了文件名就等于给了半份文件系统索引 —— 用户能从名字推断出
    `工资表.xlsx`、`身份证扫描件.jpg` 这类信息。而**目录名对定位已经足够**：
    你找项目靠的是 `WXP/简历/MyAgent` 这串路径，不是里面的文件叫什么。

    【为什么不上"管理员开关"】
    有一个想法是"加个开关，开了才能浏览"——但那样用户为了选工作区
    还得先去找开关，而开关一旦打开就再也没人关。
    **用"能做什么"划界，比用"开没开"划界更稳。**
    """

    path: str
    parent: str | None = None
    entries: list[BrowseEntry] = Field(default_factory=list)
    # Windows 上从空路径开始会返回盘符列表
    roots: list[BrowseEntry] = Field(default_factory=list)


@router.get("/browse", response_model=BrowseListing, summary="浏览目录（选择工作区用）")
async def browse(
    path: str = Query(default="", description="绝对路径；留空则列出根位置"),
) -> BrowseListing:
    """列出某个目录下的**子目录**，供用户挑选工作区根目录。

    见 `BrowseListing` 的说明：这里可以走出工作区（否则选不了），
    但只返回目录名，绝不返回文件名或内容。

    文件系统操作全部在 `_browse_sync` 里做、并丢进线程 ——
    用户可能把这个指到一个网络盘，那时 `iterdir()` 会阻塞好几秒，
    而阻塞的是整个事件循环。**用户提供的路径是不可信输入，
    拿它做文件系统操作之前应当假定它会慢。**
    """
    import asyncio

    return await asyncio.to_thread(_browse_sync, path)


def _browse_sync(path: str) -> BrowseListing:
    raw = (path or "").strip()

    # 留空：Windows 给盘符，其它平台给用户主目录。
    # 不给"整个文件系统根"是因为那上面挂着一堆对用户无意义的挂载点。
    if not raw:
        roots: list[BrowseEntry] = []
        if Path("C:/").exists():
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                drive = Path(f"{letter}:/")
                if drive.exists():
                    roots.append(BrowseEntry(name=f"{letter}:", path=str(drive)))
        if not roots:
            home = Path.home()
            roots.append(BrowseEntry(name=home.name or "/", path=str(home)))
        return BrowseListing(path="", parent=None, roots=roots)

    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="请给出绝对路径（目录选择器需要从根开始定位）")
    try:
        target = target.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法解析该路径：{exc}") from exc

    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"目录不存在：{target}")

    try:
        children = sorted(
            (p for p in target.iterdir() if _safe_is_dir(p) and not p.name.startswith(".")),
            key=lambda p: p.name.lower(),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"没有权限读取该目录：{target}") from exc
    except OSError as exc:
        raise HTTPException(status_code=403, detail=f"无法读取该目录：{exc}") from exc

    entries = [
        BrowseEntry(name=child.name, path=str(child), child_count=_count_children(child))
        for child in children[:500]
    ]

    parent = target.parent
    return BrowseListing(
        path=str(target),
        parent=(str(parent) if parent != target else None),
        entries=entries,
    )


def _safe_is_dir(p: Path) -> bool:
    """判断是否目录，且**吃掉权限异常**。

    一个受保护的子目录（比如 Windows 的 `System Volume Information`）
    会让整个列表崩掉 —— 而用户只是想在旁边选个文件夹。
    **一个坏条目不该让整次操作失败。**
    """
    try:
        return p.is_dir()
    except OSError:
        return False


def _count_children(p: Path) -> int:
    """数一下子项数量，给用户一个"这层有没有东西"的信号。

    只返回**数量**，不返回名字 —— 见 BrowseListing 的说明。
    """
    try:
        return sum(1 for _ in p.iterdir())
    except OSError:
        return 0


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
    import asyncio

    return await asyncio.to_thread(_workspace_info_sync)


def _workspace_info_sync() -> WorkspaceInfo:
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
    import asyncio

    return await asyncio.to_thread(_list_sync, path, include_hidden)


def _list_sync(path: str, include_hidden: bool) -> DirListing:
    _require_root()
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
        # 用 _require_root() 现取一次，而不是从外层闭包捕获 ——
        # 抽成同步函数之后外层已经不再持有 root 变量
        can_go_up=target != _require_root(),
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
    import asyncio

    cap = max_chars or get_settings().agent.file_max_chars
    return await asyncio.to_thread(_content_sync, path, cap)


def _content_sync(path: str, cap: int) -> FileContent:
    _require_root()
    settings = get_settings()
    try:
        target = _resolve(path)
        _check_secret(target, allow=settings.agent.profile == "jobhunt")
    except FileAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"{path!r} 不是文件")

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
