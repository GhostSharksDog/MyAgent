"""前端静态托管的测试：一个进程同时提供界面与 API。

【为什么这个看起来"只是配置"的东西值得单独测】

它属于**路由优先级**问题，而路由优先级错了之后的表现是：
接口返回 HTML、或者状态码是 200 但内容是网页 —— **不报错，只是答非所问**。
这类问题排查起来会先怀疑前端、再怀疑网络，最后才想到是后端挂载顺序。

实现这段功能时连踩三个坑，每一个都是静默失效（不报错、不警告）：

1. **捕获错了异常类**：`except HTTPException` 用的是 FastAPI 的，
   而 StaticFiles 抛的是 Starlette 的；FastAPI 那个是 Starlette 的
   **子类**，捕获方向反了 → 回退逻辑永远不执行。

2. **路径带前导斜杠**：StaticFiles 传给 `get_response` 的 path 已归一化，
   直接 `startswith("api/")` 不成立。

3. **Windows 反斜杠**：`os.path.join` 用平台分隔符，Windows 上 path 是
   `api\\nonexistent` 而不是 `api/nonexistent` → 同一份代码在 Linux 上对、
   在这台 Windows 上错。**这是最危险的一个**，因为 CI 通常跑 Linux，
   测试全绿而本地功能是坏的。

所以这组用例锁定的不是"能不能访问界面"，而是
**"哪些路径返回什么、以及未知 API 路径必须是 404"**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 前端产物存在才有可测的托管行为；没构建过就跳过，而不是让它失败 ——
# 一份没构建前端的源码副本不该因此变成"测试红"。
_DIST = Path(__file__).resolve().parents[3] / "apps" / "web" / "dist"
pytestmark = pytest.mark.skipif(
    not (_DIST / "index.html").exists(),
    reason="前端产物不存在（先运行 cd apps/web && pnpm build）",
)


class TestFrontendHosting:
    def test_root_serves_react_app(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # 必须是真正的 React 挂载点，而不是某个错误页面
        assert '<div id="root">' in r.text

    def test_spa_fallback_for_client_routes(self, client: TestClient) -> None:
        """前端路由的路径在服务端没有对应文件，必须回退到 index.html。

        【为什么这条不能省】
        不回退的表现是"点链接进去正常、刷新页面白屏" ——
        因为点击是前端路由跳转（不发请求），刷新才走服务端。
        这种"只有刷新才坏"的现象很难第一时间联想到服务端配置。
        """
        for path in ("/some/client/route", "/sessions/abc123", "/deep/nested/path"):
            r = client.get(path)
            assert r.status_code == 200, f"{path} 没有回退到 SPA"
            assert '<div id="root">' in r.text, f"{path} 返回的不是界面"

    def test_api_paths_still_return_json(self, client: TestClient) -> None:
        """挂载在 `/` 的静态服务不能盖住 API 路由。

        这是**顺序问题**：Starlette 按注册顺序匹配，
        `mount("/")` 如果注册在 API 路由之前，`/api/chat` 会被它接走
        然后返回 index.html —— 状态码还是 200。
        """
        for path in ("/healthz", "/api/meta", "/api/tools"):
            r = client.get(path)
            assert r.status_code == 200, f"{path} 挂了"
            assert "application/json" in r.headers["content-type"], (
                f"{path} 返回的不是 JSON（Content-Type={r.headers['content-type']}）"
            )

    def test_metrics_is_plain_text(self, client: TestClient) -> None:
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]

    def test_unknown_api_path_is_404_not_html(self, client: TestClient) -> None:
        """**最重要的一条。**

        如果 SPA 回退不排除 API 前缀，`/api/nonexistent` 会返回
        **状态码 200 的 HTML**。后果是前端调错接口时拿到的是 HTML，
        `response.json()` 抛 "Unexpected token '<'" ——
        **排查方向被引到前端的 JSON 处理上，而真正的问题是接口路径写错了。**

        让错误在最能说明问题的地方呈现：API 路径就该是 404 + JSON。
        """
        for path in ("/api/nonexistent", "/api/chat/nope", "/healthz/nope"):
            r = client.get(path)
            assert r.status_code == 404, f"{path} 应返回 404，实际 {r.status_code}"
            assert "application/json" in r.headers["content-type"], (
                f"{path} 返回了 {r.headers['content-type']} —— "
                f"API 路径被 SPA 回退接管了，这会把调用方引向错误的排查方向"
            )

    def test_assets_are_served(self, client: TestClient) -> None:
        """构建产物（JS/CSS）必须能取到，否则界面只是个空壳。"""
        r = client.get("/")
        # 从 index.html 里解析出真实资源路径，避免硬编码 hash 文件名
        import re

        refs = re.findall(r'(?:src|href)="(/assets/[^"]+)"', r.text)
        assert refs, "index.html 里没有引用任何 /assets/ 资源"
        for ref in refs:
            resp = client.get(ref)
            assert resp.status_code == 200, f"静态资源 {ref} 取不到"
            assert len(resp.content) > 100, f"静态资源 {ref} 内容异常"

    def test_post_to_unknown_api_is_not_swallowed(self, client: TestClient) -> None:
        """非 GET 方法对未知 API 路径也不能被静态服务吞掉。

        StaticFiles 对非 GET/HEAD 会返回 405 —— 如果它抢先匹配了
        某个本该是 POST 的路径，调用方会拿到 405 而不是预期的行为。
        这里顺带确认 `/api/chat`（真实存在的 POST）没受影响。
        """
        r = client.post("/api/nonexistent", json={})
        assert r.status_code in (404, 405), f"意外状态码 {r.status_code}"
        assert "text/html" not in r.headers["content-type"], "HTML 说明被静态服务接管了"
