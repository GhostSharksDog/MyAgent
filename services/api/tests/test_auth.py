"""访问控制测试（技术债 T03 / T14）。

【这一组测试真正在防的是什么】

绝大多数服务被"打穿"，不是因为没有鉴权代码，而是因为**组合不自洽**：

  · 加了密钥，但预检（OPTIONS）也被拦 → 所有跨域请求 401，
    而人会去怀疑密钥写错了（真正的原因是预检不带自定义头）；
  · 加了密钥，但健康检查也要求密钥 → 探针全挂，
    然后有人把密钥写进探活配置里 → 密钥扩散到更多地方；
  · 收紧了 CORS，但服务仍然裸奔在 0.0.0.0 上 → 浏览器拦住了"页面"，
    却拦不住 curl；
  · 密钥比较用 `==` → 响应时间泄漏"前几个字符猜对了"；
  · 中间件顺序写反 → 401 响应上没有 CORS 头，浏览器把整个响应拦成
    无语义的跨域错误，用户**永远看不到"请带上 API Key"这句话**。

所以下面每一条都对应上面一个具体的坑，而不是"调用一下 API 看返回什么"。
"""

from __future__ import annotations

import pytest
from app.api.auth import (
    ExposureRefused,
    check_exposure_posture,
    extract_api_key,
    is_public_path,
)
from app.core.config import SecuritySettings, Settings, get_settings


def _settings(*, app_host: str = "127.0.0.1", **security: object) -> Settings:
    """造一个改了安全配置的 Settings（不碰 .env）。"""
    base = get_settings()
    return base.model_copy(
        update={
            "app_host": app_host,
            "security": SecuritySettings(**security),  # type: ignore[arg-type]
        }
    )


class TestPosture:
    """启动前的姿态检查：危险的组合必须**起不来**。"""

    def test_local_without_key_is_fine(self) -> None:
        """默认形态：回环 + 无密钥 —— 这是本地开发，必须零配置可用。"""
        posture = check_exposure_posture(_settings())
        assert "回环" in posture
        assert "未启用" in posture

    def test_local_with_key_says_so(self) -> None:
        """回环 + **有**密钥时，日志不能说"未启用鉴权"。

        【这条是端到端跑出来才补的】最初的回环分支直接写死了
        "未启用密钥鉴权"，于是配了密钥的本地部署会在启动日志里看到一句
        说反的话。启动日志是要被信任的输出 —— 说反比不说更危险：
        它会让人以为"配了没生效"，然后去改本来没问题的配置。
        """
        posture = check_exposure_posture(_settings(api_key="k" * 40))
        assert "已启用" in posture
        assert "未启用" not in posture

    def test_public_without_key_refuses_to_start(self) -> None:
        """**这一条是 T03 的核心**：改一个环境变量就裸奔上路，必须被拦住。"""
        with pytest.raises(ExposureRefused) as exc:
            check_exposure_posture(_settings(app_host="0.0.0.0"))
        msg = str(exc.value)
        # 报错必须**能照着做**：三条出路都写出来，否则用户只会把检查注释掉
        assert "127.0.0.1" in msg
        assert "SECURITY_API_KEY" in msg
        assert "SECURITY_ALLOW_UNAUTHENTICATED_EXPOSURE" in msg

    def test_public_with_key_is_fine(self) -> None:
        posture = check_exposure_posture(_settings(app_host="0.0.0.0", api_key="k" * 40))
        assert "已启用" in posture

    def test_explicit_opt_out_is_allowed_but_loud(self) -> None:
        """明知故犯的逃生舱：允许，但结论里必须写清楚"未启用鉴权"。"""
        posture = check_exposure_posture(
            _settings(app_host="0.0.0.0", allow_unauthenticated_exposure=True)
        )
        assert "未启用鉴权" in posture

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "LOCALHOST", " ::1 "])
    def test_loopback_spellings(self, host: str) -> None:
        """回环的几种写法都要认，否则"本地"会被误判成"公网"而拒绝启动。"""
        assert "回环" in check_exposure_posture(_settings(app_host=host))


class TestPublicPaths:
    @pytest.mark.parametrize(
        "path", ["/healthz", "/", "/index.html", "/assets/x.js", "/sessions/1"]
    )
    def test_public(self, path: str) -> None:
        assert is_public_path(path)

    @pytest.mark.parametrize(
        "path", ["/api/chat", "/api/files/content", "/metrics", "/docs", "/openapi.json"]
    )
    def test_protected(self, path: str) -> None:
        assert not is_public_path(path)

    def test_healthz_is_public_on_purpose(self) -> None:
        """健康检查必须免密钥：探针拿不到密钥，硬要就会逼人把密钥写进探活配置。"""
        assert is_public_path("/healthz")


