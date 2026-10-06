"""写文件工具的测试（T23）。

【用户的问题是这个文件存在的起点】

> 为什么现在我的 agent 还不能写文件？

查下去发现两件事：写工具**从来没实现过**（`build_file_tools` 只返回四个只读工具），
而全项目有七处对外文案写着"读写"。也就是说：**承诺了一个不存在的能力**。

【这组测试的重点与只读工具那组不同】

只读工具怕的是**越界**（读到了界外的东西）。写工具怕的是**不可撤销的破坏**：

    覆盖掉用户没打算改的文件      → 数据没了
    old_text 匹配到多处却随便挑一处 → 改错了地方，而且看起来"成功"了
    把 CRLF 文件写成 LF            → git diff 整篇变红，真正的改动被淹没
    写进 .env / 私钥               → 不是"读到"，是**改写**凭据

所以下面每一组都对应一类真实的破坏方式。默认不加载这件事本身也有测试
（`TestDisabledByDefault`）—— 它是这次改动的核心决定。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.agent.prompts import build_system_prompt
from app.core.config import get_settings
from app.tools.builtin import build_default_registry
from app.tools.files import (
    MAX_WRITE_CHARS,
    EditFileTool,
    ReadFileTool,
    WriteFileTool,
    build_file_tools,
)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把工作区指到一个临时目录。每个用例都是干净的。"""
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# 项目说明\n", encoding="utf-8")
    (root / "src" / "main.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    (root / ".env").write_text("SECRET_KEY=very-secret\n", encoding="utf-8")

    outer = tmp_path / "outside"
    outer.mkdir()
    (outer / "secret.txt").write_text("外部机密\n", encoding="utf-8")

    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "agent",
        settings.agent.model_copy(
            update={
                "workspace_root": str(root),
                "file_write_enabled": True,
                "file_approval_required": False,
            }
        ),
        raising=False,
    )
    return root


def _call(tool, **kwargs):  # type: ignore[no-untyped-def]
    return asyncio.run(tool.run(tool.params_model(**kwargs)))


