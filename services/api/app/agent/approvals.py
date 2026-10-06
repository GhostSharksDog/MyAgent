"""请求内的文件审批通道。决定不执行写入，只有原工具任务能应用已批准的快照。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import anyio

from app.agent.events import AgentEvent, EventType


class ApprovalUnavailable(RuntimeError):
    pass


@dataclass
class PendingApproval:
    view: dict
    future: asyncio.Future[bool]
    expires_at: float | None


class ApprovalBroker:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.events: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self.items: dict[str, PendingApproval] = {}
        self.active = True
        self.rejected = False
        self.blocked_reason: str | None = None

    def _block(self, reason: str, status: str) -> None:
        """任何一份拒绝/超时都撤销本轮尚未应用的其它写入批准。"""
        if self.blocked_reason is not None:
            return
        self.blocked_reason = reason
        for approval_id, item in self.items.items():
            if item.view["status"] not in {"pending", "approved"}:
                continue
            self.update(approval_id, status, reason)
            if not item.future.done():
                item.future.set_result(False)

    def update(self, approval_id: str, status: str, message: str) -> None:
        item = self.items[approval_id]
        item.view = {**item.view, "status": status, "message": message}
        self.events.put_nowait(
            AgentEvent(type=EventType.APPROVAL_UPDATE, approval=item.view.copy())
        )

    async def request(self, view: dict) -> tuple[str, bool]:
        if not self.active:
            raise ApprovalUnavailable("本轮已停止；如需修改，请重新发起任务并核对预览。")
        if self.blocked_reason is not None:
            raise ApprovalUnavailable(self.blocked_reason)
        loop = asyncio.get_running_loop()
        approval_id = uuid4().hex
        view = {**view, "id": approval_id, "status": "pending", "message": "等待批准，尚未写入"}
        item = PendingApproval(
            view, loop.create_future(), loop.time() + self.timeout if self.timeout > 0 else None
        )
        self.items[approval_id] = item
        self.events.put_nowait(AgentEvent(type=EventType.APPROVAL_REQUEST, approval=view.copy()))
        try:
            approved = await asyncio.wait_for(
                item.future, timeout=self.timeout if self.timeout > 0 else None
            )
            return approval_id, approved
        except TimeoutError:
            self._block(
                "等待批准超时，本轮不再修改文件；请重新发起任务，可在工作区设置调整确认等待时间。",
                "expired",
            )
            raise ApprovalUnavailable(self.blocked_reason) from None
        except asyncio.CancelledError:
            self.update(approval_id, "cancelled", "运行已停止，此预览失效，未应用修改。")
            raise

    def decide(self, approval_id: str, decision: str) -> str:
        item = self.items.get(approval_id)
        if not self.active or item is None:
            raise ApprovalUnavailable("预览不存在或运行已结束；请重新发起修改。")
        if self.blocked_reason is not None:
            raise ApprovalUnavailable(self.blocked_reason)
        if decision not in {"approve", "reject"}:
            raise ApprovalUnavailable("审批决定必须是 approve 或 reject，请核对请求后重试。")
        if item.expires_at is not None and asyncio.get_running_loop().time() >= item.expires_at:
            self._block("等待批准超时，本轮不再修改文件；请重新生成差异后确认。", "expired")
            raise ApprovalUnavailable("预览已超时；请重新生成差异后确认。")
        if item.future.done() or item.view["status"] != "pending":
            raise ApprovalUnavailable("该预览已处理或失效，不能重复批准。")
        approved = decision == "approve"
        if not approved:
            self.rejected = True
            self._block(
                "用户已拒绝文件修改，本轮不再写入；如需修改，请重新发起任务并核对预览。", "rejected"
            )
            return "rejected"
        self.update(
            approval_id,
            "approved",
            "已批准，正在重新核验文件与权限；尚未确认写入成功",
        )
        item.future.set_result(True)
        return "approved"

    def close(self) -> None:
        self.active = False
        for item in self.items.values():
            if not item.future.done():
                item.future.cancel()


async def merge_approval_events(
    source: AsyncIterator[AgentEvent], broker: ApprovalBroker
) -> AsyncIterator[AgentEvent]:
    """工具等待时也转发预览；不依赖 Plan/Supervisor 转发子 Agent 的工具事件。"""
    next_event = asyncio.create_task(anext(source))
    next_approval = asyncio.create_task(broker.events.get())
    try:
        while True:
            await asyncio.wait((next_event, next_approval), return_when=asyncio.FIRST_COMPLETED)
            # 优先清空已经发生的审批事件，让实际写入结果不会越过它们。
            if next_approval.done():
                yield next_approval.result()
                next_approval = asyncio.create_task(broker.events.get())
                continue
            if not broker.events.empty():
                continue
            if next_event.done():
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                yield event
                if event.type is EventType.DONE:
                    return
                next_event = asyncio.create_task(anext(source))
    finally:
        broker.close()
        # AnyIO 的断流取消作用域不能打断子任务回收或生成器关闭。
        with anyio.CancelScope(shield=True):
            for task in (next_event, next_approval):
                if not task.done():
                    task.cancel()
            await asyncio.gather(next_event, next_approval, return_exceptions=True)
            await source.aclose()  # type: ignore[attr-defined]
