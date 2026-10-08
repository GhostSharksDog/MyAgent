"""目录选择 seam：把"打开文件夹"变成一次**真正的系统对话框**。

《为什么浏览器做不到，而宿主进程做得到》

网页永远拿不到服务端的绝对路径 —— 这是浏览器的隐私设计，不是实现难度：

    `<input type="file" webkitdirectory>` 弹出的**就是**系统文件夹选择器，
    但每个文件只带一个 `webkitRelativePath`（形如 `MyAgent/src/App.tsx`），
    **绝对路径被浏览器剥掉了**。File System Access API 也一样，
    它给的是目录 handle，`handle.name` 是名字、不是路径。

所以"打开一个文件夹"这件事，只有**跑在用户这台机器上的宿主进程**能做完 ——
它就在文件系统旁边，可以让操作系统弹自己的对话框，然后把路径交给网页。

这条 seam 复刻的就是 DSH（本项目的 Agent 运行环境）的做法：
宿主进程持有 `directoryPicker` 能力对象，后端有两种，**交互方式不同而不只是
实现不同**。

《为什么要有两个后端，而不是一个"最好的"实现》

    native —— 宿主进程弹出**系统**对话框，一次点击直接拿到绝对路径。
              前提是"操作者就坐在这台机器的屏幕前"。
    browse —— 应用内浏览目录（`app/api/files.py` 的 `/browse`）。
              它到处都能用，因为不需要宿主有屏幕 ——
              但要绕一圈：浏览器给不出路径，所以只能靠"用户点选目录名"。

两者不是新旧关系，而是**适用条件不同**。远程浏览器访问时 native 根本不成立
（对话框会开在一台没人的服务器屏幕上），而本地使用时 browse 又明显更笨拙。
消费方按 `capability().kind` 分支：**不认识的 kind 就不显示目录选择入口，
而不是让它失败** —— 用户看不到入口，好过点了之后收到一个错误。

《为什么这个判断必须在启动时采样一次》

能力对象在服务生命周期内保持稳定：挂载哪个交互不该在"用户点下去的那一刻"
才变。否则会出现"上次能弹对话框、这次不能"的随机体验，而原因（绑定地址、
SSH 会话）用户完全看不出来。采样一次，然后把结论固定下来。

《和 DSH 的对应关系》

| DSH | 这里 |
|---|---|
| `dsh-host-directory-picker-native` | `NativeDirectoryPicker` |
| `dsh-host-directory-picker-browse` | `BrowseDirectoryPicker` |
| `dsh-host-directory-picker-auto` | `resolve_directory_picker_backend()` |
| `host.pickDirectory` / `host.listDirectory` | `POST /api/files/pick` / `GET /api/files/browse` |
| `directory-picker-unavailable` | `DirectoryPickerUnavailable` |
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from app.core.config import Settings, get_settings
from app.desktop.entry import PICKER_ARGUMENT

logger = logging.getLogger(__name__)

PickerKind = Literal["native", "browse"]

# 对话框 worker 的路径。用相对 `__file__` 而不是当前工作目录 ——
# 后者会随"从哪个目录启动"变化，是"本地能跑、换目录就找不到文件"的经典来源。
_WORKER_PATH = Path(__file__).with_name("picker_worker.py")


# ============================================================
# 数据模型
# ============================================================
@dataclass(frozen=True)
class PickerCapability:
    """宿主能提供哪种目录选择交互。

    `detail` 是给**人**看的一句话，说明"为什么是这一种"。它是这条 seam 里
    最容易被省掉、却最该留下的东西：用户看到"没有系统对话框"时的第一个
    问题就是"为什么"，而答案是机器判定出来的（绑定地址/SSH/显示会话），
    不写出来就只能靠猜。
    """

    kind: PickerKind
    detail: str = ""


@dataclass(frozen=True)
class PickOutcome:
    """一次目录选择的结果。三种状态互斥：选中 / 取消 / 出错。"""

    path: str | None = None
    cancelled: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None and not self.error


class DirectoryPickerUnavailable(RuntimeError):
    """当前后端不提供这个交互（例如在 browse 后端上调 `pick()`）。"""

    code = "directory-picker-unavailable"


class DirectoryPickerBusy(RuntimeError):
    """已经有一个对话框开着。"""

    code = "directory-picker-busy"


@dataclass(frozen=True)
class PickerFacts:
    """启动时采样一次的宿主事实。

    【为什么把这些做成入参而不是到处读全局】
    判定逻辑因此成了一个**纯函数**（见 `resolve_directory_picker_backend`），
    可以对它写一张真值表。若它直接去读 `os.environ` 和 `sys.platform`，
    测它就只能在真实环境里测 —— 而"远程浏览器访问时不该弹对话框"这条
    恰恰是本地最难复现的情况。
    """

    bind_host: str
    platform: str
    env: Mapping[str, str] = field(default_factory=dict)
    # Linux 上的对话框程序（zenity / kdialog）绝对路径，没有则为空
    linux_chooser: str = ""


# ============================================================
# 判定：纯函数，一张真值表
# ============================================================
def resolve_directory_picker_backend(facts: PickerFacts) -> PickerKind:
    """从宿主事实判定该挂哪个后端。

    三个条件缺一不可，且**每一个都对应一种"对话框弹了但你看不到"的真实场景**：

    1. **只监听回环地址**。绑在 `0.0.0.0` 时浏览器可能来自别的机器，
       而任何系统对话框都只会开在宿主屏幕上 —— 用户在那台机器上等一个
       永远不会出现的窗口。
    2. **不是 SSH 会话**。端口转发下服务跑在远端，对话框会开在那台
       无人值守的服务器上。
    3. **有可用的显示会话**。macOS/Windows 视为成立；Linux 需要
       `DISPLAY`/`WAYLAND_DISPLAY`，并且 PATH 上有 zenity 或 kdialog
       （否则没有"能程序化驱动的对话框程序"）。

    任何含糊的情形都判成 `browse` —— 它到处都能用。**判错方向的代价不对称**：
    该 native 却给了 browse，用户体验差一点但功能可用；
    该 browse 却给了 native，用户点了之后什么都没有发生。
    """
    if facts.bind_host != "127.0.0.1":
        return "browse"
    env = facts.env or {}
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return "browse"
    if facts.platform in ("win32", "darwin"):
        return "native"
    if facts.platform != "linux" or not facts.linux_chooser:
        return "browse"
    return "native" if (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")) else "browse"


def describe_backend(facts: PickerFacts, kind: PickerKind) -> str:
    """把判定结果翻译成一句人能看懂的理由。"""
    env = facts.env or {}
    if kind == "native":
        if facts.platform == "linux":
            return (
                f"宿主是本机图形会话（{Path(facts.linux_chooser).name}），可以直接弹出系统对话框。"
            )
        return "服务只监听本机地址，且宿主有图形界面 —— 可以直接弹出系统对话框。"

    if facts.bind_host != "127.0.0.1":
        return (
            f"服务绑定在 {facts.bind_host}，浏览器可能来自其他机器，"
            "系统对话框会开在你看不到的屏幕上 —— 改用应用内浏览目录。"
        )
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return "检测到 SSH 会话：对话框会开在远端服务器上，改用应用内浏览目录。"
    if facts.platform == "linux" and not facts.linux_chooser:
        return "Linux 上没有找到 zenity 或 kdialog（无法程序化弹出对话框），改用应用内浏览目录。"
    if facts.platform == "linux":
        return "Linux 上没有图形显示会话（DISPLAY/WAYLAND_DISPLAY），改用应用内浏览目录。"
    return "当前平台没有可用的系统对话框实现，改用应用内浏览目录。"


# ============================================================
# runner：怎么把"一次对话框"跑出来
# ============================================================
class ChooserRunner(ABC):
    """跑一次对话框并返回结果。

    抽成接口是为了**可测**：真实的 runner 会弹窗、要人点，测试里不可能用。
    注入一个假的 runner，就能把"取消 / 选到路径 / 进程挂了"三条路径全测到。
    """

    @abstractmethod
    def run(self) -> PickOutcome: ...


class ComDialogRunner(ChooserRunner):
    """Windows：开一个子进程跑 COM `IFileOpenDialog`。

    见 `picker_worker.py` 的模块文档 —— 为什么必须开子进程（DPI 感知、
    模态阻塞、崩溃隔离）。这里只负责进程与结果通道。
    """

    def __init__(
        self,
        worker_path: Path = _WORKER_PATH,
        title: str = "选择工作区文件夹",
        python: str | None = None,
    ) -> None:
        self._worker_path = worker_path
        self._title = title
        # 解释器可注入：唯一能造出"启动失败"的办法就是给它一个不存在的程序，
        # 而那正是最需要被覆盖的一条分支（真出事时它就是唯一的线索）。
        self._python = python or sys.executable
        self._frozen = python is None and getattr(sys, "frozen", False)

    def _command(self, out_file: Path) -> list[str]:
        if self._frozen:
            # sys.executable 在发行包中是 Legacy.exe，不能再用它执行 .py 路径。
            return [self._python, PICKER_ARGUMENT, str(out_file), self._title]
        return [self._python, str(self._worker_path), str(out_file), self._title]

    def run(self) -> PickOutcome:
        out_dir = Path(tempfile.mkdtemp(prefix="legacy-picker-"))
        out_file = out_dir / "result.json"
        try:
            try:
                proc = subprocess.Popen(
                    self._command(out_file),
                    # 不用管道：子进程的输出我们一个字都不需要，
                    # 而管道在受限环境里可能根本建不起来。DEVNULL 两边都省事。
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    # 【为什么**不**加 CREATE_NO_WINDOW】
                    # 试过，然后去掉了。它看着很对（"别让控制台闪一下"），
                    # 但它隐含 STARTF_USESHOWWINDOW + SW_HIDE，而进程**第一次**
                    # ShowWindow 会被这个值覆盖 —— 也就是说它可能把我们要弹的
                    # **对话框本身**一起隐藏掉。
                    # 代价对比很清楚：控制台闪一下是观感问题，对话框不出现是功能坏了。
                    # 实际上服务进程自己有控制台（worker 继承它），根本不会闪。
                )
            except OSError as exc:
                return PickOutcome(error=f"无法启动目录选择进程：{exc}")

            # 用户可能对着对话框想很久 —— 这里**不设超时**，
            # "没选完"和"卡住了"在外部无法区分，而误杀一个正开着的对话框
            # 比多等一会儿糟糕得多。
            code = proc.wait()

            if not out_file.exists():
                hint = (
                    "请从托盘退出后重新启动 Legacy；仍失败时可在页面手动填写目录。"
                    if self._frozen
                    else f'可手动复现：python "{self._worker_path}" "<结果文件.json>"'
                )
                return PickOutcome(
                    error=f"目录选择进程异常退出（退出码 {code}），没有返回结果。" + hint
                )

            try:
                payload = json.loads(out_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return PickOutcome(error=f"无法解析目录选择结果：{exc}")

            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("error", ""), str)
                or not isinstance(payload.get("cancelled"), bool)
                or (payload.get("path") is not None and not isinstance(payload["path"], str))
            ):
                return PickOutcome(error="无法解析目录选择结果：格式无效。请重试或手动填写目录。")
            error = str(payload.get("error") or "")
            if error:
                return PickOutcome(error=error)
            if code != 0:
                return PickOutcome(
                    error=f"目录选择进程异常退出（退出码 {code}）。请重试或手动填写目录。"
                )
            path = payload.get("path")
            if payload["cancelled"] and not path:
                return PickOutcome(cancelled=True)
            if not path or payload["cancelled"]:
                return PickOutcome(
                    error="无法解析目录选择结果：缺少选中目录。请重试或手动填写目录。"
                )
            return PickOutcome(path=str(path))
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


class CommandChooserRunner(ChooserRunner):
    """macOS / Linux：调平台上现成的对话框程序。

    与 DSH 的做法一致：macOS 用 `osascript` 的 `choose folder`，
    Linux 用 zenity（回退 kdialog）。它们都是"退出码 0 = 选中，1 = 用户取消"，
    选中目录的路径打在 stdout 上。
    """

    def __init__(self, argv: Sequence[str], platform: str) -> None:
        self._argv = list(argv)
        self._platform = platform

    @staticmethod
    def for_platform(
        platform: str, chooser: str, title: str = "选择工作区文件夹"
    ) -> CommandChooserRunner:
        if platform == "darwin":
            script = f'POSIX path of (choose folder with prompt "{title}")'
            return CommandChooserRunner(["osascript", "-e", script], platform)
        return CommandChooserRunner(
            [chooser or "zenity", "--file-selection", "--directory", f"--title={title}"],
            platform,
        )

    def run(self) -> PickOutcome:
        try:
            completed = subprocess.run(
                self._argv,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return PickOutcome(error=f"无法启动目录选择程序（{self._argv[0]}）：{exc}")

        # 三个程序在"用户取消"上都返回 1。这是它们的约定，
        # 而不是"某个错误码碰巧是 1"—— 真正的执行失败会直接抛 OSError 走上面那条路。
        if completed.returncode == 1:
            return PickOutcome(cancelled=True)
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            reason = detail[-1] if detail else f"退出码 {completed.returncode}"
            return PickOutcome(error=f"目录选择程序失败：{reason}")

        line = next((ln.strip() for ln in (completed.stdout or "").splitlines() if ln.strip()), "")
        return PickOutcome(path=line) if line else PickOutcome(cancelled=True)


# ============================================================
# 两个后端
# ============================================================
class DirectoryPicker(ABC):
    @abstractmethod
    def capability(self) -> PickerCapability: ...

    @abstractmethod
    async def pick(self) -> PickOutcome: ...


class NativeDirectoryPicker(DirectoryPicker):
    """系统对话框后端。"""

    def __init__(self, runner: ChooserRunner, detail: str = "") -> None:
        self._runner = runner
        self._detail = detail
        # 用 threading.Lock（而不是 asyncio.Lock）做"同一时刻只开一个对话框"的
        # 守卫：它没有事件循环亲和性，不会因为换了一个 loop（比如测试里
        # 每个用例一个新 loop）而报 "attached to a different loop"。
        self._busy = threading.Lock()

    def capability(self) -> PickerCapability:
        return PickerCapability(kind="native", detail=self._detail)

    async def pick(self) -> PickOutcome:
        """弹出对话框并把结果等回来。

        【为什么非阻塞地抢锁】
        连点两下按钮就会开出两个对话框，第二个压在第一个上面，
        用户关掉一个之后发现"还有一个" —— 像是程序卡住了。
        前端会禁用按钮，但**界面层的防护不能当作唯一的防护**。
        """
        if not self._busy.acquire(blocking=False):
            raise DirectoryPickerBusy("已经有一个文件夹对话框打开了，请先完成或关闭它。")
        try:
            # 落在线程里：`subprocess` 与"等用户点完"都是阻塞的，
            # 直接 await 会占住事件循环（ruff 的 ASYNC 规则管这个）。
            return await asyncio.to_thread(self._runner.run)
        finally:
            self._busy.release()


class BrowseDirectoryPicker(DirectoryPicker):
    """应用内浏览后端：`pick()` 明确不可用。

    【为什么不"降级"成一个假的 pick()】
    让它返回一个错误字符串或空路径都会让调用方以为"调用成功了但没选到"，
    从而显示一个含糊的提示。**失败要失败得响亮**：调用方据此知道
    "这个能力不存在"，从而改为渲染浏览界面 —— 这正是 DSH 里
    `directory-picker-unavailable` 的作用。
    """

    def __init__(self, detail: str = "") -> None:
        self._detail = detail

    def capability(self) -> PickerCapability:
        return PickerCapability(kind="browse", detail=self._detail)

    async def pick(self) -> PickOutcome:
        raise DirectoryPickerUnavailable(
            "directory-picker-unavailable：当前后端是应用内浏览，没有系统对话框。"
            f"原因：{self._detail}"
        )


# ============================================================
# 装配
# ============================================================
def _linux_chooser() -> str:
    """在 PATH 上找一个能用的对话框程序（zenity 优先，回退 kdialog）。"""
    for name in ("zenity", "kdialog"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def sample_facts(settings: Settings, platform: str | None = None) -> PickerFacts:
    """从真实环境采样一次宿主事实。"""
    return PickerFacts(
        # 用**实际绑定地址**而不是"配置里写着的"：能绑到哪就代表谁能访问到它
        bind_host=settings.app_host,
        platform=platform or sys.platform,
        env=dict(os.environ),
        linux_chooser=_linux_chooser(),
    )


def build_directory_picker(settings: Settings, platform: str | None = None) -> DirectoryPicker:
    """按配置与宿主事实装配目录选择器。"""
    mode = settings.agent.directory_picker
    facts = sample_facts(settings, platform=platform)

    if mode == "auto":
        kind = resolve_directory_picker_backend(facts)
        detail = describe_backend(facts, kind)
    else:
        # 显式指定（native/browse）优先于自动判定：配置写了什么就给什么，
        # 否则这个配置项就没有"强制"的能力，而强制恰恰是它存在的理由
        # （比如你知道自己就在宿主屏幕前，而某个信号判错了）。
        kind = mode
        detail = "由 AGENT_DIRECTORY_PICKER 显式指定。"
        if kind != resolve_directory_picker_backend(facts):
            logger.warning(
                "目录选择器被显式指定为 %s，但按宿主事实自动判定会选 %s —— "
                "若对话框开在你看不到的屏幕上，删掉 AGENT_DIRECTORY_PICKER 即可回到自动判定。",
                kind,
                resolve_directory_picker_backend(facts),
            )

    if kind == "native":
        if facts.platform == "win32":
            runner: ChooserRunner = ComDialogRunner()
        else:
            runner = CommandChooserRunner.for_platform(facts.platform, facts.linux_chooser)
        logger.info("目录选择器：native（%s）", detail)
        return NativeDirectoryPicker(runner, detail=detail)

    logger.info("目录选择器：browse（%s）", detail)
    return BrowseDirectoryPicker(detail=detail)


@lru_cache(maxsize=1)
def get_directory_picker() -> DirectoryPicker:
    """进程内单例。

    【为什么必须缓存而不是每次请求现算】
    采样结果关乎"用户在界面上看到哪种交互" —— 每次现算的话，绑定地址或
    环境变量一变，交互就在两次点击之间换了 —— 用户会认为是随机故障。
    **能力声明必须比"一次点击"活得久。**
    """
    return build_directory_picker(get_settings())


def reset_directory_picker() -> None:
    """丢弃缓存。设置变更后调用（见 `app/api/settings.py` 的 `_apply`）。"""
    get_directory_picker.cache_clear()
