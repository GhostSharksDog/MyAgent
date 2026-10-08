"""桌面实例协调；源码服务没有控制器时返回 404。"""

import secrets

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(include_in_schema=False)


@router.get("/_desktop/instance")
async def identify(request: Request):
    control = getattr(request.app.state, "desktop_control", None)
    if control is None:
        raise HTTPException(404, "此服务不是桌面实例。")
    return {"instance": control["instance"]}


@router.post("/_desktop/exit")
async def exit_desktop(request: Request):
    control = getattr(request.app.state, "desktop_control", None)
    if control is None:
        raise HTTPException(404, "此服务不是桌面实例。")
    if not secrets.compare_digest(
        request.headers.get("X-Legacy-Instance", ""), control["instance"]
    ):
        raise HTTPException(403, "请从 Legacy 托盘菜单退出。")
    control["exit"]()
    return {"exiting": True}
