"""目录选择 seam 的测试。

【这个文件盯的是哪几类错误】

   1. **判定错方向** —— 该给 browse 的时候给了 native。用户点一下按钮，
      然后什么都不会发生（对话框开在一台没人看的屏幕上）。
      这个错误的代价是不对等的：判成 browse 只是笨一点，判错成 native 就是死路。
   2. **取消被当成失败** —— 用户点"取消"是**正常操作**，不是错误。
      如果它对用户表现成红色报错，用户会以为程序坏了。
   3. **把"进程没了"当成"用户没选"** —— 这两种情况在结果上都是"没有路径"，
      但含义完全不同：前者要报错（并给出复现方式），后者什么都不用说。
      这是靠"结果文件存不存在"区分的，所以必须专门测。
   4. **接口在错误的时刻被调用** —— 没有系统对话框的部署上，
      `POST /pick` 必须**响亮地失败**（409），而不是返回一个空路径假装成功。

【为什么这里不需要弹出真的对话框】

真的弹窗是 `scripts/verify_picker_dialog.py` 的事（它从进程外枚举窗口，
证明窗口真的出现在屏幕上）。单元测试要能跑在 CI 上、能被人反复跑，
所以这里注入**假的 runner**：一个不起进程、不弹窗、想返回什么就返回什么的对象。
两者是分工，不是重复 —— 一个证明"逻辑对"，另一个证明"真的弹出来了"。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.core.directory_picker import (
    BrowseDirectoryPicker,
    ComDialogRunner,
    CommandChooserRunner,
    DirectoryPickerBusy,
    DirectoryPickerUnavailable,
    NativeDirectoryPicker,
    PickerFacts,
    PickOutcome,
    build_directory_picker,
    describe_backend,
    get_directory_picker,
    reset_directory_picker,
    resolve_directory_picker_backend,
)
from fastapi.testclient import TestClient


# ============================================================
# 判定：一张真值表
# ============================================================
class TestBackendResolution:
    """`resolve_directory_picker_backend` 是纯函数，所以能穷举它的输入。"""

    def _f(self, **kwargs: object) -> PickerFacts:
        base: dict[str, object] = {"bind_host": "127.0.0.1", "platform": "win32", "env": {}}
        base.update(kwargs)
        return PickerFacts(**base)  # type: ignore[arg-type]

    @pytest.mark.parametrize("platform", ["win32", "darwin"])
    def test_local_desktop_gets_native(self, platform: str) -> None:
        """本机回环 + 桌面系统 → 系统对话框可用。"""
        assert resolve_directory_picker_backend(self._f(platform=platform)) == "native"

    def test_non_loopback_bind_falls_back_to_browse(self) -> None:
        """绑到 0.0.0.0 时浏览器可能来自别的机器 —— 对话框会开在看不到的屏幕上。

        这是**最重要的一条**：把服务暴露到局域网之后，native 就不再成立。
        """
        assert resolve_directory_picker_backend(self._f(bind_host="0.0.0.0")) == "browse"
        assert resolve_directory_picker_backend(self._f(bind_host="192.168.1.10")) == "browse"

    @pytest.mark.parametrize("var", ["SSH_CONNECTION", "SSH_TTY"])
    def test_ssh_session_falls_back_to_browse(self, var: str) -> None:
        """SSH 端口转发下服务跑在远端，对话框会开在那台无人值守的机器上。"""
        assert (
            resolve_directory_picker_backend(self._f(env={var: "1.2.3.4 22 5.6.7.8 22"}))
            == "browse"
        )

    def test_empty_env_value_counts_as_unset(self) -> None:
        """空字符串按 shell 惯例等于"没设置"，不该被当成"在 SSH 里"。"""
        assert resolve_directory_picker_backend(self._f(env={"SSH_TTY": ""})) == "native"

    def test_linux_needs_both_display_and_chooser(self) -> None:
        """Linux 上两个条件都要：有图形会话**且**有能驱动的对话框程序。"""
        linux = {"platform": "linux", "linux_chooser": "/usr/bin/zenity"}
        assert resolve_directory_picker_backend(self._f(**linux, env={"DISPLAY": ":0"})) == "native"
        assert (
            resolve_directory_picker_backend(self._f(**linux, env={"WAYLAND_DISPLAY": "wayland-0"}))
            == "native"
        )
        # 有显示会话但没有 zenity/kdialog → 没有能弹的东西
        assert resolve_directory_picker_backend(self._f(**linux, env={})) == "browse"
        # 有程序但没有显示会话（服务器上的 zenity 也一样）→ 弹不出来
        assert (
            resolve_directory_picker_backend(
                self._f(platform="linux", linux_chooser="", env={"DISPLAY": ":0"})
            )
            == "browse"
        )

    def test_other_platforms_fall_back_to_browse(self) -> None:
        """native 后端只驱动 win32/darwin/linux；别的平台一律 browse。

        注意它**无视**绑定地址与显示会话：即便这是本机回环、环境里还有
        DISPLAY，native 也无从下手（没有对应平台的实现）。
        """
        for platform in ("freebsd", "aix", "emscripten"):
            facts = PickerFacts(
                bind_host="127.0.0.1",
                platform=platform,
                env={"DISPLAY": ":0"},
                linux_chooser="/usr/bin/zenity",
            )
            assert resolve_directory_picker_backend(facts) == "browse"

    @pytest.mark.parametrize("kind", ["native", "browse"])
    def test_every_backend_has_a_human_readable_reason(self, kind: str) -> None:
        """理由必须能解释"为什么"，否则用户只能猜自己哪里做错了。"""
        reason = describe_backend(PickerFacts(bind_host="0.0.0.0", platform="win32", env={}), kind)  # type: ignore[arg-type]
        assert reason.strip()
        assert len(reason) > 10


# ============================================================
# 两个后端
# ============================================================
class _FakeRunner:
    """假 runner：不弹窗、不起进程，想返回什么就返回什么。"""

    def __init__(self, outcome: PickOutcome, delay: float = 0.0) -> None:
        self.outcome = outcome
        self.delay = delay
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self) -> PickOutcome:
        self.calls += 1
        self.entered.set()
        if self.delay:
            time.sleep(self.delay)
        if self.release.is_set():
            self.release.wait(timeout=5)
        return self.outcome


class TestNativePicker:
    async def test_returns_the_selected_path(self) -> None:
        picker = NativeDirectoryPicker(_FakeRunner(PickOutcome(path="D:/WXP/简历/MyAgent")))
        outcome = await picker.pick()
        assert outcome.path == "D:/WXP/简历/MyAgent"
        assert outcome.ok and not outcome.cancelled

    async def test_cancel_is_a_normal_result_not_an_error(self) -> None:
        """取消必须是"正常返回 + cancelled=True"，不是 error。

        否则用户点一下"取消"就会看到一条红色错误 —— 那会让人以为程序坏了。
        """
        picker = NativeDirectoryPicker(_FakeRunner(PickOutcome(cancelled=True)))
        outcome = await picker.pick()
        assert outcome.cancelled and outcome.path is None
        assert outcome.error == ""

    async def test_second_dialog_is_refused(self) -> None:
        """同时只允许一个对话框。

        连点两下按钮会开出两个对话框，第二个压在第一个上面 ——
        用户关掉一个之后发现"还有一个"，像是程序卡住了。
        前端会禁用按钮，但**界面层的防护不能当作唯一的防护**。
        """
        import asyncio

        runner = _FakeRunner(PickOutcome(path="D:/x"), delay=0.4)
        picker = NativeDirectoryPicker(runner)

        first = asyncio.create_task(picker.pick())
        # 必须先让出一次事件循环：`create_task` 只是排队，
        # 而下面等的 threading.Event 会**阻塞整个循环**，任务永远起不来。
        # （第一版就是这么写的，于是这个用例自己把自己卡住了。）
        await asyncio.sleep(0.05)
        assert runner.entered.wait(timeout=2), "第一个 pick 没有真的进入 runner"

        with pytest.raises(DirectoryPickerBusy):
            await picker.pick()

        await first
        # 第一个结束后锁要释放，否则这个功能就"只能用一次"
        assert not picker._busy.locked()

    async def test_capability_is_native(self) -> None:
        picker = NativeDirectoryPicker(_FakeRunner(PickOutcome()), detail="因为本机回环")
        cap = picker.capability()
        assert cap.kind == "native"
        assert cap.detail == "因为本机回环"


class TestBrowsePicker:
    async def test_pick_fails_loudly(self) -> None:
        """browse 后端上调用 pick() 必须**响亮地失败**。

        若它返回一个空结果假装成功，调用方就会显示一个含糊的提示，
        而真正的原因是"这个部署根本没有这个能力" —— 那是两件不同的事。
        """
        picker = BrowseDirectoryPicker(detail="服务绑定在 0.0.0.0")
        assert picker.capability().kind == "browse"
        with pytest.raises(DirectoryPickerUnavailable) as exc:
            await picker.pick()
        assert DirectoryPickerUnavailable.code in str(exc.value)
        assert "0.0.0.0" in str(exc.value)  # 原因要带出来，用户才知道为什么


# ============================================================
# 文件通道：父进程怎么知道子进程干了什么
# ============================================================
class TestWorkerChannel:
    """用假的 worker 脚本测通道本身 —— 真弹窗的验证在 verify 脚本里。"""

    def _runner(self, tmp_path: Path, body: str) -> ComDialogRunner:
        worker = tmp_path / "fake_worker.py"
        worker.write_text(body, encoding="utf-8")
        return ComDialogRunner(worker_path=worker)

    def test_reads_the_selected_path(self, tmp_path: Path) -> None:
        runner = self._runner(
            tmp_path,
            "import json,sys; open(sys.argv[1],'w',encoding='utf-8')"
            ".write(json.dumps({'path':'D:/picked','cancelled':False,'error':''}))",
        )
        outcome = runner.run()
        assert outcome.path == "D:/picked" and outcome.ok

    def test_reads_cancel(self, tmp_path: Path) -> None:
        runner = self._runner(
            tmp_path,
            "import json,sys; open(sys.argv[1],'w',encoding='utf-8')"
            ".write(json.dumps({'path':None,'cancelled':True,'error':''}))",
        )
        assert runner.run().cancelled is True

    def test_propagates_the_worker_error(self, tmp_path: Path) -> None:
        runner = self._runner(
            tmp_path,
            "import json,sys; open(sys.argv[1],'w',encoding='utf-8')"
            ".write(json.dumps({'path':None,'cancelled':False,'error':'OSError: E_FAIL'}))",
        )
        outcome = runner.run()
        assert "E_FAIL" in outcome.error and not outcome.cancelled

    def test_worker_that_dies_without_writing_is_an_error_not_a_cancel(
        self, tmp_path: Path
    ) -> None:
        """**这条是重点**：进程崩了与用户没选，结果都必须区分开。

        "没有路径"这个事实有两种含义 —— 如果混在一起，
        用户会看到"已取消"，而真相是对话框崩了、什么都没发生。
        判据是"结果文件写没写"。
        """
        runner = self._runner(tmp_path, "import sys; sys.exit(7)")
        outcome = runner.run()
        assert outcome.error and not outcome.cancelled
        assert "7" in outcome.error
        # 报错里要给出**可复现**的方式，否则用户除了重试没有别的办法
        assert "python" in outcome.error

    def test_unreadable_result_is_reported(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path, "import sys; open(sys.argv[1],'w').write('{不是 json')")
        assert "无法解析" in runner.run().error

    def test_missing_worker_script_is_an_error_not_a_cancel(self, tmp_path: Path) -> None:
        """worker 脚本没了（比如部署时漏了文件）也要报成错误。

        注意这里**不能**指望拿到"无法启动"：解释器本身是好的，
        它启动之后才发现脚本不存在，于是表现为"没写结果就退出"。
        这与"进程崩了"是同一条路径 —— 所以这个用例与上一条是在验证
        同一条判据对不同原因都成立。
        """
        outcome = ComDialogRunner(worker_path=tmp_path / "不存在.py").run()
        assert outcome.error and not outcome.cancelled
        assert "python" in outcome.error

    def test_unlaunchable_interpreter_is_reported(self, tmp_path: Path) -> None:
        """解释器都起不来时（Popen 直接抛 OSError）要给出"无法启动"。

        这是最坏的一种失败，也是最需要一句话说清的一种：
        用户看到"无法启动"就知道问题不在"选没选文件夹"上。
        """
        runner = ComDialogRunner(
            worker_path=tmp_path / "w.py",
            python=str(tmp_path / "根本没有这个解释器.exe"),
        )
        outcome = runner.run()
        assert "无法启动" in outcome.error and not outcome.cancelled


class TestCommandChooserRunner:
    """macOS / Linux 的命令行对话框（osascript / zenity）。"""

    def test_darwin_uses_osascript(self) -> None:
        runner = CommandChooserRunner.for_platform("darwin", "")
        assert runner._argv[0] == "osascript"
        assert "choose folder" in runner._argv[2]

    def test_linux_uses_the_detected_chooser(self) -> None:
        runner = CommandChooserRunner.for_platform("linux", "/usr/bin/zenity")
        assert runner._argv[0] == "/usr/bin/zenity"
        assert "--directory" in runner._argv

    def test_exit_code_1_means_cancel(self) -> None:
        """三个程序在"用户取消"上都返回 1（它们自己的约定，不是碰巧）。"""
        runner = CommandChooserRunner([sys.executable, "-c", "import sys; sys.exit(1)"], "darwin")
        assert runner.run().cancelled is True

    def test_other_exit_codes_are_errors_with_the_stderr_tail(self) -> None:
        runner = CommandChooserRunner(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"],
            "linux",
        )
        outcome = runner.run()
        assert "boom" in outcome.error and not outcome.cancelled

    def test_stdout_is_the_path(self) -> None:
        runner = CommandChooserRunner([sys.executable, "-c", "print('/home/me/project')"], "darwin")
        assert runner.run().path == "/home/me/project"

    def test_missing_binary_is_reported_not_raised(self) -> None:
        runner = CommandChooserRunner(["zenity-不存在"], "linux")
        outcome = runner.run()
        assert "无法启动" in outcome.error


# ============================================================
# 装配
# ============================================================
class TestBuildPicker:
    def test_auto_picks_native_on_a_local_windows_desktop(self) -> None:
        picker = build_directory_picker(get_settings(), platform="win32")
        assert picker.capability().kind == "native"
        assert isinstance(picker, NativeDirectoryPicker)

    def test_auto_picks_browse_on_a_remote_bind(self) -> None:
        settings = get_settings()
        original = settings.app_host
        settings.app_host = "0.0.0.0"  # type: ignore[misc]
        try:
            picker = build_directory_picker(settings, platform="win32")
            assert picker.capability().kind == "browse"
        finally:
            settings.app_host = original  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("native", "native"), ("browse", "browse")],
    )
    def test_explicit_mode_overrides_the_automatic_decision(self, mode: str, expected: str) -> None:
        """显式配置必须能**覆盖**自动判定 —— 否则这个配置项就没有意义。

        覆盖的正当理由：你知道自己就坐在宿主屏幕前，而某个信号判错了
        （比如服务绑到了 0.0.0.0，但你本人就在这台机器上用它）。
        """
        settings = get_settings()
        original = settings.agent.directory_picker
        settings.agent.directory_picker = mode  # type: ignore[assignment]
        try:
            picker = build_directory_picker(settings, platform="win32")
            assert picker.capability().kind == expected
        finally:
            settings.agent.directory_picker = original  # type: ignore[assignment]

    def test_singleton_is_cached_and_can_be_reset(self) -> None:
        """能力声明必须比"一次点击"活得久，但设置变了要能重建。"""
        first = get_directory_picker()
        assert get_directory_picker() is first
        reset_directory_picker()
        assert get_directory_picker() is not first


# ============================================================
# HTTP 接口
# ============================================================
class _StubPicker:
    """替身：直接给接口一个确定的能力与结果。"""

    def __init__(self, kind: str = "native", outcome: PickOutcome | None = None) -> None:
        self._kind = kind
        self._outcome = outcome or PickOutcome(path="D:/picked")
        self.pick_calls = 0

    def capability(self):  # type: ignore[no-untyped-def]
        from app.core.directory_picker import PickerCapability

        return PickerCapability(kind=self._kind, detail="替身说明")  # type: ignore[arg-type]

    async def pick(self) -> PickOutcome:
        self.pick_calls += 1
        if self._kind != "native":
            raise DirectoryPickerUnavailable("directory-picker-unavailable：替身没有对话框")
        return self._outcome


class _BusyPicker(_StubPicker):
    async def pick(self) -> PickOutcome:
        raise DirectoryPickerBusy("已经有一个文件夹对话框打开了。")


@pytest.fixture
def stub_picker(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """替换掉接口模块里的 picker 工厂。"""
    from app.api import files as files_api

    def install(picker: object) -> None:
        monkeypatch.setattr(files_api, "get_directory_picker", lambda: picker)

    return install


class TestPickerApi:
    def test_capability_endpoint(self, client: TestClient) -> None:
        r = client.get("/api/files/picker")
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] in ("native", "browse")
        assert body["detail"]  # 理由不能是空串

    def test_pick_returns_the_absolute_path(
        self,
        client: TestClient,
        stub_picker,  # type: ignore[no-untyped-def]
    ) -> None:
        picker = _StubPicker()
        stub_picker(picker)
        r = client.post("/api/files/pick", json={})
        assert r.status_code == 200
        assert r.json()["path"] == "D:/picked"
        assert r.json()["cancelled"] is False
        assert picker.pick_calls == 1

    def test_cancel_is_a_200_with_a_hint(
        self,
        client: TestClient,
        stub_picker,  # type: ignore[no-untyped-def]
    ) -> None:
        """取消不是错误：200 + cancelled=true + 一句说明，而不是 4xx/5xx。"""
        stub_picker(_StubPicker(outcome=PickOutcome(cancelled=True)))
        r = client.post("/api/files/pick", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["cancelled"] is True and body["path"] is None
        assert body["hint"]

    def test_browse_backend_refuses_with_409(
        self,
        client: TestClient,
        stub_picker,  # type: ignore[no-untyped-def]
    ) -> None:
        """没有这个能力的部署要**响亮地**拒绝，并带上原因。

        409 而不是 404：接口是存在的，只是这个部署提供不了这个交互。
        前端据此知道"该渲染另一种交互"，而不是"接口写错了"。
        """
        stub_picker(_StubPicker(kind="browse"))
        r = client.post("/api/files/pick", json={})
        assert r.status_code == 409
        assert DirectoryPickerUnavailable.code in r.json()["detail"]
        assert "替身说明" in r.json()["detail"]

    def test_second_dialog_returns_409(
        self,
        client: TestClient,
        stub_picker,  # type: ignore[no-untyped-def]
    ) -> None:
        stub_picker(_BusyPicker())
        r = client.post("/api/files/pick", json={})
        assert r.status_code == 409
        assert "已经有一个" in r.json()["detail"]

    def test_worker_failure_returns_500_with_the_reason(
        self,
        client: TestClient,
        stub_picker,  # type: ignore[no-untyped-def]
    ) -> None:
        stub_picker(_StubPicker(outcome=PickOutcome(error="OSError: E_FAIL")))
        r = client.post("/api/files/pick", json={})
        assert r.status_code == 500
        assert "E_FAIL" in r.json()["detail"]

    def test_body_is_required(self, client: TestClient) -> None:
        """**要一个 JSON 体是在挡 CSRF**，不是形式主义。

        带 `Content-Type: application/json` 的请求会触发 CORS 预检，
        第三方网页因此无法让你的浏览器悄悄发出这次调用；而"无参 POST"
        属于简单请求、根本不预检。这个接口的副作用是在你屏幕上弹窗。
        """
        assert client.post("/api/files/pick").status_code == 422

    def test_extra_fields_are_rejected(self, client: TestClient) -> None:
        """多余的字段直接报错，而不是被静默忽略（前后端字段名漂移的护栏）。"""
        assert client.post("/api/files/pick", json={"path": "/etc"}).status_code == 422
