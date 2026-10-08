"""显式验证 Exa 公开 Tools；最多两次业务调用，不调用模型、不读取应用配置。"""

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "api"))


async def verify(output, proxy):
    from app.core.config import MCPSettings
    from app.mcp_client.catalog import ServerConfig
    from app.mcp_client.manager import MCPManager

    report = {"source": "public Exa MCP; no LLM", "tool_calls": 0, "results": []}
    with tempfile.TemporaryDirectory(prefix="legacy-mcp-") as folder:
        manager = MCPManager(MCPSettings(_env_file=None, enabled=True, config_path=Path(folder) / "mcp.json", connect_timeout=30, tool_timeout=30))
        config = ServerConfig(name="Exa public verification", enabled=True,
                              url="https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa", proxy=proxy)
        manager.servers[config.id] = config
        started = time.monotonic()
        try:
            await manager.start()
            conn = manager.connections[config.id]
            report["connect_ms"] = round((time.monotonic() - started) * 1000)
            report["tools"] = list(conn.definitions)
            report["protocol"] = conn.protocol
            if not conn.client:
                report["error"] = conn.error
            else:
                for name, args in [
                    ("web_search_exa", {"query": "Model Context Protocol official documentation", "objective": "Find official MCP introduction documentation; exclude unofficial mirrors.", "numResults": 2}),
                    ("web_fetch_exa", {"urls": ["https://example.com"], "maxCharacters": 500}),
                ]:
                    if name not in conn.definitions:
                        report["results"].append({"tool": name, "verified": False, "reason": "tool not discovered"})
                        continue
                    from jsonschema import Draft202012Validator
                    try:
                        Draft202012Validator(conn.definitions[name]["inputSchema"]).validate(args)
                    except Exception:
                        report["results"].append({"tool": name, "verified": False, "reason": "example arguments do not match current schema", "schema": conn.definitions[name]["inputSchema"]})
                        continue
                    start = time.monotonic()
                    report["tool_calls"] += 1
                    try:
                        async with asyncio.timeout(35):
                            result = await conn.client.call_tool(name, args)
                        texts = "\n".join(c.text for c in result.content if c.type == "text")
                        report["results"].append({"tool": name, "verified": not result.is_error, "duration_ms": round((time.monotonic() - start) * 1000), "characters": len(texts), "preview": texts[:500]})
                    except Exception as exc:
                        report["results"].append({"tool": name, "verified": False, "reason": type(exc).__name__, "duration_ms": round((time.monotonic() - start) * 1000)})
        finally:
            await manager.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exa", action="store_true", help="显式允许两次公开 Exa 调用")
    parser.add_argument("--proxy", default="", help="显式代理，不读取系统代理")
    parser.add_argument("--output", type=Path, default=Path("data/mcp-exa.json"))
    args = parser.parse_args()
    if not args.exa:
        parser.error("联网验证必须显式使用 --exa")
    asyncio.run(verify(args.output, args.proxy))
