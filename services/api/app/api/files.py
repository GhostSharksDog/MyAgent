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

【这个文件里同时存在两套"选目录"的机制，不是历史包袱】

    GET  /picker   问宿主：你能弹出系统对话框吗？（启动时采样一次的能力声明）
    POST /pick     能弹 —— 宿主进程弹系统对话框，一次点击直接拿到绝对路径
    GET  /browse   不能弹 —— 应用内浏览目录名（远程部署时的唯一选择）
    POST /locate   browse 的兜底：用浏览器给的文件夹名反查磁盘路径

它们的分工由 `capability().kind` 决定，前端只会渲染其中一种。
"两套机制"听起来像冗余，但它们是**两种适用条件不同的交互**：
宿主有屏幕时用第一种（准确、一步到位），没有屏幕时用第二种（到处能用）。
判断只在启动时做一次，见 app/core/directory_picker.py。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.directory_picker import (
    DirectoryPickerBusy,
    DirectoryPickerUnavailable,
    get_directory_picker,
)
from app.core.telemetry import METRICS
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
class PickerCapabilityView(BaseModel):
    """宿主能提供哪种"选目录"的交互。

    `kind=native` 时前端只渲染一个按钮（宿主进程弹**系统**对话框）；
    `kind=browse` 时前端渲染应用内浏览面板。两种交互长得完全不一样，
    所以前端必须先问、再决定渲染什么。
    """

    kind: str
    # 人话解释"为什么是这一种"（绑定地址 / SSH / 显示会话……）
    detail: str = ""


@router.get("/picker", response_model=PickerCapabilityView, summary="目录选择能力")
async def picker_capability() -> PickerCapabilityView:
    """告诉前端"打开文件夹"该渲染成哪种交互。

    【为什么让前端先问，而不是"点了再说"】
    让前端去试（先按有对话框渲染，失败再换）会把一个**启动时就确定的静态事实**
    变成一次失败的用户操作：用户点了一个按钮，什么也没发生。
    能力先声明出来，界面就能一开始就长对的样子，并且能顺手说明原因。
    """
    cap = get_directory_picker().capability()
    return PickerCapabilityView(kind=cap.kind, detail=cap.detail)


class PickRequest(BaseModel):
    """请求体刻意是空的。

    【为什么要一个 JSON 请求体，而不是一个无参 POST】
    `Content-Type: application/json` 会触发浏览器的 CORS 预检。第三方网页
    因此**无法**让用户的浏览器悄悄发出这次调用（预检会被 allow_origin_regex
    拒掉），而"无参 POST"属于简单请求、根本不预检。
    这个接口的副作用是在用户屏幕上弹出窗口 —— 该挡的正是"谁能让它弹"。
    """

    model_config = ConfigDict(extra="forbid")


class PickResponse(BaseModel):
    # 用户取消时为 None。前端据此区分"取消"（静默）与"出错"（要提示）
    path: str | None = None
    cancelled: bool = False
    hint: str = ""


