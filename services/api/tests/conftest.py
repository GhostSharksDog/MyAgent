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
