"""模型管理测试（多供应商 + 切换生效）。

【这组测试盯的是什么】

多模型功能最容易坏在两个地方，而它们都是**静默**的：

  1. **切了但没生效** —— 界面显示"当前使用 X"，进程里还在用 Y。
     根因是 `LLMClient` 构造时就把配置快照进了自己（连 httpx 的
     `Authorization` 头都是那时写死的），所以"清配置缓存"对它毫无影响。
     这条必须由测试钉住：切换后**新构建的客户端**必须带上新配置。

  2. **密钥被界面回传的掩码覆盖** —— 用户只是改个名字，密钥就变成了
     `sk-o******7890`，之后所有请求 401，而配置页还显示"密钥已配置"。
     本项目在 `/api/settings` 上踩过这个坑，模型接口是同一类入口，
     所以同样要有断言。

另外还守两条"清单 vs 事实"的一致性：
  · `active` 是**算出来的**（比对地址+模型名+密钥），不是存一个 active_id ——
    用户可以随时手改 `.env`，存 id 就会在那种情况下说谎；
  · 手改 `.env` 后界面要能显示"当前配置未保存为模型"，而不是让人对着
    一个不在清单里的配置发懵。
"""

from __future__ import annotations

import json

import pytest
from app.llm.library import ModelLibrary, SavedModel
from fastapi.testclient import TestClient


