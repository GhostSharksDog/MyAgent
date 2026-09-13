r"""跨进程验证：证明 Agent 侧真的通过网络调用了独立的 RAG 服务。

【为什么必须有这个脚本 —— 单元测试在这里是不够的】

`TestContractAgainstRealServer` 用 ASGITransport 测了协议一致性，但它
**没有经过 socket**：ASGI 传输把请求直接交给了同一个进程内的 app 对象。
所以下面这几种失败它一个都测不出来：

  · RAG_SERVICE_URL 写错（端口、路径、协议）
  · 服务根本没起来 / 防火墙拦截
  · 两个进程对 `X-Trace-Id` 之类的头部处理不一致
  · **配置压根没生效** —— 最阴险的一种：代码悄悄走了本地路径，
    一切正常、结果也对，只是你以为的拆分根本没发生

最后一条是本脚本最重要的目标。所以它不满足于"有结果返回"，
而是去 RAG 服务**自己的指标端点**确认请求数真的涨了：

    有结果  ≠  请求真的到达了那个进程

【怎么用】
    # 终端 1
    .\.venv\Scripts\python.exe -m uvicorn app.rag_service.main:app \
        --port 8001 --app-dir services/api

    # 终端 2
    .\.venv\Scripts\python.exe scripts/verify_split.py

退出码 0 表示全部通过。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "api"))

RAG_URL = "http://127.0.0.1:8001"

PASS = "  [PASS]"
FAIL = "  [FAIL]"
_failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    print(f"{PASS if ok else FAIL} {label}")
    if not ok:
        _failures.append(label)
    return ok


def _metric_count(text: str, name: str) -> int:
    """从 Prometheus 文本里取出某个指标的累计计数。

    只认 `_count` 后缀的那一行 —— 直方图会同时输出多个分位数
    和 `_sum`，取错行会得到一个看似合理的错数字。
    """
    total = 0
    for line in text.splitlines():
        if line.startswith(f"{name}_count"):
            total += int(float(line.rsplit(" ", 1)[1]))
    return total


async def main() -> int:
    import httpx

    from app.core.config import get_settings
    from app.rag.backend import (
        RemoteKnowledgeBackend,
        build_knowledge_backend,
        describe_knowledge_backend,
    )
    from app.tools.knowledge import KnowledgeSearchTool, SearchKnowledgeParams

    print("=" * 62)
    print("跨进程验证：Agent → HTTP → 独立 RAG 服务")
    print("=" * 62)

    async with httpx.AsyncClient(timeout=10.0) as probe:
        # ---------- 1. RAG 服务可达 ----------
        print("\n[1] RAG 服务可达性")
        try:
            health = (await probe.get(f"{RAG_URL}/healthz")).json()
        except httpx.HTTPError as exc:
            print(f"{FAIL} 无法连接 {RAG_URL}：{exc}")
            print(
                "\n请先启动 RAG 服务：\n"
                "  .\\.venv\\Scripts\\python.exe -m uvicorn app.rag_service.main:app "
                "--port 8001 --app-dir services/api"
            )
            return 1

        check(health.get("status") == "ok", f"/healthz 返回 ok（chunks={health.get('chunks')}）")
        check(
            int(health.get("chunks", 0)) > 0,
            f"索引非空（{health.get('chunks')} 块，mode={health.get('mode')}）",
        )

        # ---------- 2. 配置确实指向远程 ----------
        print("\n[2] 配置解析")
        settings = get_settings().model_copy(update={"rag_service_url": RAG_URL})
        info = describe_knowledge_backend(settings)
        check(info["backend"] == "remote", f"describe 判定为 remote：{info}")
        check(
            isinstance(build_knowledge_backend(settings), RemoteKnowledgeBackend),
            "工厂构造出 RemoteKnowledgeBackend（而不是 Local）",
        )

        # ---------- 3. 走真实工具路径 ----------
        print("\n[3] Agent 工具路径（不注入后端 → 走工厂 → 走 HTTP）")
        before = _metric_count((await probe.get(f"{RAG_URL}/metrics")).text,
                               "jobpilot_rag_request_ms")

        tool = KnowledgeSearchTool(settings=settings)
        queries = [
            ("用了哪些大数据和分布式技术", "all", ("Kafka", "Flink", "ClickHouse")),
            ("有没有 RAG 或向量数据库经验", "all", ()),
        ]
        for q, scope, expect in queries:
            result = await tool.run(SearchKnowledgeParams(query=q, scope=scope, limit=3))
            if expect:
                hit = result.ok and any(k in result.content for k in expect)
                check(hit, f"{q!r} → 命中 {expect}")
            else:
                # 这条只要求"链路通"：有没有命中取决于真实语料，
                # 不该把脚本的成败绑在语料内容上
                check(
                    result.ok or "没有检索到" in result.content,
                    f"{q!r} → 链路正常（{'命中' if result.ok else '无命中'}）",
                )

        # ---------- 4. 关键证据：请求真的到达了另一个进程 ----------
        print("\n[4] 关键证据：RAG 服务自己的指标")
        after = _metric_count((await probe.get(f"{RAG_URL}/metrics")).text,
                              "jobpilot_rag_request_ms")
        delta = after - before
        check(
            delta > 0,
            f"RAG 服务的请求计数增长了 {delta}（{before} → {after}）"
            "—— 证明调用真的跨了进程，而不是悄悄走了本地路径",
        )

        # ---------- 5. trace 贯穿 ----------
        print("\n[5] trace 串联")
        resp = await probe.post(
            f"{RAG_URL}/context",
            json={"query": "Kafka", "k": 2},
            headers={"X-Trace-Id": "verify-split-abc123"},
        )
        check(
            resp.headers.get("X-Trace-Id") == "verify-split-abc123",
            f"RAG 服务回显了上游 trace id：{resp.headers.get('X-Trace-Id')}",
        )

        # ---------- 6. 故障降级 ----------
        print("\n[6] 故障降级：指向一个不存在的端口")
        dead = RemoteKnowledgeBackend("http://127.0.0.1:9", timeout=2.0)
        dead_tool = KnowledgeSearchTool(settings=settings, backend=dead)
        result = await dead_tool.run(SearchKnowledgeParams(query="任何"))
        check(not result.ok, "后端不可用时工具返回失败而不是抛异常")
        check(
            "RAG 服务" in result.content and "不可用" in result.content,
            "提示信息可操作（指出了 RAG 服务）",
        )
        await dead.aclose()

    print("\n" + "=" * 62)
    if _failures:
        print(f"验证失败 {len(_failures)} 项：")
        for f in _failures:
            print(f"  · {f}")
        print("=" * 62)
        return 1
    print("全部通过：拆分后的跨进程调用链路成立。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
