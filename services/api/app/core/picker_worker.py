"""系统文件夹对话框 worker（Windows）：用 COM 的 `IFileOpenDialog` 弹一次原生选择器。

《为什么对话框不能直接开在服务进程里》—— 三个理由，每一个单独都足以成立：

1. **DPI 感知是进程级状态，而且必须在进程创建任何窗口之前设置。**

   API 服务在启动阶段早就建过窗口/消息队列，那时再调
   `SetProcessDpiAwarenessContext` 只会返回失败并被系统忽略。
   子进程是唯一能"干净地"设置它的地方。不设置的后果是：在高 DPI 屏上
   对话框被系统位图放大成糊的 —— 功能没坏，但一眼就能看出这东西不专业。
   （DSH 的 Windows 目录选择器同样是为了这件事才开子进程。）

2. **COM 模态对话框会一直占住调用它的线程，直到用户把它关掉。**
   放在服务进程里，就等于一个线程池线程被用户"发呆"占用若干分钟；
   隔离到子进程后，父进程只是等待，还能在需要时直接把它杀掉。

3. **原生代码的崩溃不该带走整个服务。** 子进程崩了，父进程只是拿到一个
   非零退出码，然后如实报错。

《为什么不把结果打在 stdout 上》

父进程用**文件通道**而不是管道读回结果。两个原因：

· 管道在受限/沙箱环境里可能不可用，而临时文件在任何环境都能用；
· 出问题时可人工复现：直接 `python app/core/picker_worker.py out.json`
  跑一遍，看它写出什么，比翻一段被缓冲吞掉的 stdout 快得多。

《为什么不用 tkinter》

`tkinter.filedialog.askdirectory` 只要三行，但它给的是**旧式**对话框
（没有地址栏、没有搜索、不能新建文件夹），而且 Tk 的解释器有自己的线程
模型。这里用 ctypes 直接驱动 `IFileOpenDialog` —— 也就是 Windows 自己
"打开文件夹"用的那个对话框，零第三方依赖。
"""

from __future__ import annotations

import ctypes
import json
import sys
import uuid
from pathlib import Path
from typing import Any

_IS_WINDOWS = sys.platform == "win32"

# ============================================================
# COM 样板：GUID 与虚函数槽位
# ============================================================
# 用显式位宽的 ctypes 类型而不是 `ctypes.wintypes`：后者在非 Windows 上
# **导入即报错**（它内部有 `_type_ = "v"` 的 VARIANT_BOOL），而本文件会被
# 测试收集、也会被人在别的平台上打开来看。只用固定位宽类型，模块在任何平台
# 都能安全导入，真正的 Windows 调用被 `_IS_WINDOWS` 挡在函数内部。
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_BYTE = ctypes.c_uint8


