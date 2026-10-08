# Legacy

一个运行在本机的通用 AI Agent。配置兼容 OpenAI 接口的模型服务后，即可聊天、拆解任务，并按需接入文件、终端和联网工具。

Legacy 提供简约的聊天界面，也能展示计划、工具调用与执行结果。对话和已确认的记忆保存在本机。

## 主要功能

- **三种模式**：ReAct 适合连续对话；Plan 先规划再执行；Supervisor 协调多个专家处理任务。
- **MCP 工具**：支持本地 stdio 和远程 Streamable HTTP，可配置 Tavily、Exa、Filesystem、Desktop Commander 或自定义服务。
- **文件与终端**：明确选择工作区后使用；写文件和执行命令需要相应权限与确认。
- **会话与记忆**：桌面版使用 SQLite 保存对话；长期记忆由用户明确保存或批准后写入，可查看、修改和删除。
- **知识检索**：对显式配置的本地资料进行检索，并展示来源。
- **执行控制**：支持停止任务、查看执行过程和用量，以及上下文与预算状态提示。

## Windows 使用

适用于 Windows 10/11 x64。下载可用的 `Legacy-Windows-x64.zip`，完整解压后双击 `Legacy.exe`。无需另外安装 Python、Node 或 Redis。

首次打开填写 **供应商、API 地址、模型名、API Key**，点击「保存并开始使用」。保存配置不会自动发送模型测试请求；聊天及摘要可能产生模型服务费用。

文件、终端和联网均为可选能力，不影响基础聊天：

1. 连续追问时选择 **ReAct**，并新建或选择一个会话。
2. 使用文件功能时，通过左下角入口选择工作区。
3. 联网时打开「设置 → MCP」，添加 Tavily 并填写 API Key，或配置其他服务；确认连接状态及可用工具数量。
4. 需要终端时，可开启内置终端权限，或连接 Desktop Commander。工具执行时会请求确认。
5. 在「记忆与存储」中管理需要长期保留的内容。

Plan 和 Supervisor 每轮处理独立任务，不使用过去的会话消息；未选择会话的请求也不保留连续对话历史。

关闭浏览器不会退出程序。退出时，在系统托盘的 Legacy 图标菜单中选择「退出」。

## 数据与权限

桌面版数据位于 `%LOCALAPPDATA%\Legacy`，包括配置、会话、记忆和日志。更新程序时先退出旧版，再完整解压新版；程序目录与用户数据目录分开。

模型服务会收到当前任务需要的消息和工具结果。「保存在本机」指本机持久化存储，并不意味着模型推理离线进行。

文件写入和内置终端默认关闭。终端命令拥有当前服务账户的权限，工作区目录不是终端沙箱；外部 MCP 的访问范围由对应服务配置决定。只连接可信服务，并在批准前检查调用内容。

## 从源码启动

需要 Python 3.12、Node 24 和 pnpm 10。在项目根目录的 PowerShell 中执行：

```powershell
python -m venv .venv
Remove-Item Env:PIP_TARGET -ErrorAction SilentlyContinue
& .venv\Scripts\python.exe -m pip install -r services\api\requirements.lock -e "./services/api[dev]" -c services\api\requirements.lock
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在 `.env` 中配置模型服务；已有配置时保留原文件。随后构建界面并启动：

```powershell
Set-Location apps\web
pnpm install --frozen-lockfile
pnpm build
Set-Location ..\..
powershell -File scripts\dev.ps1 serve -NoReload
```

打开 [本机界面](http://127.0.0.1:8000)。源码运行使用项目配置，存储默认值与桌面发行版不同；需要持久保存会话时显式配置 `SESSION_BACKEND=sql`。

## 技术栈

Python / FastAPI、React / TypeScript / Vite、SQLite、官方 MCP Python SDK。Agent 内核与 OpenAI 兼容客户端为手写实现，三种编排共用工具、取消和运行预算机制。

Windows 桌面包在本机 Windows 11 环境验收；Windows 10、全新设备和 Windows Sandbox 尚未实测。
