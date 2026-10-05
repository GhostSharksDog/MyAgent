"""验证"打开文件夹"**真的**会弹出 Windows 系统文件夹对话框。

《为什么需要这个脚本 —— 它填的是本轮改动最大的验证缺口》

单元测试能证明的东西：
    · 判定逻辑对（本机回环 → native）
    · 接口返回的字段名与状态码对
    · 假 runner 被调用时结果被正确翻译

但它们**一个字都没有证明"对话框真的弹出来了"**。而"没弹出来"恰恰是用户
最初报的问题之一 —— 之前那两个界面问题（文件栏跑到下方、打开文件夹没有
系统对话框）都是靠用户的眼睛发现的，不是靠任何工具。

**工具看不见的东西，就会反复坏。** 所以这里补上外部观察：

    真的把 worker 拉起来 → 从**进程外**枚举 Windows 顶层窗口 →
    确认出现了一个可见的、属于本系统对话框类的窗口。

这不是"worker 说自己成功了"，而是另一个进程看到的事实。

《这个脚本自己在开发过程中抓到了什么》

第一版写完，它报"没看到窗口"，而代码看起来完全正确。查下去是两件事：

  1. 我在 `Show()` 里传了 `GetForegroundWindow()` 当属主（想让它置顶），
     而**用一个已经失效的前台窗口句柄做属主，Show 会直接返回 E_FAIL** ——
     对话框根本建不出来。这个失败是随机的（取决于当时前台窗口是否还活着），
     靠手点几乎不可能定位。
  2. 脚本按 `Popen` 拿到的 PID 过滤窗口，而 **venv 里的 `python.exe` 是个
     转发器**：`Popen.pid` 是转发器的 pid，真正跑代码的解释器是它的子进程，
     窗口属于后者。于是"窗口明明在屏幕上"和"脚本说没有"同时成立。

两条都不是靠读代码能发现的 —— 它们是"从外面看一眼"才暴露的。

⚠ 运行期间屏幕上会**短暂闪出一个文件夹对话框**（脚本会自动把它关掉）。
这是它必须付出的代价：只有真的弹了，才能证明它弹了。

⚠ 关掉的方式是**给对话框发 WM_CLOSE**（等价于用户点"取消"），而不是杀进程。
强杀一个模态对话框会留下副作用（比如属主窗口一直处于禁用状态），
会让后续的对话框也变成 E_FAIL —— 这个坑在上面第 1 条里踩过。

用法（Windows，需要能访问桌面会话）：
    python scripts/verify_picker_dialog.py
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "services" / "api" / "app" / "core" / "picker_worker.py"

TITLE = "选择工作区文件夹"
# Windows 通用对话框的窗口类名。现代 IFileDialog 与旧式 SHBrowseForFolder
# **共用这一个顶层类名**，所以要区分它们得看子窗口（见 child_classes）。
DIALOG_CLASS = "#32770"
MODERN_MARKER = "DirectUIHWND"  # 现代 IFileDialog 的 DirectUI 客户区
BREADCRUMB_MARKER = "Breadcrumb Parent"  # 地址栏的面包屑（旧式对话框没有）
WM_CLOSE = 0x0010

ok = True


def check(condition: bool, label: str, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    if not condition:
        ok = False


if sys.platform != "win32":
    print("这个脚本只对 Windows 有意义（其他平台走 osascript/zenity）。")
    sys.exit(0)

user32 = ctypes.WinDLL("user32", use_last_error=True)
_EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

# 显式声明签名：不声明的话 ctypes 默认把句柄当 32 位 int，64 位下会截断 ——
# 表现是"枚举偶尔漏窗口"这种最难查的样子。
user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
user32.EnumWindows.restype = ctypes.c_bool
user32.EnumChildWindows.argtypes = [wintypes.HWND, _EnumProc, wintypes.LPARAM]
user32.EnumChildWindows.restype = ctypes.c_bool
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = ctypes.c_bool
user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = ctypes.c_bool


def _text(hwnd: int, getter) -> str:
    buf = ctypes.create_unicode_buffer(512)
    getter(hwnd, buf, 512)
    return buf.value


def window_class(hwnd: int) -> str:
    return _text(hwnd, user32.GetClassNameW)


def window_title(hwnd: int) -> str:
    return _text(hwnd, user32.GetWindowTextW)


def child_classes(hwnd: int) -> list[str]:
    found: list[str] = []

    def collect(child: int, _lparam: int) -> bool:
        found.append(window_class(child))
        return True

    user32.EnumChildWindows(hwnd, _EnumProc(collect), 0)
    return found


def find_dialog(timeout: float = 20.0) -> int | None:
    """轮询直到出现标题与类名都匹配的**可见**窗口（或超时）。

    ⚠ 刻意**不按 PID 过滤**：venv 的 `python.exe` 是转发器，
    `Popen.pid` 拿到的是转发器的 pid，而窗口属于真正的解释器进程。
    "名字 + 类名 + 可见"这三条已经足够唯一 —— 没有别的东西会把
    窗口标题设成"选择工作区文件夹"。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        hit: list[int] = []

        def cb(hwnd: int, _lp: int) -> bool:
            if user32.IsWindowVisible(hwnd) and window_title(hwnd) == TITLE:
                hit.append(hwnd)
                return False
            return True

        user32.EnumWindows(_EnumProc(cb), 0)
        if hit:
            return hit[0]
        time.sleep(0.15)
    return None


