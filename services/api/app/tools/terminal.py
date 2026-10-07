"""显式授权、逐命令确认的本机终端。cwd 是起点，不是文件或网络沙箱。"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.approvals import ApprovalBroker, ApprovalUnavailable
from app.agent.runtime import RunBudgetExceeded, current_run_context
from app.core.config import get_settings
from app.llm.types import ToolCall
from app.tools.base import Tool, ToolResult, _truncate
from app.tools.files import FileAccessError, _resolve, workspace_root
from app.tools.terminal_process import available, run_command, shell_name


class TerminalParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(
        min_length=1, max_length=16000, description="完整非交互命令，不含密钥；逐条确认后执行"
    )
    cwd: str = Field(
        default=".", min_length=1, max_length=4096, description="起始目录，相对工作区；不是沙箱"
    )

    @field_validator("command", "cwd")
    @classmethod
    def printable_input(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("不能是空白或包含 NUL；请核对完整命令和目录")
        return value


def terminal_environment() -> dict[str, str]:
    """仅传 OS 基础环境；不默认传模型/访问密钥、代理、PIP_TARGET 或 PYTHONPATH。

    这只避免直接继承服务配置；自由命令仍可读磁盘与用户配置，不能宣称凭据隔离。
    """
    names = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "ALLUSERSPROFILE",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "OS",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in names}


def _directory_identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    if not path.is_dir():
        raise FileAccessError("命令起始目录不是目录；请在工作区中选择现有目录。")
    return stat.st_dev, stat.st_ino


def _directory_snapshot(cwd_argument: str) -> tuple[Path, Path, tuple[int, int], tuple[int, int]]:
    # UNC 或挂载盘的 resolve/stat 可能等待 OS；只读准备放到线程，不能阻塞整轮取消。
    root = workspace_root()
    if root is None:
        raise FileAccessError("请先在工作区设置指定现有目录（AGENT_WORKSPACE_ROOT），未执行。")
    cwd = _resolve(cwd_argument)
    return root, cwd, _directory_identity(root), _directory_identity(cwd)


@dataclass(frozen=True)
class ApprovedCommand:
    command: str
    cwd_argument: str
    root_setting: str
    root: Path
    cwd: Path
    root_identity: tuple[int, int]
    cwd_identity: tuple[int, int]
    timeout: float
    shell: str
    broker: ApprovalBroker
    approval_id: str


class TerminalTool(Tool):
    name = "run_terminal"
    description = (
        "执行本机非交互终端命令：Windows 为 PowerShell，POSIX 为 sh。"
        "需独立开启终端权限并配置工作区，每条完整命令、目录与时限必须由用户批准。"
        "cwd 不是沙箱，命令拥有服务账户权限并可能写文件或联网；不能绕过用户授权。"
        "返回退出码、stdout/stderr、超时与截断信息；失败或停止不回滚已完成的修改。"
    )
    params_model = TerminalParams
    serial = True

    async def run(self, params: BaseModel) -> ToolResult:
        # 只有注册表在锁内调用 execute_prepared 才能使用批准；直接调用不执行。
        return ToolResult.failure("命令尚未取得有效批准；请使用聊天流式界面逐条确认，未执行。")

    async def prepare_execution(self, call: ToolCall) -> ApprovedCommand | ToolResult:
        context = current_run_context()
        if context is None or context.approvals is None:
            return ToolResult.failure("当前入口没有命令审批通道；请使用聊天流式界面确认，未执行。")
        context.check()
        broker: ApprovalBroker = context.approvals
        try:
            if broker.blocked_reason or not broker.active:
                raise ApprovalUnavailable(broker.blocked_reason or "本轮已停止；请重新发起任务。")
            settings = get_settings().agent
            if not settings.terminal_enabled:
                raise FileAccessError("终端权限已关闭；请在工作区设置显式开启，未执行。")
            if not available():
                raise FileAccessError(
                    "当前平台没有可用命令运行器；请使用 Windows PowerShell 或 POSIX sh。"
                )
            params = TerminalParams.model_validate(call.arguments)
            root_setting = settings.workspace_root
            root, cwd, root_identity, cwd_identity = await asyncio.to_thread(
                _directory_snapshot, params.cwd
            )
            context.check()
            if (
                not get_settings().agent.terminal_enabled
                or get_settings().agent.workspace_root != root_setting
            ):
                raise FileAccessError("权限或工作区在准备期间已变化；请核对设置后重新发起任务。")
            timeout = settings.terminal_timeout
            shell = shell_name()
            approval_id, approved = await broker.request(
                {
                    "kind": "command",
                    "command": params.command,
                    "cwd": str(cwd),
                    "shell": shell,
                    "timeout_seconds": timeout,
                },
                timeout=settings.terminal_approval_timeout,
            )
            if not approved:
                return ToolResult.failure(f"命令未执行：{broker.blocked_reason or '用户拒绝命令'}")
            context.check()
            return ApprovedCommand(
                params.command,
                params.cwd,
                root_setting,
                root,
                cwd,
                root_identity,
                cwd_identity,
                timeout,
                shell,
                broker,
                approval_id,
            )
        except (FileAccessError, ApprovalUnavailable, OSError, ValidationError) as exc:
            return ToolResult.failure(f"命令未执行：{exc}")

    def _recheck(self, call: ToolCall, prepared: ApprovedCommand) -> None:
        settings = get_settings().agent
        if not settings.terminal_enabled:
            raise FileAccessError("终端权限已关闭，旧批准失效；请重新确认权限。")
        if not prepared.broker.active or prepared.broker.blocked_reason:
            raise FileAccessError(prepared.broker.blocked_reason or "运行已结束，命令批准已失效。")
        item = prepared.broker.items.get(prepared.approval_id)
        if item is None or item.view["status"] != "approved" or item.view.get("started") is True:
            raise FileAccessError("此命令没有有效批准或已执行；请重新发起任务。")
        params = TerminalParams.model_validate(call.arguments)
        if call.name != self.name or (params.command, params.cwd) != (
            prepared.command,
            prepared.cwd_argument,
        ):
            raise FileAccessError("命令与批准的参数不匹配；请重新确认完整命令。")
        if settings.workspace_root != prepared.root_setting:
            raise FileAccessError("工作区或起始目录已变化，旧批准失效；请重新确认。")
        if (
            settings.terminal_timeout != prepared.timeout
            or shell_name() != prepared.shell
            or not available()
        ):
            raise FileAccessError("命令时限或运行器已变化，旧批准失效；请重新确认。")
        if context := current_run_context():
            if context.approvals is not prepared.broker:
                raise FileAccessError("此命令批准不属于当前请求；请在本轮重新确认。")
            context.check()
        else:
            raise FileAccessError("本轮上下文已失效；请重新发起命令任务。")

    def _recheck_directories(self, prepared: ApprovedCommand) -> None:
        if _directory_snapshot(prepared.cwd_argument) != (
            prepared.root,
            prepared.cwd,
            prepared.root_identity,
            prepared.cwd_identity,
        ):
            raise FileAccessError("工作区或起始目录已变化，旧批准失效；请重新确认。")

    def _invalidate_unstarted(self, prepared: ApprovedCommand, status: str, message: str) -> None:
        context = current_run_context()
        item = prepared.broker.items.get(prepared.approval_id)
        if (
            context is not None
            and context.approvals is prepared.broker
            and item is not None
            and item.view["status"] == "approved"
            and item.view.get("started") is not True
        ):
            prepared.broker.update(prepared.approval_id, status, message)

    async def execute_prepared(self, call: ToolCall, preparation: Any) -> ToolResult:
        if not isinstance(preparation, ApprovedCommand):
            return ToolResult.failure("命令没有有效批准；请使用聊天流式界面确认，未执行。")
        broker, approval_id = preparation.broker, preparation.approval_id
        try:
            self._recheck(call, preparation)
            await asyncio.to_thread(self._recheck_directories, preparation)
            # 等只读核验时权限、审批或预算仍可变化，启动前再次在事件循环核验。
            self._recheck(call, preparation)
        except (FileAccessError, OSError, ValidationError) as exc:
            self._invalidate_unstarted(preparation, "conflict", f"命令未执行：{exc}")
            return ToolResult.failure(f"命令未执行：{exc}")
        except RunBudgetExceeded:
            self._invalidate_unstarted(preparation, "expired", "本轮预算已耗尽，命令未执行。")
            raise
        except asyncio.CancelledError:
            self._invalidate_unstarted(preparation, "cancelled", "本轮已停止，命令未启动。")
            raise
        broker.update(
            approval_id, "approved", "命令正在执行；停止或超时不回滚已完成的修改。", started=True
        )
        from app.agent.operations import record_operation

        record_operation("running")
        try:
            result = await run_command(
                preparation.command,
                preparation.cwd,
                timeout=preparation.timeout,
                env=terminal_environment(),
            )
        except asyncio.CancelledError as exc:
            cleanup_failed = any("清理失败" in note for note in getattr(exc, "__notes__", ()))
            broker.update(
                approval_id,
                "cancelled",
                "已请求停止，但进程清理失败；请检查服务日志与系统进程，可能仍在运行。已完成的修改不回滚。"
                if cleanup_failed
                else "运行已停止，已清理普通子进程；已完成的修改不回滚。",
            )
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            message = f"命令启动或清理失败：{exc}；请核对运行器与系统权限后重新确认，不自动重试。"
            broker.update(approval_id, "failed", message)
            return ToolResult.failure(message)
        record_operation(
            "unknown" if result.timed_out else "succeeded" if result.exit_code == 0 else "failed",
            exit_code=result.exit_code,
        )
        # 分别保留 stdout/stderr 的头尾，避免通用裁剪将其中一路完全丢掉。
        stdout, out_cut = _truncate(result.stdout, 3000)
        stderr, err_cut = _truncate(result.stderr, 3000)
        truncated = result.truncated or out_cut or err_cut
        content = json.dumps(
            {
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": truncated,
                "encoding_errors": getattr(result, "encoding_errors", False),
                "duration_ms": int(result.duration_ms),
            },
            ensure_ascii=False,
        )
        if result.timed_out:
            message = "命令执行超时，已清理普通子进程；可能已有部分修改，不自动重试。"
        elif result.exit_code != 0:
            message = f"命令退出码 {result.exit_code}；请核对输出，可能已有部分修改，不自动重试。"
        else:
            message = "命令执行完成（退出码 0）" + ("；输出已截断。" if truncated else "。")
        content, final_cut = _truncate(message + "\n" + content)
        truncated |= final_cut
        ok = result.exit_code == 0 and not result.timed_out
        broker.update(approval_id, "applied" if ok else "failed", message)
        # 失败 observation 取 error 而非 content，必须同样含 stdout/stderr，不能丢掉诊断。
        return ToolResult(
            ok=ok,
            content=content,
            error=None if ok else content,
            duration_ms=int(result.duration_ms),
            truncated=truncated,
        )


def build_terminal_tools() -> list[Tool]:
    if not get_settings().agent.terminal_enabled or workspace_root() is None or not available():
        return []
    return [TerminalTool()]
