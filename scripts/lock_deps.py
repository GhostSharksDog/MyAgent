"""生成 / 校验 `services/api/requirements.lock`（技术债 T21：依赖没有锁定）。

【为什么这个项目必须锁依赖，别的项目可以偷懒】

本项目的**主要证据是一组检索指标**（Recall@k / MRR，见 eval_results/），
而 scikit-learn / numpy / scipy 的小版本差异足以改变 TF-IDF 与稀疏矩阵的
计算结果 —— 指标跟着漂移。于是"指标涨了"这件事会变得无法归因：
可能是检索策略真的改好了，也可能只是装到了一个不同的版本。
`pyproject.toml` 只写了下限（`>=`），不锁的话，别人 clone 之后装出来的版本
取决于他装的那一天，"我这边 Recall@5 是 0.71"这句话一周后就不成立了。

锁文件记的是**在某个具体环境上求出的一个已知可用解（known-good solution）**，
不是"最新版本"清单：它的价值在**可复现**，不在新。
所以本脚本不是解析器（它不做版本选择），而是**记录器 + 一致性校验器** ——
记录"这一个已经跑通了全部测试的环境"，然后校验 lock 与它是否还对得上。

【为什么不用 pip-tools / uv / poetry】
本机网速约 60–90 kB/s，装任何新工具都是几分钟起步、还经常失败；
而这件事用标准库就做完了：`importlib.metadata` 里已经有每个包的已装版本与
`Requires-Dist`，走一遍依赖图就够了。少一个工具就少一个"工具本身装不上"的
失败点，也就少一个"CI 上只有它跑不起来"的原因。

用法：
    python scripts/lock_deps.py                  # 按当前环境重新生成 lock
    python scripts/lock_deps.py --check          # 只校验，不写文件（CI 用）
    python scripts/lock_deps.py --lock /tmp/x    # 指向别的 lock（测试用）

退出码：0 = 一致 / 写入成功；1 = --check 发现差异；2 = 无法求解（缺包、标记不认识）
"""

from __future__ import annotations

import argparse
import ast
import os
import platform
import re
import sys
from collections import deque
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYPROJECT = REPO_ROOT / "services" / "api" / "pyproject.toml"
DEFAULT_LOCK = REPO_ROOT / "services" / "api" / "requirements.lock"

# CI 只看非 0，但人排查时要能一眼分开两类失败：
# "lock 过期了（重新生成即可）" 与 "脚本自己没能求解（是环境缺东西）"。
EXIT_OK = 0
EXIT_STALE = 1
EXIT_UNRESOLVED = 2

# Native Windows wheels must not be unconditionally installed in the Linux image.
PLATFORM_MARKERS = {"pywin32": 'sys_platform == "win32"'}

# 版本变量必须按**版本**比大小，不能按字符串比：
# 字符串比较下 "3.12" < "3.8" 为真，于是 `python_version < "3.8"` 会把
# 本不该装的包拉进 lock（sqlalchemy 真的写着这样一条）。
_VERSION_VARS = frozenset(
    {"python_version", "python_full_version", "implementation_version"}
)

# PEP 508 的依赖串：name[extras] 版本约束 ; 环境标记
_REQUIREMENT_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\s*(?:\[(?P<extras>[^\]]*)\])?"
    r"\s*(?P<specifier>[^;]*)"
    r"(?:;\s*(?P<marker>.+))?$",
    re.DOTALL,
)


class LockError(RuntimeError):
    """求解失败。

    **故意不吞掉**：一个"猜"出来的 lock 比一句报错危险得多 ——
    它会让人以为依赖已经锁好了，而实际上锁的是一套没人验证过的版本组合。
    """


@dataclass(frozen=True)
class Requirement:
    """一条依赖声明里我们真正需要的东西：包名、extras、环境标记。"""

    name: str
    extras: frozenset[str]
    marker: str | None

    @classmethod
    def parse(cls, text: str) -> Requirement:
        match = _REQUIREMENT_RE.match(text)
        if match is None:
            raise LockError(f"看不懂这条依赖声明：{text!r}")
        extras = frozenset(
            canonical(part)
            for part in (match.group("extras") or "").split(",")
            if part.strip()
        )
        return cls(
            canonical(match.group("name")),
            extras,
            (match.group("marker") or "").strip() or None,
        )


