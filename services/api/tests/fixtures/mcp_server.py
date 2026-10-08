"""公开合成 MCP 服务；仅供临时工作区内的集成测试。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("Legacy public test")


@server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
async def echo(text: str, delay: float = 0) -> dict:
    await asyncio.sleep(delay)
    return {"text": text}


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
def write_public(text: str) -> str:
    Path("public.txt").write_text(text, encoding="utf-8")
    return "public.txt written"


@server.tool()
def fail() -> str:
    raise ValueError("public failure")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        import uvicorn

        uvicorn.run(
            server.streamable_http_app(), host="127.0.0.1", port=int(sys.argv[1]), log_level="error"
        )
    else:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        Path("mcp-pids.txt").write_text(f"{os.getpid()}\n{child.pid}\n", encoding="utf-8")
        server.run()
