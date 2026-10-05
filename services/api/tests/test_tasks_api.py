"""任务 HTTP 接口测试。

存储与执行逻辑由 test_tasks.py 覆盖，这里只测 HTTP 层的契约：
路由、状态码、错误信息是否可操作，以及**任务真的会被执行**。
"""

from __future__ import annotations

import time

import pytest
from app.main import app
from app.tasks.queue import InProcessTaskQueue
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _fresh_queue() -> None:
    """每个用例一个干净队列，并**关掉自动启动**。

    后台 worker 会让测试变得不确定（异步副作用晚于断言是最难查的一类问题），
    所以这里用 ImmediateTaskQueue 的语义：需要执行时显式驱动。
    """
    from app.tasks.factory import register_default_handlers

    queue = InProcessTaskQueue(worker_count=1)
    register_default_handlers(queue)
    app.state.tasks = queue


class TestTaskApi:
    def test_list_empty(self, client: TestClient) -> None:
        r = client.get("/api/tasks")
        assert r.status_code == 200
        body = r.json()
        assert body["tasks"] == []
        assert body["backend"] == "memory"
        assert set(body["known_types"]) == {"reindex", "ingest_resume", "batch_match"}

    def test_submit_returns_pending(self, client: TestClient) -> None:
        """提交必须**立即返回**，而不是等任务跑完。

        同步等待会占着 HTTP 连接、没有进度反馈，且反向代理通常先超时。
        """
        r = client.post("/api/tasks", json={"type": "reindex", "payload": {}})
        assert r.status_code == 200
        body = r.json()
        assert body["id"]
        # 未启动 worker，所以仍是 pending —— 这恰好验证了"立即返回"
        assert body["status"] == "pending"
        assert body["progress"] == 0

    def test_unknown_type_rejected_with_known_list(self, client: TestClient) -> None:
        """早期失败要给出可操作信息：把已知类型列出来。"""
        r = client.post("/api/tasks", json={"type": "不存在的任务"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "未知任务类型" in detail
        assert "reindex" in detail

    def test_get_unknown_returns_404_with_hint(self, client: TestClient) -> None:
        """404 要顺带说明常见成因：进程内队列重启后记录不保留。"""
        r = client.get("/api/tasks/not-a-real-id")
        assert r.status_code == 404
        assert "重启" in r.json()["detail"]

    def test_detail_includes_payload(self, client: TestClient) -> None:
        r = client.post(
            "/api/tasks", json={"type": "ingest_resume", "payload": {"source": "/tmp/a.pdf"}}
        )
        task_id = r.json()["id"]
        detail = client.get(f"/api/tasks/{task_id}").json()
        assert detail["payload"] == {"source": "/tmp/a.pdf"}

    def test_cancel_pending(self, client: TestClient) -> None:
        task_id = client.post("/api/tasks", json={"type": "reindex"}).json()["id"]
        r = client.delete(f"/api/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_cancel_unknown_returns_404(self, client: TestClient) -> None:
        assert client.delete("/api/tasks/nope").status_code == 404

    def test_limit_clamped(self, client: TestClient) -> None:
        assert client.get("/api/tasks?limit=100000").status_code == 200

    def test_task_actually_executes(self, client: TestClient, seeded_corpus: None) -> None:
        """端到端：提交 reindex → 手动驱动 worker → 结果可查。

        这里直接调用队列的执行入口而不是启动 worker，
        让"任务执行"这一步变成确定性的，断言才有意义。

        `seeded_corpus` 同样是前提声明：默认配置下语料为空，reindex 必然失败
        （那是有意行为），所以"能成功执行"的前提要先摆出来。
        """
        import asyncio

        queue: InProcessTaskQueue = app.state.tasks
        task_id = client.post("/api/tasks", json={"type": "reindex"}).json()["id"]

        async def run_it() -> None:
            record = await queue.get(task_id)
            assert record is not None
            await queue._execute(record)

        asyncio.run(run_it())

        detail = client.get(f"/api/tasks/{task_id}").json()
        assert detail["status"] == "succeeded"
        assert detail["progress"] == 100
        assert detail["result"]["chunk_count"] > 0
        assert detail["duration_ms"] is not None

    def test_failed_task_exposes_error(self, client: TestClient) -> None:
        import asyncio

        queue: InProcessTaskQueue = app.state.tasks
        # 缺 source 参数 → 处理器抛 ValueError
        task_id = client.post("/api/tasks", json={"type": "ingest_resume"}).json()["id"]

        async def run_it() -> None:
            record = await queue.get(task_id)
            assert record is not None
            await queue._execute(record)

        asyncio.run(run_it())

        detail = client.get(f"/api/tasks/{task_id}").json()
        assert detail["status"] == "failed"
        assert "source" in detail["error"]
        assert detail["result"] is None  # 失败时不留半成品结果

    def test_list_after_submit(self, client: TestClient) -> None:
        for _ in range(3):
            client.post("/api/tasks", json={"type": "reindex"})
            time.sleep(0.005)
        tasks = client.get("/api/tasks").json()["tasks"]
        assert len(tasks) == 3
        # 列表不含 result（可能很大）
        assert "result" not in tasks[0]

    def test_healthz_and_meta_expose_task_backend(self, client: TestClient) -> None:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/meta").status_code == 200
