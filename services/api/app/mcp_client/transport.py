"""SDK Transport：有界行协议，Windows 在运行应用代码前绑定 Job。"""

import asyncio
import os
import signal
import subprocess
from contextlib import asynccontextmanager

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage
from pydantic import TypeAdapter

from app.desktop.process import external_popen
from app.tools.terminal import terminal_environment
from app.tools.terminal_windows import WindowsJob

MAX_MESSAGE = 1024 * 1024
MESSAGE_ADAPTER = TypeAdapter(JSONRPCMessage)


@asynccontextmanager
async def stdio_transport(server):
    job = None
    options = (
        {"start_new_session": True}
        if os.name != "nt"
        else {"creationflags": subprocess.CREATE_NO_WINDOW | 0x00000004}
    )
    # Popen 只创建管道和进程，不等待服务器就绪；绑定 Job 前没有 await。
    process = external_popen(  # synchronous: bind suspended process before any await
        [server.command, *server.args],
        cwd=server.cwd,
        env=terminal_environment() | server.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        **options,
    )
    try:
        if os.name == "nt":
            job = WindowsJob()
            job.attach_and_resume(int(process._handle), process.pid)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        if job:
            job.close()
        raise
    incoming, reader = anyio.create_memory_object_stream(0)
    writer, outgoing = anyio.create_memory_object_stream(0)

    async def read():
        try:
            async with incoming:
                while line := await asyncio.to_thread(process.stdout.readline, MAX_MESSAGE + 1):
                    if len(line) > MAX_MESSAGE:
                        await incoming.send(ValueError("MCP 消息超过 1 MiB；请减少服务返回内容"))
                        return
                    try:
                        message = SessionMessage(MESSAGE_ADAPTER.validate_json(line))
                    except ValueError:
                        await incoming.send(
                            ValueError("MCP stdout 不是合法协议消息；请让服务把日志写到 stderr")
                        )
                        return
                    await incoming.send(message)
        except (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass

    def send_bytes(data):
        process.stdin.write(data)
        process.stdin.flush()

    async def write():
        try:
            async with outgoing:
                async for value in outgoing:
                    data = (
                        value.message.model_dump_json(by_alias=True, exclude_unset=True) + "\n"
                    ).encode()
                    if len(data) > MAX_MESSAGE:
                        raise ValueError("MCP 请求超过 1 MiB")
                    await asyncio.to_thread(send_bytes, data)
        except (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
            await incoming.aclose()

    tasks = [asyncio.create_task(read()), asyncio.create_task(write())]
    try:
        yield reader, writer
    finally:
        with anyio.CancelScope(shield=True):
            try:
                if job:
                    try:
                        await asyncio.to_thread(job.terminate)
                    finally:
                        job.close()
                elif os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    process.kill()
                await asyncio.to_thread(process.wait, 5)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for stream in (incoming, reader, writer, outgoing):
                    await stream.aclose()
                process.stdin.close()
                process.stdout.close()
