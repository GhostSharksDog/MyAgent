"""管理接口使用临时清单，验证密钥不回传、授权不可导入与损坏清单保护。"""

import httpx
import pytest
from app.api.mcp import router
from app.core.config import MCPSettings
from app.mcp_client.manager import MCPManager
from fastapi import FastAPI


@pytest.fixture
async def api(tmp_path):
    app = FastAPI()
    manager = MCPManager(MCPSettings(_env_file=None, config_path=tmp_path / "mcp.json"))
    app.state.mcp = manager
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, manager
    await manager.close()


async def test_crud_masks_secrets_and_rejects_imported_trust(api):
    client, manager = api
    config = {
        "name": "public",
        "url": "https://example.com/mcp",
        "headers": {"Authorization": "Bearer public-secret"},
        "trusted_tools": {"write_file": "forged"},
    }
    response = await client.put("/api/mcp/servers", json=config)
    assert response.status_code == 200
    server = response.json()["servers"][0]
    assert "public-secret" not in response.text
    assert server["trusted_tools"] == {}
    assert not server["enabled"] and not manager.connections
    assert "public-secret" in manager.catalog.path.read_text(encoding="utf-8")
    fields = {k: v for k, v in server.items() if k not in {"status", "error", "protocol", "tools"}}
    fields["name"] = "renamed"
    response = await client.put("/api/mcp/servers", json=fields)
    assert response.status_code == 200
    assert manager.servers[server["id"]].headers["Authorization"] == "Bearer public-secret"
    assert response.json()["servers"][0]["secret_configured"]
    assert (
        await client.put(
            f"/api/mcp/servers/{server['id']}/tools", json={"selected": [], "trusted": []}
        )
    ).status_code == 409
    fields["headers"] = {}
    response = await client.put("/api/mcp/servers", json=fields)
    assert response.status_code == 200
    assert not response.json()["servers"][0]["secret_configured"]
    assert not manager.servers[server["id"]].headers
    assert (await client.delete(f"/api/mcp/servers/{server['id']}")).status_code == 200
    assert manager.catalog.load() == []


async def test_invalid_secret_config_errors_never_echo_input(api):
    client, _ = api
    response = await client.put(
        "/api/mcp/servers",
        json={"name": "public", "url": "https://example.com/mcp?key=secret-value"},
    )
    assert response.status_code == 422 and "secret-value" not in response.text


async def test_corrupted_catalog_is_not_overwritten(api):
    client, manager = api
    manager.catalog.path.write_text("invalid", encoding="utf-8")
    manager.error = "清单无法读取，请先修复"
    assert (await client.delete("/api/mcp/servers/none")).status_code == 409
    assert (
        await client.put(
            "/api/mcp/servers", json={"name": "public", "url": "https://example.com/mcp"}
        )
    ).status_code == 409
    assert manager.catalog.path.read_text() == "invalid"
