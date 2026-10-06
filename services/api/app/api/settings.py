"""设置读写接口：让用户能在界面上改配置，而不是去编辑 .env。

【最重要的设计决定：配置只有**一个**来源】

做设置界面时最诱人的做法是"另存一份运行时配置"（比如 `data/settings.json`），
因为它简单、不用担心改坏 `.env`。但那个做法会立刻制造出一个经典问题：

    "我明明在界面上改了模型，为什么还是用旧的？"

因为此时系统里有两份配置（`.env` 与 json），而"哪一份生效"取决于
加载顺序 —— 这个顺序没有人记得住，包括写它的人。而且一旦两份不一致，
排查会变成"你到底看的哪一份"的争论。

所以这里反过来：**界面直接编辑 `.env`**，那是唯一的事实来源。
代价是写入要小心（不能把用户的注释和顺序搞乱），
但这个代价是**一次性的**，而双来源的代价是**永久的**。

【为什么 GET 不能原样返回 API Key】

界面需要知道"密钥配了没有"，但**不需要知道密钥是什么**。
原样返回意味着：任何能打开这个页面的人（同事路过、投屏演示、浏览器历史里的
一个快照、前端的一个 console.log）都能拿到它。

所以 GET 只返回**掩码**（`sk-1****abcd`）和一个布尔值。PUT 时留空表示
"不改动"，而不是"清空" —— 这个区分很重要，否则用户每次改模型都会把密钥清掉。
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import PROJECT_ROOT, get_settings
from app.core.directory_picker import reset_directory_picker
from app.rag.factory import reset_shared_retriever

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])

ENV_PATH = PROJECT_ROOT / ".env"

# 允许通过界面修改的键。**白名单而不是黑名单** —— 黑名单意味着
# 以后新增的任何配置项都默认可以被界面改，包括那些不该被改的。
EDITABLE_KEYS = {
    "AGENT_PLAN_MAX_TOTAL_TOKENS",
    "AGENT_MULTI_MAX_TOTAL_TOKENS",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_TEMPERATURE",
    "AGENT_PROFILE",
    "AGENT_WORKSPACE_ROOT",
    "AGENT_CORPUS_PATHS",
    "AGENT_CORPUS_INCLUDE_SEED",
    "AGENT_FILE_MAX_CHARS",
    # 写权限（T23）：**必须能在界面上开关**，否则"Agent 能不能改我的文件"
    # 就只能去翻 .env —— 而那正是这个设置界面想消灭的事。
    "AGENT_FILE_WRITE_ENABLED",
    "AGENT_FILE_ALLOW_SECRETS",
}

# 这些键写进 .env 时**不加引号会被 shell/docker 解析出问题**，统一加引号
_QUOTE_KEYS = {"LLM_API_KEY"}


def _mask(secret: str) -> str:
    """把密钥变成可展示的掩码。

    【为什么保留头尾几位】
    只显示 `******` 的话，用户无法确认"我配的是哪个 key" ——
    在有多把密钥（测试/生产/别人的）时这一点很重要。
    保留前 4 位和后 4 位足以区分，又不足以被使用。
    """
    if not secret:
        return ""
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 6}{secret[-4:]}"


# ============================================================
# 读写 .env
# ============================================================
def _read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def _write_env(updates: dict[str, str]) -> list[str]:
    """只替换指定键的**那一行**，其余原样保留。

    【为什么不做"反序列化成 dict 再重写一遍"】
    用户的 `.env` 里通常有大量注释（这个项目里每个配置项都带一段解释），
    还有自己调整过的顺序和分组。反序列化再写回会**把这些全部抹掉** ——
    用户下次打开文件会发现自己的笔记没了，而那是不可恢复的。

    所以这里按行处理：命中就替换那一行，没命中就追加一行到末尾。
    **改动范围最小化是处理用户文件时最重要的一条纪律。**

    Returns:
        实际写入的键列表（用于日志）。
    """
    unknown = set(updates) - EDITABLE_KEYS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"不允许通过界面修改这些配置：{sorted(unknown)}。"
            f"允许的有：{sorted(EDITABLE_KEYS)}",
        )

    lines = _read_env_lines()
    written: list[str] = []
    handled: set[str] = set()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={_quote(key, updates[key])}"
            written.append(key)
            handled.add(key)

    # 没出现过的键追加到末尾，并给个分组注释，
    # 免得用户下次打开 .env 看到几行孤零零的配置不知道哪来的
    pending = {k: v for k, v in updates.items() if k not in handled}
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# ---- 由设置界面写入 ----")
        for k, v in pending.items():
            lines.append(f"{k}={_quote(k, v)}")
            written.append(k)

    # newline="\n" 很重要：Windows 上默认写 \r\n，而 .env 被 git 与
    # Docker 读取时 \r 会跑到值里（"deepseek-chat\r" 这种），
    # 排查起来极其费劲 —— 模型名看着对，但就是匹配不上。
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return written


def _quote(key: str, value: str) -> str:
    if key in _QUOTE_KEYS and value and not value.startswith('"'):
        return f'"{value}"'
    return value


def _apply() -> None:
    """让改动立即生效。

    【为什么必须显式清两个缓存】
    1. `get_settings` 是 lru_cache 的 —— 不清就永远读的是旧配置，
       表现是"改了没反应"，而这是最让人怀疑自己没保存的一类问题。
    2. 检索器是进程内共享的 —— 语料/工作区变了必须重建索引，
       否则新加的文档搜不到、去掉的文档还在被召回。

    目录选择器也要跟着重建：它的能力是**启动时按宿主事实采样**的
    （绑定地址、显示会话……），而那正是 pull 单例缓存的理由 ——
    缓存了就必须在事实可能变化的地方清掉它。
    """
    get_settings.cache_clear()
    reset_shared_retriever()
    reset_directory_picker()


# ============================================================
# 接口模型
# ============================================================
class LLMView(BaseModel):
    base_url: str
    model: str
    temperature: float
    # 掩码而非原值。前端只需要"配没配"和"是不是那一把"
    api_key_masked: str
    api_key_set: bool


class AgentView(BaseModel):
    plan_max_total_tokens: int = 60000
    multi_max_total_tokens: int = 80000
    profile: str
    workspace_root: str
    corpus_paths: list[str]
    corpus_include_seed: bool
    file_max_chars: int
    # 写权限（T23）。界面据此显示开关状态 —— 它是"Agent 能不能改我的文件"
    # 这个问题的唯一答案，必须能一眼看到，而不是要去翻 .env。
    file_write_enabled: bool = False
    file_allow_secrets: bool = False
    # 便于界面显示当前实际加载了多少文档（改完配置能立刻看到效果）
    corpus_loaded: bool = False
    corpus_doc_count: int = 0


class SettingsView(BaseModel):
    llm: LLMView
    agent: AgentView
    env_path: str


class SettingsUpdate(BaseModel):
    """全部可选：只传要改的字段。**未传的字段保持原样。**

    尤其是 `api_key`：留空表示"不改动"，不是"清空"。
    否则用户每次调模型名都会顺手把密钥清掉，而且不会有任何提示。
    """

    # 【为什么拒绝未知字段，而不是忽略它们】
    # pydantic 默认忽略多余字段，那意味着前端把 `temprature`（拼错）
    # 发过来时会**静默成功** —— 用户以为改了，实际没改，而且没有任何提示。
    # 报 422 能让这类前后端不一致当场暴露。
    model_config = ConfigDict(extra="forbid")

    api_key: str | None = Field(default=None, description="留空 = 不修改已保存的密钥")
    base_url: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    profile: str | None = None
    workspace_root: str | None = None
    corpus_paths: list[str] | None = None
    corpus_include_seed: bool | None = None
    file_max_chars: int | None = Field(default=None, gt=0)
    # 写权限（T23）。`None` = 不改动 —— 与其它字段同一语义。
    file_write_enabled: bool | None = None
    file_allow_secrets: bool | None = None
    plan_max_total_tokens: int | None = Field(default=None, ge=0)
    multi_max_total_tokens: int | None = Field(default=None, ge=0)


class TestConnectionResult(BaseModel):
    ok: bool
    model: str
    latency_ms: int = 0
    # 失败时给出**可操作**的原因，而不是一个原始异常字符串
    error: str = ""
    hint: str = ""


# ============================================================
# 端点
# ============================================================
def _current_view() -> SettingsView:
    s = get_settings()
    key = s.llm.api_key.get_secret_value()
    return SettingsView(
        llm=LLMView(
            base_url=s.llm.base_url,
            model=s.llm.model,
            temperature=s.llm.temperature,
            api_key_masked=_mask(key),
            api_key_set=bool(key),
        ),
        agent=AgentView(
            plan_max_total_tokens=s.agent.plan_max_total_tokens,
            multi_max_total_tokens=s.agent.multi_max_total_tokens,
            profile=s.agent.profile,
            workspace_root=s.agent.workspace_root,
            corpus_paths=s.agent.corpus_path_list,
            corpus_include_seed=s.agent.corpus_include_seed,
            file_max_chars=s.agent.file_max_chars,
            file_write_enabled=s.agent.file_write_enabled,
            file_allow_secrets=s.agent.file_allow_secrets,
        ),
        env_path=str(ENV_PATH),
    )


@router.get("", response_model=SettingsView, summary="读取当前设置")
async def get_settings_view() -> SettingsView:
    """读取当前设置。**API Key 只返回掩码。**

    另外附上"知识库实际加载了多少文档" —— 用户改完语料配置后
    最想知道的就是"生效了没有"，而让他去翻日志不算答案。
    """
    view = _current_view()
    try:
        from app.rag.factory import get_shared_retriever

        r = get_shared_retriever()
        view.agent.corpus_loaded = True
        view.agent.corpus_doc_count = len({c.doc_id for c in r.chunks})
    except Exception as exc:
        logger.debug("读取知识库统计失败（不影响设置读取）：%s", exc)
    return view


def _validate_paths_sync(
    workspace_root: str | None, corpus_paths: list[str] | None
) -> tuple[str | None, list[str] | None]:
    """校验并归一化用户给的路径（**同步函数，必须在线程里调用**）。

    【为什么单独抽出来而不是直接写在 async 端点里】
    这些是 `stat` 系统调用：本地路径上是微秒级，但**用户完全可能填一个
    网络盘或已断开的映射盘** —— 那时 `is_dir()` 会阻塞好几秒，
    而阻塞的是整个事件循环，也就是所有人的请求。

    ruff 的 `ASYNC240` 抓的正是这个。可以用 `# noqa` 压掉，
    但那条规则指出的风险是真实存在的：**用户提供的路径是不可信输入，
    拿它做文件系统操作之前应当假定它会慢。**
    所以走 `asyncio.to_thread`，而不是把规则关掉。
    """
    out_root = workspace_root
    if workspace_root is not None:
        root = workspace_root.strip()
        if root:
            p = Path(root).expanduser()
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            if not p.is_dir():
                # 早失败：配一个不存在的目录，用户会以为"文件功能坏了"
                raise HTTPException(status_code=400, detail=f"工作区目录不存在或不是目录：{p}")
            root = str(p.resolve())
        out_root = root

    out_corpus = corpus_paths
    if corpus_paths is not None:
        cleaned: list[str] = []
        for raw in (p.strip() for p in corpus_paths):
            if not raw:
                continue
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            if not p.exists():
                raise HTTPException(status_code=400, detail=f"知识库路径不存在：{p}")
            cleaned.append(raw)
        out_corpus = cleaned

    return out_root, out_corpus


@router.put("", response_model=SettingsView, summary="更新设置")
async def update_settings(payload: SettingsUpdate, request: Request) -> SettingsView:
    """更新设置并写入 `.env`。

    写完之后**立即生效**（清缓存 + 重建索引），不需要重启服务。
    改完还要用户手动重启，那和让他直接编辑 .env 没有区别。
    """
    import asyncio

    s = get_settings()
    updates: dict[str, str] = {}
    for name in ("plan_max_total_tokens", "multi_max_total_tokens"):
        value = getattr(payload, name)
        if value is not None:
            updates[f"AGENT_{name.upper()}"] = str(value)

    # 【密钥的特殊处理：三种"不改动"的写法都要认】
    #
    # 前端每次把整个表单发回来，而密钥栏里显示的是**掩码**。所以下面三个值
    # 都表示"用户没动这个字段"，而不是"把密钥改成这个"：
    #     None   —— 字段没传
    #     ""     —— 传了空字符串
    #     掩码本身 —— 前端把当前显示值原样回传
    #
    # 【第三条是我写了注释却漏了实现的地方】
    # 我一开始在注释里就写了"要防前端回传掩码"，但代码只判断了
    # `if payload.api_key:` —— 于是掩码作为**非空字符串**被照单收下，
    # 写进 .env 变成 `LLM_API_KEY=sk-o******7890`。
    # 后果是：用户只是改个模型名，之后**所有请求 401，而配置页面显示"密钥已配置"**。
    #
    # 这个 bug 是被我自己的测试抓到的（test_frontend_sending_mask_does_not_clobber_key），
    # 而它恰好证明了一件事：**注释里的意图不会自动变成实现**。
    # 关键防护必须落在能被执行的地方 —— 这里就是那一行比较。
    if payload.api_key and payload.api_key.strip() != _mask(s.llm.api_key.get_secret_value()):
        updates["LLM_API_KEY"] = payload.api_key.strip()
    if payload.base_url is not None:
        updates["LLM_BASE_URL"] = payload.base_url.strip()
    if payload.model is not None:
        updates["LLM_MODEL"] = payload.model.strip()
    if payload.temperature is not None:
        updates["LLM_TEMPERATURE"] = str(payload.temperature)

    if payload.profile is not None:
        if payload.profile not in ("general", "jobhunt"):
            raise HTTPException(status_code=400, detail="profile 只能是 general 或 jobhunt")
        updates["AGENT_PROFILE"] = payload.profile

    # 路径校验与写文件都在线程里做 —— 见 _validate_paths_sync 的说明
    root, corpus = await asyncio.to_thread(
        _validate_paths_sync, payload.workspace_root, payload.corpus_paths
    )
    if root is not None:
        updates["AGENT_WORKSPACE_ROOT"] = root
    if corpus is not None:
        updates["AGENT_CORPUS_PATHS"] = ",".join(corpus)
    if payload.corpus_include_seed is not None:
        updates["AGENT_CORPUS_INCLUDE_SEED"] = "true" if payload.corpus_include_seed else "false"
    if payload.file_max_chars is not None:
        updates["AGENT_FILE_MAX_CHARS"] = str(payload.file_max_chars)
    if payload.file_write_enabled is not None:
        updates["AGENT_FILE_WRITE_ENABLED"] = "true" if payload.file_write_enabled else "false"
    if payload.file_allow_secrets is not None:
        updates["AGENT_FILE_ALLOW_SECRETS"] = "true" if payload.file_allow_secrets else "false"

    if updates:
        written = _write_env(updates)
        logger.info("设置已更新：%s", "、".join(written))
        _apply()
        # 在途请求保留原配置；后续请求使用新对象，避免修改共享 Settings 导致预算漂移。
        if hasattr(request.app.state, "settings"):
            fresh = get_settings().agent
            current = request.app.state.settings
            budget_settings = current.agent.model_copy(
                update={
                    name: getattr(fresh, name)
                    for name in ("plan_max_total_tokens", "multi_max_total_tokens")
                }
            )
            request.app.state.settings = current.model_copy(update={"agent": budget_settings})

    return await get_settings_view()


@router.post("/test", response_model=TestConnectionResult, summary="测试模型连通性")
async def test_connection() -> TestConnectionResult:
    """向当前配置的模型发一个最小请求，验证能不能用。

    【为什么这个端点值得单独做】
    配置模型时有三种失败长得一模一样：密钥错、base_url 错、模型名不存在。
    用户改完只能靠"发一条消息试试"，而 Agent 的失败路径很长
    （工具调用、检索、多步循环），从里面反推出"其实是密钥错了"很难。

    这里用一个**最小请求**把失败面收敛到只剩配置本身 ——
    这是配置类功能该有的自检。
    """
    import time

    from app.llm.client import LLMClient
    from app.llm.types import ChatMessage

    s = get_settings()
    if not s.llm.is_configured:
        return TestConnectionResult(
            ok=False,
            model=s.llm.model,
            error="尚未配置 API Key",
            hint="在上面填入密钥后重试。如果用的是本地模型（如 Ollama），"
            "密钥可以随便填一个非空值。",
        )

    client = LLMClient(s.llm)
    started = time.perf_counter()
    try:
        resp = await client.chat(
            [ChatMessage.user("回复两个字：正常")],
            # 只让它回两个字，把这次调用的成本压到最低
            max_tokens=8,
            temperature=0.0,
        )
        latency = int((time.perf_counter() - started) * 1000)
        return TestConnectionResult(
            ok=True,
            model=resp.model or s.llm.model,
            latency_ms=latency,
        )
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        kind = type(exc).__name__
        msg = str(exc)
        # 把异常翻译成**用户能自己动手修**的提示。
        # 原始异常（比如 "Error code: 401 - {'error': {...}}"）对用户没有用，
        # 他需要知道的是"去改哪个字段"。
        if "401" in msg or "Unauthorized" in msg or "invalid_api_key" in msg.lower():
            hint = "密钥无效。请检查是否复制完整、是否已过期。"
        elif "404" in msg or "model_not_found" in msg or "does not exist" in msg:
            hint = f"模型名 {s.llm.model!r} 在该服务上不存在。请核对模型名。"
        elif "Connection" in msg or "Connect" in msg or "getaddrinfo" in msg:
            hint = f"无法连接 {s.llm.base_url}。请检查地址、网络与代理设置。"
        elif "timeout" in msg.lower():
            hint = "请求超时。服务可能过载，或该地址不可达。"
        elif "429" in msg:
            hint = "触发了服务方的速率限制。稍后重试，或检查账户额度。"
        else:
            hint = "请检查上面三项配置是否正确。"
        return TestConnectionResult(
            ok=False, model=s.llm.model, latency_ms=latency, error=f"{kind}: {msg[:300]}", hint=hint
        )
    finally:
        await client.aclose()
