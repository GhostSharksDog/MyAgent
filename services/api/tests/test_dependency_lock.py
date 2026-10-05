"""依赖锁（技术债 T21）的清单类测试。

【为什么这组断言值得存在 —— 它们的失败方式全是"静默"的】

T21 的问题是"依赖没有锁"，而没锁的后果不是崩溃，是**指标慢慢不可复现**：
今天 scikit-learn 是 1.9.1，下个月 clone 的人装到 1.10，同一份语料算出来的
Recall@5 变了一点 —— 没有任何地方会报错，而"指标变好了"这件事从此无法归因。

所以这里盯的是"几份清单还对得上吗"：

  1. lock 本身是不是一份**精确**的清单（每行 `name==version`，
     没有 `>=` / `~=` / `*`）—— 带范围约束的文件根本不是 lock；
  2. pyproject 里声明的每个运行时依赖，是不是**都在 lock 里**。
     这条是这组里最值钱的：它挡住的是"加了依赖、忘了重新生成 lock"——
     那时代码在本机能跑（本机装着），别人按 lock 装就没有它，
     失败出现在**运行期**的某个请求里，离原因很远；
  3. `lock_deps.py --check` 在 lock 过期时**真的返回非 0**
     （否则 CI 里那条命令只是一句安慰：它永远绿，谁也不看）。

【为什么这里没有"仓库里那份 lock == 当前环境"的断言】
因为 lock 是在**某一个具体平台**上求出的解：`uvicorn[standard]` 在 Linux 上
带 uvloop、在 Windows 上不带。`--check` 换个平台跑本来就会报差异，
而那是**预期差异**，不是 lock 过期（脚本自己会打印这句解释）。
把它写成测试等于要求"每个平台都必须通过"，那条断言在 Linux 上只会逼着人
去重新生成 lock、于是两边改来改去。
**平台相关的校验交给 `--check`，平台无关的清单一致性交给这里。**
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
API_DIR = PROJECT_ROOT / "services" / "api"
LOCK_PATH = API_DIR / "requirements.lock"
PYPROJECT_PATH = API_DIR / "pyproject.toml"
LOCK_SCRIPT = PROJECT_ROOT / "scripts" / "lock_deps.py"

# name==version，其中版本必须是**精确**的：数字开头，之后只允许版本号里
# 真会出现的字符（`+` 本地版本、`!` epoch、`.` 与 `-` 分隔）。
# `*`、`>=`、`~=` 之类一律匹配失败 —— 这正是这个正则存在的意义。
_PIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[0-9][A-Za-z0-9._+!-]*$")

# 这些是 dev extras 里的工具：它们出现在**运行时** lock 里就说明
# 生成脚本把 extras 也一起解了（那样运行镜像会白装一堆用不到的东西）。
_DEV_ONLY = ("pytest", "pytest-asyncio", "ruff", "mypy", "fakeredis")


def _lock_text() -> str:
    return LOCK_PATH.read_text(encoding="utf-8")


def _body_lines(text: str | None = None) -> list[str]:
    """lock 里的**包行**（注释与空行不算）。"""
    source = _lock_text() if text is None else text
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _canonical(name: str) -> str:
    """PEP 503 规范化：`-` / `_` / `.` 等价，统一成小写与连字符再比较。

    不统一的话，`python-dotenv` 与 `python_dotenv` 会被当成两个包 ——
    于是"缺了一个依赖"这种真问题会被一条假差异盖住。
    """
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _declared_runtime_names() -> set[str]:
    """pyproject 的 `[project].dependencies` 里的包名（去掉 extras 与版本约束）。"""
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    for item in data["project"]["dependencies"]:
        bare = re.split(r"[\[<>=!~;@ ]", item, maxsplit=1)[0]
        names.add(_canonical(bare))
    return names


def _run_lock_script(*args: str) -> subprocess.CompletedProcess[str]:
    """跑一次锁定脚本。

    显式要求子进程用 UTF-8 输出：本机的 PowerShell/控制台是 GBK，
    子进程按 GBK 写、父进程按 UTF-8 读的话，看到的差异是乱码 ——
    **而失败信息本身也变成乱码**，那正是最该看清的东西。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, str(LOCK_SCRIPT), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def _stale_lock(tmp_path: Path, lines: list[str]) -> Path:
    """把一份（改坏了的）lock 写到临时目录，返回它的路径。

    **绝不改仓库里那一份**：`--check` 的测试如果靠在真文件上做手脚，
    它就会在失败时把工作区一起弄脏 —— 而"测试改坏了仓库状态"是最难查的一类问题。
    """
    path = tmp_path / "requirements.lock"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


