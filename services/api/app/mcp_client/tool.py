"""将 MCP 原始 schema 适配到共享工具注册表，审批与执行沿用原 RunContext。"""

import asyncio
import copy
import json
import re
import time

from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for
from pydantic import BaseModel

from app.agent.approvals import ApprovalUnavailable
from app.agent.operations import record_operation
from app.agent.runtime import current_run_context
from app.mcp_client.catalog import digest
from app.mcp_client.errors import rate_limited
from app.tools.base import Tool, ToolResult, _truncate


def validate_definition(value):
    schema = value.get("inputSchema", {})
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("仅支持 object 类型工具参数，请调整服务 schema")

    def walk(item, depth=0):
        if depth > 32:
            raise ValueError("参数 schema 嵌套过深")
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(child, str) or not child.startswith("#/")
                ):
                    raise ValueError("不支持外部或动态参数引用；请服务提供内联 schema")
                if key == "$id":
                    raise ValueError("不支持 schema 内的外部标识，请移除 $id")
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)

    walk(schema)
    try:
        schema_validator(schema).check_schema(schema)
    except Exception as exc:
        raise ValueError("工具参数 schema 无效，请修复服务定义") from exc
    return schema


def schema_validator(schema):
    # Unknown dialects must not silently fall back to a different validator.
    cls = validator_for(schema, default=None) if "$schema" in schema else Draft202012Validator
    if cls is None:
        raise ValueError("不支持此 JSON Schema 方言，请使用标准 draft-07 或 2020-12")
    return cls


def trust_allowed(name, definition):
    annotations = definition.get("annotations", {})
    # 声明只允许出现授权入口，绝不会自动授权。明显写入/执行能力始终需人审。
    known_read = name in {"web_search_exa", "web_fetch_exa"}
    normalized = re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower().replace("-", "_")
    destructive_name = re.search(
        r"(^|_)(write|edit|delete|remove|move|execute|exec|run|terminal|shell|create|update|send)(_|$)",
        normalized,
    )
    return bool(
        not destructive_name
        and annotations.get("destructiveHint") is not True
        and (known_read or annotations.get("readOnlyHint") is True)
    )


