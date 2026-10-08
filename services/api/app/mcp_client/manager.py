"""一个服务一个 SDK 生命周期任务；请求取消不重启其它服务或重发操作。"""

import asyncio
from contextlib import AsyncExitStack

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.mcp_client.catalog import Catalog, digest
from app.mcp_client.errors import rate_limited
from app.mcp_client.transport import stdio_transport


class Connection:
    def __init__(self, config, settings):
        self.config = config
        self.settings = settings
        self.client = None
        self.definitions = {}
        self.error = ""
        self.protocol = ""
        self.stop = asyncio.Event()
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._serve())

    async def _serve(self):
        try:
            async with AsyncExitStack() as stack:
                if self.config.transport == "stdio":
                    transport = stdio_transport(self.config)
                else:
                    http = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            headers=self.config.headers,
                            proxy=self.config.proxy or None,
                            trust_env=False,
                            timeout=self.settings.tool_timeout,
                        )
                    )
                    transport = streamable_http_client(self.config.url, http_client=http)
                client = await stack.enter_async_context(
                    Client(
                        transport,
                        read_timeout_seconds=self.settings.tool_timeout,
                        input_required_max_rounds=0,
                    )
                )
                cursor, seen = None, set()
                for _ in range(32):
                    page = await client.list_tools(cursor=cursor)
                    for tool in page.tools:
                        if tool.name in self.definitions:
                            raise ValueError("duplicate tool")
                        value = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                        if len(str(value)) > 65536 or len(self.definitions) >= 256:
                            raise ValueError("tool definitions too large")
                        self.definitions[tool.name] = value
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                    if cursor in seen:
                        raise ValueError("pagination cycle")
                    seen.add(cursor)
                else:
                    raise ValueError("too many pages")
                self.client = client
                self.protocol = client.protocol_version
                self.ready.set_result(True)
                await self.stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # SDK 异常可能含 URL、请求头或服务端原文；只给安全分类。
            self.error = (
                "MCP 服务限流（HTTP 429）；免密钥入口受服务方频率限制，请稍后再测试或配置自己的鉴权。"
                if rate_limited(exc)
                else f"连接或工具发现失败（{type(exc).__name__}）；请检查地址、鉴权、代理及服务日志后重新测试。"
            )
        finally:
            self.client = None
            if not self.ready.done():
                self.ready.set_result(False)

    async def start(self):
        try:
            async with asyncio.timeout(self.settings.connect_timeout):
                await asyncio.shield(self.ready)
        except (TimeoutError, asyncio.CancelledError):
            await self.close()
            self.error = "连接超时；请检查程序、地址或代理，再测试连接。"
            raise

    async def close(self):
        self.stop.set()
        if not self.ready.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    def fingerprint(self, name):
        return digest([self.config.connection_fingerprint(), self.definitions[name]])


class MCPManager:
    def __init__(self, settings, on_change=lambda: None):
        self.settings = settings
        self.catalog = Catalog(settings.config_path)
        self.connections = {}
        self.servers = {}
        self.error = ""
        self.on_change = on_change
        self.lock = asyncio.Lock()
        try:
            self.servers = {s.id: s for s in self.catalog.load()}
            if (
                sum(len(s.selected_tools) for s in self.servers.values() if s.enabled)
                > settings.max_tools
            ):
                raise ValueError("too many selected tools")
        except (OSError, ValueError, TypeError):
            self.error = (
                "MCP 清单无法读取；请检查 MCP_CONFIG_PATH，修复原文件后重启。未覆盖原文件。"
            )

    async def start(self):
        if self.settings.enabled and not self.error:
            for server in self.servers.values():
                if server.enabled:
                    await self.connect(server.id)

    async def connect(self, server_id):
        if server_id in self.connections:
            await self.connections.pop(server_id).close()
        connection = Connection(self.servers[server_id].model_copy(deep=True), self.settings)
        self.connections[server_id] = connection
        try:
            await connection.start()
        except TimeoutError:
            pass
        self.on_change()
        return connection

    async def close(self):
        await asyncio.gather(*(c.close() for c in self.connections.values()))
        self.connections.clear()

    def tools(self):
        from app.mcp_client.tool import MCPTool

        if not self.settings.enabled or self.error:
            return []
        result = []
        for server in self.servers.values():
            conn = self.connections.get(server.id)
            if not server.enabled or conn is None or conn.client is None:
                continue
            for name in server.selected_tools:
                if name in conn.definitions:
                    try:
                        result.append(MCPTool(self, server.id, name, conn))
                    except ValueError:
                        continue  # discovery view gives the reason; never exposes an invalid schema
        return result[: self.settings.max_tools]

    def valid(self, server_id, name, fingerprint):
        server = self.servers.get(server_id)
        conn = self.connections.get(server_id)
        return bool(
            self.settings.enabled
            and server
            and server.enabled
            and name in server.selected_tools
            and conn
            and conn.client
            and conn.config.connection_fingerprint() == server.connection_fingerprint()
            and name in conn.definitions
            and conn.fingerprint(name) == fingerprint
        )

    def view(self):
        from app.mcp_client.tool import trust_allowed, validate_definition

        servers = []
        for server in self.servers.values():
            conn = self.connections.get(server.id)
            tools = []
            for name, value in conn.definitions.items() if conn else []:
                error = ""
                try:
                    validate_definition(value)
                except ValueError as exc:
                    error = str(exc)
                tools.append(
                    {
                        "name": name,
                        "description": value.get("description", ""),
                        "selected": name in server.selected_tools,
                        "trust_allowed": not error and trust_allowed(name, value),
                        "trusted": server.trusted_tools.get(name) == conn.fingerprint(name),
                        "error": error,
                    }
                )
            servers.append(
                {
                    **server.public(),
                    "status": "connected"
                    if conn and conn.client
                    else "error"
                    if conn
                    else "disabled",
                    "error": conn.error if conn else "",
                    "protocol": conn.protocol if conn else "",
                    "tools": tools,
                }
            )
        return {
            "enabled": self.settings.enabled,
            "error": self.error,
            "servers": servers,
            "max_tools": self.settings.max_tools,
        }