class _GUID(ctypes.Structure):
    """Windows 的 GUID 结构。

    内存布局必须与 COM 完全一致（4+2+2+8 = 16 字节，小端），否则
    `CoCreateInstance` 会找不到类 —— 报错是一句无信息量的
    "Class not registered"，能让人查很久。
    """

    _fields_ = [
        ("Data1", _DWORD),
        ("Data2", _WORD),
        ("Data3", _WORD),
        ("Data4", _BYTE * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> _GUID:
        """从标准 GUID 字符串构造（`uuid` 负责解析，自己解析必错）。"""
        inst = cls()
        ctypes.memmove(ctypes.byref(inst), uuid.UUID(text).bytes_le, 16)
        return inst


CLSID_FILE_OPEN_DIALOG = _GUID.parse("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")
IID_IFILE_OPEN_DIALOG = _GUID.parse("D57C7288-D4AD-4768-BE02-9D969532D960")
IID_ISHELL_ITEM = _GUID.parse("43826D1E-E718-42EE-BC55-A1E261C37BFE")

CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2

# 对话框选项。三个都要：
#   FOS_PICKFOLDERS       —— 选目录而不是选文件（否则这个类就是"打开文件"）
#   FOS_FORCEFILESYSTEM   —— 只允许文件系统路径，排除"此电脑/网络"这类虚拟项
#                            （否则用户能选中一个没有绝对路径的虚拟位置）
#   FOS_PATHMUSTEXIST     —— 不接受手打出来的不存在的路径
FOS_PICKFOLDERS = 0x20
FOS_FORCEFILESYSTEM = 0x40
FOS_PATHMUSTEXIST = 0x800

# IShellItem::GetDisplayName 的格式：要完整文件系统路径（而不是显示名）。
# 这个常量最容易被写错成 SIGDN_NORMALDISPLAY(0)，那样拿到的是文件夹**名字**，
# 而"名字"正是浏览器给不了、我们特意用原生对话框来拿的东西 —— 拿错了整件事
# 就白做了，而且症状是"配了个不存在的相对路径"，很难往回追。
SIGDN_FILESYSPATH = 0x80058000

# HRESULT_FROM_WIN32(ERROR_CANCELLED)：用户点了取消
_HR_CANCELLED = 0x800704C7

# IUnknown 占前三个槽位（QueryInterface / AddRef / Release），其后的编号按接口
# 声明顺序排列 —— 这是 COM ABI 的一部分。**写错一个数字会调到别的函数上**，
# 表现为崩溃或静默返回垃圾值（而不是一个漂亮的报错），所以每个用到的槽位
# 都在这里起个名字，而不是在调用处写裸数字。
_SLOT_RELEASE = 2
_SLOT_DIALOG_SHOW = 3  # IModalWindow::Show
_SLOT_DIALOG_SET_OPTIONS = 9
_SLOT_DIALOG_GET_OPTIONS = 10
_SLOT_DIALOG_SET_TITLE = 17
_SLOT_DIALOG_GET_RESULT = 20
_SLOT_SHELL_ITEM_GET_DISPLAY_NAME = 5


def _windll() -> Any:
    """取 `ctypes.WinDLL`。

    用 getattr 而不是直接写属性：`ctypes.WinDLL` 只在 Windows 上存在，
    直接引用会让本文件在 Linux/macOS 上**导入即失败**，从而连累测试收集。
    """
    return ctypes.WinDLL


def _method(ptr: Any, slot: int, restype: Any, *argtypes: Any) -> Any:
    """取 COM 接口的第 `slot` 个虚函数，返回一个**已绑好 this 指针**的可调用对象。

    COM 对象在 C 层面的真身是"指向虚函数表的指针"。ctypes 没有内置的
    "调用第 N 个虚函数"，所以这里手动走一遍：解引用 → 取表 → 取表项 →
    按调用约定包成可调用对象。

    **必须显式声明签名**：不声明的话 ctypes 默认按 int 处理参数和返回值，
    64 位下指针会被截断成 32 位 —— 这是 COM + ctypes 里最经典的崩溃原因。

    【为什么把 this 绑在这儿，而不是让每个调用处自己传】
    COM 每个方法的第一个参数都是接口指针本身。让调用处传的话，
    `Show(byref(...))` 这类"看起来只传了一个参数"的调用会直接抛
    `TypeError: this function takes 2 arguments (1 given)` ——
    错误信息完全没提 this 指针，很容易被理解成"参数个数对不上"而去改签名。
    绑掉它，调用处剩下的就只是真正的业务参数。
    """
    # 指针指向的是"虚函数表指针"，所以要**解引用一次**才拿到表本身。
    # 【踩坑记录】写成 `.contents[0]` 再索引会抛 TypeError: 'int' object is
    # not subscriptable —— 因为 c_void_p 在 ctypes 里就是"值为整数"的简单类型，
    # 取下标拿到的是那个整数，不是另一个可索引的对象。正确做法是让
    # `.contents` 直接给出表视图（POINTER(c_void_p)），再按下标取函数地址。
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    address = vtable[slot]
    if not address:
        raise OSError(f"COM 接口的第 {slot} 个方法为空指针（接口版本不符？）")

    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    bound = prototype(address)
    this = ptr.value if isinstance(ptr, ctypes.c_void_p) else ptr

    def call(*args: Any) -> Any:
        return bound(this, *args)

    return call


def _release(ptr: Any) -> None:
    """Release 一个 COM 接口指针（失败不影响结果，绝不往上抛）。"""
    try:
        _method(ptr, _SLOT_RELEASE, ctypes.c_ulong)()
    except Exception:  # pragma: no cover - 释放失败不该让"用户已选好目录"变成错误
        pass


def _prepare_dpi() -> None:
    """在创建任何窗口之前声明本进程的 DPI 感知能力。

    按"越新越好、能拿到哪个用哪个"的顺序试，全失败也无所谓
    （只是对话框在高 DPI 屏上会被系统缩放，功能不受影响）。
    这一步必须发生在建窗口之前 —— 之后再设会被系统直接忽略。
    """
    try:
        user32 = _windll()("user32")
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4（Win10 1703+）
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE == 2（Win8.1+）
        _windll()("shcore").SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        _windll()("user32").SetProcessDPIAware()  # Vista+，系统 DPI 感知
    except Exception:
        pass


def pick_folder(title: str = "选择工作区文件夹") -> str | None:
    """弹出一次系统文件夹对话框。

    Returns:
        选中的**绝对路径**；用户取消时返回 `None`。

    Raises:
        RuntimeError / OSError: 平台不对或 COM 调用失败（由调用方翻译成提示）。
    """
    if not _IS_WINDOWS:
        raise RuntimeError("picker_worker 只能在 Windows 上运行")

    ole32 = _windll()("ole32")
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, _DWORD]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        _DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    _prepare_dpi()

    # STA（单线程套间）：Shell 的对话框要求宿主线程是 STA。
    # S_OK(0) 与 S_FALSE(1，表示本线程已初始化过) 都算成功，
    # 只有真正的错误码（如 RPC_E_CHANGED_MODE）才该放弃。
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr not in (0, 1):
        raise OSError(f"CoInitializeEx 失败：0x{hr & 0xFFFFFFFF:08X}")

    dialog = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_FILE_OPEN_DIALOG),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_IFILE_OPEN_DIALOG),
            ctypes.byref(dialog),
        )
        if hr < 0:
            raise OSError(f"无法创建文件对话框对象：0x{hr & 0xFFFFFFFF:08X}")

        # 先读回现有选项再**或**上新选项：直接赋值会把系统/主题已经设好的
        # 选项（比如"记住上次位置"）抹掉。
        options = _DWORD(0)
        _method(dialog, _SLOT_DIALOG_GET_OPTIONS, ctypes.c_long, ctypes.POINTER(_DWORD))(
            ctypes.byref(options)
        )
        _method(dialog, _SLOT_DIALOG_SET_OPTIONS, ctypes.c_long, _DWORD)(
            options.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
        )
        _method(dialog, _SLOT_DIALOG_SET_TITLE, ctypes.c_long, ctypes.c_wchar_p)(title)

        # 【踩坑记录：属主窗口传了反而会失败，所以这里是 None】
        #
        # 最初的版本把 `GetForegroundWindow()` 传给 Show 当属主，理由很正当：
        # "不带属主的模态对话框可能被压在别的窗口下面，用户点了按钮却看不见"。
        # 实测结果是**对话框根本建不出来** —— Show 直接返回 0x80004005 (E_FAIL)，
        # 而同一份代码不传属主就一切正常。
        #
        # 根因：那个句柄的有效性不由我们掌握。前台窗口随时可能是**另一个进程
        # 正在销毁的窗口**（比如刚退出的终端），而用一个失效句柄做属主，
        # Show 是**失败**而不是忽略它。于是这个失败是随机的 ——
        # 取决于当时前台是什么窗口，靠手点几乎不可能定位。
        #
        # 还有第二个理由：模态对话框会**禁用它的属主窗口**。属主是浏览器的话，
        # worker 一旦被强杀，用户可能剩下一个点不动的浏览器窗口 ——
        # 那比"对话框被挡在后面"严重得多。
        #
        # DSH 的 Windows 目录选择器也不传属主。所以 `Show(None)`：
        # 位置交给系统，用户看得见（实测就在最前面），失败面只剩一个。
        hr = _method(dialog, _SLOT_DIALOG_SHOW, ctypes.c_long, ctypes.c_void_p)(None)
        if (hr & 0xFFFFFFFF) == _HR_CANCELLED:
            return None
        if hr < 0:
            raise OSError(f"对话框返回错误：0x{hr & 0xFFFFFFFF:08X}")

        item = ctypes.c_void_p()
        hr = _method(
            dialog, _SLOT_DIALOG_GET_RESULT, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
        )(ctypes.byref(item))
        if hr < 0 or not item:
            raise OSError(f"无法取回选择结果：0x{hr & 0xFFFFFFFF:08X}")

        try:
            buffer = ctypes.c_wchar_p()
            hr = _method(
                item,
                _SLOT_SHELL_ITEM_GET_DISPLAY_NAME,
                ctypes.c_long,
                _DWORD,
                ctypes.POINTER(ctypes.c_wchar_p),
            )(SIGDN_FILESYSPATH, ctypes.byref(buffer))
            if hr < 0 or not buffer.value:
                raise OSError(f"无法取回选中目录的路径：0x{hr & 0xFFFFFFFF:08X}")
            path = str(buffer.value)
            # 这块字符串是 Shell 用 CoTaskMemAlloc 分配的 —— 必须用配套的
            # CoTaskMemFree 还回去，用 free() 或让 GC 处理都是堆损坏。
            ole32.CoTaskMemFree(buffer)
            return path
        finally:
            _release(item)
    finally:
        if dialog:
            _release(dialog)
        ole32.CoUninitialize()


def _write_result(out_path: Path, payload: dict[str, Any]) -> None:
    """把结果写进父进程指定的文件（见模块文档：文件通道而非 stdout）。"""
    out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：picker_worker.py <结果文件>", file=sys.stderr)
        return 2

    out_path = Path(argv[1])
    try:
        path = pick_folder()
    except Exception as exc:
        # 异常也要**写文件**，不能只靠退出码：父进程需要知道失败原因，
        # 好把它翻译成给用户看的提示。沉默的退出码等于让用户猜。
        _write_result(
            out_path,
            {"path": None, "cancelled": False, "error": f"{type(exc).__name__}: {exc}"},
        )
        return 1

    _write_result(out_path, {"path": path, "cancelled": path is None, "error": ""})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
