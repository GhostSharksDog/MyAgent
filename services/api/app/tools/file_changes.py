"""只读差异快照、批准后复核、应用。预览原文仅在活跃请求内存中存活。"""

from __future__ import annotations

import difflib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agent.approvals import ApprovalBroker, ApprovalUnavailable
from app.agent.runtime import current_run_context
from app.core.config import get_settings
from app.llm.types import ToolCall
from app.tools.base import Tool, ToolResult
from app.tools.files import (
    MAX_EDIT_BYTES,
    MAX_WRITE_CHARS,
    EditFileParams,
    FileAccessError,
    WriteFileParams,
    _check_secret,
    _rel,
    _resolve,
    workspace_root,
)

# 完整展示，不用截断差异冒充可审查。更大的文件明确拒绝，改用本机编辑器。
MAX_PREVIEW_LINES = 10000
_prepared: ContextVar[FileChange | None] = ContextVar("approved_file_change", default=None)


def _identity(path: Path) -> tuple[int, ...] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _text(raw: bytes) -> str:
    if b"\x00" in raw:
        raise FileAccessError("二进制文件不能生成文字差异；请使用本机编辑器。")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FileAccessError(
            "文件不是有效 UTF-8，不能安全预览或替换；请使用本机编辑器转换编码。"
        ) from None
    if len(text) > MAX_WRITE_CHARS or len(text.splitlines()) > MAX_PREVIEW_LINES:
        raise FileAccessError(
            "文件超出完整差异预览上限（200000 字符 / 10000 行）；请拆分修改或使用本机编辑器。"
        )
    return text


def unified_diff(before: str, after: str, path: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    )
    # 保留 CR/LF 字符差异；无结尾换行的行单独标注，否则两条 diff 行会粘在一起。
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    )


def text_format(raw: bytes | None) -> str:
    if raw is None:
        return "文件不存在"
    text = raw.decode("utf-8")
    endings = []
    if "\r\n" in text:
        endings.append("CRLF")
    remainder = text.replace("\r\n", "")
    if "\n" in remainder:
        endings.append("LF")
    if "\r" in remainder:
        endings.append("CR")
    return " · ".join(
        [
            "UTF-8 BOM" if raw.startswith(b"\xef\xbb\xbf") else "UTF-8",
            "/".join(endings) if endings else "无行分隔符",
            "有末尾换行" if text.endswith(("\r", "\n")) else "无末尾换行",
        ]
    )


@dataclass
class FileChange:
    tool_name: str
    arguments: dict[str, Any]
    root: Path
    target: Path
    before: bytes | None
    after: bytes
    identity: tuple[int, ...] | None
    operation: str
    broker: ApprovalBroker
    approval_id: str = ""


def build_change(tool: Tool, call: ToolCall, broker: ApprovalBroker) -> FileChange:
    params = tool.params_model.model_validate(call.arguments)
    settings = get_settings().agent
    if not settings.file_write_enabled:
        raise FileAccessError("写权限已关闭，未写入；请在工作区设置显式开启写权限。")
    target = _resolve(params.path, must_exist=False)  # type: ignore[attr-defined]
    _check_secret(target, allow=settings.file_allow_secrets, verb="写入")
    if target.exists() and not target.is_file():
        raise FileAccessError("目标是目录或非普通文件，不能修改；请指定文本文件。")
    if target.exists() and target.stat().st_size > MAX_EDIT_BYTES:
        raise FileAccessError("文件超过 2MB，不能完整预览；请使用本机编辑器。")
    identity = _identity(target)
    before = target.read_bytes() if identity is not None else None
    original = _text(before or b"")
    if isinstance(params, WriteFileParams):
        if before is not None and not params.overwrite:
            raise FileAccessError(
                "目标已存在；请先 read_file，再使用 edit_file，或显式 overwrite=true。"
            )
        updated = params.content
        operation = "create" if before is None else "overwrite"
    elif isinstance(params, EditFileParams):
        if before is None:
            raise FileAccessError("文件不存在；新建请使用 write_file。")
        if not params.old_text or params.old_text == params.new_text:
            raise FileAccessError(
                "old_text 不能为空，且 new_text 必须不同；请先 read_file 核对原文。"
            )
        if original.count(params.old_text) != 1:
            raise FileAccessError("old_text 必须唯一匹配；请先 read_file 并扩大原文上下文后重试。")
        updated = original.replace(params.old_text, params.new_text, 1)
        operation = "edit"
    else:
        raise FileAccessError("不支持此修改参数；请核对工具 Schema。")
    after = updated.encode("utf-8")
    _text(after)
    if before == after:
        raise FileAccessError("内容未变化，无需写入；请核对修改内容。")
    if _identity(target) != identity or (before is not None and target.read_bytes() != before):
        raise FileAccessError("生成预览期间文件已变化；请重新读取并生成差异。")
    root = workspace_root()
    assert root is not None  # _resolve 已校验，准备过程没有 await
    return FileChange(
        call.name, params.model_dump(), root, target, before, after, identity, operation, broker
    )