# ============================================================
# 1. lock 本身是一份精确清单
# ============================================================
class TestLockFileShape:
    def test_exists_and_is_utf8_with_lf_and_a_final_newline(self) -> None:
        """存在、非空、UTF-8、`\\n` 换行、末尾恰好一个换行。

        【为什么换行符也要断言】
        默认的文本写入在 Windows 上会转成 CRLF。一旦如此，同一份 lock
        在 Windows 与 Linux 上会产生一行行"全都变了"的 diff ——
        而真正的变更（某个版本变了）会被淹没在里面，没人看得出来。
        """
        assert LOCK_PATH.is_file(), f"没有 lock 文件：{LOCK_PATH}（跑 python scripts/lock_deps.py）"
        raw = LOCK_PATH.read_bytes()
        assert raw.strip(), "lock 是空的"
        assert not raw.startswith(b"\xef\xbb\xbf"), "lock 不该带 BOM"
        text = raw.decode("utf-8")  # 解不开就是编码坏了，让异常自己说话
        assert "\r" not in text, "出现了 CRLF：换行符必须统一成 \\n"
        assert text.endswith("\n") and not text.endswith("\n\n"), "末尾应当恰好一个换行"

    def test_declares_utf8_on_the_first_lines_for_pip(self) -> None:
        """前两行必须有 PEP 263 编码声明 —— 这条是实测出来的。

        pip 读 requirements 文件用的是 `auto_decode`：找不到 BOM 与编码声明时
        它会退回**系统 locale 编码**。本机的 locale 是 cp936，于是那份 UTF-8
        的中文注释会被按 GBK 解码，直接抛：

            UnicodeDecodeError: 'gbk' codec can't decode byte 0xad in position 23

        也就是"注释里写了中文"会让 `pip install -r requirements.lock` 整个失败 ——
        而头部注释正是这个文件最有价值的部分（它写着为什么锁、怎么重新生成）。
        """
        head = _lock_text().splitlines()[:2]
        assert any(re.search(r"coding[:=]\s*utf-?8", line) for line in head), (
            "lock 的前两行缺少 `# -*- coding: utf-8 -*-`：pip 在 GBK 机器上会按 "
            "cp936 解码这个文件，遇到中文注释直接 UnicodeDecodeError"
        )

    def test_every_line_is_an_exact_pin(self) -> None:
        """每行都必须是 `name==version`，不能有范围约束或注释尾巴。

        带 `>=` 的文件不是 lock：它把"装哪一版"重新交回给了 pip 和当天的 PyPI，
        而这正是 T21 要消除的东西。
        """
        offenders = [line for line in _body_lines() if not _PIN_RE.match(line)]
        assert not offenders, (
            "这些行不是精确的 name==version：\n  "
            + "\n  ".join(offenders)
            + "\n（lock 只记精确版本；需要范围约束请写在 pyproject.toml 里）"
        )

    def test_versions_have_no_range_operators(self) -> None:
        """明确禁止 `>=` / `~=` / `*` 这些写法（比上面那条更直白地表达意图）。"""
        body = "\n".join(_body_lines())
        for token in (">=", "<=", "~=", "!=", ">", "<", "*", " @ ", "^"):
            assert token not in body, f"lock 里出现了范围/间接写法 {token!r}：它就不是一份锁了"

    def test_names_are_unique_and_sorted(self) -> None:
        """名字唯一且按名字排序。

        排序是为了让 diff 只有"真正变了的那一行"：手工追加的条目会让
        每次重新生成都产生大片移动，而移动的噪音会掩盖真实的版本变化。
        """
        names = [line.split("==", 1)[0] for line in _body_lines()]
        assert len(names) == len(set(names)), (
            f"重复的包名：{sorted({n for n in names if names.count(n) > 1})}"
        )
        assert names == sorted(names), "lock 应当按包名排序"


# ============================================================
# 2. 清单之间必须对得上（这组里最值钱的一条）
# ============================================================
class TestDeclaredDependenciesAreLocked:
    def test_every_declared_runtime_dependency_is_pinned(self) -> None:
        """pyproject 声明的每个运行时依赖，都必须出现在 lock 里。

        【它挡住的是哪一种失败】
        "加了依赖、忘了重新生成 lock"：开发机上装着那个包，所以测试全绿、
        接口正常；别人 clone 下来按 lock 装，缺的正是它 —— 直到某个请求
        走进那条新路径才 ModuleNotFoundError。报错点在运行期、在容器里，
        和"清单少了一行"离得非常远。
        """
        locked = {_canonical(line.split("==", 1)[0]) for line in _body_lines()}
        missing = sorted(_declared_runtime_names() - locked)
        assert not missing, (
            "pyproject 里声明了、lock 里没有的运行时依赖：\n  "
            + "\n  ".join(missing)
            + "\n本机能跑只是因为环境里装着它，别人按 lock 装就没有。"
            "\n重新生成：python scripts/lock_deps.py"
        )

    def test_dev_only_tools_are_not_locked(self) -> None:
        """反过来：dev extras 里的工具不该进运行时 lock。

        lock 是给**运行镜像**用的，把 pytest / ruff / mypy 塞进去意味着
        每次构建都多装一堆运行期用不到的东西 —— 而且它们与检索指标无关，
        锁进来只会让"这份解"更难解释。
        （注意 `pyyaml` 不在此列：它同时是 dev 依赖和 `uvicorn[standard]`
        的运行时依赖，所以在 lock 里是**对的**。这类"两边都出现"的包正是
        不能用"dev 里出现过的名字一律排除"这种规则的原因。）
        """
        locked = {_canonical(line.split("==", 1)[0]) for line in _body_lines()}
        leaked = sorted(name for name in _DEV_ONLY if _canonical(name) in locked)
        assert not leaked, f"这些是 dev 依赖，不该出现在运行时 lock 里：{leaked}"