class TestExtractApiKey:
    def _request(self, headers: dict[str, str]):  # type: ignore[no-untyped-def]
        from starlette.requests import Request

        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "method": "GET", "path": "/api/meta"})

    def test_bearer_wins(self) -> None:
        key = extract_api_key(self._request({"Authorization": "Bearer abc", "X-API-Key": "def"}))
        assert key == "abc"

    def test_x_api_key_fallback(self) -> None:
        assert extract_api_key(self._request({"X-API-Key": "def"})) == "def"

    def test_missing_is_empty(self) -> None:
        assert extract_api_key(self._request({})) == ""

    def test_other_auth_scheme_is_not_mistaken_for_a_key(self) -> None:
        """`Basic xxx` 不该被当成密钥 —— 它只是"带了 Authorization 头"。"""
        assert extract_api_key(self._request({"Authorization": "Basic abc"})) == ""


# ============================================================
# HTTP 层：真的走一遍中间件
# ============================================================
_KEY = "s3cret-key-value"


def _secured_app(*, cors_outermost: bool = True):  # type: ignore[no-untyped-def]
    """搭一个"启用了密钥"的应用实例。

    【为什么不能改 .env 或全局单例来测】
    中间件栈在应用构造时就定下来了，改配置对象影响不到已经建好的 app。
    所以这里**重新构造一个 FastAPI 应用**（只挂中间件 + 几个路由），
    测的是中间件本身的行为 —— 而不是"碰巧装出来的那个栈"。
    真实装配（main.py 里的顺序与 CORS 策略）由 TestRealAppWiring 覆盖。

    `cors_outermost=False` 用来构造"顺序写反"的对照组：Starlette 的
    `add_middleware` 是 insert(0)，**最后添加的在最外层**，
    所以"先加鉴权"才是让 CORS 落到外层的那种顺序。
    """
    from app.api.auth import ApiKeyMiddleware
    from fastapi import FastAPI
    from starlette.middleware.cors import CORSMiddleware

    settings = _settings(app_host="0.0.0.0", api_key=_KEY)
    app = FastAPI()

    def add_auth() -> None:
        app.add_middleware(ApiKeyMiddleware, settings=settings)

    def add_cors() -> None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://example.com"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Trace-Id"],
        )

    if cors_outermost:
        add_auth()
        add_cors()
    else:
        add_cors()
        add_auth()

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"status": True}

    @app.get("/api/meta")
    async def meta() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/api/chat")
    async def chat() -> dict[str, str]:
        return {"ok": "yes"}

    return app


@pytest.fixture
def secured_client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    with TestClient(_secured_app()) as c:
        yield c