@pytest.fixture
def library(tmp_path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """把模型库指到临时文件。

    **绝不能碰真实的 data/models.json** —— 那是用户的文件。
    """
    from app.api import models as models_api

    path = tmp_path / "models.json"
    monkeypatch.setattr(models_api, "_library", lambda: ModelLibrary(path))
    return ModelLibrary(path)


@pytest.fixture
def env_file(tmp_path, monkeypatch: pytest.MonkeyPatch, client: TestClient):  # type: ignore[no-untyped-def]
    """把配置层**读写的** `.env` 都指到临时文件（切换模型会写它、也要能读回来）。

    【为什么必须逐个 Settings 类改 env_file】
    pydantic-settings 的 `env_file` 是在**类体求值时**就绑定到
    `PROJECT_ROOT/.env` 的常量，不是在读取时现算。所以只 patch
    `settings_api.ENV_PATH`（写入路径）会出现一个很容易误判的假象：
    写进去了、读回来的还是旧配置 —— 测试于是"失败"，而失败原因
    与它想验证的行为毫无关系。

    这类"测试脚手架自己没接对"的失败最浪费时间，所以这里把两件事
    一起做掉：写入路径 + 所有 Settings 类的读取路径。

    【为什么结束时要重建 app.state】
    `client` 是会话级的：激活操作会把 `app.state.llm` 换成一个用**临时配置**
    构建的客户端。不还原的话，后续测试文件会继承一个指向不存在端点的客户端，
    而症状是别处莫名其妙地失败。
    """
    from app.api import settings as settings_api
    from app.core import config as config_module
    from app.core.config import get_settings

    env = tmp_path / ".env"
    env.write_text(
        "LLM_BASE_URL=http://old-endpoint/v1\n"
        "LLM_MODEL=old-model\n"
        "LLM_API_KEY=sk-existing-key-123456\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_api, "ENV_PATH", env)

    for obj in list(vars(config_module).values()):
        cfg = getattr(obj, "model_config", None)
        if isinstance(obj, type) and isinstance(cfg, dict) and "env_file" in cfg:
            monkeypatch.setitem(cfg, "env_file", env)

    get_settings.cache_clear()
    yield env

    get_settings.cache_clear()
    # 还原成"按真实 .env 装配"的那一栈
    from app.agent.factory import build_agent_stack, mount_agent_stack

    mount_agent_stack(client.app, build_agent_stack())  # type: ignore[arg-type]


# ============================================================
# 存储层
# ============================================================
class TestLibrary:
    def test_missing_file_is_empty_list(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        assert ModelLibrary(tmp_path / "nope.json").load() == []

    def test_roundtrip(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        lib = ModelLibrary(tmp_path / "m.json")
        lib.save([SavedModel(label="A", base_url="http://x/v1", model="m1", api_key="k")])
        loaded = lib.load()
        assert len(loaded) == 1 and loaded[0].label == "A" and loaded[0].api_key == "k"

    def test_corrupt_file_does_not_break_the_ui(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """文件坏了要当作空清单继续，而不是让整个模型管理界面打不开。

        这个文件在 data/ 下，**用户可以手改** —— 一次手滑不该让功能消失。
        """
        path = tmp_path / "m.json"
        path.write_text("{ 这不是合法 JSON", encoding="utf-8")
        assert ModelLibrary(path).load() == []

    def test_bad_field_types_fall_back_per_field(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """坏字段逐条退回默认值，而不是整份清单读不出来。"""
        path = tmp_path / "m.json"
        path.write_text(
            json.dumps([{"label": 123, "base_url": None, "model": "ok", "temperature": "热"}]),
            encoding="utf-8",
        )
        loaded = ModelLibrary(path).load()
        assert len(loaded) == 1
        assert loaded[0].model == "ok"
        assert loaded[0].label == "123"  # 数字被收敛成字符串
        assert loaded[0].temperature is None  # 非法温度退回 None

    def test_save_is_atomic(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """写盘用"临时文件 + 替换"：不会留下被截断的 JSON。

        【为什么值得测】直接覆盖写时，进程在写到一半被杀会留下半份 JSON，
        下次读取直接失败 —— 用户会失去整份清单。os.replace 是同分区原子操作，
        要么旧内容、要么新内容。
        """
        lib = ModelLibrary(tmp_path / "m.json")
        lib.save([SavedModel(label="A", base_url="u", model="m")])
        lib.save([SavedModel(label="B", base_url="u", model="m")])
        assert not (tmp_path / "m.json.tmp").exists(), "临时文件没有被清理掉"
        assert lib.load()[0].label == "B"

    def test_upsert_and_delete(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        lib = ModelLibrary(tmp_path / "m.json")
        a = SavedModel(label="A", base_url="u1", model="m")
        lib.upsert(a)
        lib.upsert(SavedModel(id=a.id, label="A2", base_url="u2", model="m"))
        models = lib.load()
        assert len(models) == 1 and models[0].label == "A2" and models[0].base_url == "u2"

        lib.upsert(SavedModel(label="B", base_url="u3", model="m"))
        assert len(lib.load()) == 2
        assert len(lib.delete(a.id)) == 1

    def test_same_target_compares_address_model_and_key(self) -> None:
        """判"是不是同一个模型"要比地址+模型名+密钥，**不能比 label**。

        label 是用户起的名字：改个名字不该让它变成另一个模型。
        而地址相同、模型名不同（同一个网关下的两个模型）确实是两个东西。
        """
        base = SavedModel(label="A", base_url="http://x/v1", model="m1", api_key="k")
        assert base.same_target_as(
            SavedModel(label="完全不同的名字", base_url="http://x/v1/", model="m1", api_key="k")
        )
        assert not base.same_target_as(
            SavedModel(label="A", base_url="http://x/v1", model="m2", api_key="k")
        )
        assert not base.same_target_as(
            SavedModel(label="A", base_url="http://x/v1", model="m1", api_key="other")
        )


# ============================================================
# HTTP 接口
# ============================================================
class TestModelsApi:
    def test_list_is_empty_initially(
        self,
        client: TestClient,
        library,
        env_file,  # type: ignore[no-untyped-def]
    ) -> None:
        r = client.get("/api/models")
        assert r.status_code == 200
        body = r.json()
        assert body["models"] == []
        # 当前 `.env` 里有一份没存进清单的配置 → 要如实说出来
        assert body["current_unsaved"] is True
        assert body["current"]["model"] == "old-model"

    def test_saved_model_never_returns_the_raw_key(
        self,
        client: TestClient,
        library,
        env_file,  # type: ignore[no-untyped-def]
    ) -> None:
        """接口只回掩码 —— 与 /api/settings 同一条纪律。"""
        r = client.post(
            "/api/models",
            json={
                "label": "公司网关",
                "provider": "custom",
                "base_url": "http://gw.internal/v1",
                "model": "qwen-max",
                "api_key": "sk-super-secret-value",
            },
        )
        assert r.status_code == 200, r.text
        text = r.text
        assert "sk-super-secret-value" not in text
        assert "sk-s" in text and "alue" in text  # 掩码保留了头尾（便于辨认）

    def test_add_appends_to_the_list(
        self,
        client: TestClient,
        library,
        env_file,  # type: ignore[no-untyped-def]
    ) -> None:
        """**这就是用户要的"配好后显示多了一个模型"。**"""
        before = len(client.get("/api/models").json()["models"])
        client.post(
            "/api/models",
            json={
                "label": "DeepSeek",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": "k1",
            },
        )
        client.post(
            "/api/models",
            json={
                "label": "本地 Ollama",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "qwen2.5:7b",
                "api_key": "k2",
            },
        )
        after = client.get("/api/models").json()["models"]
        assert len(after) == before + 2
        assert {m["label"] for m in after} == {"DeepSeek", "本地 Ollama"}

    def test_update_without_key_keeps_the_existing_one(
        self,
        client: TestClient,
        library,
        env_file,  # type: ignore[no-untyped-def]
    ) -> None:
        """改名字/改模型名时留空密钥 = 不改动，**不能把密钥清掉**。

        这条对应的真实事故是"回传掩码把密钥覆盖掉"：用户只改了个模型名，
        之后所有请求 401，而界面还显示"密钥已配置"。
        """
        created = client.post(
            "/api/models",
            json={
                "label": "A",
                "base_url": "http://x/v1",
                "model": "m1",
                "api_key": "sk-keepme-1234567890",
            },
        ).json()["models"][0]
        masked_before = created["api_key_masked"]

        updated = client.post(
            "/api/models",
            json={"id": created["id"], "label": "A2", "base_url": "http://x/v1", "model": "m1"},
        ).json()["models"][0]

        assert updated["label"] == "A2"
        assert updated["api_key_masked"] == masked_before, "密钥被清掉了"
        assert updated["api_key_set"] is True

    def test_activate_writes_env_and_builds_a_client_with_the_new_config(
        self,
        client: TestClient,
        library,
        env_file,
        monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
    ) -> None:
        """**切换必须真的生效**：写 .env + 重建装配 + 新客户端带上新配置。

        这是本组测试里最重要的一个断言。原来的设置面板宣称"改完立即生效"，
        而模型这一项**并没有做到** —— `LLMClient` 构造时就把配置快照进了自己
        （连 httpx 的 Authorization 头都是那时写死的），清配置缓存对它无效。
        只断言"接口返回 200"会让这个缺陷一直藏着。
        """
        from app.agent import factory as agent_factory

        built: list[object] = []
        real_build = agent_factory.build_agent_stack

        def spy(settings=None):  # type: ignore[no-untyped-def]
            stack = real_build(settings)
            built.append(stack.llm)
            return stack

        monkeypatch.setattr(agent_factory, "build_agent_stack", spy)
        from app.api import models as models_api

        monkeypatch.setattr(models_api, "build_agent_stack", spy)

        created = client.post(
            "/api/models",
            json={
                "label": "切换目标",
                "base_url": "https://api.example.com/v1",
                "model": "brand-new-model",
                "api_key": "sk-new-key-abcdefg",
            },
        ).json()["models"][0]

        r = client.post(f"/api/models/{created['id']}/activate")
        assert r.status_code == 200, r.text
        assert r.json()["models"][0]["active"] is True

        # 1) 落到了 .env（唯一事实来源）
        env_text = env_file.read_text(encoding="utf-8")
        assert "LLM_MODEL=brand-new-model" in env_text
        assert "LLM_BASE_URL=https://api.example.com/v1" in env_text
        assert "sk-new-key-abcdefg" in env_text

        # 2) 真的重建了装配，而且新客户端用的是**新**配置
        assert built, "激活时没有重建 Agent 全栈 —— 那界面会显示切了、进程还在用旧的"
        assert built[-1]._s.model == "brand-new-model"  # type: ignore[attr-defined]

    def test_activate_closes_the_previous_client(
        self,
        client: TestClient,
        library,
        env_file,  # type: ignore[no-untyped-def]
    ) -> None:
        """切换时要关掉被替换下来的客户端，否则每切一次漏一个连接池。"""
        created = client.post(
            "/api/models",
            json={"label": "X", "base_url": "http://x/v1", "model": "m", "api_key": "k"},
        ).json()["models"][0]

        client.post(f"/api/models/{created['id']}/activate")
        # 关闭后 httpx 客户端会被标记为已关闭（再关一次是幂等的，不会抛）
        llm = client.app.state.llm  # type: ignore[attr-defined]
        assert llm._s.model == "m"  # type: ignore[attr-defined]

    def test_activate_unknown_id_is_404(
        self,
        client: TestClient,
        library,
        env_file,  # type: ignore[no-untyped-def]
    ) -> None:
        assert client.post("/api/models/nope/activate").status_code == 404

    def test_delete_removes_it(
        self,
        client: TestClient,
        library,
        env_file,  # type: ignore[no-untyped-def]
    ) -> None:
        created = client.post(
            "/api/models",
            json={"label": "删我", "base_url": "http://x/v1", "model": "m", "api_key": "k"},
        ).json()["models"][0]
        remaining = client.delete(f"/api/models/{created['id']}").json()["models"]
        assert all(m["id"] != created["id"] for m in remaining)
        assert client.delete(f"/api/models/{created['id']}").status_code == 404

    def test_import_current_saves_the_live_config(
        self,
        client: TestClient,
        library,
        env_file,  # type: ignore[no-untyped-def]
    ) -> None:
        """界面看不到密钥原文，所以"把当前配置存起来"必须由服务端代劳。

        没有这个入口，用户想把自己一直在用的配置存进清单，
        只能重抄地址与密钥 —— 而密钥在界面上只有掩码，等于抄不了。
        """
        r = client.post("/api/models/import-current", json={"label": "我一直在用的"})
        assert r.status_code == 200, r.text
        models = r.json()["models"]
        assert len(models) == 1
        assert models[0]["label"] == "我一直在用的"
        assert models[0]["model"] == "old-model"
        assert models[0]["active"] is True
        assert r.json()["current_unsaved"] is False

        # 再点一次不该重复添加
        again = client.post("/api/models/import-current", json={"label": "我一直在用的"})
        assert len(again.json()["models"]) == 1

    def test_unknown_field_is_rejected(
        self,
        client: TestClient,
        library,
        env_file,  # type: ignore[no-untyped-def]
    ) -> None:
        """字段名写错要当场 422，而不是被静默忽略（前后端字段名漂移的护栏）。"""
        r = client.post(
            "/api/models",
            json={"label": "A", "baseUrl": "http://x/v1", "model": "m"},
        )
        assert r.status_code == 422

    def test_missing_required_fields_are_rejected(
        self,
        client: TestClient,
        library,
        env_file,  # type: ignore[no-untyped-def]
    ) -> None:
        assert client.post("/api/models", json={"label": "只有名字"}).status_code == 422
