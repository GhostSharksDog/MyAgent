"""MCP 管理入口与其它 /api 接口共用访问控制。"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.api.settings import _apply, _write_env
from app.core.config import get_settings
from app.mcp_client.catalog import ServerConfig, merge_secrets
from app.mcp_client.tool import trust_allowed, validate_definition

router = APIRouter(prefix="/api/mcp", tags=["MCP"])


def manager(request):
    return request.app.state.mcp


@router.get("")
async def view(request: Request):
    return manager(request).view()


class Enabled(BaseModel):
    enabled: bool


@router.patch("/config")
async def configure(payload: Enabled, request: Request):
    m = manager(request)
    async with m.lock:
        if payload.enabled and m.error:
            raise HTTPException(409, m.error)
        _write_env({"MCP_ENABLED": "true" if payload.enabled else "false"})
        _apply()
        m.settings = get_settings().mcp
        request.app.state.settings = request.app.state.settings.model_copy(
            update={"mcp": m.settings}
        )
        if m.settings.enabled:
            await m.start()
        else:
            await m.close()
        m.on_change()
        return m.view()


@router.put("/servers")
async def save_server(payload: dict, request: Request):
    m = manager(request)
    async with m.lock:
        if m.error:
            raise HTTPException(409, m.error)
        try:
            # 只读授权由独立接口绑定真实已发现的定义，不能随配置导入。
            supplied = {**payload, "trusted_tools": {}}
            new = ServerConfig.model_validate(supplied)
            old = m.servers.get(new.id)
            new = merge_secrets(new, old)
            if old and new.connection_fingerprint() == old.connection_fingerprint():
                new.trusted_tools = old.trusted_tools.copy()
            updated = {**m.servers, new.id: new}
            if (
                len(updated) > 16
                or sum(len(s.selected_tools) for s in updated.values() if s.enabled)
                > m.settings.max_tools
            ):
                raise ValueError("limit")
            m.catalog.save(list(updated.values()))
        except (ValueError, TypeError):
            raise HTTPException(
                422,
                "MCP 配置无效：检查地址、绝对工作目录、字段格式；最多16个服务和32个启用工具。密钥请放在请求头中。",
            ) from None
        except OSError:
            raise HTTPException(503, "无法保存 MCP 清单，请检查配置目录的写权限。") from None
        m.servers = updated
        if new.id in m.connections:
            await m.connections.pop(new.id).close()
        if new.enabled and m.settings.enabled:
            await m.connect(new.id)
        m.on_change()
        return m.view()


@router.post("/servers/{server_id}/test")
async def test_server(server_id: str, request: Request):
    m = manager(request)
    async with m.lock:
        if server_id not in m.servers:
            raise HTTPException(404, "MCP 服务不存在，请刷新列表。")
        # 显式点击测试会启动已配置的本地程序，但不调用任何业务工具。
        await m.connect(server_id)
        return m.view()


@router.delete("/servers/{server_id}")
async def delete_server(server_id: str, request: Request):
    m = manager(request)
    async with m.lock:
        if m.error:
            raise HTTPException(409, m.error)
        updated = {k: v for k, v in m.servers.items() if k != server_id}
        try:
            m.catalog.save(list(updated.values()))
        except OSError:
            raise HTTPException(503, "无法保存 MCP 清单，请检查配置目录的写权限。") from None
        m.servers = updated
        if server_id in m.connections:
            await m.connections.pop(server_id).close()
        m.on_change()
        return m.view()


class Selection(BaseModel):
    selected: list[str]
    trusted: list[str]


@router.put("/servers/{server_id}/tools")
async def select_tools(server_id: str, payload: Selection, request: Request):
    m = manager(request)
    async with m.lock:
        conn = m.connections.get(server_id)
        if not conn or not conn.client:
            raise HTTPException(409, "先测试连接并读取当前工具，再选择与授权。")
        selected = list(dict.fromkeys(payload.selected))
        total = len(selected) + sum(
            len(s.selected_tools) for k, s in m.servers.items() if k != server_id and s.enabled
        )
        if total > m.settings.max_tools or not set(payload.trusted).issubset(selected):
            raise HTTPException(422, "最多启用32个工具，只能信任已选择的工具。")
        try:
            for name in selected:
                validate_definition(conn.definitions[name])
            if any(not trust_allowed(name, conn.definitions[name]) for name in payload.trusted):
                raise ValueError("cannot trust")
        except (KeyError, ValueError):
            raise HTTPException(
                422, "工具不可用、参数定义不受支持或不是可授权的只读工具，请刷新列表。"
            ) from None
        server = m.servers[server_id].model_copy(deep=True)
        server.selected_tools = selected
        server.trusted_tools = {name: conn.fingerprint(name) for name in payload.trusted}
        updated = {**m.servers, server_id: server}
        try:
            m.catalog.save(list(updated.values()))
        except OSError:
            raise HTTPException(503, "无法保存 MCP 清单，请检查配置目录的写权限。") from None
        m.servers = updated
        m.on_change()
        return m.view()
