"""设置读写接口的测试。

【这个文件盯的不是"能不能存配置"，而是几个**不会报错的错误**】

设置界面最麻烦的地方：绝大多数写错配置的方式**都不会立刻报错**，
而是过一会儿以别的形式表现出来。每一条对应一个真实陷阱：

    1. GET 把 API Key 原样返回 → 任何能看到页面的人都能拿到密钥
    2. 前端回传掩码 → 密钥变成 "sk-1****abcd" 写进 .env，之后所有请求 401
    3. 重写 .env 时丢掉注释 → 用户自己的配置笔记永久消失（不可恢复）
    4. 写进 .env 的键没有白名单 → 以后新增的任何配置项都默认可被界面改

【为什么分成三层，而不是全走 HTTP】

读写走的是两条完全不同的路：
  · **写** —— `app.api.settings.ENV_PATH`，我们可以随便改
  · **读** —— pydantic-settings 内部的 `env_file` 解析，它在类定义时就固定了

我第一版试图在夹具里同时改这两条路（给 `Settings.model_config` 换 env_file），
结果**读的那条路没生效**：PUT 写进临时文件、GET 仍读真实 `.env`，
于是 7 个用例失败 —— 而生产代码其实是对的。

这种"测试自身构造错误伪装成产品缺陷"，最危险的地方是它会**诱导你去改本来没问题的代码**。
所以改成三层，每层只测自己能可靠控制的东西：

  TestEnvWriter   —— 纯函数测试（临时文件），写文件的正确性全在这里
  TestHttpApi     —— 用环境变量控制"读"，用临时文件控制"写"
  TestApply       —— 用替身验证"改完立即生效"这条契约
"""

from __future__ import annotations

import pytest
from app.api import settings as settings_api
from app.core.config import get_settings
from fastapi.testclient import TestClient


