"""文件工具测试 —— 重点全在**边界校验**上。

【为什么这个文件的测试重点不是"功能对不对"】

列目录、读文件、glob、grep 都是简单功能，写错了立刻能发现。
真正危险的是**边界**：一个能读文件的 Agent，如果路径校验有一个口子，
它就能读到 `~/.ssh/id_rsa`、`.env`、别的项目 —— 而且**不会有任何报错**，
因为从这个工具的角度看，它只是"成功读取了一个文件"。

所以下面 `TestSandboxEscape` 那一组才是这个文件的主体。
每一例都对应一个真实的绕过手法：

    ..\\..\\..         路径拼接
    符号链接           指向外面的软链
    绝对路径           直接给出界外的绝对路径
    前缀相同的兄弟目录  /root/project-evil 骗过字符串 startswith
    敏感文件名         .env / id_rsa / *.pem
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.tools.files import (
    FileAccessError,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    _rel,
    _resolve,
    build_file_tools,
    workspace_root,
)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把工作区根目录指到一个临时沙箱，并造出一份小巧的目录结构。"""
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
        settings.agent.model_copy(update={"workspace_root": str(root)}),
        raising=False,
    )
    return root


def _call(tool, **kwargs):  # type: ignore[no-untyped-def]
    """同步跑一个工具（测试里不需要 async 的复杂度）。"""
    import asyncio

    return asyncio.run(tool.run(tool.params_model(**kwargs)))


# ============================================================
# 边界校验：这个文件的主体
# ============================================================
class TestSandboxEscape:
    """**每一例都是一次越界尝试，必须全部失败。**"""

    def test_rejects_parent_traversal(self, sandbox: Path) -> None:
        r = _call(ReadFileTool(), path="../../outside/secret.txt")
        assert not r.ok, "`..` 穿越没有被拦住"
        assert "越界" in r.content or "不存在" in r.content

    def test_rejects_deep_parent_traversal(self, sandbox: Path) -> None:
        r = _call(ReadFileTool(), path="src/../../" + ".." * 5 + "/etc/hosts")
        assert not r.ok, "多级 `..` 穿越没有被拦住"

    def test_rejects_absolute_path_outside(self, sandbox: Path, tmp_path: Path) -> None:
        """绝对路径不能因为"看起来是显式的"就被放行。

        拒绝绝对路径是没意义的（相对路径绕一圈照样能到），
        所以这里走的是"解析后校验"，结果是同样的：越界就拒绝。
        """
        r = _call(ReadFileTool(), path=str(tmp_path / "outside" / "secret.txt"))
        assert not r.ok, "界外的绝对路径没有被拦住"

    def test_rejects_sibling_with_common_prefix(self, sandbox: Path) -> None:
        """`/root/project-evil` 不能被当成 `/root/project` 的子目录。

        这是用字符串 `startswith` 做校验时的经典漏洞：
        `"/tmp/workspace-evil".startswith("/tmp/workspace")` 是 True。
        `Path.is_relative_to` 按路径分量比较，不会中招。

        这条测试就是为"有人哪天把 is_relative_to 改回 startswith"准备的。
        """
        evil = sandbox.parent / (sandbox.name + "-evil")
        evil.mkdir()
        (evil / "gotcha.txt").write_text("不该读到\n", encoding="utf-8")

        r = _call(ReadFileTool(), path=f"../{sandbox.name}-evil/gotcha.txt")
        assert not r.ok, f"前缀相同的兄弟目录被误判为工作区内：{evil}"

    def test_rejects_symlink_escape(self, sandbox: Path, tmp_path: Path) -> None:
        """符号链接指向界外时必须被拦住。

        【为什么这条最重要】
        `resolve()` 会把软链展开 —— 所以校验必须发生在解析**之后**。
        先校验再解析的实现会放过这一例：沙箱内一个叫 `escape` 的路径
        在字符串层面完全合法，只有展开后才知道它指向外面。

        真实场景：`node_modules/.bin/*` 常是软链，用户让 Agent 看项目时，
        某条链指到外面很常见。

        【为什么 Windows 上要退到 junction】
        `os.symlink` 需要 SeCreateSymbolicLinkPrivilege（或开发者模式），
        普通用户跑不了 —— 于是这条安全检查在 Windows CI 上会被 skip 掉，
        而"被跳过的安全检查"等于没有。目录 junction 不需要那个权限，
        所以这里退到 `mklink /J` 让它在 Windows 上也能真跑。
        """
        link = sandbox / "escape"
        created = False
        try:
            link.symlink_to(tmp_path / "outside", target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            if os.name == "nt":
                import subprocess

                r = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "outside")],
                    capture_output=True,
                    text=True,
                )
                created = r.returncode == 0
        if not created:
            pytest.skip("本机既不能建符号链接也不能建 junction")

        r = _call(ReadFileTool(), path="escape/secret.txt")
        assert not r.ok, "链接穿越没有被拦住 —— 校验是不是发生在 resolve 之前？"

    def test_junction_or_symlink_listing_does_not_leak_outside(self, sandbox: Path) -> None:
        """即便链接存在，列举时也不能把界外的内容列出来。"""
        link = sandbox / "escape2"
        created = False
        try:
            link.symlink_to(sandbox.parent / "outside", target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            if os.name == "nt":
                import subprocess

                r = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(sandbox.parent / "outside")],
                    capture_output=True,
                    text=True,
                )
                created = r.returncode == 0
        if not created:
            pytest.skip("本机既不能建符号链接也不能建 junction")

        r = _call(ListDirTool(), path="escape2")
        assert not r.ok, f"越界目录被成功列举了：{r.content}"

    def test_glob_cannot_escape(self, sandbox: Path) -> None:
        """glob 的 `**` 也不能逃出根目录。"""
        r = _call(GlobTool(), pattern="../outside/*.txt")
        assert not r.ok, "glob 模式里的 `..` 没有被拒绝"

    def test_out_of_range_error_does_not_leak_resolved_path(
        self, sandbox: Path, tmp_path: Path
    ) -> None:
        """越界报错可以回显**用户自己给的输入**，但**不能回显解析后的真实路径**。

        【这条边界很容易搞错，我自己第一版就断言错了】
        我最初断言"报错里不能出现 outside 这个词" —— 但 `../../outside/secret.txt`
        是**用户自己提供的输入**，回显它不构成泄露：攻击者本来就知道自己问的是什么。

        真正不能出现的是**解析后的位置** —— 那才是在告诉试探者
        "你问的东西实际在哪、外面有什么"。所以下面断言的是绝对路径没被回显。
        """
        r = _call(ReadFileTool(), path="../../outside/secret.txt")
        assert not r.ok

        resolved_outside = (tmp_path / "outside" / "secret.txt").resolve()
        assert str(resolved_outside) not in r.content, (
            f"报错里回显了界外文件的真实绝对路径：{r.content}"
        )
        # 允许的范围本身可以说（那是用户配置的，不是秘密）
        assert "可访问" in r.content, "应告诉用户允许访问的范围，方便自查"