def canonical(name: str) -> str:
    """PEP 503 规范化名字。

    `-` / `_` / `.` 在包名里等价（`typing_extensions` 与 `typing-extensions`
    是同一个包），所以**比较之前必须统一**：不统一的话，pyproject 里写
    `python-dotenv`、lock 里写 `python_dotenv` 会被判成"缺一个依赖"，
    而真正的问题（少了别的包）就被这条假差异盖住了。
    """
    return re.sub(r"[-_.]+", "-", name).strip().lower()


# ============================================================
# 环境标记（PEP 508）
# ============================================================
def _implementation_version() -> str:
    info = sys.implementation.version
    version = f"{info.major}.{info.minor}.{info.micro}"
    if info.releaselevel != "final":
        version += f"{info.releaselevel[0]}{info.serial}"
    return version


def marker_environment(extra: str) -> dict[str, str]:
    """当前环境在环境标记里能看到的那几个变量。"""
    return {
        "extra": extra,
        "os_name": os.name,
        "sys_platform": sys.platform,
        "platform_machine": platform.machine(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "platform_python_implementation": platform.python_implementation(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "python_full_version": platform.python_version(),
        "implementation_name": sys.implementation.name,
        "implementation_version": _implementation_version(),
    }


def resolved_on() -> str:
    """求解环境的可读描述，写进 lock 头部、并在 --check 时用来解释平台差异。"""
    return f"{platform.python_implementation()} {platform.python_version()} / {platform.platform()}"


def _version_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """把版本串切成可比较的元组。

    【为什么不能直接拿字符串比】
    `python_version >= "3.8"` 在字符串比较下对 3.12 是**假**（"3.12" < "3.8"），
    于是本该装的包会被静默丢掉 —— 这类错误不会报错，只会让 lock 少几个包。
    这里把数字段与字母段分开，并去掉结尾的 0（3.13 与 3.13.0 视为相等）。

    这是**够用**而不是完整的 PEP 440 偏序：本项目要比较的只有
    `python_version` / `python_full_version` 对纯数字下界，预发布版本的
    先后顺序（rc 排在正式版之前）不在其列。
    """
    parts: list[tuple[int, int, str]] = []
    for chunk in re.findall(r"\d+|[a-zA-Z]+", text):
        parts.append((1, int(chunk), "") if chunk.isdigit() else (0, 0, chunk.lower()))
    while len(parts) > 1 and parts[-1] == (1, 0, ""):
        parts.pop()
    return tuple(parts)


def _operand(node: ast.AST, env: dict[str, str]) -> tuple[str, bool]:
    """比较表达式的一端：返回值 + 它是不是"版本"（决定比较方式）。"""
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise LockError(f"环境标记里出现了不认识的变量：{node.id}")
        return env[node.id], node.id in _VERSION_VARS
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    raise LockError(f"环境标记里出现了不支持的写法：{ast.dump(node)}")


def _compare(left: tuple[str, bool], op: ast.cmpop, right: tuple[str, bool]) -> bool:
    left_value, left_is_version = left
    right_value, right_is_version = right

    # 集合/字符串成员判断（`python_version in "3.8 3.9"`）只可能是字符串语义
    if isinstance(op, ast.In):
        return left_value in right_value
    if isinstance(op, ast.NotIn):
        return left_value not in right_value

    if left_is_version or right_is_version:
        left_key: object = _version_key(left_value)
        right_key: object = _version_key(right_value)
    else:
        left_key, right_key = left_value, right_value

    if isinstance(op, ast.Eq):
        return bool(left_key == right_key)
    if isinstance(op, ast.NotEq):
        return bool(left_key != right_key)
    if isinstance(op, ast.Lt):
        return bool(left_key < right_key)
    if isinstance(op, ast.LtE):
        return bool(left_key <= right_key)
    if isinstance(op, ast.Gt):
        return bool(left_key > right_key)
    if isinstance(op, ast.GtE):
        return bool(left_key >= right_key)
    raise LockError(f"环境标记里出现了不支持的比较运算符：{ast.dump(op)}")


def _evaluate(node: ast.AST, env: dict[str, str]) -> bool:
    """在给定环境里求值一个环境标记表达式。

    【为什么用 ast 而不是自己写一个标记解析器】
    标记用的就是 Python 的表达式语法：`and`/`or`/`not`、括号、链式比较的
    优先级全都由 `ast` 免费给对（`a or b and c` 与 `(a or b) and c` 不是一回事）。
    这里只走白名单节点，不做 `eval`，所以既准确又不执行任何代码。
    """
    if isinstance(node, ast.BoolOp):
        values = [_evaluate(value, env) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _evaluate(node.operand, env)
    if isinstance(node, ast.Compare):
        left = _operand(node.left, env)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _operand(comparator, env)
            if not _compare(left, op, right):
                return False
            left = right
        return True
    raise LockError(f"环境标记里出现了不支持的写法：{ast.dump(node)}")


def marker_holds(marker: str | None, extras: frozenset[str]) -> bool:
    """按 **pip 的规则**判断一条依赖的环境标记是否成立。

    规则抄自 `pip/_internal/metadata/importlib/_dists.py::iter_dependencies`
    （照抄而不是自创，是因为这里一旦与 pip 不一致，lock 就会与
    `pip install -r` 的实际行为分叉）：

      · 没有标记 → 成立；
      · 没请求 extras → 用 `extra = ""` 求值一次（于是所有 `extra == "dev"`
        之类的可选依赖都被排除 —— 运行时 lock 不该含 dev extras）；
      · 请求了 extras → 对每个 extra 各求值一次，**任一成立**即算成立。
        这条同时是 `uvicorn[standard]` 能带进 pyyaml / httptools / websockets
        的原因：它们在 metadata 里都挂着 `extra == "standard"`。

    解析不了的标记直接抛 `LockError`：宁可让人看见一句报错，
    也不要靠猜把某个包静默地放进来或漏掉。
    """
    if not marker:
        return True
    try:
        tree = ast.parse(marker, mode="eval")
    except SyntaxError as exc:  # PEP 508 允许 `~=`，而它不是合法的 Python 表达式
        raise LockError(f"环境标记不是合法的表达式，无法判断：{marker!r}") from exc
    if not extras:
        return _evaluate(tree.body, marker_environment(""))
    return any(
        _evaluate(tree.body, marker_environment(extra)) for extra in sorted(extras)
    )


# ============================================================
# 求解：从 pyproject 的运行时依赖出发，走一遍已装环境的依赖图
# ============================================================
def declared_requirements(pyproject: Path) -> list[Requirement]:
    """`[project].dependencies`（运行时依赖）。

    刻意**只读这一个列表**：`optional-dependencies` 里的 dev / formats /
    fake-redis 都是开发机上才需要的东西，锁进运行时 lock 会让运行镜像
    白装一堆包（lxml、pypdf…），而它们与检索指标无关。
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project") or {}
    declared = [Requirement.parse(item) for item in project.get("dependencies") or []]
    if not declared:
        raise LockError(
            f"{pyproject} 里没有 [project].dependencies，这不像是一份真的清单"
        )
    return declared


def _installed(name: str, pyproject: Path) -> metadata.Distribution:
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError as exc:
        raise LockError(
            f"{name} 没有装在当前环境里，无法确定它的版本和它自己的依赖。\n"
            f"    先装上再生成：{sys.executable} -m pip install -e {pyproject.parent}\n"
            f"    （本脚本记录的是**这份环境**，不是重新求解一套理想版本，"
            f"所以缺东西时只能报错，不能替你跳过）"
        ) from exc


def resolve_runtime_closure(
    declared: list[Requirement], pyproject: Path
) -> dict[str, str]:
    """运行时依赖的传递闭包 → {规范化包名: 已装版本}。

    广度优先，并**累积 extras**：同一个包可能先被不带 extras 地需要、
    再被带 extras 地需要（`uvicorn` 与 `uvicorn[standard]`），
    第二次必须重新展开，否则 `[standard]` 那一串依赖会被漏掉。
    """
    pins: dict[str, str] = {}
    expanded: dict[str, frozenset[str]] = {}
    pending: deque[Requirement] = deque(declared)

    while pending:
        requirement = pending.popleft()
        # 注意这里必须先判断"见过没有"：不带 extras 的依赖，它的 extras 是空集，
        # 而空集是任何集合的子集 —— 少了前半句，第一个包就会被当成"已展开"跳过，
        # 结果是 lock 里只剩一个包（而且它还是错的）。
        if (
            requirement.name in expanded
            and requirement.extras <= expanded[requirement.name]
        ):
            continue  # 这个包的这套 extras 已经展开过了
        expanded[requirement.name] = (
            expanded.get(requirement.name, frozenset()) | requirement.extras
        )

        dist = _installed(requirement.name, pyproject)
        pins[requirement.name] = dist.version
        for raw in dist.requires or []:
            child = Requirement.parse(raw)
            if marker_holds(child.marker, requirement.extras):
                pending.append(child)

    return pins


# ============================================================
# 读写
# ============================================================
def render_lock(
    pins: dict[str, str], pyproject: Path, declared: list[Requirement]
) -> str:
    """渲染 lock 全文。

    【为什么第一行是 coding 声明 —— 这一条是实测出来的】
    pip 读 requirements 文件用的 `auto_decode` 在找不到 BOM 与 coding 声明时
    会退回**系统 locale 编码**。本机是 cp936，于是一份 UTF-8 的中文注释会被
    按 GBK 解码，直接抛：

        UnicodeDecodeError: 'gbk' codec can't decode byte 0xad in position 23

    也就是"注释里写了中文"会让 `pip install -r requirements.lock` 整个失败。
    头部注释是这个文件最有价值的部分（它写着为什么锁、怎么重新生成），
    不能为了绕开这个坑把注释改成英文，所以显式声明编码。
    """
    direct = len({requirement.name for requirement in declared})
    header = f"""# -*- coding: utf-8 -*-
# ============================================================
# services/api 的运行时依赖锁（技术债 T21）
# ============================================================
#
# 【这是什么】在**某一个具体环境**上求出的一个已知可用解
# （known-good solution），不是"最新版本"清单。它记录的是
# "装出来并且测试全绿的那一套"，所以它的价值在**可复现**，不在新。
#
# 【为什么要锁】本项目的证据是检索指标（Recall@k / MRR）。
# scikit-learn / numpy / scipy 的小版本差异会改变 TF-IDF 与稀疏矩阵的
# 行为，指标跟着漂移 —— 于是"指标变好了"就无法归因：可能是检索策略
# 真的改好了，也可能只是依赖换了个版本。pyproject.toml 只写了下限，
# 不锁的话，clone 之后装出来的版本取决于装的那一天。
#
# 【怎么重新生成】python scripts/lock_deps.py
# 【怎么校验】    python scripts/lock_deps.py --check
#                 （CI 用：有不一致就退出码非 0，并且不写文件）
#
# 求解环境：{resolved_on()}
#           上面这一行是**求解的地方**。带 marker 的依赖按那个平台算：
#           在 Windows 上求出的 lock 不含 uvloop（它要 `sys_platform != "win32"`），
#           所以 Linux 镜像里 uvicorn 走的是 asyncio 而不是 uvloop —— 能跑，
#           但性能特性不同。换平台请**重新生成**，不要手工往这里加行。
# 来源清单：{_display(pyproject)} 的 [project].dependencies
#           只含**运行时**传递闭包：dev / formats / fake-redis 这些 extras
#           不在其中（它们只在开发机上需要，装进运行镜像纯属浪费）。
#           要跑测试请另外装：pip install -e ".[dev]"
#
# 【为什么没有哈希】锁的是**版本**，不是字节。加 --hash 需要把每个包
# （含 sdist）下载一遍，而本机网速 60–90 kB/s；本项目要防的是
# "指标因为依赖版本漂移"，不是供应链投毒 —— 后者是另一个问题，
# 该由别的机制解决，不该顺手塞进这份文件里。
#
# 包数：{len(pins)}（直接依赖 {direct} + 传递依赖 {len(pins) - direct}）
# ============================================================
"""
    body = "".join(
        f"{name}=={pins[name]}" + (f"; {PLATFORM_MARKERS[name]}" if name in PLATFORM_MARKERS else "") + "\n"
        for name in sorted(pins)
    )
    return f"{header}\n{body}"


def read_lock(path: Path) -> dict[str, str]:
    """磁盘上的 lock → {规范化包名: 版本}。

    用 `utf-8-sig` 读：Windows 上的编辑器（记事本、某些 IDE）保存时**可能加上
    BOM**，而 pip 自己会吃掉 BOM（它的 `auto_decode` 先看 BOM）。如果这里按
    纯 utf-8 读，一个 BOM 会让第 1 行变成 `'\\ufeff# -*- coding: utf-8 -*-'` ——
    于是校验的报错说的是"第 1 行不是 name==version 形式"，
    而真正的问题是"文件被别的编辑器碰过一下"，排查方向会完全歪掉。
    """
    pins: dict[str, str] = {}
    text = path.read_text(encoding="utf-8-sig")
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        requirement, _, marker = stripped.partition(";")
        if marker and not marker_holds(marker.strip(), frozenset()):
            continue
        name, separator, version = requirement.partition("==")
        if not separator or not version:
            raise LockError(f"{path}:{lineno} 不是 name==version 形式：{stripped!r}")
        pins[canonical(name)] = version.strip()
    return pins


def _display(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


# ============================================================
# 两种模式
# ============================================================
def write_lock(
    path: Path, pins: dict[str, str], pyproject: Path, declared: list[Requirement]
) -> None:
    """
    显式 `newline="\\n"`：默认的换行转换在 Windows 上会写出 CRLF，
    于是"同一份 lock"在 Windows 与 Linux 上产生一行行无意义的 diff，
    而真正的变更（版本变了）会被淹没在里面。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_lock(pins, pyproject, declared), encoding="utf-8", newline="\n"
    )
    direct = len({requirement.name for requirement in declared})
    print(
        f"OK: 已写入 {_display(path)}（{len(pins)} 个包 = 直接 {direct} + 传递 {len(pins) - direct}）"
    )


def check_lock(path: Path, pins: dict[str, str]) -> int:
    """校验磁盘上的 lock 与当前环境一致；返回退出码。**绝不写文件。**"""
    if not path.is_file():
        print(
            f"错误：找不到 {_display(path)}。先跑一次 python scripts/lock_deps.py 生成它。"
        )
        return EXIT_STALE
    try:
        on_disk = read_lock(path)
    except LockError as exc:
        print(f"错误：{exc}")
        return EXIT_STALE

    differences: list[str] = []
    for name in sorted(set(pins) | set(on_disk)):
        expected, actual = pins.get(name), on_disk.get(name)
        if expected is None:
            differences.append(f"  + {name}=={actual}    只在 lock 里，当前环境没装")
        elif actual is None:
            differences.append(f"  - {name}=={expected}    只在当前环境里，lock 里没有")
        elif expected != actual:
            differences.append(
                f"  ~ {name}    lock 里是 {actual}，当前环境是 {expected}"
            )

    if not differences:
        print(f"OK: {_display(path)} 与当前环境一致（{len(pins)} 个包）")
        return EXIT_OK

    print(f"不一致：{_display(path)} 与当前环境对不上，共 {len(differences)} 处：\n")
    print("\n".join(differences))
    print(
        "\n常见原因：加了/升了依赖却没有重新生成 lock；或者这份环境不是按 lock 装的。\n"
        "重新生成：python scripts/lock_deps.py\n"
        '（校验模式**不会**替你改 lock：CI 里静默"修好"等于把问题藏起来）'
    )
    note = _platform_note(path)
    if note:
        print(f"\n{note}")
    return EXIT_STALE


def _platform_note(path: Path) -> str | None:
    """lock 与当前环境的平台/解释器不同时，解释一下差异的来源。

    `uvicorn[standard]` 在 Windows 上带 colorama、在 Linux 上带 uvloop ——
    这是**标记决定的预期差异**，不是 lock 过期。不解释一句的话，
    跨平台跑 CI 的人会去重新生成 lock，把两边互相改来改去。
    """
    match = re.search(
        r"^# 求解环境：(.+)$", path.read_text(encoding="utf-8"), re.MULTILINE
    )
    if match is None:
        return None
    recorded, current = match.group(1).strip(), resolved_on()
    if recorded == current:
        return None
    return (
        f"注意：这份 lock 是在 {recorded} 上求出的，当前环境是 {current}。\n"
        f"      marker 相关的包在两个平台上本来就不同（Windows 有 colorama、"
        f"Linux 有 uvloop），\n      所以上面这些差异不一定意味着 lock 过期。"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按当前已装环境生成 / 校验 services/api/requirements.lock",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验磁盘上的 lock 与当前环境是否一致，不写文件（CI 用；不一致时退出码 1）",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=DEFAULT_PYPROJECT,
        help=f"依赖清单（默认 {_display(DEFAULT_PYPROJECT)}）",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=DEFAULT_LOCK,
        help=(
            f"lock 文件路径（默认 {_display(DEFAULT_LOCK)}）。"
            "可指向临时文件 —— 测试要靠它构造一份明显过期的 lock，"
            "而不是去改动仓库里那一份"
        ),
    )
    args = parser.parse_args(argv)

    try:
        declared = declared_requirements(args.pyproject)
        pins = resolve_runtime_closure(declared, args.pyproject)
    except (LockError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_UNRESOLVED

    if args.check:
        return check_lock(args.lock, pins)
    write_lock(args.lock, pins, args.pyproject, declared)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
