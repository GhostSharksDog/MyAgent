"""配置与依赖清单的**覆盖率**测试（技术债 T16 / T17）。

【为什么这两件事值得各写一个测试】

它们都属于「清单与代码不一致」这一类问题，而这类问题的共同特征是
**完全静默**：

  1. **`.env.example` 里的键，`Settings` 读不到**（T16）。
     pydantic-settings 的默认行为是 `extra="ignore"` —— 一个拼错的键
     或一个已经改名的字段，不会报错，只会被**悄悄丢掉**。
     用户的体验是"我明明按文档配了，怎么没生效？"，而日志里连一行 WARNING 都没有。
     这个坑本项目已经踩过一次（ADR-007：嵌套的 BaseSettings 不继承父级 env_file，
     于是 `.env` 里的 LLM_API_KEY 读不到，表现为"写了 .env 却报未配置密钥"）。

  2. **代码 import 了没声明的包**（T17）。
     本机装过、所以跑得通；别人 clone 下来装依赖时会 ModuleNotFoundError，
     而报错发生在运行期而不是安装期 —— 也就是"在你机器上是好的"。
     这类漂移在没有 lock 文件的项目里必然发生，所以至少要有一条测试盯着。

两条都是"清单类"断言：不测行为，只测**清单与代码是否还互相对得上**。
它们不昂贵，但能挡住一整类"改了代码忘了改文档/清单"的回归。
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
from app.core.config import PROJECT_ROOT, Settings, get_settings
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings

ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
API_DIR = PROJECT_ROOT / "services" / "api"
PYPROJECT = API_DIR / "pyproject.toml"


# ============================================================
# T16：.env.example 的每个键都必须真的被读到
# ============================================================
def _env_keys() -> list[str]:
    """`.env.example` 里声明的所有键（忽略注释与空行）。"""
    keys: list[str] = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.split("=", 1)[0].strip())
    return keys


def _nested_settings() -> dict[str, type[BaseSettings]]:
    """Settings 里所有的嵌套配置类（按字段名）。"""
    out: dict[str, type[BaseSettings]] = {}
    for name, field in Settings.model_fields.items():
        if isinstance(field.annotation, type) and issubclass(field.annotation, BaseSettings):
            out[name] = field.annotation
    return out


def _readable_keys() -> dict[str, tuple[str, str, str]]:
    """Settings 真正能读到的环境变量名。

    Returns:
        {环境变量名: (嵌套字段名, 子字段名, 前缀)}
        顶层字段的"嵌套字段名"用空串表示。
    """
    keys: dict[str, tuple[str, str, str]] = {}
    top_prefix = str(Settings.model_config.get("env_prefix", "") or "")
    for name in Settings.model_fields:
        if name in _nested_settings():
            continue
        keys[f"{top_prefix}{name}".upper()] = ("", name, top_prefix)

    for outer, cls in _nested_settings().items():
        prefix = str(cls.model_config.get("env_prefix", "") or "")
        for sub in cls.model_fields:
            keys[f"{prefix}{sub}".upper()] = (outer, sub, prefix)
    return keys


class TestEnvExampleCoverage:
    """`.env.example` 必须与配置层一致。

    【`_NORMALIZED` 这张小表是什么】
    绝大多数键的规则是"文档写什么，字段就是什么"。但有极少数键的值会被
    **有意规范化**，其中最典型的是 `DATABASE_URL`：

        文档写  sqlite+aiosqlite:///./data/legacy.db（相对路径）
        字段是  sqlite+aiosqlite:///D:/WXP/简历/MyAgent/data/legacy.db

    因为那个 `./` 是相对**当前工作目录**的 —— 从 `services/api` 启动与从仓库根
    启动会落到两个不同文件，而两个都写成功，用户以为历史丢了。
    规范化的目的就是消除这个 CWD 依赖。

    所以这里对这类键比对"规范化后的结果"。**这不是放松检查**：
    键名映射写错时它照样会红（映射错就会落到另一个字段上）。
    """

    _NORMALIZED: ClassVar[dict[str, Callable[[str], object]]] = {
        # 相对路径 → 项目根下的绝对路径（见 Settings._resolve_sqlite_path）
        "DATABASE_URL": staticmethod(lambda raw: Settings._resolve_sqlite_path(raw)),
        "RUN_HISTORY_PATH": staticmethod(lambda raw: str((PROJECT_ROOT / raw).resolve())),
    }

    def test_every_documented_key_is_readable(self) -> None:
        """文档里写了的键，代码必须真的读它（否则它是骗人的）。"""
        readable = _readable_keys()
        unknown = [k for k in _env_keys() if k not in readable]
        assert not unknown, (
            "这些键写在 .env.example 里，但没有任何配置类会读它们 —— "
            "用户按文档配置后不会有任何反应，也不会有报错：\n  " + "\n  ".join(unknown)
        )

    def test_documented_value_actually_lands_in_the_right_field(self) -> None:
        """不只"键被认识"，还要"值真的落到对应字段上"。

        【为什么要做到这一步】
        键名对得上、字段也存在，仍然可能因为前缀写错而落到别处
        （`RESILIENCE_` 写成 `RESILIENCY_` 时，键会"看起来"存在）。
        所以这里直接用文档里的**示例值**去实例化一次 Settings，
        再断言它出现在那个字段上 —— 这比"键名匹配"强得多。
        """
        import os

        # 只跳过密钥类：它是 SecretStr 包装的，拿来当指纹只会多一层包装逻辑，
        # 而"键被认识"已由上一条测试覆盖。
        skip = {"LLM_API_KEY"}

        values: dict[str, str] = {}
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key in skip:
                continue
            values[key] = value.strip()

        # 用文档里的值真的构造一次 Settings（环境变量优先于 .env）
        saved = {k: os.environ.get(k) for k in values}
        os.environ.update(values)
        get_settings.cache_clear()
        try:
            settings = Settings()
            readable = _readable_keys()
            # 逐键断言：至少要有**一个**字段拿到了文档里的值
            # （空值键改成断言"该字段存在"即可，因为空串无法作为指纹）
            mismatched: list[str] = []
            for key, raw in values.items():
                outer, sub, _prefix = readable[key]
                field = (
                    Settings.model_fields[sub]
                    if not outer
                    else _nested_settings()[outer].model_fields[sub]
                )
                target = settings if not outer else getattr(settings, outer)
                actual = getattr(target, sub)
                if raw == "":
                    continue  # 空值没有指纹，键存在性已由上一条测试覆盖
                if _matches(actual, raw, field):
                    continue
                if self._NORMALIZED.get(key, lambda _raw: None)(raw) == actual:
                    # 值被有意规范化（见类文档里的 _NORMALIZED 说明）
                    continue
                mismatched.append(f"{key}={raw!r} → {outer or '(顶层)'}.{sub} 实际是 {actual!r}")
            assert not mismatched, "文档里的示例值与实际读到的值不一致：\n  " + "\n  ".join(
                mismatched
            )
        finally:
            for key, original in saved.items():
                if original is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original
            get_settings.cache_clear()

    def test_every_nested_settings_declares_its_own_env_file(self) -> None:
        """ADR-007 的坑：嵌套 BaseSettings **不会**继承父级的 env_file。

        症状是"写了 .env 却读不到"，而报错发生在完全无关的地方
        （比如"未配置密钥"）。这条断言把这个坑变成一句会失败的测试。
        """
        for name, cls in _nested_settings().items():
            config = cls.model_config
            assert config.get("env_prefix"), f"{name} 没有自己的 env_prefix"
            assert config.get("env_file"), (
                f"{name} 没有声明 env_file —— 它的键无法从 .env 读到（ADR-007 的坑）"
            )


def _matches(actual: Any, raw: str, field: FieldInfo) -> bool:
    """文档里的字符串值是否与读到的字段值一致（尽量宽松，避免误报）。"""
    from pydantic import SecretStr

    if isinstance(actual, SecretStr):
        return actual.get_secret_value() == raw
    if isinstance(actual, bool):
        return actual is (raw.strip().lower() in ("1", "true", "yes", "on"))
    if isinstance(actual, (int, float)) and not isinstance(actual, bool):
        try:
            return float(actual) == float(raw)
        except ValueError:
            return False
    if isinstance(actual, str):
        # 枚举类字段（如 AppEnv）比较的是值；空串一律算匹配
        return actual == raw or actual == raw.strip()
    # 列表等结构：只要解析后非空就算对上（当前 .env.example 里没有这类键）
    return True


# ============================================================
# T17：代码 import 的第三方包必须在依赖清单里
# ============================================================
_STDLIB = set(sys.stdlib_module_names)

# **发布名 → import 名**（键是"统一成下划线"的发布名）。
# 缺了这份映射，测试会把已经声明过的包误报成未声明，而一个会误报的检查
# 很快就会被人忽略 —— 那比没有检查更糟，因为它给的是虚假的安全感。
_IMPORT_NAME = {
    "scikit_learn": "sklearn",
    "python_docx": "docx",
    "python_dotenv": "dotenv",
    "pyyaml": "yaml",
    "sse_starlette": "sse_starlette",  # 名字相同，留着表明它被想过
    "pillow": "PIL",
}


def _declared_distributions() -> set[str]:
    """依赖清单里声明的**发布名**（含可选 extras），统一成下划线形式。"""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data.get("project", {})
    raw: list[str] = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        raw.extend(group)

    names: set[str] = set()
    for spec in raw:
        # "uvicorn[standard]>=0.32" → "uvicorn"；"pydantic-settings" → "pydantic_settings"
        name = re.split(r"[<>=!\[;]", spec.strip(), maxsplit=1)[0].strip()
        names.add(name.lower().replace("-", "_"))
    return names


def _import_name(distribution: str) -> str:
    """发布名 → 顶层 import 名（没有映射的按同名处理）。"""
    return _IMPORT_NAME.get(distribution, distribution)


def _imported_top_level(package_dir: Path) -> set[str]:
    """扫描某个包目录下所有 .py 的**顶层** import 名。"""
    found: set[str] = set()
    for path in package_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    found.add(node.module.split(".")[0])
    return found


def _is_installed(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


class TestDependencyDeclaration:
    """代码 import 的东西必须写在清单里，而且必须真的装得上。"""

    def test_every_third_party_import_is_declared(self) -> None:
        """代码 import 的每个第三方包，都必须在依赖清单里有对应条目。

        【这条测试第一次跑就抓到了 4 个真实问题】
        它一上来就报了 `pypdf` / `python-docx` / `fakeredis` / `starlette` /
        `scipy` —— 全都是"本机装了所以跑得通、别人 clone 下来会在运行期才炸"
        的那一类。其中：
          · pypdf / python-docx / fakeredis 是**设计上可选**的（加载器与
            会话工厂都写了明确的安装指引），所以修法是加 extras；
          · starlette / scipy 是被**直接 import** 的传递依赖，修法是显式声明。
        """
        declared = _declared_distributions()
        # 声明侧也换算成 import 名，两边才有可比性（sklearn ← scikit-learn）
        declared_imports = {_import_name(d) for d in declared}
        app_dir = API_DIR / "app"

        # 本项目自己的包与标准库不算第三方
        local = {"app", "tests", "scripts"}
        undeclared: list[str] = []
        for name in sorted(_imported_top_level(app_dir)):
            if name in _STDLIB or name in local:
                continue
            if name.lower() in declared or name.lower() in declared_imports:
                continue
            undeclared.append(name)

        assert not undeclared, (
            "这些包在 app/ 里被 import，但没有写进 pyproject.toml —— "
            "本机装了所以跑得通，别人 clone 下来会在运行期才炸：\n  " + "\n  ".join(undeclared)
        )

    def test_declared_requirements_are_all_installed(self) -> None:
        """反过来：清单里声明的包必须真的装着。

        【为什么这条同样重要】
        "清单里有、环境里没装"意味着开发环境与清单已经分叉 ——
        跑测试时会以一个看似无关的 ImportError 暴露出来。
        """
        missing = [
            dist
            for dist in sorted(_declared_distributions())
            if not _is_installed(_import_name(dist))
        ]
        assert not missing, f"依赖清单里有但环境里没装：{missing}"


def test_mypy_and_ruff_are_declared_as_dev_dependencies() -> None:
    """开发工具也要声明 —— 否则 CI 上装不出 lint 环境，而本地一切正常。"""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev = " ".join(data.get("project", {}).get("optional-dependencies", {}).get("dev", []))
    for tool in ("pytest", "ruff", "mypy"):
        assert tool in dev, f"dev 依赖里缺少 {tool}"


class TestProjectRootInference:
    """`PROJECT_ROOT` 的推断必须同时适配**源码树**与**镜像**两种目录布局。

    【为什么这组测试值得存在 —— 它是"从没跑过容器"的直接产物】
    原来是 `Path(__file__).resolve().parents[4]`，也就是把**仓库的目录深度**
    写进了代码（services/api/app/core/config.py 正好五层）。
    镜像里代码在 /app/app/core/config.py（三层）→ 取下标直接 IndexError
    → rag / worker 两个容器无限重启，而单元测试全绿（它们跑在源码树里）。

    更隐蔽的是**修法本身也会错**：`services/api` 恰好长得像镜像的 `/app`
    （有 pyproject.toml、有 app/），所以"在同一层里依次判断两条规则"
    会让源码树命中 services/api —— 后果是 `.env` 被找到不存在的地方，
    表现为"未配置 LLM_API_KEY"。**规则顺序错了不会报错，只会给出一个
    看起来合理的错误答案**，所以两种布局都要断言。
    """

    @staticmethod
    def _tree(tmp_path: Path, layout: str) -> Path:
        """按指定布局造一个假的项目树，返回"config.py 应该在哪"。"""
        if layout == "source":
            (tmp_path / "services" / "api" / "app" / "core").mkdir(parents=True)
            (tmp_path / "services" / "api" / "pyproject.toml").write_text("", encoding="utf-8")
            (tmp_path / "data").mkdir()
            return tmp_path / "services" / "api" / "app" / "core" / "config.py"
        # 镜像：/app/app/core/config.py + /app/pyproject.toml + /app/app/
        (tmp_path / "app" / "core").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        (tmp_path / "data").mkdir()
        return tmp_path / "app" / "core" / "config.py"

    def test_source_tree_resolves_to_repo_root(self, tmp_path: Path) -> None:
        """源码树里必须得到**仓库根**，而不是 services/api。

        这是上面说的"规则顺序"陷阱：`services/api` 也满足镜像那条规则。
        判断错的后果是 `.env` 找错地方 —— 密钥读不到，而报错说的是
        "未配置 LLM_API_KEY"，排查方向直接歪掉。
        """
        from app.core.config import _infer_project_root

        here = self._tree(tmp_path, "source")
        assert _infer_project_root(here) == tmp_path

    def test_image_layout_resolves_to_app_dir(self, tmp_path: Path) -> None:
        """镜像布局里必须得到 /app（data/ 与 pyproject.toml 所在的那层）。"""
        from app.core.config import _infer_project_root

        assert _infer_project_root(self._tree(tmp_path, "image")) == tmp_path

    def test_no_marker_does_not_raise(self, tmp_path: Path) -> None:
        """两种标记都没有时也**不能抛异常**。

        配置层在导入期崩掉会让所有排查手段都用不上（连 --help 都跑不起来）。
        给一个"看起来对"的目录，比抛异常好。
        """
        from app.core.config import _infer_project_root

        lonely = tmp_path / "a" / "b" / "c" / "d" / "e" / "config.py"
        lonely.parent.mkdir(parents=True)
        assert isinstance(_infer_project_root(lonely), Path)

    def test_env_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """非常规布局可以用 PROJECT_ROOT 环境变量直接指定。"""
        from app.core.config import _infer_project_root

        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "custom"))
        assert _infer_project_root(tmp_path / "whatever" / "config.py") == tmp_path / "custom"


@pytest.mark.parametrize("module", ["fakeredis", "tiktoken"])
def test_optional_helpers_are_declared_or_absent(module: str) -> None:
    """可选依赖：要么声明，要么代码里别用 —— 不能"本机碰巧装了"。

    `fakeredis`（会话/队列的假 Redis）与 `tiktoken`（token 估算）都属于
    "本机装了但清单里没有"的那一类。这条测试把这种状态变成显式的选择：
    用，就必须声明；不用，就不该出现在代码里。
    """
    used_in_code = module in _imported_top_level(API_DIR / "app")
    declared = module in _declared_distributions() or module in {
        _import_name(d) for d in _declared_distributions()
    }
    assert not used_in_code or declared, (
        f"{module} 在代码里被使用，但没写进依赖清单 —— 本机能跑只是因为环境里碰巧装了它"
    )