@pytest.fixture
def env_file(tmp_path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """把注释里的模板写进一个临时 .env，并把 ENV_PATH 指过去。

    **绝不碰用户真实的 .env**：它被 gitignore，改坏了没有历史可以回滚。
    """
    f = tmp_path / ".env"
    f.write_text(
        "# 用户自己的注释，必须被保留\n"
        "LLM_API_KEY=sk-original-key-1234567890\n"
        "LLM_BASE_URL=https://api.example.com/v1\n"
        "LLM_MODEL=original-model\n"
        "\n"
        "# 这一行也在注释里：LLM_MODEL 不该被误伤\n"
        "AGENT_PROFILE=general\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_api, "ENV_PATH", f)
    return f


@pytest.fixture
def http_settings(env_file, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """用**环境变量**控制读取路径。

    pydantic-settings 的优先级是：环境变量 > .env 文件。
    所以这样设值最可靠 —— 不去碰框架内部的 env_file 解析。
    """
    monkeypatch.setenv("LLM_API_KEY", "sk-original-key-1234567890")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "original-model")
    monkeypatch.setenv("AGENT_PROFILE", "general")
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", "")
    monkeypatch.setenv("AGENT_CORPUS_PATHS", "")
    get_settings.cache_clear()
    yield env_file
    get_settings.cache_clear()


# ============================================================
# 第一层：.env 写入器（纯函数）
# ============================================================
class TestEnvWriter:
    def test_only_target_line_replaced(self, env_file) -> None:  # type: ignore[no-untyped-def]
        settings_api._write_env({"LLM_MODEL": "new-model"})
        lines = env_file.read_text(encoding="utf-8").splitlines()
        assert "LLM_MODEL=new-model" in lines
        assert "LLM_API_KEY=sk-original-key-1234567890" in lines, "别的键被改了"
        assert "LLM_BASE_URL=https://api.example.com/v1" in lines

    def test_comments_preserved(self, env_file) -> None:  # type: ignore[no-untyped-def]
        """**处理用户文件时，改动范围最小化是最重要的一条纪律。**

        用户的 `.env` 里通常有大量注释（这个项目每个配置项都带一段解释）。
        反序列化再写回会把它们全部抹掉 —— 而那是不可恢复的，
        因为 `.env` 被 gitignore，没有历史可以回滚。
        """
        before = [
            ln
            for ln in env_file.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("#")
        ]
        settings_api._write_env({"LLM_MODEL": "changed"})
        after = env_file.read_text(encoding="utf-8").splitlines()
        for c in before:
            assert c in after, f"注释被抹掉了：{c}"

    def test_comment_containing_key_name_untouched(self, env_file) -> None:  # type: ignore[no-untyped-def]
        """注释里出现的 `LLM_MODEL 不该被误伤` 不能被当成配置行改写。

        这是很容易踩的坑：用 `in` 或宽松正则匹配行就会中招。
        """
        settings_api._write_env({"LLM_MODEL": "xyz"})
        assert "# 这一行也在注释里：LLM_MODEL 不该被误伤" in env_file.read_text(encoding="utf-8")

    def test_key_order_preserved(self, env_file) -> None:  # type: ignore[no-untyped-def]
        def keys(text: str) -> list[str]:
            return [
                ln.split("=", 1)[0].strip()
                for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith("#") and "=" in ln
            ]

        before = keys(env_file.read_text(encoding="utf-8"))
        settings_api._write_env({"LLM_MODEL": "x"})
        after = keys(env_file.read_text(encoding="utf-8"))
        assert after[: len(before)] == before, f"原有键顺序变了：{before} -> {after}"

    def test_new_key_appended_with_section(self, env_file) -> None:  # type: ignore[no-untyped-def]
        settings_api._write_env({"AGENT_WORKSPACE_ROOT": "D:/ws"})
        text = env_file.read_text(encoding="utf-8")
        assert "AGENT_WORKSPACE_ROOT=D:/ws" in text
        assert "由设置界面写入" in text, "追加的键应带分组注释，否则用户不知道它哪来的"

    def test_lf_not_crlf(self, env_file) -> None:  # type: ignore[no-untyped-def]
        """必须写 LF。

        Windows 上默认写 `\\r\\n`，而 `.env` 被 docker / 各解析器读取时
        `\\r` 会跑到值里 —— 变成 `"deepseek-chat\\r"`。
        排查极其费劲：**模型名看着完全正确，但就是匹配不上。**
        """
        settings_api._write_env({"LLM_MODEL": "lf-test"})
        assert b"\r\n" not in env_file.read_bytes()

    def test_secret_is_quoted(self, env_file) -> None:  # type: ignore[no-untyped-def]
        settings_api._write_env({"LLM_API_KEY": "sk-a b#c"})
        assert 'LLM_API_KEY="sk-a b#c"' in env_file.read_text(encoding="utf-8")

    def test_whitelist_blocks_unlisted_key(self, env_file) -> None:  # type: ignore[no-untyped-def]
        """**白名单而不是黑名单。**

        黑名单意味着"以后新增的任何配置项都默认可以被界面改" ——
        包括不该被改的（数据库地址、内部开关）。
        """
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            settings_api._write_env({"DATABASE_URL": "sqlite:///evil.db"})
        assert ei.value.status_code == 400
        assert "DATABASE_URL" not in env_file.read_text(encoding="utf-8")

    def test_whitelist_covers_expected_keys(self) -> None:
        for k in (
            "LLM_API_KEY",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "AGENT_PROFILE",
            "AGENT_WORKSPACE_ROOT",
            "AGENT_CORPUS_PATHS",
        ):
            assert k in settings_api.EDITABLE_KEYS
        for k in ("DATABASE_URL", "REDIS_URL", "TASK_BACKEND"):
            assert k not in settings_api.EDITABLE_KEYS


# ============================================================
# 第二层：HTTP 接口
# ============================================================
class TestReadNeverLeaksKey:
    def test_budget_update_applies_to_next_run_without_changing_inflight_settings(
        self, client: TestClient, env_file, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import AgentSettings

        before = client.app.state.settings
        old_limit = before.agent.plan_max_total_tokens
        monkeypatch.setattr(client.app.state, "settings", before)
        monkeypatch.setattr(settings_api, "_apply", lambda: None)
        monkeypatch.setattr(
            settings_api,
            "get_settings",
            lambda: before.model_copy(update={"agent": AgentSettings(_env_file=env_file)}),
        )
        response = client.put(
            "/api/settings", json={"plan_max_total_tokens": 1234, "multi_max_total_tokens": 0}
        )
        assert response.status_code == 200
        assert response.json()["agent"]["plan_max_total_tokens"] == 1234
        assert client.app.state.settings.agent.plan_max_total_tokens == 1234
        assert client.app.state.settings.agent.multi_max_total_tokens == 0
        assert before.agent.plan_max_total_tokens == old_limit
        assert "AGENT_PLAN_MAX_TOTAL_TOKENS=1234" in env_file.read_text(encoding="utf-8")
        assert client.put("/api/settings", json={"plan_max_total_tokens": -1}).status_code == 422

    def test_get_returns_masked_key_only(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        """**最重要的是"不返回什么"。**

        界面需要知道"密钥配了没有"，但**不需要知道密钥是什么**。
        原样返回意味着：任何能打开这个页面的人（同事路过、投屏演示、
        浏览器里的一次快照）都能拿到它。
        """
        r = client.get("/api/settings")
        assert r.status_code == 200
        assert "sk-original-key-1234567890" not in r.text, "响应里出现了真实密钥"

        llm = r.json()["llm"]
        assert llm["api_key_set"] is True
        assert "******" in llm["api_key_masked"]
        # 掩码保留头尾，用户才能确认"我配的是哪一把"
        assert llm["api_key_masked"].startswith("sk-o")
        assert "1234567890" not in llm["api_key_masked"]

    def test_no_key_reports_not_set(self, client: TestClient, http_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("LLM_API_KEY", "")
        get_settings.cache_clear()
        llm = client.get("/api/settings").json()["llm"]
        assert llm["api_key_set"] is False
        assert llm["api_key_masked"] == ""


class TestKeyIsNotOverwrittenByMask:
    """**这个文件里最值钱的一组。**"""

    def test_frontend_sending_mask_does_not_clobber_key(
        self, client: TestClient, http_settings
    ) -> None:  # type: ignore[no-untyped-def]
        """前端每次都把整个表单发回来，而密钥栏显示的是掩码。

        如果后端不区分"用户没动这个字段"和"用户填了掩码"，
        用户**只是改个模型名**，密钥就被写成 `sk-o******7890` ——
        之后每个请求都 401，而配置页面看起来"密钥已配置"。

        这类 bug 特别难查：**配置显示正常，功能全挂**。
        正确做法是 None 和空字符串都表示"不改动"。
        """
        masked = client.get("/api/settings").json()["llm"]["api_key_masked"]
        assert masked  # 前提：确实有掩码可回传

        r = client.put("/api/settings", json={"api_key": masked, "model": "new-model"})
        assert r.status_code == 200

        content = http_settings.read_text(encoding="utf-8")
        assert masked not in content, f"掩码被写进 .env 了：{content}"

    def test_empty_string_means_no_change(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        r = client.put("/api/settings", json={"api_key": "", "model": "another-model"})
        assert r.status_code == 200
        assert "******" not in http_settings.read_text(encoding="utf-8")

    def test_explicit_new_key_is_written(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        """真的给了新密钥必须写进去 —— 否则用户永远改不了密钥。"""
        client.put("/api/settings", json={"api_key": "sk-brand-new-key"})
        assert "sk-brand-new-key" in http_settings.read_text(encoding="utf-8")

    def test_omitted_key_means_no_change(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        r = client.put("/api/settings", json={"model": "only-model"})
        assert r.status_code == 200
        # 断言文件内容而不是响应体：响应体读的是环境变量（优先级高于 .env），
        # 而这里要验证的是"PUT 把什么写进了文件"
        assert "LLM_MODEL=only-model" in http_settings.read_text(encoding="utf-8")


class TestValidation:
    def test_invalid_profile_rejected(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        r = client.put("/api/settings", json={"profile": "nonsense"})
        assert r.status_code == 400
        assert "general" in r.json()["detail"]

    def test_nonexistent_workspace_root_rejected(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        """配一个不存在的目录必须**当场报错**。

        静默接受的话，用户会以为设置好了，然后发现"文件功能坏了" ——
        而真正的原因是他多打了一个字符。
        """
        r = client.put("/api/settings", json={"workspace_root": "Z:/definitely/not/here"})
        assert r.status_code == 400
        assert "不存在" in r.json()["detail"]

    def test_nonexistent_corpus_path_rejected(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        r = client.put("/api/settings", json={"corpus_paths": ["Z:/nope/nope.md"]})
        assert r.status_code == 400

    def test_valid_workspace_root_accepted(
        self, client: TestClient, http_settings, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        d = tmp_path / "ws"
        d.mkdir()
        r = client.put("/api/settings", json={"workspace_root": str(d)})
        assert r.status_code == 200
        # 断言写进文件的值：响应体读环境变量，而环境变量在本夹具里是刻意设死的
        assert f"AGENT_WORKSPACE_ROOT={d.resolve()}" in http_settings.read_text(encoding="utf-8")

    def test_valid_corpus_path_accepted(self, client: TestClient, http_settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
        f = tmp_path / "notes.md"
        f.write_text("# 笔记\n", encoding="utf-8")
        r = client.put("/api/settings", json={"corpus_paths": [str(f)]})
        assert r.status_code == 200
        assert f"AGENT_CORPUS_PATHS={f}" in http_settings.read_text(encoding="utf-8")

    def test_relative_path_stored_as_given(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        """相对路径按 **仓库根** 解析校验，但**原样存储**。

        改成绝对路径存的话，仓库换个位置 `.env` 就失效了 ——
        而用户本来写的就是一个相对路径。
        """
        r = client.put("/api/settings", json={"corpus_paths": ["services/api/seed/jobs.json"]})
        assert r.status_code == 200
        assert "AGENT_CORPUS_PATHS=services/api/seed/jobs.json" in http_settings.read_text(
            encoding="utf-8"
        )

    def test_unknown_field_rejected(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        """拼错的字段名必须报错，而不是被静默忽略。

        pydantic 默认忽略多余字段 —— 那意味着前端把 `temprature` 发过来时
        会**静默成功**：用户以为改了，实际没改，也没有任何提示。
        """
        r = client.put("/api/settings", json={"temprature": 0.9})
        assert r.status_code == 422

    def test_temperature_out_of_range_rejected(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        assert client.put("/api/settings", json={"temperature": 9.0}).status_code == 422


class TestValidationRunsOffEventLoop:
    def test_validation_is_a_sync_function_called_via_to_thread(self) -> None:
        """路径校验必须**抽成同步函数**并走 `to_thread`。

        【为什么这条值得测】
        `stat` 在本地路径上是微秒级，但用户完全可能填一个网络盘 ——
        那时 `is_dir()` 会阻塞好几秒，而**阻塞的是整个事件循环**，
        也就是所有人的请求。

        ruff 的 `ASYNC240` 抓直接写在 async 函数里的 pathlib 操作。
        这条测试锁的是"校验逻辑被抽出去了"这个结构：
        哪天有人把它挪回 async 端点里图省事，ASYNC240 会先报，这条是第二道。

        （我第一版在这里写了 `assert x is False or True` —— 一个恒真断言，
        什么都测不到。**看起来像测试的空断言比没有测试更糟**，
        因为它会让人以为这块被覆盖了。）
        """
        import inspect
        import re

        assert not inspect.iscoroutinefunction(settings_api._validate_paths_sync)
        src = inspect.getsource(settings_api._validate_paths_sync)
        assert "is_dir()" in src and "exists()" in src, "文件系统校验不在这个函数里"

        endpoint_src = inspect.getsource(settings_api.update_settings)
        assert re.search(r"await\s+asyncio\.to_thread\(\s*_validate_paths_sync", endpoint_src), (
            "端点没有用 to_thread 调用校验函数 —— 它会在事件循环里阻塞"
        )


class TestCorpusVisibility:
    def test_view_reports_loaded_doc_count(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        """读设置时要带上"知识库实际加载了多少文档"。

        用户改完语料配置最想知道的就是"生效了没有"，
        而让他去翻日志不算答案。
        """
        body = client.get("/api/settings").json()["agent"]
        assert "corpus_loaded" in body
        assert isinstance(body["corpus_doc_count"], int)


class TestConnectionTest:
    def test_missing_key_gives_actionable_hint(
        self, client: TestClient, http_settings, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """没配密钥时要给**可操作**的提示，而不是一个裸异常。"""
        monkeypatch.setenv("LLM_API_KEY", "")
        get_settings.cache_clear()

        r = client.post("/api/settings/test")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["hint"], "必须给出下一步该做什么"
        # 本地模型（Ollama 等）没有密钥，提示要覆盖这种情况
        assert "本地" in body["hint"] or "密钥" in body["hint"]


# ============================================================
# 第三层：立即生效的契约
# ============================================================
class TestApply:
    def test_apply_clears_both_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ "改完立即生效"靠的是清两个缓存，缺一个就会"改了没反应"。

        【为什么用替身而不是真去改配置】
        真实生效路径要读 pydantic 内部的 env_file 解析，测试里难以可靠控制
        （见文件头说明）。而这里真正需要守住的契约是**这条链路有没有断**：

            写文件 → 清设置缓存 → 重置检索器

        任何一环被删掉，用户都会看到"我明明改了"，所以逐环验证。
        """
        calls: list[str] = []

        import app.api.settings as mod

        real_clear = get_settings.cache_clear
        monkeypatch.setattr(get_settings, "cache_clear", lambda: calls.append("settings"))
        monkeypatch.setattr(mod, "reset_shared_retriever", lambda: calls.append("retriever"))

        try:
            settings_api._apply()
        finally:
            monkeypatch.undo()
            real_clear()

        assert "settings" in calls, "没清设置缓存 —— 改了配置不生效"
        assert "retriever" in calls, "没重置检索器 —— 语料/工作区变了但索引没重建"

    def test_invalid_input_does_not_write(self, client: TestClient, http_settings) -> None:  # type: ignore[no-untyped-def]
        """校验失败时**不能留下半成品**。

        先写后校验的实现会把非法值写进 .env，然后返回 400 ——
        用户看到"保存失败"，但配置其实已经被改成非法的了。
        下次启动服务会因为一个非法配置起不来，而用户以为自己的操作没生效。
        """
        before = http_settings.read_text(encoding="utf-8")
        r = client.put("/api/settings", json={"profile": "bogus", "model": "should-not-be-written"})
        assert r.status_code == 400
        after = http_settings.read_text(encoding="utf-8")
        assert after == before, "校验失败却写入了部分配置"
        assert "should-not-be-written" not in after