async def prepare_change(tool: Tool, call: ToolCall) -> FileChange | ToolResult | None:
    context = current_run_context()
    if context is not None and context.approvals is not None:
        if reason := context.approvals.blocked_reason:
            return ToolResult.failure(f"本轮禁止后续文件修改，未写入：{reason}")
    if not get_settings().agent.file_approval_required:
        return None
    if context is None or context.approvals is None:
        return ToolResult.failure(
            "写入需要差异批准，但当前入口没有审批通道；请使用聊天流式界面确认。未写入。"
        )
    context.check()
    broker: ApprovalBroker = context.approvals
    try:
        change = build_change(tool, call, broker)
        path = _rel(change.target)
        from app.agent.operations import record_operation

        record_operation("succeeded", target=path)
        approval_id, approved = await broker.request(
            {
                "path": path,
                "operation": change.operation,
                "diff": unified_diff(_text(change.before or b""), _text(change.after), path),
                "before_bytes": len(change.before or b""),
                "after_bytes": len(change.after),
                "before_format": text_format(change.before),
                "after_format": text_format(change.after),
            }
        )
        if not approved:
            return ToolResult.failure(
                f"未写入：{broker.blocked_reason or '用户拒绝修改'}；本轮不再请求文件修改。"
            )
        change.approval_id = approval_id
        context.check()
        return change
    except (FileAccessError, OSError, ValidationError, ApprovalUnavailable) as exc:
        return ToolResult.failure(f"未写入：{exc}")


@contextmanager
def bind_change(change: FileChange | None) -> Iterator[None]:
    token = _prepared.set(change)
    try:
        yield
    finally:
        _prepared.reset(token)


def _recheck(change: FileChange) -> None:
    settings = get_settings().agent
    if not settings.file_write_enabled:
        raise FileAccessError("写权限已关闭；此次批准失效，请重新确认权限。")
    if not change.broker.active:
        raise FileAccessError("运行已结束，此次批准失效；请重新发起修改。")
    if change.broker.blocked_reason:
        raise FileAccessError(f"本轮已禁止文件修改，批准失效：{change.broker.blocked_reason}")
    if context := current_run_context():
        context.check()
    target = _resolve(change.arguments["path"], must_exist=False)
    _check_secret(target, allow=settings.file_allow_secrets, verb="写入")
    if workspace_root() != change.root or target != change.target:
        raise FileAccessError("工作区或目标路径已变化；请重新生成差异并批准。")
    if _identity(target) != change.identity:
        raise FileAccessError("文件已变化，旧批准失效；请重新读取并生成差异。")
    if change.before is not None and target.read_bytes() != change.before:
        raise FileAccessError("文件内容已变化，旧批准失效；请重新生成差异。")


def guarded_change(tool_name: str, params: BaseModel) -> ToolResult | None:
    change = _prepared.get()
    if change is None:
        if context := current_run_context():
            if context.approvals is not None and context.approvals.blocked_reason:
                return ToolResult.failure(
                    f"本轮禁止后续文件修改，未写入：{context.approvals.blocked_reason}"
                )
        settings = get_settings().agent
        if not settings.file_write_enabled:
            return ToolResult.failure("写权限已关闭；请在工作区设置显式开启。未写入。")
        if settings.file_approval_required:
            return ToolResult.failure("尚未取得差异批准；请使用聊天流式界面确认，未写入。")
        return None
    if change.tool_name != tool_name or change.arguments != params.model_dump():
        return ToolResult.failure("批准与当前工具参数不匹配，未写入；请重新生成预览。")
    temporary: Path | None = None
    try:
        _recheck(change)
        change.target.parent.mkdir(parents=True, exist_ok=True)
        if change.before is None:
            # 排他新建：外部程序在核验后新建同名文件也不会被覆盖。
            _recheck(change)
            with change.target.open("xb") as stream:
                stream.write(change.after)
        else:
            # 同目录临时文件 + replace，避免截成半份；仅保留 mode，不承诺保留 ACL。
            with tempfile.NamedTemporaryFile(dir=change.target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(change.after)
            os.chmod(temporary, change.target.stat().st_mode)
            _recheck(change)
            os.replace(temporary, change.target)
            temporary = None
        path = _rel(change.target)
        message = f"已批准并应用{ {'create': '新建', 'overwrite': '覆盖', 'edit': '编辑'}[change.operation] }：{path}（{len(change.after)} 字节）"
        change.broker.update(change.approval_id, "applied", message)
        return ToolResult.success(message)
    except FileAccessError as exc:
        change.broker.update(change.approval_id, "conflict", f"未写入：{exc}")
        return ToolResult.failure(f"未写入：{exc}")
    except OSError as exc:
        message = f"文件写入失败：{exc}；请核对文件权限与占用情况后重新生成预览。"
        change.broker.update(change.approval_id, "failed", message)
        return ToolResult.failure(message)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
