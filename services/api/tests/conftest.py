"""pytest 共享夹具。

【为什么 TestClient 必须是 session 级，而不是每个模块一个】

`sse-starlette` 在**模块级**维护了一个全局的
`AppStatus.should_exit_event`（`asyncio.Event`）。它第一次被使用时
就绑定到当时的事件循环上，之后换循环就会抛：

    RuntimeError: <asyncio.locks.Event object> is bound to a different event loop
      File sse_starlette/sse.py, in _listen_for_exit_signal
        await AppStatus.should_exit_event.wait()

`TestClient` 的 `with` 块会启动一个**新的事件循环**来跑 lifespan。
所以"每个测试模块各建一个 TestClient"必然在第二个模块的流式端点上报错 ——
而单独跑任何一个模块都是通过的，属于典型的跨模块污染。

修法是整个测试会话共用一个 TestClient：一个应用、一个事件循环、一个进程，
这也更贴近真实运行形态。

（这是测试基础设施对框架全局状态的适配，不是被测代码的缺陷 ——
分清这一点很重要，否则会跑去改本来没问题的生产代码。）
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    """全测试会话共享的 HTTP 客户端（含 lifespan）。"""
    with TestClient(app) as c:
        yield c


def _set_corpus_paths(monkeypatch: pytest.MonkeyPatch, paths: str) -> None:
    """改写"知识库数据源"这一项配置，并清掉进程内共享的检索索引。

    直接改 `get_settings()` 返回的那个实例上的 `agent`（与 test_file_tools.py
    改 workspace_root 的做法一致）：配置是单例，改实例才能让懒加载工厂
    真的读到新值；`monkeypatch` 会在用例结束时还原。

    共享检索器是**进程级**的，必须一起清掉 —— 否则上一个用例建好的索引
    会被下一个用例当成自己的，失败信息会指向完全无关的地方。
    """
    from app.core.config import get_settings
    from app.rag.factory import reset_shared_retriever

    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "agent",
        settings.agent.model_copy(update={"corpus_paths": paths}),
        raising=False,
    )
    reset_shared_retriever()


@pytest.fixture(scope="session", autouse=True)
def _hermetic_baseline() -> Iterator[None]:
    """把 Agent 配置钉在一个确定的基线上，**不受开发者 `.env` 影响**。

    ============================================================
    这个夹具是为一次真实的、莫名其妙的失败加的
    ============================================================
    我把设置界面接好之后，"打开文件夹"的验证脚本往 `.env` 里写了一次工作区路径。
    之后跑测试时：

        FAILED test_tools.py::TestRegistry::test_general_profile_registers_only_core_tools

    它断言通用形态只有 3 个核心工具 —— 而文件工具也被注册了，
    因为 `.env` 里配了工作区，文件工具就会被加载。

    **被测代码没有错，测试的口径也没错，错的是"测试读了开发者的机器配置"。**
    这类失败最消耗人的地方在于：它看起来像功能坏了，而实际上"换台机器跑就绿了"——
    于是你会去改本来没问题的代码。

    ============================================================
    为什么必须是**会话级**，而不是每个用例一个
    ============================================================
    我第一版写成了 function 级，结果 `test_tools` 绿了、三个 API 测试还是红的：

        test_api.py:155: assert 7 == 3

    因为那几个用例走的是**会话级**的 `client` 夹具 —— 它在任何 per-test 夹具
    之前就把 app 建好了（lifespan 里读配置、注册工具），钉晚了就没用。

    **钉基线的夹具必须比它要影响的对象的生命周期更长。** 这是个很容易踩的坑：
    function 级夹具看起来"更规范"，但在这里根本来不及。

    【为什么用 autouse】
    需要这一层的用例是"绝大多数"，而漏加一个的代价是偶发的、依赖机器状态的失败。
    默认生效、需要时显式覆盖（seeded_corpus / empty_corpus / test_file_tools 的
    工作区夹具），比反过来安全。

    ============================================================
    为什么最后改成了**设环境变量**，而不是改 `settings` 实例
    ============================================================
    我前两版都在改 `get_settings()` 返回的那个对象（function 级 → session 级），
    每一版都能让一部分用例变绿，但总有新的漏出来：

        function 级 → test_tools 绿了，三个 API 用例还是红的（app 早建好了）
        session 级  → API 用例绿了，test_tools / test_tasks 又红了

    根因是 **`get_settings` 是 `lru_cache` 的，而 `test_settings_api.py` 的夹具
    会调 `cache_clear()`** —— 那个调用把缓存连同我打在旧实例上的补丁一起丢掉，
    下一次 `get_settings()` 会重新从 `.env` 读出一个**没被钉住的**实例。

    所以补丁的位置错了：**改一个会被替换掉的对象，等于没改。**
    改成设 `os.environ` 之后，无论实例被重建多少次、从哪读，
    环境变量的优先级都高于 `.env`，基线始终成立。

    这条经验值得记：**要钉住一个可重建的缓存对象，就去钉它的数据来源，
    而不是钉它的某个副本。**
    """
    from app.core.config import get_settings
    from app.rag.factory import reset_shared_retriever

    baseline = {
        "AGENT_PROFILE": "general",
        "AGENT_WORKSPACE_ROOT": "",
        "AGENT_CORPUS_PATHS": "",
        "AGENT_CORPUS_INCLUDE_SEED": "false",
    }
    # 手动存取而不是 monkeypatch —— 后者是 function 级的，在 session 夹具里用不了
    saved = {k: os.environ.get(k) for k in baseline}
    os.environ.update(baseline)
    get_settings.cache_clear()
    reset_shared_retriever()
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()
    reset_shared_retriever()


@pytest.fixture
def seeded_corpus(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把知识库语料显式指向可提交的**公开示例数据**（services/api/seed/）。

    【为什么需要这个前提声明，而不是依赖默认配置】
    通用（默认）形态下语料就是**空的** —— 这是有意的默认值：
    数据源由用户声明，不是系统替用户猜（见 `build_corpus` 的说明）。
    所以任何"需要非空语料"的用例都必须自己把来源说清楚，
    否则它实际测到的是"空语料下的行为"，而失败信息完全不会提示这一点。

    用 seed/ 而不是 data/：示例数据不含隐私、可提交，CI 与协作者都能跑。
    """
    from app.core.config import PROJECT_ROOT
    from app.rag.factory import reset_shared_retriever

    _set_corpus_paths(monkeypatch, str(PROJECT_ROOT / "services" / "api" / "seed"))
    try:
        yield
    finally:
        reset_shared_retriever()


@pytest.fixture
def empty_corpus(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把知识库固定为**默认形态：空语料**（未声明任何数据源）。

    默认值本来就是空的，但前提仍要写出来：本机 .env 里任何一个
    AGENT_CORPUS_PATHS 都会让"空语料"用例悄悄变成"非空语料"用例 ——
    而它测的恰恰是空语料下的行为。
    """
    from app.rag.factory import reset_shared_retriever

    _set_corpus_paths(monkeypatch, "")
    try:
        yield
    finally:
        reset_shared_retriever()
