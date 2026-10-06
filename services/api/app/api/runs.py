"""运行摘要查询受同一 /api 访问控制保护，不触发模型或工具。"""

from fastapi import APIRouter, HTTPException, Query, Request

from app.runs.history import REASONS, RunHistory, RunRecord

router = APIRouter(prefix="/api/runs", tags=["runs"])


def get_history(request: Request) -> RunHistory:
    return request.app.state.run_history


@router.get("", summary="查询运行摘要")
async def list_runs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session_id: str | None = None,
    stopped_reason: str | None = None,
) -> dict[str, object]:
    if stopped_reason is not None and stopped_reason not in REASONS | {"running"}:
        raise HTTPException(422, "未知结束状态，请使用运行列表中的状态值")
    store = get_history(request)
    rows = store.list(session_id=session_id, reason=stopped_reason)
    return {
        "backend": store.backend,
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "runs": [r.model_dump(exclude={"events"}) for r in rows[offset : offset + limit]],
        "max_records": store.settings.max_records,
        "summary_only": True,
    }


@router.get("/{run_id}", response_model=RunRecord, summary="查看一轮执行摘要")
async def get_run(run_id: str, request: Request) -> RunRecord:
    r = get_history(request).get(run_id)
    if r is None:
        raise HTTPException(404, "运行记录不存在或已淘汰；内存记录在服务重启后不会保留")
    return r


@router.delete("/{run_id}", summary="删除一条运行摘要")
async def delete_run(run_id: str, request: Request) -> dict[str, bool]:
    try:
        deleted = await get_history(request).delete(run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, "运行记录不存在或已淘汰")
    return {"deleted": True}