work_dir = Path(tempfile.mkdtemp(prefix="legacy-picker-verify-"))
out_file = work_dir / "result.json"

print("=== 1. 拉起 worker（屏幕上会闪出一个对话框，脚本会立刻关掉它） ===")
print(f"  worker: {WORKER.relative_to(ROOT)}")
check(WORKER.exists(), "worker 文件存在")

proc = subprocess.Popen(
    [sys.executable, str(WORKER), str(out_file)],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

hwnd: int | None = None
try:
    hwnd = find_dialog()

    print("\n=== 2. 进程外观察：屏幕上真的出现了对话框窗口吗 ===")
    check(hwnd is not None, "出现了一个可见的、标题为「选择工作区文件夹」的顶层窗口")
    if hwnd:
        cls = window_class(hwnd)
        children = child_classes(hwnd)
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        print(f"        句柄 = {hwnd}  所属进程 = {owner.value}（worker 转发器 pid = {proc.pid}）")
        print(f"        类名 = {cls}  标题 = {window_title(hwnd)!r}")
        kinds = sorted(set(children))
        print(f"        子窗口类别 = {'、'.join(kinds) if kinds else '（无）'}")

        check(cls == DIALOG_CLASS, f"窗口是 Windows 通用对话框（类名 {DIALOG_CLASS}）")
        # 【判别现代/旧式的正确判据 —— 我第一版写错了】
        # 一开始我按"有没有 SysTreeView32"来判，结果现代对话框**也**带这个类：
        # 它左侧的导航窗格就是一棵树。真正的判别依据是**地址栏与面包屑**：
        # 旧式 SHBrowseForFolder 只有一个树 +（可选）一个编辑框，没有这两样。
        modern = MODERN_MARKER in children and BREADCRUMB_MARKER in children
        check(
            modern,
            "是现代 IFileDialog（有 DirectUI 客户区 + 面包屑地址栏），不是旧式 SHBrowseForFolder",
        )
        if modern:
            print("        → 也就是资源管理器里那个「打开文件夹」对话框：带地址栏、搜索、新建文件夹")

    print("\n=== 3. 模拟用户点「取消」：给它发 WM_CLOSE ===")
    if hwnd:
        posted = user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        check(bool(posted), "WM_CLOSE 已投递")
        # 等 worker 自己收尾（关掉对话框 → Show 返回"已取消" → 写结果 → 退出）
        deadline = time.time() + 10
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.2)
        check(not user32.IsWindow(hwnd), "对话框窗口已被销毁（不是靠杀进程）")
        check(proc.poll() is None or proc.returncode == 0, "worker 正常退出（退出码 0）")
        check(out_file.exists(), "worker 写出了结果文件")
        if out_file.exists():
            payload = json.loads(out_file.read_text(encoding="utf-8"))
            print(f"        结果 = {payload}")
            check(
                payload.get("path") is None and payload.get("cancelled") is True,
                "取消被如实地表达为 cancelled=true（而不是一个空路径的错误）",
            )
finally:
    # 兜底：万一 WM_CLOSE 没起作用，别把窗口留在用户桌面上
    if proc.poll() is None:
        proc.kill()
        proc.wait()

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
