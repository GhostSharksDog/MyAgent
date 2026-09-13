"""异步任务接口。

    POST   /api/tasks            提交任务，立即返回 task_id
    GET    /api/tasks            任务列表（不含 result，可能很大）
    GET    /api/tasks/{id}       任务详情（含 result 与进度）
    DELETE /api/tasks/{id}       取消任务

【为什么是"提交 + 轮询"而不是同步等待】
`reindex` 这类任务会跑几秒到几十秒。同步等待有三重问题：
  1. 期间占着一个 HTTP 连接，反向代理通常会先超时
  2. 用户没有任何进度反馈，会以为页面卡死
  3. CPU 密集的活会阻塞事件循环，把别人的流式对话一起拖住

"提交立即返回 + 轮询进度"是这类操作的通用形态。前端只需每 500ms
查一次详情，代价极低，而进度可见带来的体验差异是数量级的。
（真要做到"服务端推送进度"就得上 WebSocket 或复用 SSE ——
那是更重的方案，当前阶段轮询足够。）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import TaskListResponse, TaskStatusModel, TaskSubmitRequest
from app.tasks.models import TaskRecord
from app.tasks.queue import TaskQueue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _queue(request: Request) -> TaskQueue:
    queue: TaskQueue = request.app.state.tasks
    return queue


def _to_model(record: TaskRecord) -> TaskStatusModel:
    return TaskStatusModel(
        id=record.id,
        type=record.type,
        status=str(record.status),
        progress=record.progress,
        message=record.message,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        duration_ms=record.duration_ms,
        result=record.result,
        error=record.error,
        payload=record.payload,
    )


@router.post("", response_model=TaskStatusModel, summary="提交任务")
async def submit_task(payload: TaskSubmitRequest, request: Request) -> TaskStatusModel:
    queue = _queue(request)

    if payload.type not in queue.known_types():
        # 早期失败给出可操作的信息：把已知类型列出来，
        # 而不是抛一个"未知类型"让它自己去猜
        raise HTTPException(
            status_code=400,
            detail=f"未知任务类型 {payload.type!r}。已知类型：{', '.join(queue.known_types()) or '（无）'}",
        )

    record = await queue.submit(
        payload.type, payload=payload.payload, session_id=payload.session_id
    )
    logger.info("已提交任务 %s（%s）", record.id, record.type)
    return _to_model(record)


@router.get("", response_model=TaskListResponse, summary="任务列表")
async def list_tasks(request: Request, limit: int = 20) -> TaskListResponse:
    queue = _queue(request)
    summaries = await queue.list(limit=max(1, min(limit, 100)))
    return TaskListResponse(
        tasks=[s.model_dump() for s in summaries],
        backend=queue.backend,
        known_types=queue.known_types(),
    )


@router.get("/{task_id}", response_model=TaskStatusModel, summary="任务详情")
async def get_task(request: Request, task_id: str) -> TaskStatusModel:
    record = await _queue(request).get(task_id)
    if record is None:
        # 404 而不是 200 + 空任务：让"任务不存在"显式暴露。
        # 但也提示一种常见成因：进程内队列在重启后记录会消失。
        raise HTTPException(
            status_code=404,
            detail=f"任务 {task_id} 不存在。若服务刚重启过，"
            f"进程内队列的任务记录不会保留（多进程部署请使用 Redis 后端）。",
        )
    return _to_model(record)


@router.delete("/{task_id}", response_model=TaskStatusModel, summary="取消任务")
async def cancel_task(request: Request, task_id: str) -> TaskStatusModel:
    queue = _queue(request)
    record = await queue.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    cancelled = await queue.cancel(task_id)
    if not cancelled:
        # 终态任务不可取消。返回 200 加当前状态，比 409 更利于前端处理 ——
        # 前端拿到最新状态就能正确渲染，不需要额外分支
        logger.info("任务 %s 处于终态 %s，忽略取消请求", task_id, record.status)
        return _to_model(record)

    refreshed = await queue.get(task_id)
    return _to_model(refreshed or record)
