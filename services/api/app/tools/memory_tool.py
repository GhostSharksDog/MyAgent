"""仅申请保存用户明确要求记住的事实；逐次批准后事务写入。"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, ValidationError

from app.agent.approvals import ApprovalUnavailable
from app.agent.memory import LongTermMemory
from app.agent.runtime import current_run_context
from app.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)


class RememberFactParams(BaseModel):
    fact: str = Field(
        description=(
            "要记住的事实，写成**完整、自足的陈述句**。"
            "要包含主语，因为这条记录将来会脱离当前对话被单独召回 —— "
            "例如『用户希望优先用中文回答』。不能自行提取未获用户确认的偏好。"
        ),
        min_length=1,
        max_length=500,
    )
    tags: list[str] = Field(
        default_factory=list,
        description="分类标签，例如 ['语言偏好']、['长期目标']。",
        max_length=20,
    )


class RememberFactTool(Tool):
    """把关于用户的重要事实写入长期记忆。"""

    name = "remember_fact"
    description = (
        "申请保存用户明确希望记住的事实或长期偏好。系统先展示完整内容，用户批准后才保存。"
        "不得自动提取或偷偷保存聊天内容；拒绝后不得声称已保存。"
    )
    params_model = RememberFactParams
    serial = True

    def __init__(self, memory: LongTermMemory) -> None:
        self._memory = memory

    def run(self, params: BaseModel) -> ToolResult:
        return ToolResult.failure("记忆尚未获批准；请使用聊天流式界面的记忆确认卡片。")

    async def prepare_execution(self, call):
        context = current_run_context()
        if not context or not context.approvals:
            return ToolResult.failure(
                "记忆需要用户逐次确认，请使用聊天流式界面，或在设置中直接添加。"
            )
        context.check()
        if not self._memory.enabled:
            return ToolResult.failure("长期记忆已关闭，请在记忆与存储设置中开启。")
        try:
            params = RememberFactParams.model_validate(call.arguments)
        except ValidationError:
            return ToolResult.failure("记忆内容需要 1–500 字。")
        try:
            approval_id, approved = await context.approvals.request(
                {"kind": "memory", "fact": params.fact, "tags": params.tags, "started": False}
            )
        except ApprovalUnavailable:
            return ToolResult.failure("本轮记忆确认已失效，未保存；请重新发起。")
        if not approved:
            return ToolResult.failure("用户拒绝保存记忆，未写入。")
        return approval_id, params, context

    async def execute_prepared(self, call, preparation):
        if not isinstance(preparation, tuple):
            return ToolResult.failure("记忆缺少有效批准，未保存。")
        approval_id, params, context = preparation
        context.check()
        broker = context.approvals
        item = broker.items.get(approval_id) if broker else None
        if (
            context is not current_run_context()
            or not self._memory.enabled
            or not broker
            or not broker.active
            or broker.blocked_reason
            or not item
            or item.view["status"] != "approved"
            or item.view.get("started")
            or RememberFactParams.model_validate(call.arguments) != params
        ):
            return ToolResult.failure("记忆批准或权限已变化，未保存；请重新确认。")
        broker.update(approval_id, "approved", "正在保存已确认的记忆", started=True)
        try:
            result = await self._invoke_sync(self._write, params)
        except BaseException:
            broker.update(approval_id, "failed", "保存被中断，请在记忆设置中核对实际状态")
            raise
        broker.update(
            approval_id,
            "applied" if result.ok else "failed",
            "记忆已保存到本机" if result.ok else result.content,
        )
        return result

    def _write(self, params):
        if not self._memory.enabled:
            return ToolResult.failure("长期记忆已关闭，未保存。")
        with self._memory._lock:
            added = self._memory.remember(params.fact, params.tags)
            self._memory.save()
        return ToolResult.success(
            "用户确认的记忆已保存。" if added else "这条记忆已存在，未重复保存。"
        )
