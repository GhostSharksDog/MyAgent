"""验证"打开文件夹"这条链路：目录选择器 → 选一个 → 文件栏能列出来。"""

import sys

import httpx

BASE = "http://127.0.0.1:8000"
ok = True


def check(cond: bool, label: str, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


with httpx.Client(base_url=BASE, timeout=60.0) as c:
    print("=== 1. 界面用的是新产物吗 ===")
    r = c.get("/")
    check(r.status_code == 200, f"/ → {r.status_code}")
    check("Legacy" in r.text, "标题已改成 Legacy", r.text[r.text.find("<title>") :][:60])
    check("index-ByM0jtSc.js" in r.text, "引用的是最新构建的前端")

    print("\n=== 2. 目录选择器（可以从工作区之外开始）===")
    r = c.get("/api/files/browse", params={"path": ""})
    check(r.status_code == 200, f"留空 → 返回起点 → {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        roots = d["roots"] or d["entries"]
        print(f"        起点 {len(roots)} 个：{[x['name'] for x in roots][:6]}")
        check(bool(roots), "给了可选的起点（Windows 是盘符）")

    r = c.get("/api/files/browse", params={"path": "D:/WXP"})
    check(r.status_code == 200, f"浏览 D:/WXP → {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        print(f"        path={d['path']}  子目录 {len(d['entries'])} 个  parent={d['parent']}")
        for e in d["entries"][:6]:
            print(f"          {e['name']}  ({e['child_count']} 项)")
        check(all("child_count" in e for e in d["entries"]), "只返回目录 + 子项数量")
        # 关键：不能返回文件名
        names = [e["name"] for e in d["entries"]]
        check("resume.md" not in names, "选择器**不返回文件名**（只有目录）")

    print("\n=== 3. 相对路径与越界路径的处理 ===")
    r = c.get("/api/files/browse", params={"path": "not/absolute"})
    check(r.status_code == 400, f"相对路径被拒 → {r.status_code}", str(r.json().get("detail"))[:50])
    r = c.get("/api/files/browse", params={"path": "Z:/definitely/not/here"})
    check(r.status_code in (403, 404), f"不存在的路径 → {r.status_code}")

    print("\n=== 4. 选一个文件夹后，工作区生效 ===")
    r = c.put("/api/settings", json={"workspace_root": "D:/WXP/简历/MyAgent"})
    check(r.status_code == 200, f"设置工作区 → {r.status_code}")
    r = c.get("/api/files/workspace")
    check(r.json()["configured"] is True, "工作区已生效")

    r = c.get("/api/files/list", params={"path": "."})
    check(r.status_code == 200, f"列工作区 → {r.status_code}")
    if r.status_code == 200:
        names = {e["name"] for e in r.json()["entries"]}
        check("apps" in names and "services" in names, f"看到真实目录：{sorted(names)[:5]}")

    print("\n=== 5. 换一个工作区（边栏的\"换一个\"按钮走这条）===")
    r = c.put("/api/settings", json={"workspace_root": "D:/WXP/简历/MyAgent/services"})
    check(r.status_code == 200, "切到子目录成功")
    r = c.get("/api/files/list", params={"path": "."})
    if r.status_code == 200:
        names = {e["name"] for e in r.json()["entries"]}
        check("api" in names, f"新工作区的内容：{sorted(names)}")
        check("apps" not in names, "旧工作区的目录不再可见（说明真的切换了）")

    print("\n=== 6. 关闭工作区 ===")
    r = c.put("/api/settings", json={"workspace_root": ""})
    check(r.status_code == 200, "清空成功")
    r = c.get("/api/files/list", params={"path": "."})
    check(r.status_code == 409, f"关闭后文件接口拒绝服务 → {r.status_code}")

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