class MCPTool(Tool):
    params_model = BaseModel

    def __init__(self, manager, server_id, remote_name, connection):
        self.manager, self.server_id, self.remote_name = manager, server_id, remote_name
        self.definition = copy.deepcopy(connection.definitions[remote_name])
        self.schema = validate_definition(self.definition)
        self.fingerprint = connection.fingerprint(remote_name)
        self.name = f"mcp_{server_id}_{digest(remote_name)[:16]}"
        self.description = (
            f"MCP {manager.servers[server_id].name} / {remote_name}："
            + self.definition.get("description", "")[:4000]
        )
        self.timeout = manager.settings.tool_timeout
        self.source = "mcp"

    @property
    def serial(self):
        return not self.trusted()

    def trusted(self):
        server = self.manager.servers.get(self.server_id)
        return bool(
            server
            and trust_allowed(self.remote_name, self.definition)
            and server.trusted_tools.get(self.remote_name) == self.fingerprint
        )

    def json_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.schema),
            },
        }

    async def run(self, params):
        return ToolResult.failure("MCP 必须通过注册表与审批执行")

    async def prepare_execution(self, call):
        context = current_run_context()
        if context is None:
            return ToolResult.failure("MCP 调用需要运行上下文，请使用聊天入口")
        context.check()
        if self.name in context.mcp_uncertain_tools:
            return ToolResult.failure(
                "本轮此 MCP 工具已有结果未确认的调用；请用户核实后另起一轮，不再重发"
            )
        if not self.manager.valid(self.server_id, self.remote_name, self.fingerprint):
            return ToolResult.failure("MCP 服务或工具定义已变化；请刷新工具后重新发起任务")
        try:
            arguments = copy.deepcopy(call.arguments)
            if len(json.dumps(arguments)) > 65536:
                raise ValueError("参数超过 64 KiB")
            schema_validator(self.schema)(self.schema).validate(arguments)
        except Exception:
            return ToolResult.failure("MCP 参数不符合工具 schema；请按声明的字段和类型重新调用")
        broker = context.approvals
        if broker and broker.blocked_reason:
            return ToolResult.failure("本轮后续外部调用已被拒绝；请重新发起任务")
        if self.trusted():
            return (arguments, None, context)
        if broker is None:
            return ToolResult.failure(
                "外部工具尚未授权；请在聊天流式界面逐次确认，或显式信任只读工具"
            )
        try:
            approval_id, approved = await broker.request(
                {
                    "kind": "mcp",
                    "server_id": self.server_id,
                    "server_name": self.manager.servers[self.server_id].name,
                    "tool_name": self.remote_name,
                    "arguments": arguments,
                    "fingerprint": self.fingerprint,
                    "started": False,
                },
                timeout=self.manager.settings.approval_timeout,
            )
        except ApprovalUnavailable:
            return ToolResult.failure("MCP 调用未获批准；请重新发起任务并确认")
        if not approved:
            return ToolResult.failure("用户已拒绝外部调用，未发送")
        return (arguments, approval_id, context)

    async def execute_prepared(self, call, preparation):
        if not isinstance(preparation, tuple):
            return ToolResult.failure("MCP 缺少有效准备或批准")
        arguments, approval_id, context = preparation
        context.check()
        broker = context.approvals
        if (
            context is not current_run_context()
            or arguments != call.arguments
            or not self.manager.valid(self.server_id, self.remote_name, self.fingerprint)
            or (broker and (not broker.active or broker.blocked_reason))
            or (approval_id is None and not self.trusted())
            or self.name in context.mcp_uncertain_tools
        ):
            if approval_id and broker:
                broker.update(
                    approval_id, "conflict", "服务、工具或授权已变化，未发送调用；请重新确认"
                )
            return ToolResult.failure("MCP 参数、服务或权限已变化；调用未发送，请重新确认")
        if approval_id:
            item = broker.items.get(approval_id)
            if not item or item.view["status"] != "approved" or item.view.get("started"):
                return ToolResult.failure("MCP 批准已失效或已消费，未发送")
            broker.update(approval_id, "approved", "已发送外部调用，正在等待结果", started=True)
        record_operation("running")
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.timeout):
                conn = self.manager.connections[self.server_id]
                value = await conn.client.call_tool(self.remote_name, arguments)
            parts = []
            unsupported = []
            for block in value.content:
                if block.type == "text":
                    parts.append(block.text)
                else:
                    unsupported.append(block.type)
            if value.structured_content is not None:
                parts.append(json.dumps(value.structured_content, ensure_ascii=False))
            if unsupported:
                parts.append(
                    "[未处理的内容类型：" + ", ".join(unsupported) + "；未自动读取资源链接]"
                )
            ok = not value.is_error
            if not ok and re.search(r"rate.?limit|\b429\b|限流", "\n".join(parts), re.IGNORECASE):
                parts.append(
                    "[MCP 服务限流；免密钥入口受服务方频率限制，请稍后重试或配置自己的鉴权]"
                )
            content, truncated = _truncate("\n".join(parts))
            record_operation("succeeded" if ok else "failed")
            if approval_id:
                broker.update(
                    approval_id,
                    "applied" if ok else "failed",
                    "外部调用返回成功" if ok else "外部工具返回失败",
                )
            return ToolResult(
                ok=ok,
                content=content,
                error=None if ok else content,
                truncated=truncated or bool(unsupported),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except BaseException as exc:
            context.mcp_uncertain_tools.add(self.name)
            record_operation("unknown")
            message = (
                "MCP 服务限流（HTTP 429）；结果未确认，本轮不再重发。免密钥入口受频率限制，请稍后核实或配置自己的鉴权"
                if rate_limited(exc)
                else "MCP 调用中断或超时，结果未确认；请检查服务状态，不自动重发"
            )
            if approval_id:
                broker.update(
                    approval_id,
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    message,
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, Exception):
                raise
            return ToolResult.failure(
                message,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