class TestMiddleware:
    def test_no_key_configured_means_open(self, client) -> None:  # type: ignore[no-untyped-def]
        """默认（没配密钥）必须一切照旧 —— 这是"默认值最无害"的底线。"""
        assert client.get("/api/meta").status_code == 200

    def test_missing_key_is_rejected(self, secured_client) -> None:  # type: ignore[no-untyped-def]
        r = secured_client.get("/api/meta")
        assert r.status_code == 401
        assert "X-API-Key" in r.json()["detail"]

    def test_correct_key_via_x_api_key(self, secured_client) -> None:  # type: ignore[no-untyped-def]
        assert secured_client.get("/api/meta", headers={"X-API-Key": _KEY}).status_code == 200

    def test_correct_key_via_bearer(self, secured_client) -> None:  # type: ignore[no-untyped-def]
        r = secured_client.get("/api/meta", headers={"Authorization": f"Bearer {_KEY}"})
        assert r.status_code == 200

    def test_wrong_key_is_rejected(self, secured_client) -> None:  # type: ignore[no-untyped-def]
        assert secured_client.get("/api/meta", headers={"X-API-Key": "nope"}).status_code == 401

    def test_key_prefix_is_rejected(self, secured_client) -> None:  # type: ignore[no-untyped-def]
        """前缀必须失败。用 `==` 比较时它会"差不多对"，
        而常数时间比较下前缀与完全错误无法区分 —— 这条断言的是行为，不是实现。"""
        assert secured_client.get("/api/meta", headers={"X-API-Key": "s3cret"}).status_code == 401

    def test_healthz_stays_public(self, secured_client) -> None:  # type: ignore[no-untyped-def]
        assert secured_client.get("/healthz").status_code == 200

    def test_preflight_is_not_blocked(self, secured_client) -> None:  # type: ignore[no-untyped-def]
        """**预检必须放行**：OPTIONS 不带自定义头，拦它等于废掉所有跨域调用。

        而它的失败现象（所有跨域请求 401）会把人引向"密钥写错了"这个
        完全错误的方向。
        """
        r = secured_client.options(
            "/api/chat",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-api-key",
            },
        )
        assert r.status_code < 400
        assert r.headers.get("access-control-allow-origin") == "http://example.com"

    def test_rejection_message_does_not_leak_the_key(self, secured_client) -> None:  # type: ignore[no-untyped-def]
        """错误信息里绝不能出现正确密钥 —— 那等于把密钥发给每一个猜错的人。"""
        assert _KEY not in secured_client.get("/api/meta").text

    def test_rejection_does_not_say_which_part_was_wrong(self, secured_client) -> None:  # type: ignore[no-untyped-def]
        """不区分"没带"和"带错了"：对用户修法一样，对攻击者却是免费信息。"""
        missing = secured_client.get("/api/meta").json()["detail"]
        wrong = secured_client.get("/api/meta", headers={"X-API-Key": "nope"}).json()["detail"]
        assert missing == wrong

    def test_401_carries_cors_headers_so_the_browser_can_read_it(
        self,
        secured_client,  # type: ignore[no-untyped-def]
    ) -> None:
        """**这一条就是中间件顺序存在的理由。**

        CORS 在内层时，401 响应上没有 CORS 头 —— 浏览器会把整个响应拦成
        一个无内容的跨域错误，前端**读不到 `detail` 里那句提示**。
        用户的体验是"页面报了一个看不懂的错"，而实际发生的事是
        "服务要求 API Key"。
        """
        r = secured_client.get("/api/meta", headers={"Origin": "http://example.com"})
        assert r.status_code == 401
        assert r.headers.get("access-control-allow-origin") == "http://example.com"

    def test_wrong_order_really_would_break_the_browser(self) -> None:
        """上一条测的是"顺序对时能读到 401"；这一条是它的**反证**。

        构造一个顺序写反的应用，断言它的 401 确实**没有** CORS 头 ——
        两条合起来才说明"顺序"不是形式要求，而是那条错误的成因。

        【为什么用"构造对照组"而不是"临时改代码再跑一遍"】
        我一开始是去改文件再跑测试来验证的，结果用 PowerShell 的
        Get-Content/Set-Content 把整个文件的中文注释搞成了双重编码
        （PS 5.1 默认按 GBK 读、按 UTF-8 写）。**把验证手段做进测试里，
        比每次手工破坏源码安全得多** —— 它还能长期留在仓库里讲这件事。
        """
        from fastapi.testclient import TestClient

        with TestClient(_secured_app(cors_outermost=False)) as broken:
            r = broken.get("/api/meta", headers={"Origin": "http://example.com"})
            assert r.status_code == 401
            assert r.headers.get("access-control-allow-origin") is None, (
                "顺序写反时本应读不到 CORS 头；如果这里也有头，"
                "说明这条反证失效了（那 test_401_carries_cors_headers 就不再是有效证据）"
            )


class TestRealAppWiring:
    """装配层：main.py 里真实的中间件顺序与 CORS 策略。"""

    def test_auth_middleware_is_installed_inside_cors(self) -> None:
        """鉴权必须在 CORS **之内**（列表里越靠前越靠外）。

        这条测的是顺序本身：Starlette 的 `user_middleware` 列表，
        CORS 必须出现在 ApiKeyMiddleware 之前。
        """
        from app.api.auth import ApiKeyMiddleware
        from app.main import app
        from starlette.middleware.cors import CORSMiddleware

        classes = [m.cls for m in app.user_middleware]
        assert CORSMiddleware in classes, "CORS 中间件不见了"
        assert ApiKeyMiddleware in classes, "鉴权中间件没装上"
        assert classes.index(CORSMiddleware) < classes.index(ApiKeyMiddleware), (
            "CORS 必须在鉴权之外（列表里更靠前），否则预检 OPTIONS 会被鉴权拦成 401，"
            "且 401 响应读不到 CORS 头"
        )

    def test_default_loopback_keeps_the_dev_cors_regex(self) -> None:
        """默认（回环）保留开发用的宽松正则：`pnpm dev` 零配置可用。"""
        from app.main import app
        from starlette.middleware.cors import CORSMiddleware

        cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
        kwargs = cors.kwargs
        # 要么走精确白名单，要么退回开发正则 —— 二者必居其一
        assert kwargs.get("allow_origins") or kwargs.get("allow_origin_regex")
