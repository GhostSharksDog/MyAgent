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