class TestSecretFiles:
    """敏感文件即便在**工作区内**也默认不读。"""

    def test_env_is_blocked(self, sandbox: Path) -> None:
        """工作区里天然会有 .env —— 用户的本意是"看看我的代码"，
        不是"把我的 API key 发给模型厂商"。"""
        r = _call(ReadFileTool(), path=".env")
        assert not r.ok, ".env 被读出来了"
        assert "敏感文件" in r.content

    def test_pem_is_blocked(self, sandbox: Path) -> None:
        (sandbox / "server.pem").write_text("-----BEGIN KEY-----\n", encoding="utf-8")
        r = _call(ReadFileTool(), path="server.pem")
        assert not r.ok, "*.pem 被读出来了"

    def test_grep_skips_secrets(self, sandbox: Path) -> None:
        """grep 也必须跳过敏感文件 —— 否则"按内容搜索"会成为绕过 read_file 黑名单的后门。

        这是很容易漏的一处：只给 read_file 加了黑名单，
        而 grep 能把同一个文件的内容搜出来。

        【断言的是"没有命中行"，不是"输出里不出现这个词"】
        我第一版断言 `"very-secret" not in content` —— 但搜索词是**用户自己给的**，
        它当然会出现在"没有匹配 'very-secret' 的内容"这句提示里。
        回显用户输入不是泄露；**泄露是从文件里读出了什么**。
        所以这里断言的是：没有任何一行以 `.env:行号:` 形式返回。
        """
        import re

        r = _call(GrepTool(), pattern="very-secret")
        assert not re.search(r"\.env:\d+", r.content), f"grep 返回了 .env 的行内容：{r.content}"
        assert "SECRET_KEY" not in r.content, f"grep 泄露了 .env 内容：{r.content}"

    def test_normal_file_is_readable(self, sandbox: Path) -> None:
        """黑名单不能误伤正常文件。"""
        r = _call(ReadFileTool(), path="README.md")
        assert r.ok, f"正常文件被误拦：{r.content}"
        assert "项目说明" in r.content