@router.post("/pick", response_model=PickResponse, summary="弹出系统文件夹对话框")
async def pick_directory(payload: PickRequest) -> PickResponse:
    """让**宿主进程**弹出系统文件夹对话框，并把绝对路径带回来。

    ============================================================
    这是"打开文件夹"的唯一正解
    ============================================================
    网页永远拿不到绝对路径（`webkitdirectory` 只给文件夹名、
    File System Access API 只给 handle 的 name），所以能拿到路径的只有
    跑在用户这台机器上的进程。这条路一次点击就结束，不需要任何猜测。

    （同目录的 `/locate` 是**旧方案**：从浏览器对话框拿到文件夹名，
    再反查磁盘。它只在宿主弹不出对话框（browse 后端）时才作为兜底出现。）

    ============================================================
    这个请求是"用户节奏"的，不要给它加超时
    ============================================================
    同一份服务里其他接口都在几十毫秒内返回，而这个接口会一直挂着，
    直到用户点完对话框 —— 可能是几秒，也可能是几分钟（他去接了个电话）。
    **不能套用"超时就失败"的直觉**：误杀一个正开着的对话框，
    用户看到的是"我明明选好了，它却说失败了"。

    反过来，这里也**不需要**超时来防"卡死"：对话框是操作系统在管，
    用户随时可以点取消；真的卡住了，用户关掉它就行。
    """
    picker = get_directory_picker()
    cap = picker.capability()
    if cap.kind != "native":
        # 409 而不是 404：接口存在，只是这个部署没有这个能力。
        # 带上 reason，前端就能直接把原因显示给用户，而不是"未知错误"。
        raise HTTPException(
            status_code=409,
            detail=f"{DirectoryPickerUnavailable.code}：当前部署没有系统文件夹对话框。{cap.detail}",
        )

    logger.info("等待用户在宿主屏幕上选择目录……")
    started = time.perf_counter()
    try:
        outcome = await picker.pick()
    except (DirectoryPickerUnavailable, DirectoryPickerBusy) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    elapsed = time.perf_counter() - started
    result = "error" if outcome.error else ("cancelled" if outcome.cancelled else "selected")
    METRICS.inc("legacy_directory_pick_total", result=result)
    # 这个直方图量的是**用户思考时间**，不是服务耗时 ——
    # 它存在的意义是把"用户节奏"和"服务延迟"分开，免得有人看到
    # 这条曲线的 P99 是几十秒就以为服务出了问题。
    METRICS.observe("legacy_directory_pick_seconds", elapsed, result=result)

    if outcome.error:
        logger.warning("目录选择失败（%.1fs）：%s", elapsed, outcome.error)
        raise HTTPException(status_code=500, detail=f"系统对话框出错：{outcome.error}")
    if outcome.cancelled or not outcome.path:
        logger.info("用户取消了目录选择（%.1fs）", elapsed)
        return PickResponse(cancelled=True, hint="已取消选择，工作区未改变。")

    # 路径只记 debug：它往往含有用户名等个人信息，INFO 日志会被长期留存
    logger.info("用户选定了目录（%.1fs）", elapsed)
    logger.debug("选中的目录：%s", outcome.path)
    return PickResponse(path=outcome.path)


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


class LocateRequest(BaseModel):
    """用系统对话框选到的线索，请服务端反查绝对路径。"""

    name: str = Field(min_length=1, description="文件夹名（系统对话框能给出的只有这个）")
    samples: list[str] = Field(
        default_factory=list,
        description="相对于该文件夹的若干路径，如 'src/main.tsx'。用来在多个同名目录中确定是哪一个",
    )


class LocateCandidate(BaseModel):
    path: str
    matched: int = 0


class LocateResponse(BaseModel):
    candidates: list[LocateCandidate] = Field(default_factory=list)
    scanned: int = 0
    truncated: bool = False
    hint: str = ""


@router.post("/locate", response_model=LocateResponse, summary="按文件夹名反查绝对路径")
async def locate(payload: LocateRequest) -> LocateResponse:
    """用系统对话框选到的**文件夹名**，在磁盘上反查它的绝对路径。

    ============================================================
    为什么需要这么一个"绕一圈"的接口
    ============================================================
    浏览器**无法**打开一个返回服务端绝对路径的原生文件夹对话框 ——
    这是浏览器的隐私设计，不是实现偷懒：

        `<input type="file" webkitdirectory>` 弹的确实是**系统**文件夹选择器，
        但拿到的每个文件只有 `webkitRelativePath`（形如 `MyAgent/src/main.tsx`）——
        **绝对路径被浏览器剥掉了**，任何网页都拿不到。

    （有人会想用 File System Access API。它同样只给一个目录 handle，
    `handle.name` 是名字、不是路径，而且只有 Chromium 系支持。）

    所以能做的极限是：**从对话框拿到名字和结构，反过来在磁盘上找到它。**
    这条路可行，因为两者加起来基本能唯一定位：

        name    = "MyAgent"            → 先筛出所有叫这个的目录
        samples = ["src/App.tsx", ...] → 再验证哪些候选里真的有这些文件

    **名字用来筛选，结构用来确认。** 只看名字会在有多个同名目录时选错
    （比如 `node_modules/foo/` 和 `projects/foo/`），加上结构校验就能排除。

    ============================================================
    扫描的边界
    ============================================================
    只扫盘符根与用户主目录下的前几层，并且：
      · 跳过噪音目录（node_modules / .git / Windows / Program Files…）
      · 深度与访问数量都设上限
    **用户的工程目录不会埋在 10 层深的系统目录里**，而全盘递归会让这个
    操作从"等一秒"变成"等到放弃"。宁可扫不到（然后退回手动浏览），
    也不要转三十秒。
    """
    import asyncio

    return await asyncio.to_thread(_locate_sync, payload.name, payload.samples)


