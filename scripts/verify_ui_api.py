"""端到端验证 P6 的文件浏览：配置 → 列目录 → 预览 → 安全边界。

【为什么这个脚本值得存在，而不是只跑单元测试】

单元测试（tests/test_file_tools.py）测的是**工具**，
tests/test_settings_api.py 测的是**设置接口** —— 两者都是绿的，
但前端与后端之间仍然可能对不上字段名。

这个脚本测的正是那个缝：**HTTP 上的真实字段名、真实状态码、真实交互顺序**
（先配置 → 再浏览）。

它确实抓到过一次：我在前端把字段写成了 camelCase（`workspaceRoot`），
而后端期望 `workspace_root`。typecheck 是绿的（TS 只保证各自自洽），
单元测试也是绿的（两边各自 mock），只有真连起来才暴露 —— 422。

⚠ 字段名一律用**后端的**（snake_case）：项目约定线上格式就是后端字段名。

⚠ **这个脚本会改写你的 `.env`** —— 它测的正是"配置能不能保存并生效"，
不写就没法测。最后一步会把工作区配置清空，但会留下一个
`AGENT_WORKSPACE_ROOT=` 空行，那是"已清空"的正常状态、不是残留。
介意的话跑之前先备份：`copy .env %TEMP%\\env.bak`，跑完再 copy 回来。

用法（需要后端在 8000 端口运行）：
    .\\scripts\\dev.ps1 serve        # 另一个终端
    python scripts/verify_ui_api.py
"""

import sys

import httpx

BASE = "http://127.0.0.1:8000"
ok = True


def check(condition: bool, label: str, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    if not condition:
        ok = False


with httpx.Client(base_url=BASE, timeout=60.0) as c:
    print("=== 1. 配置工作区 ===")
    r = c.put("/api/settings", json={"workspace_root": "."})
    detail = "" if r.status_code == 200 else r.text[:140]
    check(r.status_code == 200, f"PUT /api/settings → {r.status_code}", detail)
    if r.status_code == 200:
        root = r.json()["agent"]["workspace_root"]
        check(bool(root), f"工作区已设置为 {root}")

    print("\n=== 2. 列目录 ===")
    r = c.get("/api/files/list", params={"path": "."})
    check(r.status_code == 200, f"列根目录 → {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        print(f"        path={d['path']}  条目 {len(d['entries'])} 个  can_go_up={d['can_go_up']}")
        for e in d["entries"][:8]:
            tag = "[D]" if e["is_dir"] else "[F]"
            print(f"        {tag} {e['name']}")
        check(not d["can_go_up"], "根目录的 can_go_up 应为 False")
        names = {e["name"] for e in d["entries"]}
        check(".env" not in names, "隐藏文件默认不出现（.env 不在列表里）")
        check(bool(names & {"apps", "services", "docs"}), "能看到真实目录")

    print("\n=== 3. 预览文件 ===")
    r = c.get("/api/files/content", params={"path": "README.md"})
    check(r.status_code == 200, f"读 README.md → {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        print(f"        size={d['size']}  truncated={d['truncated']}  is_binary={d['is_binary']}")
        print("        内容开头：" + d["content"][:80].replace("\n", " / "))
        check(d["size"] > 0 and not d["is_binary"], "正常文本文件可预览")

    print("\n=== 4. 安全边界（这是重点）===")
    r = c.get("/api/files/content", params={"path": "../../Windows/win.ini"})
    check(r.status_code in (403, 404), f"路径穿越被拦住 → {r.status_code}")
    if r.status_code != 200:
        print(f"        {str(r.json().get('detail', ''))[:80]}")

    r = c.get("/api/files/content", params={"path": ".env"})
    check(r.status_code == 403, f"读取 .env 被拒绝 → {r.status_code}")
    if r.status_code != 200:
        text = str(r.json().get("detail", ""))
        print(f"        {text[:80]}")
        check("敏感文件" in text, "给出的理由是「敏感文件」而不是含糊的失败")

    r = c.get("/api/files/list", params={"path": "../../"})
    check(r.status_code in (403, 404), f"列界外目录被拦住 → {r.status_code}")

    print("\n=== 5. 隐藏文件开关 ===")
    r = c.get("/api/files/list", params={"path": ".", "include_hidden": "true"})
    if r.status_code == 200:
        names = {e["name"] for e in r.json()["entries"]}
        check(".env" in names, "开启后列表里能看到隐藏文件")
        # 关键：列表可见 ≠ 内容可读 —— 这是两层不同的防护，
        # 只做一层的话，用户点一下就能看到本该拒绝的内容
        r2 = c.get("/api/files/content", params={"path": ".env"})
        check(r2.status_code == 403, "即便在列表里可见，读取内容仍被拒绝")

    print("\n=== 6. 目录选择能力（这一条是新加的）===")
    r = c.get("/api/files/picker")
    check(r.status_code == 200, f"GET /api/files/picker → {r.status_code}")
    if r.status_code == 200:
        cap = r.json()
        print(f"        能力 = {cap['kind']}")
        print(f"        理由 = {cap['detail']}")
        check(cap["kind"] in ("native", "browse"), "能力值是前端认识的那两种之一")
        check(bool(cap["detail"]), "给出了人话理由（用户看不到对话框时最需要的就是它）")

    # ⚠ 这里**刻意不调用** POST /api/files/pick：它会在你的屏幕上弹出真实对话框
    # 并一直挂着等人操作。验证脚本可以自动做很多事，但"替用户点一个模态窗口"不行。
    # 那个功能由 scripts/verify_picker_dialog.py 验证（从进程外枚举窗口 + 发 WM_CLOSE）。
    r = c.post("/api/files/pick")
    check(r.status_code == 422, f"不带 JSON 体调用 /pick 被拒绝 → {r.status_code}（CSRF 护栏）")

    print("\n=== 7. 恢复原状（不留副作用）===")
    r = c.put("/api/settings", json={"workspace_root": ""})
    check(r.status_code == 200 and r.json()["agent"]["workspace_root"] == "", "已清空工作区配置")

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