class TestDisabledByDefault:
    def test_no_tools_without_workspace_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未配置根目录时**不返回任何文件工具**。

        返回一堆注定失败的工具会诱导模型去调用它们、拿一串错误 ——
        白白消耗步数与 token。**能力不存在时就不该出现在菜单上。**
        """
        settings = get_settings()
        monkeypatch.setattr(
            settings,
            "agent",
            settings.agent.model_copy(update={"workspace_root": ""}),
            raising=False,
        )
        assert workspace_root() is None
        assert build_file_tools() == []

    def test_tools_present_with_root(self, sandbox: Path) -> None:
        names = {t.name for t in build_file_tools()}
        assert names == {"list_dir", "read_file", "glob", "grep"}


class TestFunctionality:
    def test_list_dir(self, sandbox: Path) -> None:
        r = _call(ListDirTool())
        assert r.ok
        assert "src/" in r.content, "目录应带 / 后缀以区分"
        assert "README.md" in r.content
        assert ".env" not in r.content, "隐藏文件默认不显示"

    def test_list_dir_include_hidden(self, sandbox: Path) -> None:
        r = _call(ListDirTool(), include_hidden=True)
        assert ".env" in r.content

    def test_glob_finds_files(self, sandbox: Path) -> None:
        r = _call(GlobTool(), pattern="**/*.py")
        assert r.ok
        assert "src/main.py" in r.content

    def test_glob_no_match_is_success_not_failure(self, sandbox: Path) -> None:
        """没找到不是错误 —— 报成 failure 会让模型以为工具坏了去重试。"""
        r = _call(GlobTool(), pattern="**/*.rs")
        assert r.ok
        assert "没有匹配" in r.content

    def test_grep_finds_line_numbers(self, sandbox: Path) -> None:
        r = _call(GrepTool(), pattern="def hello")
        assert r.ok
        assert "src/main.py:1" in r.content, f"应带文件与行号：{r.content}"

    def test_grep_invalid_regex_is_actionable(self, sandbox: Path) -> None:
        """正则写错是**模型自己能修**的错，必须把原因告诉它。"""
        r = _call(GrepTool(), pattern="[unclosed")
        assert not r.ok
        assert "正则表达式无效" in r.content

    def test_read_truncates_large_file(self, sandbox: Path) -> None:
        big = sandbox / "big.txt"
        big.write_text("行\n" * 20000, encoding="utf-8")
        r = _call(ReadFileTool(), path="big.txt")
        assert r.ok
        assert "截断" in r.content, "大文件必须截断并告知 —— 这是成本性质的要求"

    def test_binary_file_is_rejected(self, sandbox: Path) -> None:
        (sandbox / "bin.dat").write_bytes(b"\x00\x01\x02" * 100)
        r = _call(ReadFileTool(), path="bin.dat")
        assert not r.ok
        assert "二进制" in r.content

    def test_listing_a_file_suggests_right_tool(self, sandbox: Path) -> None:
        """工具用错时要指向**正确的那个工具**，模型才能自纠。"""
        r = _call(ListDirTool(), path="README.md")
        assert not r.ok
        assert "read_file" in r.content


class TestPathDisplay:
    def test_relative_paths_only(self, sandbox: Path) -> None:
        """返回给模型的必须是相对路径。

        绝对路径会带上用户名（隐私），而且模型倾向于在回答里复述它 ——
        用户分享对话时就泄露了本机目录结构。
        """
        assert _rel(sandbox / "src" / "main.py") == "src/main.py"

    def test_resolve_returns_absolute_inside(self, sandbox: Path) -> None:
        p = _resolve("src/main.py")
        assert p.is_absolute()
        assert p.is_relative_to(sandbox)

    def test_resolve_root_itself_is_allowed(self, sandbox: Path) -> None:
        """根目录自己当然要能访问 —— 边界是"含根"，不是"不含根"。"""
        assert _resolve(".") == sandbox

    def test_error_type_is_business_error(self, sandbox: Path) -> None:
        """越界应是**业务错误**（可回灌给模型自纠），不是系统异常。"""
        with pytest.raises(FileAccessError):
            _resolve("../../etc/passwd")


class TestWorkspaceRootParsing:
    def test_empty_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = get_settings()
        monkeypatch.setattr(
            settings,
            "agent",
            settings.agent.model_copy(update={"workspace_root": "  "}),
            raising=False,
        )
        assert workspace_root() is None

    def test_relative_resolves_against_project_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """相对路径按**仓库根**解析，而不是当前工作目录。

        按 cwd 解析会让"从哪个目录启动"影响行为 ——
        这是配置里最常见的"本地能跑、换个目录就找不到"的来源。
        """
        from app.core.config import PROJECT_ROOT

        settings = get_settings()
        monkeypatch.setattr(
            settings,
            "agent",
            settings.agent.model_copy(update={"workspace_root": "services"}),
            raising=False,
        )
        assert workspace_root() == (PROJECT_ROOT / "services").resolve()

    def test_nonexistent_is_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """目录不存在时视为未启用，而不是让工具在运行时才炸。"""
        settings = get_settings()
        monkeypatch.setattr(
            settings,
            "agent",
            settings.agent.model_copy(update={"workspace_root": str(tmp_path / "nope")}),
            raising=False,
        )
        assert workspace_root() is None