# ============================================================
# 默认关闭 —— 这次改动的核心决定
# ============================================================
class TestDisabledByDefault:
    def test_write_tools_are_not_built_unless_asked(self, sandbox: Path) -> None:
        """默认只给四个只读工具。"""
        names = [t.name for t in build_file_tools()]
        assert names == ["list_dir", "read_file", "glob", "grep"]
        assert "write_file" not in names and "edit_file" not in names

    def test_registry_has_no_write_tools_by_default(self, sandbox: Path) -> None:
        """工具表里没有它们 —— 所以模型不会尝试，也不会承诺"我写好了"。"""
        registry = build_default_registry(file_write=False)
        assert "write_file" not in registry.names()
        assert "edit_file" not in registry.names()

    def test_registry_includes_them_when_enabled(self, sandbox: Path) -> None:
        registry = build_default_registry(file_write=True)
        assert "write_file" in registry.names()
        assert "edit_file" in registry.names()

    def test_no_workspace_means_no_tools_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没配工作区时，即使开了写权限也**一个文件工具都不注册**。

        "能力不存在时就不该出现在菜单上" —— 这条对写权限同样成立。
        """
        settings = get_settings()
        monkeypatch.setattr(
            settings,
            "agent",
            settings.agent.model_copy(update={"workspace_root": "", "file_write_enabled": True}),
            raising=False,
        )
        assert build_file_tools(include_write=True) == []

    def test_prompt_stops_promising_write_when_tools_are_absent(self, sandbox: Path) -> None:
        """提示词里的写规则必须跟着工具表一起消失。

        这是本项目既有的机制（`build_system_prompt` 会裁掉引用了未注册工具的
        规则行）。它在这里的价值很具体：**不能让提示词承诺一个模型做不到的能力** ——
        那会诱导它去"假装写好了"，而这正是最坏的失败形态。
        """
        without = build_system_prompt("general", {"list_dir", "read_file", "glob", "grep"})
        assert "`write_file`" not in without
        assert "`edit_file`" not in without

        with_write = build_system_prompt(
            "general", {"list_dir", "read_file", "glob", "grep", "write_file", "edit_file"}
        )
        assert "`write_file`" in with_write
        assert "`edit_file`" in with_write


class TestWriteFile:
    def test_creates_a_new_file(self, sandbox: Path) -> None:
        r = _call(WriteFileTool(), path="notes/plan.md", content="# 计划\n第一步\n")
        assert r.ok, r.content
        target = sandbox / "notes" / "plan.md"
        assert target.read_text(encoding="utf-8") == "# 计划\n第一步\n"
        assert "已创建" in r.content and "notes/plan.md" in r.content

    def test_creates_missing_parent_directories(self, sandbox: Path) -> None:
        """父目录不存在时一并创建。

        让模型先说"写 notes/2026/summary.md"、再因为目录不存在失败，
        只会白费一轮往返 —— 而这在工作区内没有任何风险。
        """
        r = _call(WriteFileTool(), path="a/b/c/deep.md", content="x")
        assert r.ok, r.content
        assert (sandbox / "a" / "b" / "c" / "deep.md").is_file()

    def test_newlines_are_lf_on_every_platform(self, sandbox: Path) -> None:
        """写出来的必须是 LF。

        Windows 的 `write_text` 默认会把 `\\n` 翻译成 `\\r\\n`，于是同一段内容
        在不同机器上产出不同字节，git diff 会整篇变红、真正的改动被淹没。
        """
        _call(WriteFileTool(), path="lf.txt", content="第一行\n第二行\n")
        raw = (sandbox / "lf.txt").read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"\n")

    def test_refuses_to_overwrite_by_default(self, sandbox: Path) -> None:
        """已存在就失败，**并且不改动任何东西**。

        这条是"我想新建"与"我要改掉它"之间的闸门：默认能把已有文件
        悄悄覆盖掉的话，用户第一次用就会丢东西。
        """
        before = (sandbox / "README.md").read_text(encoding="utf-8")
        r = _call(WriteFileTool(), path="README.md", content="被覆盖了")
        assert not r.ok
        assert "已存在" in r.content
        assert "edit_file" in r.content, "要让模型知道有更安全的选项"
        assert "overwrite=true" in r.content, "要告诉它怎么显式覆盖"
        assert (sandbox / "README.md").read_text(encoding="utf-8") == before

    def test_overwrites_when_explicitly_allowed(self, sandbox: Path) -> None:
        r = _call(WriteFileTool(), path="README.md", content="# 新说明\n", overwrite=True)
        assert r.ok, r.content
        assert "已覆盖" in r.content
        assert (sandbox / "README.md").read_text(encoding="utf-8") == "# 新说明\n"

    def test_refuses_directory_target(self, sandbox: Path) -> None:
        r = _call(WriteFileTool(), path="src", content="x")
        assert not r.ok and "目录" in r.content

    def test_refuses_path_escape(self, sandbox: Path) -> None:
        """越界写入必须被拦下 —— 这是整套沙箱的底线。"""
        r = _call(WriteFileTool(), path="../outside/pwned.txt", content="x")
        assert not r.ok and "越界" in r.content
        assert not (sandbox.parent / "outside" / "pwned.txt").exists()

    def test_refuses_absolute_path_outside(self, sandbox: Path) -> None:
        r = _call(WriteFileTool(), path=str(sandbox.parent / "outside" / "x.txt"), content="x")
        assert not r.ok

    def test_refuses_secret_names(self, sandbox: Path) -> None:
        """**写**进 .env 比读出来更严重：那是改写凭据。

        改配置有专门的入口（设置界面 → 写 .env），不需要模型代劳。
        """
        before = (sandbox / ".env").read_text(encoding="utf-8")
        r = _call(WriteFileTool(), path=".env", content="SECRET_KEY=attacker\n", overwrite=True)
        assert not r.ok and "敏感文件" in r.content
        assert (sandbox / ".env").read_text(encoding="utf-8") == before

    def test_refuses_oversized_content(self, sandbox: Path) -> None:
        r = _call(WriteFileTool(), path="big.txt", content="x" * (MAX_WRITE_CHARS + 1))
        assert not r.ok and "太长" in r.content
        assert not (sandbox / "big.txt").exists()


def _write_exact(path: Path, text: str) -> None:
    """写文件时**不让 Python 翻译换行**。

    【为什么夹具必须显式写 newline=""】
    `Path.write_text` 默认 `newline=None`，在 Windows 上会把 `\\n` 翻译成 `\\r\\n`。
    于是测试以为文件里是 `alpha\\nbeta\\n`，实际是 `alpha\\r\\nbeta\\r\\n` ——
    而 `edit_file` 要求**逐字符精确匹配**，于是它会理直气壮地报"没有找到"。
    我第一版就栽在这里：工具是对的，夹具在骗自己。
    """
    path.write_text(text, encoding="utf-8", newline="")


def _read_exact(path: Path) -> str:
    """原样读出（不翻译换行）。

    不能用 `Path.read_text(newline="")` —— 那个参数是 **Python 3.13** 才加的，
    而本项目跑在 3.12 上（实测报 `unexpected keyword argument 'newline'`）。
    用 `open` 就没有版本问题。
    """
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


class TestEditFile:
    def test_replaces_a_unique_occurrence(self, sandbox: Path) -> None:
        r = _call(
            EditFileTool(),
            path="src/main.py",
            old_text="return 'hi'",
            new_text="return 'hello'",
        )
        assert r.ok, r.content
        assert "替换 1 处" in r.content
        assert "return 'hello'" in (sandbox / "src" / "main.py").read_text(encoding="utf-8")

    def test_reports_not_found_with_a_way_forward(self, sandbox: Path) -> None:
        """找不到原文时必须让模型去**读原文**，而不是猜。

        "凭记忆写出来的片段几乎一定对不上" —— 这条提示直接决定它下一步是
        重试一次还是重读文件。
        """
        r = _call(EditFileTool(), path="src/main.py", old_text="return 'nope'", new_text="x")
        assert not r.ok
        assert "没有找到" in r.content
        assert "read_file" in r.content

    def test_refuses_ambiguous_match(self, sandbox: Path) -> None:
        """出现多次就拒绝，并**告诉它出现了几次**。

        随便挑一处替换是最坏的失败形态：文件被改了、工具报成功、
        而改的地方不是用户想要的 —— 且很难看出来。
        """
        dup = sandbox / "dup.txt"
        _write_exact(dup, "alpha\nbeta\nalpha\n")
        r = _call(EditFileTool(), path="dup.txt", old_text="alpha", new_text="gamma")
        assert not r.ok
        assert "2 次" in r.content
        assert "唯一" in r.content
        assert _read_exact(dup) == "alpha\nbeta\nalpha\n"

    def test_disambiguated_by_more_context(self, sandbox: Path) -> None:
        """补上上下文之后就能唯一匹配 —— 上面那条拒绝是有出路的。"""
        dup = sandbox / "dup.txt"
        _write_exact(dup, "alpha\nbeta\nalpha\n")
        r = _call(
            EditFileTool(),
            path="dup.txt",
            old_text="beta\nalpha\n",
            new_text="beta\ngamma\n",
        )
        assert r.ok, r.content
        assert _read_exact(dup) == "alpha\nbeta\ngamma\n"

    def test_refuses_empty_old_text(self, sandbox: Path) -> None:
        """空串"出现无数次"，任何替换都是任意的。"""
        r = _call(EditFileTool(), path="src/main.py", old_text="", new_text="插入")
        assert not r.ok and "write_file" in r.content

    def test_refuses_identical_old_and_new(self, sandbox: Path) -> None:
        r = _call(EditFileTool(), path="src/main.py", old_text="def hello", new_text="def hello")
        assert not r.ok and "没有需要修改" in r.content

    def test_refuses_missing_file_and_points_to_write_file(self, sandbox: Path) -> None:
        r = _call(EditFileTool(), path="nope.txt", old_text="a", new_text="b")
        assert not r.ok
        assert "不存在" in r.content or "write_file" in r.content

    def test_refuses_secret_names(self, sandbox: Path) -> None:
        r = _call(EditFileTool(), path=".env", old_text="SECRET_KEY", new_text="X")
        assert not r.ok and "敏感文件" in r.content

    def test_preserves_crlf_line_endings(self, sandbox: Path) -> None:
        """原本是 CRLF 的文件改完还是 CRLF，**且不会变成 `\\r\\r\\n`**。

        这条容易被当成吹毛求疵，但它决定 git diff 是"改了一行"还是"整篇都变了"
        —— 后者会让人完全看不出这次改动做了什么。

        （`\\r\\r\\n` 这个失败形态是真出现过的：当时的实现按原文风格传
        `newline="\\r\\n"`，而 Python 会把字符串里已有的 `\\n` 再翻译一次。）
        """
        target = sandbox / "crlf.txt"
        target.write_bytes("第一行\r\n第二行\r\n".encode())
        r = _call(EditFileTool(), path="crlf.txt", old_text="第二行", new_text="第二行（改）")
        assert r.ok, r.content
        assert target.read_bytes() == "第一行\r\n第二行（改）\r\n".encode()

    def test_refuses_binary_files(self, sandbox: Path) -> None:
        (sandbox / "bin.dat").write_bytes(b"\x00\x01\x02binary")
        r = _call(EditFileTool(), path="bin.dat", old_text="\x01", new_text="x")
        assert not r.ok and "二进制" in r.content


class TestBothAreSerial:
    """有副作用的工具必须串行 —— 否则并发写同一文件的结果不可复现。"""

    def test_serial_flag_is_set(self) -> None:
        assert WriteFileTool.serial is True
        assert EditFileTool.serial is True

    def test_registry_reports_them_as_serial(self, sandbox: Path) -> None:
        registry = build_default_registry(file_write=True)
        assert registry.is_serial("write_file")
        assert registry.is_serial("edit_file")
        # 只读工具不该被拖成串行（那会白白损失并发收益）
        assert not registry.is_serial("read_file")


class TestRoundTripWithReadTool:
    def test_what_was_written_can_be_read_back(self, sandbox: Path) -> None:
        """写完能读回来 —— 这条把"写"和已有的"读"接在一起验一遍。

        单看两边各自的测试都绿，但两边的路径约定（相对根目录、UTF-8）
        不一致的话，模型会经历"写成功、读不到"这种诡异的组合。
        """
        content = "# 标题\n\n中文内容与 ascii 混排\n"
        assert _call(WriteFileTool(), path="report.md", content=content).ok
        r = _call(ReadFileTool(), path="report.md")
        assert r.ok, r.content
        assert "中文内容与 ascii 混排" in r.content
