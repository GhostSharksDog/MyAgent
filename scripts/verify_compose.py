"""验证 docker compose 的拆分拓扑**真的**跑通了（技术债 T22 的证据）。

《为什么需要它：这套编排写出来很久，从来没跑过》

`docker-compose.yml` 与 `docker/Dockerfile` 是 P4 就写好的，但一直没人执行过
`docker compose up`。而编排文件是**代码**：不跑就等于没写。第一次真跑时
（本脚本诞生的那次）立刻暴露三个只能靠"真跑一次"发现的问题：

  1. `depends_on` 里写成了 `redis: *depends-on-redis`，别名展开后多套了一层 →
     compose 直接拒绝解析（`additional properties 'redis' not allowed`）；
  2. `RUN pip install ... $(python - <<'PY' … PY)` 这种 heredoc 嵌在命令替换里的
     写法 Dockerfile 解析器不接受（`unknown instruction: )`）；
  3. 前端产物根本没进镜像 → 容器 healthy、接口 200、**界面 404**，
     而且没有任何日志会说不对（`mount_frontend` 允许前端缺失）。

所以这个脚本检查的不是"容器起来了"，而是四件**配置错了也不会报错**的事：

    · /healthz 的三个 backend 字段（session/task=redis、rag=remote）
      —— 配错了不会报错，只会静默退回内存/单体
    · 界面真的能打开（不只是 API）
    · 访问控制真的生效（无密钥 401、带密钥 200，而 /healthz 仍然免密钥）
    · **任务真的被独立 worker 容器消费掉了** —— 这是拆分部署唯一无法用
      单进程测试证明的部分（api 里 task_workers_in_api=false，
      所以任务能完成就说明它跨了进程）

用法（需要 .env 里有 SECURITY_API_KEY 与 LLM_API_KEY）：
    docker compose up -d
    python scripts/verify_compose.py
    docker compose down
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

PORT = os.environ.get("APP_PORT", "8000")
BASE = f"http://127.0.0.1:{PORT}"
KEY = os.environ.get("SECURITY_API_KEY", "").strip()

ok = True


def check(condition: bool, label: str, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    if not condition:
        ok = False


def headers() -> dict[str, str]:
    return {"X-API-Key": KEY} if KEY else {}


def _worker_log_has(task_id: str) -> bool:
    """worker 容器的日志里有没有出现过这个 task_id。

    【为什么用日志而不是"任务成功"来证明跨进程】
    任务可能因为业务原因失败（比如语料为空时 reindex 会明确拒绝），
    那依然是**worker 执行过**的证据。日志里出现 task_id 这件事，
    与业务成败无关，只与"谁取走了它"有关 —— 而这正是要证明的。
    """
    try:
        proc = subprocess.run(
            ["docker", "compose", "logs", "worker", "--no-log-prefix"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(Path(__file__).resolve().parent.parent),
            check=False,
        )
    except OSError as exc:  # docker 不在 PATH 里
        print(f"        （无法读取 worker 日志：{exc}）")
        return False
    return task_id in (proc.stdout or "")


print("=" * 70)
print(f"docker compose 拓扑验证（{BASE}）")
print("=" * 70)

with httpx.Client(base_url=BASE, timeout=60.0) as c:
    print("\n=== 0. 服务可达 ===")
    try:
        health = c.get("/healthz").json()
    except Exception as exc:
        print(f"  [FAIL] 连不上 {BASE}：{exc}")
        print("\n  先确认容器在跑：docker compose ps")
        sys.exit(1)
    check(health.get("status") == "ok", "GET /healthz 返回 ok")

    print("\n=== 1. 三个 backend（配错了不会报错，只会静默退化）===")
    session_backend = health.get("session_backend")
    task_backend = health.get("task_backend")
    rag_backend = health.get("rag_backend")
    print(f"        session={session_backend}  task={task_backend}  rag={rag_backend}")
    check(session_backend == "redis", "会话存储走 Redis（否则多副本会丢历史）")
    check(task_backend == "redis", "任务队列走 Redis（否则独立 worker 收不到任务）")
    check(rag_backend == "remote", "检索走独立的 RAG 服务（否则拆分白做了）")
    # 这一条是下面"跨进程"证明的前提
    check(
        health.get("task_workers_in_api") is False,
        "api 进程内没有 worker（任务只能由独立 worker 消费）",
    )

    print("\n=== 2. 界面（前端产物真的进镜像了吗）===")
    page = c.get("/")
    check(page.status_code == 200, f"GET / → {page.status_code}")
    body = page.text
    check("<div id=\"root\">" in body or "root" in body, "返回的是前端页面而不是空的目录列表")
    # 只提供 API 时这里会 404 —— 那正是"镜像里没有 dist"的症状
    asset = None
    for token in body.split('"'):
        if token.startswith("/assets/") and token.endswith(".js"):
            asset = token
            break
    check(asset is not None, "页面里引用了构建产物", asset or "")
    if asset:
        r = c.get(asset)
        check(r.status_code == 200 and len(r.content) > 1000, f"静态资源可访问（{asset}）")

    print("\n=== 3. 访问控制（对外暴露必须有密钥）===")
    check(health.get("auth_required") is True, "服务声明自己要求密钥", str(health.get("auth_required")))
    check(c.get("/api/meta").status_code == 401, "无密钥访问 /api/meta → 401")
    if KEY:
        check(c.get("/api/meta", headers=headers()).status_code == 200, "带密钥访问 /api/meta → 200")
    else:
        check(False, "SECURITY_API_KEY 未设置 —— 无法验证带密钥的路径")
    check(c.get("/healthz").status_code == 200, "/healthz 仍然免密钥（探针不需要凭据）")

    print("\n=== 4. 跨进程：任务由独立 worker 消费 ===")
    # 【为什么这条断言不要求"任务成功"】
    # 第一次跑这个脚本时它报 FAIL —— 因为 reindex 在**语料为空**时会明确失败
    # （handle_reindex 拒绝建一个空索引，并给出三条添加语料的指引）。
    # 那是业务层的正确行为，不是投递失败：任务确实被 worker 取走并执行了。
    # 所以这里要证明的是**"谁执行了它"**，而不是"业务是否成功"：
    #   · api 容器里 task_workers_in_api=false（上面已断言）→ api 不会消费它；
    #   · 于是任务能离开 pending/running 状态，就说明有另一个进程在消费；
    #   · 再用 **worker 容器日志里是否出现这个 task_id** 作为直接证据。
    # 只断言"任务成功"会把一个正确的业务拒绝误判成部署故障。
    r = c.post("/api/tasks", json={"type": "reindex"}, headers=headers())
    check(r.status_code in (200, 202), f"投递 reindex 任务 → {r.status_code}")
    task_id = ""
    if r.status_code in (200, 202):
        payload = r.json()
        task_id = str(payload.get("task_id") or payload.get("id") or "")
        print(f"        task_id={task_id}")
    check(bool(task_id), "拿到了 task_id")

    if task_id:
        deadline = time.time() + 120
        status, detail = "?", {}
        while time.time() < deadline:
            try:
                detail = c.get(f"/api/tasks/{task_id}", headers=headers()).json()
                status = str(detail.get("status", "?"))
            except Exception as exc:
                status = f"读取失败：{exc}"
            if status in ("succeeded", "failed", "error"):
                break
            time.sleep(1.5)
        print(f"        最终状态 = {status}")
        check(status in ("succeeded", "failed"), "任务被消费并到达终态（跨进程可达）")

        err = str(detail.get("error") or "")
        if status == "failed":
            # 说清"失败是业务原因"，否则读日志的人会以为是部署问题
            business = "语料" in err or "corpus" in err.lower()
            check(business, "失败原因是业务层的（语料为空），不是投递/连接问题", err[:60])
        check(
            _worker_log_has(task_id),
            "worker 容器日志里出现了这个 task_id（执行者是独立进程，不是 api）",
        )

    print("\n=== 5. 语料卷（三个服务看同一份数据）===")
    ws = c.get("/api/files/workspace", headers=headers())
    check(ws.status_code == 200, f"工作区接口可用 → {ws.status_code}")

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
