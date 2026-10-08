"""运行最终 ZIP：隔离数据、中文空格路径、移除开发运行时 PATH、合成模型。"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


class SyntheticModel(BaseHTTPRequestHandler):
    calls: ClassVar[list] = []
    tool = None
    parameters: ClassVar[dict] = {}

    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append(request)
        messages = request["messages"]
        rendered = json.dumps(messages, ensure_ascii=False)
        usage = {"prompt_tokens": 24, "completion_tokens": 8, "total_tokens": 32}
        if request.get("response_format"):
            text = json.dumps(
                {"specialists": ["资料分析员"], "reasoning": "合成选择"}
                if "可选专家" in rendered
                else {
                    "steps": [
                        {"description": "回答公开测试任务", "expected": "公开结论"}
                    ],
                    "reasoning": "合成计划",
                },
                ensure_ascii=False,
            )
        elif "需要压缩的对话" in rendered:
            text = "公开会话历史的合成摘要；保留用户偏好中文。"
        else:
            text = "公开测试回复；偏好已确认。"
        if not request.get("stream"):
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": usage,
                },
                ensure_ascii=False,
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        invoke = self.tool and not any(m["role"] == "tool" for m in messages)
        delta = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "public-call",
                        "type": "function",
                        "function": {
                            "name": self.tool,
                            "arguments": json.dumps(
                                self.parameters, ensure_ascii=False
                            ),
                        },
                    }
                ]
            }
            if invoke
            else {"content": text}
        )
        for chunk in [
            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls" if invoke else "stop",
                    }
                ],
                "usage": usage,
            },
        ]:
            self.wfile.write(
                ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()
            )
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--zip", type=Path, default=ROOT / "data/releases/Legacy-Windows-x64.zip"
    )
    args = parser.parse_args()
    work = ROOT / "data" / "desktop-verification" / ("中文路径 空格 " + uuid4().hex[:8])
    work.mkdir(parents=True)
    with zipfile.ZipFile(args.zip) as archive:
        archive.extractall(work)
    exe = work / "Legacy" / "Legacy.exe"
    data = work / "独立用户数据"
    data.mkdir()
    workspace = work / "允许目录"
    workspace.mkdir()
    (workspace / "public.txt").write_text("LEGACY_PUBLIC_FILE", encoding="utf-8")
    (data / "config.env").write_text(
        "MEMORY_MAX_TURNS=3\nMEMORY_KEEP_RECENT=2\nLLM_MAX_RETRIES=0\nLLM_MAX_TOKENS=512\nMCP_CONNECT_TIMEOUT=60\n",
        encoding="utf-8",
    )
    environment = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            (
                "LLM_",
                "AGENT_",
                "MCP_",
                "MEMORY_",
                "SESSION_",
                "TASK_",
                "RUN_HISTORY_",
                "LEGACY_",
            )
        )
        and k not in {"PROJECT_ROOT", "WEB_DIST", "DATABASE_URL"}
    }
    environment["LEGACY_DATA_DIR"] = str(data)
    environment["PATH"] = (
        r"C:\Windows\System32;C:\Windows;C:\Windows\System32\WindowsPowerShell\v1.0;C:\Windows\System32\Wbem"
    )
    results = []
    started_at = time.monotonic()
    archive_sha256 = hashlib.sha256(args.zip.read_bytes()).hexdigest()

    def check(condition, label):
        results.append({"check": label, "passed": bool(condition)})
        print(("PASS " if condition else "FAIL ") + label, flush=True)
        if not condition:
            raise AssertionError(label)

    client = httpx.Client(trust_env=False, timeout=70)
    fake = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticModel)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    process = None
    state = None
    conflict = socket.socket()
    try:
        try:
            conflict.bind(("127.0.0.1", 8000))
            conflict.listen(1)
        except OSError:
            conflict.close()

        def launch():
            proc = subprocess.Popen(
                [str(exe)], env=environment, creationflags=0x08000000
            )
            deadline = time.monotonic() + 70
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    info = json.loads(
                        (data / "instance.json").read_text(encoding="utf-8")
                    )
                    if (
                        client.get(info["url"] + "_desktop/instance")
                        .json()
                        .get("instance")
                        == info["instance"]
                    ):
                        return proc, info
                except (OSError, ValueError, httpx.HTTPError):
                    pass
                time.sleep(0.2)
            raise RuntimeError(f"冻结程序启动失败，查看 {data / 'logs/legacy.log'}")

        process, state = launch()
        url = state["url"]
        check(":8000/" not in url, "端口占用时选择可用回环端口")
        check(client.get(url).status_code == 200, "ZIP 前端可访问")
        check(client.get(url + "api/setup").json()["required"], "首次无配置需要引导")
        status = client.get(url + "api/storage").json()
        check(
            status["sessions"]["backend"] == "sqlite"
            and status["sessions"]["ttl_seconds"] == 0,
            "默认 SQLite 会话不过期",
        )
        check(
            status["memory"]["active_backend"] == "sql"
            and status["runs"]["active_backend"] == "sql",
            "记忆与运行摘要实际持久化",
        )
        check(
            client.get(url + "api/mcp").json()["servers"] == [],
            "无个人服务与开发机配置",
        )
        check(
            any(
                t["name"] == "get_mcp_status"
                for t in client.get(url + "api/tools").json()
            ),
            "MCP 尚未配置时也能查询实际状态",
        )
        duplicate = subprocess.Popen(
            [str(exe)], env=environment, creationflags=0x08000000
        )
        check(duplicate.wait(timeout=40) == 0, "重复启动复用原实例并退出")
        check(
            json.loads((data / "instance.json").read_text())["instance"]
            == state["instance"],
            "重复启动没有替换实例",
        )
        response = client.post(
            url + "api/setup",
            json={
                "provider": "custom",
                "base_url": f"http://127.0.0.1:{fake.server_port}/v1",
                "model": "synthetic-public",
                "api_key": "synthetic-key",
            },
        )
        check(
            response.status_code == 200 and not response.json()["required"],
            "保存模型不调用模型",
        )
        check(not SyntheticModel.calls, "首次保存零模型请求")
        response = client.post(
            url + "api/memory", json={"text": "用户偏好中文", "tags": ["语言"]}
        )
        check(response.status_code == 200, "明确保存长期记忆")
        fact = response.json()
        session = client.post(url + "api/sessions", json={}).json()["id"]
        for index in range(5):
            response = client.post(
                url + "api/chat",
                json={
                    "message": f"用户偏好中文，公开会话问题 {index}",
                    "session_id": session,
                    "mode": "react",
                },
            )
            check(
                response.status_code == 200
                and response.json()["stopped_reason"] == "finished",
                f"合成 ReAct 第 {index + 1} 轮正常保存",
            )
        detail = client.get(url + "api/sessions/" + session).json()

        def session_meta():
            with sqlite3.connect(data / "legacy.db") as connection:
                return json.loads(
                    connection.execute(
                        "SELECT meta FROM sessions WHERE id=?", (session,)
                    ).fetchone()[0]
                )

        check(
            detail["turn_count"] == 5
            and session_meta()["conversation_summary"]["processed"] > 0,
            "完整历史与增量摘要同时保存",
        )
        for mode in ("plan", "multi"):
            start = len(SyntheticModel.calls)
            response = client.post(
                url + "api/chat",
                json={
                    "message": "用户偏好中文，请回答新的公开任务",
                    "session_id": session,
                    "mode": mode,
                },
            )
            check(
                response.status_code == 200
                and response.json()["stopped_reason"] == "finished",
                mode + " 合成运行完成",
            )
            calls = SyntheticModel.calls[start:]
            requests = json.dumps(calls, ensure_ascii=False)
            recalled = all(
                any(
                    message["role"] == "system"
                    and "用户明确确认的长期偏好" in message.get("content", "")
                    and "用户偏好中文" in message.get("content", "")
                    for message in call["messages"]
                )
                for call in calls
            )
            check(
                bool(calls) and "公开会话问题" not in requests and recalled,
                mode + " 使用确认偏好，排除会话历史",
            )
        response = client.put(
            url + "api/mcp/servers/connect",
            json={
                "preset": "filesystem",
                "name": "公开文件服务",
                "workspace": str(workspace),
                "cwd": str(workspace),
                "selected_tools": ["read_text_file", "list_allowed_directories"],
            },
        )
        check(
            response.status_code == 200
            and response.json()["servers"][0]["status"] == "connected",
            "冻结程序启动随包 Filesystem Node 服务",
        )
        tool = next(
            t
            for t in client.get(url + "api/tools").json()
            if t.get("remote_name") == "read_text_file"
        )
        SyntheticModel.tool = tool["name"]
        SyntheticModel.parameters = {"path": str(workspace / "public.txt")}
        events = []
        with client.stream(
            "POST",
            url + "api/chat/stream",
            json={"message": "读取公开测试文件", "session_id": session},
        ) as stream:
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    events.append(event)
                    if event["type"] == "approval_request":
                        approval = client.post(
                            url
                            + f"api/runs/{event['run_id']}/approvals/{event['approval']['id']}",
                            json={"decision": "approve"},
                        )
                        check(approval.status_code == 200, "MCP 调用实际逐次批准")
        SyntheticModel.tool = None
        check(
            any(
                e["type"] == "tool_result"
                and "LEGACY_PUBLIC_FILE" in e.get("content", "")
                for e in events
            ),
            "MCP 实际读取公开临时文件",
        )
        check(sum(e["type"] == "done" for e in events) == 1, "MCP 结束事件恰好一次")
        response = client.put(
            url + "api/mcp/servers/connect",
            json={
                "preset": "desktop_commander",
                "name": "公开终端服务",
                "workspace": str(workspace),
                "cwd": str(workspace),
                "selected_tools": ["start_process", "read_process_output"],
            },
        )
        check(
            response.status_code == 200
            and response.json()["servers"][-1]["status"] == "connected",
            "冻结程序启动随包 Desktop Commander",
        )

        def execute_tool(name, parameters):
            SyntheticModel.tool, SyntheticModel.parameters = name, parameters
            events = []
            try:
                with client.stream(
                    "POST",
                    url + "api/chat/stream",
                    json={"message": "执行公开验收命令", "session_id": session},
                ) as stream:
                    for line in stream.iter_lines():
                        if line.startswith("data: "):
                            event = json.loads(line[6:])
                            events.append(event)
                            if event["type"] == "approval_request":
                                response = client.post(
                                    url
                                    + f"api/runs/{event['run_id']}/approvals/{event['approval']['id']}",
                                    json={"decision": "approve"},
                                )
                                check(response.status_code == 200, name + " 逐次批准")
                check(
                    sum(e["type"] == "done" for e in events) == 1,
                    name + " 结束事件恰好一次",
                )
                return "\n".join(
                    e.get("content", "") for e in events if e["type"] == "tool_result"
                )
            finally:
                SyntheticModel.tool = None

        mcp_view = client.get(url + "api/mcp").json()
        check(
            all(
                s["available_tool_count"] == 2 and not s["missing_tools"]
                for s in mcp_view["servers"]
            ),
            "已连接服务报告实际暴露的工具数量",
        )
        output = execute_tool("get_mcp_status", {})
        mcp_status = json.loads(output)
        check(
            len(mcp_status["servers"]) == 2
            and all(s["available_tool_count"] == 2 for s in mcp_status["servers"])
            and {t["name"] for s in mcp_status["servers"] for t in s["tools"]}
            == {
                "read_text_file",
                "list_allowed_directories",
                "start_process",
                "read_process_output",
            },
            "冻结程序模型实际收到 MCP 状态工具结果",
        )

        tool = next(
            t
            for t in client.get(url + "api/tools").json()
            if t.get("remote_name") == "start_process"
        )
        output = execute_tool(
            tool["name"],
            {
                "command": "Write-Output LEGACY_PUBLIC_DC",
                "shell": "powershell.exe",
                "timeout_ms": 15000,
            },
        )
        check(
            "LEGACY_PUBLIC_DC" in output and "Error:" not in output,
            "随包 Desktop Commander 实际启动系统 PowerShell",
        )
        response = client.put(
            url + "api/settings",
            json={
                "workspace_root": str(workspace),
                "terminal_enabled": True,
                "terminal_timeout": 30,
            },
        )
        check(response.status_code == 200, "冻结程序显式启用独立终端权限")
        output = execute_tool(
            "run_terminal", {"command": "Write-Output LEGACY_PUBLIC_TERMINAL"}
        )
        check("LEGACY_PUBLIC_TERMINAL" in output, "冻结程序内置终端与 Job 保护实际运行")
        pid_path = workspace / "public-child.pid"
        command = (
            "$child = Start-Process powershell.exe -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 60' -WindowStyle Hidden -PassThru; $child.Id | Set-Content -LiteralPath '"
            + str(pid_path).replace("'", "''")
            + "'; Start-Sleep -Seconds 60"
        )
        SyntheticModel.tool, SyntheticModel.parameters = (
            "run_terminal",
            {"command": command},
        )
        with client.stream(
            "POST",
            url + "api/chat/stream",
            json={"message": "公开取消与子进程回收测试", "session_id": session},
        ) as stream:
            for line in stream.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if event["type"] == "approval_request":
                        response = client.post(
                            url
                            + f"api/runs/{event['run_id']}/approvals/{event['approval']['id']}",
                            json={"decision": "approve"},
                        )
                        check(response.status_code == 200, "取消测试命令先获批准")
                        deadline = time.monotonic() + 20
                        while not pid_path.exists() and time.monotonic() < deadline:
                            time.sleep(0.1)
                        check(pid_path.exists(), "取消前受控子进程已启动")
                        break
        SyntheticModel.tool = None
        child_pid = int(pid_path.read_text().strip())

        def alive(pid):
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            api.OpenProcess.restype = ctypes.c_void_p
            api.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            api.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = api.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            api.GetExitCodeProcess(handle, ctypes.byref(code))
            api.CloseHandle(handle)
            return code.value == 259

        deadline = time.monotonic() + 20
        while alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        check(not alive(child_pid), "冻结程序断流取消回收 Job 子进程")
        response = client.post(
            url + "api/chat",
            json={"message": "取消后立即追问公开任务", "session_id": session},
        )
        check(
            response.status_code == 200
            and response.json()["stopped_reason"] == "finished",
            "取消清理完成后同会话可继续",
        )
        check(
            client.post(
                url + "_desktop/exit", headers={"X-Legacy-Instance": "wrong"}
            ).status_code
            == 403,
            "退出接口拒绝错误令牌",
        )
        client.post(
            url + "_desktop/exit", headers={"X-Legacy-Instance": state["instance"]}
        )
        check(process.wait(timeout=45) == 0, "托盘共用退出路径完成清理")
        process, state = launch()
        url = state["url"]
        check(not client.get(url + "api/setup").json()["required"], "重启保留模型配置")
        check(
            any(
                f["id"] == fact["id"]
                for f in client.get(url + "api/memory").json()["facts"]
            ),
            "重启保留长期记忆",
        )
        detail = client.get(url + "api/sessions/" + session).json()
        check(
            detail["turn_count"] >= 6 and bool(session_meta().get("execution_facts")),
            "重启保留会话与执行事实",
        )
        check(bool(session_meta().get("conversation_summary")), "重启保留摘要位置")
        check(bool(client.get(url + "api/runs").json()["runs"]), "重启保留运行摘要")
        before_clear = len(SyntheticModel.calls)
        response = client.put(url + "api/settings", json={"clear_api_key": True})
        check(
            response.status_code == 200
            and client.get(url + "api/setup").json()["required"],
            "明确清除模型密钥立即生效",
        )
        check(before_clear == len(SyntheticModel.calls), "清除密钥不发送模型请求")
        client.post(
            url + "_desktop/exit", headers={"X-Legacy-Instance": state["instance"]}
        )
        check(process.wait(timeout=45) == 0, "第二次退出完成")
    finally:
        if process and process.poll() is None:
            try:
                if state:
                    client.post(
                        state["url"] + "_desktop/exit",
                        headers={"X-Legacy-Instance": state["instance"]},
                    )
                process.wait(timeout=40)
            except (OSError, httpx.HTTPError, subprocess.TimeoutExpired):
                process.terminate()
                process.wait(timeout=10)
        client.close()
        fake.shutdown()
        fake.server_close()
        conflict.close()
        report = {
            "checks": results,
            "requests": len(SyntheticModel.calls),
            "usage": {
                "total_tokens": len(SyntheticModel.calls) * 32,
                "source": "synthetic",
            },
            "work_directory": str(work),
            "zip_sha256": archive_sha256,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "environment": "Windows x64; PATH contains Windows tools only; isolated data directory",
            "tray_exit_method": "shared tray callback through authenticated local control",
            "new_machine_verified": False,
        }
        report_path = ROOT / "data" / "desktop-verification.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("Report: " + str(report_path), flush=True)


if __name__ == "__main__":
    main()
