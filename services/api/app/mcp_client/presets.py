"""发行版内置程序的路径由资源目录解析，用户只需指定允许目录。"""

import json
import shutil
from pathlib import Path

from app.core.config import DATA_ROOT, DESKTOP, RESOURCE_ROOT

FILESYSTEM_TOOLS = [
    "read_text_file",
    "read_multiple_files",
    "write_file",
    "edit_file",
    "create_directory",
    "list_directory",
    "directory_tree",
    "move_file",
    "search_files",
    "get_file_info",
    "list_allowed_directories",
]
COMMANDER_TOOLS = [
    "start_process",
    "read_process_output",
    "interact_with_process",
    "force_terminate",
    "list_sessions",
]


def resolve_preset(payload):
    value = dict(payload)
    preset = value.get("preset")
    workspace = value.pop("workspace", "")
    if preset not in {"filesystem", "desktop_commander"}:
        return value
    root = Path(workspace or value.get("cwd", ""))
    if not root.is_absolute() or not root.is_dir():
        raise ValueError(
            "允许目录：请填写已存在的绝对目录，例如 D:\\我的文件；不会自动扫描或创建私人目录。"
        )
    resources = RESOURCE_ROOT / "mcp" if DESKTOP else DATA_ROOT / "mcp-services"
    node = RESOURCE_ROOT / "node" / "node.exe" if DESKTOP else Path(shutil.which("node") or "")
    package = (
        "@modelcontextprotocol/server-filesystem"
        if preset == "filesystem"
        else "@wonderwhy-er/desktop-commander"
    )
    script = resources / "node_modules" / package / "dist" / "index.js"
    if not node.is_file() or not script.is_file():
        raise ValueError(
            "本地服务程序未安装。请使用包含本地 MCP 的 Windows 发行包；源码安装方式见使用说明。"
        )
    value.update(command=str(node), cwd=str(root), transport="stdio", url="")
    if preset == "filesystem":
        value.update(
            args=[str(script), str(root)],
            selected_tools=value.get("selected_tools") or FILESYSTEM_TOOLS,
        )
    else:
        home = DATA_ROOT / "desktop-commander"
        config = home / ".claude-server-commander" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        previous = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
        previous.update(
            allowedDirectories=[str(root)],
            telemetryEnabled=False,
            pendingWelcomeOnboarding=False,
            welcomeOnboardingEligible=False,
        )
        # 与锁定服务的默认阻止列表一致，绝不因隔离配置而清空保护。
        previous.setdefault(
            "blockedCommands",
            [
                "mkfs",
                "format",
                "mount",
                "umount",
                "fdisk",
                "dd",
                "parted",
                "diskpart",
                "sudo",
                "su",
                "passwd",
                "adduser",
                "useradd",
                "usermod",
                "groupadd",
                "chsh",
                "visudo",
                "shutdown",
                "reboot",
                "halt",
                "poweroff",
                "init",
                "iptables",
                "firewall",
                "netsh",
                "sfc",
                "bcdedit",
                "reg",
                "net",
                "sc",
                "runas",
                "cipher",
                "takeown",
            ],
        )
        config.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
        value.update(
            args=[str(script), "--no-onboarding"],
            selected_tools=value.get("selected_tools") or COMMANDER_TOOLS,
            env={**value.get("env", {}), "USERPROFILE": str(home), "HOME": str(home)},
        )
    return value
