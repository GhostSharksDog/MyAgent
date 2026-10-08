"""只读本机 MCP 状态；不触发连接，不回传地址、鉴权或进程配置。"""

import json

from pydantic import BaseModel, ConfigDict

from app.tools.base import Tool, ToolResult


class StatusParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MCPStatusTool(Tool):
    name = "get_mcp_status"
    description = (
        "查询本机 MCP 开关、连接状态、实际可调用的外部工具及缺失工具。"
        "不会连接服务或执行远程操作；工具尚未配置时给出设置入口。"
    )
    params_model = StatusParams

    def __init__(self, manager):
        self.manager = manager

    async def run(self, params):
        view = self.manager.view()
        tools = self.manager.tools()
        servers = []
        for server in view["servers"]:
            servers.append(
                {
                    "name": server["name"],
                    "enabled": server["enabled"],
                    "status": server["status"],
                    "error": server["error"],
                    "available_tool_count": server["available_tool_count"],
                    "missing_tools": server["missing_tools"],
                    "tools": [
                        {"name": t.remote_name, "call_name": t.name}
                        for t in tools
                        if t.server_id == server["id"]
                    ],
                }
            )
        return ToolResult.success(
            json.dumps(
                {
                    "enabled": view["enabled"],
                    "error": view["error"],
                    "servers": servers,
                    "next_step": "在设置 → MCP 添加、连接服务，并在高级设置选择所需工具；终端可配置 Desktop Commander。",
                },
                ensure_ascii=False,
            )
        )
