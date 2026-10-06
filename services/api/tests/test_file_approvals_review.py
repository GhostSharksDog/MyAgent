"""审批拒绝/超时的跨专家反例，以及 Windows 大小写敏感文件保护。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.agent.approvals import ApprovalBroker, ApprovalUnavailable
from app.agent.events import AgentEvent, EventType
from app.agent.runtime import RunContext
from app.core.config import get_settings
from app.llm.types import ToolCall
from app.tools import file_changes
from app.tools.base import ToolRegistry
from app.tools.files import FileAccessError, WriteFileTool, _check_secret


async def _preview(broker: ApprovalBroker, path: str):
    task = asyncio.create_task(broker.request({"path": path, "diff": "+ synthetic\n"}))
    async with asyncio.timeout(1):
        while True:
            event = await broker.events.get()
            if event.type is EventType.APPROVAL_REQUEST:
                break
    assert event.approval is not None
    assert event.approval["path"] == path
    return task, event.approval["id"]


async def test_one_rejection_invalidates_pending_and_already_approved_changes() -> None:
    broker = ApprovalBroker(timeout=0)
    approved_task, approved_id = await _preview(broker, "approved.txt")
    rejecting_task, rejecting_id = await _preview(broker, "rejected.txt")
    pending_task, pending_id = await _preview(broker, "pending.txt")

    assert broker.decide(approved_id, "approve") == "approved"
    assert broker.decide(rejecting_id, "reject") == "rejected"
    results = await asyncio.gather(approved_task, rejecting_task, pending_task)

    assert results == [(approved_id, True), (rejecting_id, False), (pending_id, False)]
    assert broker.rejected
    assert broker.blocked_reason is not None
    assert {item.view["status"] for item in broker.items.values()} == {"rejected"}
    with pytest.raises(ApprovalUnavailable, match="拒绝"):
        broker.decide(pending_id, "approve")
    with pytest.raises(ApprovalUnavailable, match="拒绝"):
        await broker.request({"path": "retry.txt"})
    assert len(broker.items) == 3


async def test_expiry_detected_by_decision_unblocks_all_waiting_experts() -> None:
    broker = ApprovalBroker(timeout=0)
    first_task, first_id = await _preview(broker, "first.txt")
    second_task, second_id = await _preview(broker, "second.txt")
    broker.items[first_id].expires_at = asyncio.get_running_loop().time() - 1

    with pytest.raises(ApprovalUnavailable, match="超时"):
        broker.decide(first_id, "approve")
    assert await asyncio.gather(first_task, second_task) == [
        (first_id, False),
        (second_id, False),
    ]
    assert {item.view["status"] for item in broker.items.values()} == {"expired"}
    with pytest.raises(ApprovalUnavailable, match="超时"):
        broker.decide(second_id, "approve")
    with pytest.raises(ApprovalUnavailable, match="超时"):
        await broker.request({"path": "retry.txt"})


async def test_approval_wait_timeout_blocks_further_previews() -> None:
    broker = ApprovalBroker(timeout=0.01)
    with pytest.raises(ApprovalUnavailable, match="超时"):
        await asyncio.wait_for(broker.request({"path": "expired.txt"}), timeout=1)
    assert len(broker.items) == 1
    assert next(iter(broker.items.values())).view["status"] == "expired"
    assert broker.blocked_reason is not None
    with pytest.raises(ApprovalUnavailable, match="超时"):
        await broker.request({"path": "retry.txt"})
    assert len(broker.items) == 1


async def test_rejection_does_not_relabel_completed_write_or_leak_into_next_run() -> None:
    broker = ApprovalBroker(timeout=0)
    applied_task, applied_id = await _preview(broker, "completed.txt")
    broker.decide(applied_id, "approve")
    assert await applied_task == (applied_id, True)
    broker.update(applied_id, "applied", "已写入")
    rejecting_task, rejecting_id = await _preview(broker, "other.txt")
    broker.decide(rejecting_id, "reject")
    assert await rejecting_task == (rejecting_id, False)
    assert broker.items[applied_id].view["status"] == "applied"

    next_broker = ApprovalBroker(timeout=0)
    next_task, next_id = await _preview(next_broker, "next-run.txt")
    assert next_broker.blocked_reason is None
    assert next_broker.decide(next_id, "approve") == "approved"
    assert await next_task == (next_id, True)


async def test_invalid_decision_does_not_accidentally_reject_or_approve() -> None:
    broker = ApprovalBroker(timeout=0)
    task, approval_id = await _preview(broker, "valid.txt")
    with pytest.raises(ApprovalUnavailable, match="approve 或 reject"):
        broker.decide(approval_id, "unexpected")
    assert broker.items[approval_id].view["status"] == "pending"
    assert broker.blocked_reason is None
    broker.decide(approval_id, "approve")
    assert await task == (approval_id, True)


@pytest.fixture
def approval_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "agent",
        settings.agent.model_copy(
            update={
                "workspace_root": str(root),
                "file_write_enabled": True,
                "file_approval_required": True,
                "run_timeout": 0,
            }
        ),
    )
    return root


async def test_turning_approval_off_cannot_bypass_prior_rejection(
    approval_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = ApprovalBroker(timeout=0)
    task, approval_id = await _preview(broker, "not-written.txt")
    broker.decide(approval_id, "reject")
    assert await task == (approval_id, False)
    context = RunContext.create(get_settings().agent)
    context.approvals = broker
    monkeypatch.setattr(file_changes, "current_run_context", lambda: context)
    monkeypatch.setattr(get_settings().agent, "file_approval_required", False)

    registry = ToolRegistry()
    registry.register(WriteFileTool())
    result = await registry.execute(
        ToolCall(
            id="after-reject",
            name="write_file",
            arguments={"path": "retry.txt", "content": "retry"},
        )
    )
    assert not result.ok
    assert result.error is not None
    assert "拒绝" in result.error
    assert not (approval_workspace / "retry.txt").exists()


async def test_rejection_while_approved_change_waits_for_write_lock_prevents_apply(
    approval_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = ApprovalBroker(timeout=0)
    context = RunContext.create(get_settings().agent)
    context.approvals = broker
    monkeypatch.setattr(file_changes, "current_run_context", lambda: context)
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    await registry._serial_lock.acquire()
    operation = asyncio.create_task(
        registry.execute(
            ToolCall(
                id="queued-write",
                name="write_file",
                arguments={"path": "queued.txt", "content": "approved but not applied"},
            )
        )
    )
    try:
        event = await asyncio.wait_for(broker.events.get(), timeout=1)
        assert event.type is EventType.APPROVAL_REQUEST
        assert event.approval is not None
        broker.decide(event.approval["id"], "approve")
        await asyncio.sleep(0)
        assert not operation.done()
        rejecting_task, rejecting_id = await _preview(broker, "reject-another.txt")
        broker.decide(rejecting_id, "reject")
        assert await rejecting_task == (rejecting_id, False)
    finally:
        registry._serial_lock.release()
    result = await asyncio.wait_for(operation, timeout=1)
    assert not result.ok
    assert result.error is not None and "拒绝" in result.error
    assert not (approval_workspace / "queued.txt").exists()


@pytest.mark.parametrize(
    "name", [".ENV", ".Env.Local", "ID_RSA", "Credentials", ".Git-Credentials", "private.PEM"]
)
def test_secret_names_are_case_insensitive(name: str) -> None:
    with pytest.raises(FileAccessError, match="敏感文件"):
        _check_secret(Path(name), allow=False, verb="写入")
    _check_secret(Path(name), allow=True, verb="写入")


async def test_merge_close_cancels_waiter_before_closing_source() -> None:
    from app.agent.approvals import merge_approval_events

    broker = ApprovalBroker(timeout=0)
    closed = asyncio.Event()

    async def source():
        try:
            yield AgentEvent(type=EventType.START)
            await broker.request({"path": "cancelled.txt"})
            yield AgentEvent(type=EventType.DONE)
        finally:
            closed.set()

    stream = merge_approval_events(source(), broker)
    assert (await asyncio.wait_for(anext(stream), timeout=1)).type is EventType.START
    assert (await asyncio.wait_for(anext(stream), timeout=1)).type is EventType.APPROVAL_REQUEST
    await asyncio.wait_for(stream.aclose(), timeout=1)
    assert closed.is_set()
    assert not broker.active
    assert all(item.future.done() for item in broker.items.values())
    with pytest.raises(ApprovalUnavailable, match="结束"):
        broker.decide(next(iter(broker.items)), "approve")