# 扫描时跳过的目录名。两类：**噪音**（对定位无意义且数量巨大）
# 与**系统目录**（用户的工程不可能在里面，但递归它们非常贵）。
_SKIP_DIRS = {
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".pnpm-store",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "$recycle.bin",
    "system volume information",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "appdata",
    "anaconda3",
    "miniconda3",
    "site-packages",
}

_MAX_DEPTH = 5
_MAX_VISITED = 20000


def _locate_sync(name: str, samples: list[str]) -> LocateResponse:
    target = name.strip()
    if not target:
        raise HTTPException(status_code=400, detail="文件夹名不能为空")

    samples = [s.replace("\\", "/").lstrip("./") for s in samples if s.strip()][:8]

    roots: list[Path] = []
    if Path("C:/").exists():
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            d = Path(f"{letter}:/")
            if d.exists():
                roots.append(d)
    if not roots:
        roots.append(Path("/"))

    candidates: list[LocateCandidate] = []
    visited = 0
    truncated = False

    for root in roots:
        if truncated:
            break
        # 逐层展开而不是递归 —— 便于在任何一层中断，且不会把栈压爆
        frontier: list[tuple[Path, int]] = [(root, 0)]
        while frontier:
            if visited >= _MAX_VISITED:
                truncated = True
                break
            current, depth = frontier.pop()
            if depth >= _MAX_DEPTH:
                continue
            try:
                children = list(current.iterdir())
            except (OSError, PermissionError):
                continue
            for child in children:
                visited += 1
                if visited >= _MAX_VISITED:
                    truncated = True
                    break
                try:
                    if not child.is_dir():
                        continue
                except OSError:
                    continue
                if child.name.lower() in _SKIP_DIRS or child.name.startswith("."):
                    continue

                if child.name == target:
                    matched = sum(1 for s in samples if _has_relative(child, s))
                    # 没有 samples 时按名字收；有 samples 时只收真的对得上的
                    if not samples or matched > 0:
                        candidates.append(LocateCandidate(path=str(child), matched=matched))
                frontier.append((child, depth + 1))

    # 匹配数多的排前面，其次路径短的（更靠近根 = 更可能是用户自己的工程）
    candidates.sort(key=lambda c: (-c.matched, len(c.path)))
    candidates = candidates[:20]

    hint = ""
    if not candidates:
        hint = (
            f"没有在常见位置找到名为「{target}」的文件夹。"
            "可能是它藏在更深的地方，或名字被浏览器改写过了 —— 用下面的目录浏览手动定位更稳妥。"
        )
    elif len(candidates) > 1:
        hint = "找到多个同名文件夹，请选择实际的那一个。"
    if truncated:
        hint += "（扫描量已达上限，结果可能不全）"

    return LocateResponse(candidates=candidates, scanned=visited, truncated=truncated, hint=hint)


def _has_relative(base: Path, rel: str) -> bool:
    """确认 base 下真的有这个相对路径。

    这是"用结构确认名字"的那一半：光凭名字在有多个同名目录时会选错，
    加上"它里面是否真的有 src/App.tsx"就能排除掉绝大多数误匹配。
    """
    try:
        return (base / rel).exists()
    except OSError:
        return False


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