# ============================================================
# 3. --check：CI 里唯一能防住"忘了重新生成"的东西
# ============================================================
class TestCheckMode:
    def test_passes_for_a_lock_generated_from_this_environment(self, tmp_path: Path) -> None:
        """刚由当前环境生成出来的 lock，必须能被 --check 判为一致。

        这条同时钉住了"写"和"校验"两侧：只要两边有一边把包名/版本/格式
        处理得不一样（例如规范化规则不同），它立刻就会红。
        """
        path = tmp_path / "requirements.lock"
        written = _run_lock_script("--lock", str(path))
        assert written.returncode == 0, f"生成失败：{written.stdout}{written.stderr}"
        assert path.is_file(), "脚本没有写出文件"

        checked = _run_lock_script("--check", "--lock", str(path))
        assert checked.returncode == 0, (
            f"--check 判了不一致（本不该）：{checked.stdout}{checked.stderr}"
        )

    def test_fails_when_a_locked_version_differs(self, tmp_path: Path) -> None:
        """版本对不上时必须**返回非 0 并把差异打出来**。

        只打印不返回非 0 的校验脚本等于没有：CI 只看退出码。
        """
        lines = ["numpy==0.0.1" if line.startswith("numpy==") else line for line in _body_lines()]
        assert any(line == "numpy==0.0.1" for line in lines), (
            "lock 里没有 numpy，这条用例的前提不成立"
        )
        path = _stale_lock(tmp_path, lines)

        proc = _run_lock_script("--check", "--lock", str(path))
        assert proc.returncode != 0, f"lock 与环境的版本不同，却没报错：{proc.stdout}"
        assert "numpy" in proc.stdout, f"差异里没提到出问题的包：{proc.stdout}"
        assert "0.0.1" in proc.stdout, f"差异里没打出 lock 上的版本：{proc.stdout}"
        assert LOCK_SCRIPT.name in proc.stdout, '没有给出"怎么修"的指引（重新生成命令）'

    def test_fails_when_a_package_is_missing_from_the_lock(self, tmp_path: Path) -> None:
        """lock 少了一个当前环境里装着的包 → 同样必须非 0。

        这是"手动编辑 lock 删了一行"的形态：文件语法完全合法，
        只有和真实环境对比才看得出来 —— 也就是只有 --check 能发现。
        """
        lines = [line for line in _body_lines() if not line.startswith("redis==")]
        assert len(lines) == len(_body_lines()) - 1, "lock 里没有 redis，这条用例的前提不成立"
        path = _stale_lock(tmp_path, lines)

        proc = _run_lock_script("--check", "--lock", str(path))
        assert proc.returncode != 0, f"lock 少了一个包，却没报错：{proc.stdout}"
        assert "redis" in proc.stdout, f"差异里没提到缺的包：{proc.stdout}"

    def test_fails_for_a_package_the_environment_does_not_have(self, tmp_path: Path) -> None:
        """反过来：lock 里有一个环境里根本不存在的包 → 非 0。

        这一条实践里最常出现：改完 pyproject 手工往 lock 里添了一行。
        包名是编的，所以它在任何平台上都不该存在 —— 这条断言是平台无关的。
        """
        path = _stale_lock(tmp_path, [*_body_lines(), "ghost-package==0.0.1"])

        proc = _run_lock_script("--check", "--lock", str(path))
        assert proc.returncode != 0, f"lock 里有一个不存在的包，却没报错：{proc.stdout}"
        assert "ghost-package" in proc.stdout, f"差异里没提到多余的包：{proc.stdout}"

    def test_never_rewrites_the_lock_file(self, tmp_path: Path) -> None:
        """--check 绝不能顺手把 lock 改好。

        CI 里"自动修好"等于把问题藏起来：构建变绿了，而 lock 与环境的
        不一致没有任何人看见。校验模式的职责是**报告**，不是修复。
        """
        path = _stale_lock(tmp_path, [*_body_lines(), "ghost-package==0.0.1"])
        before = path.read_bytes()

        proc = _run_lock_script("--check", "--lock", str(path))

        assert path.read_bytes() == before, "--check 改了 lock 文件（它只应该读）"
        assert proc.returncode != 0, "既然 lock 是坏的，退出码就该非 0"
