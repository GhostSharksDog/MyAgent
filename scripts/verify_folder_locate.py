"""验证"用系统对话框选文件夹"这条链路：按名字 + 结构反查绝对路径。

模拟浏览器在用户用系统对话框选了 `MyAgent` 之后能给出的全部信息：
  · 文件夹名 —— "MyAgent"
  · 若干相对路径 —— "apps/web/src/App.tsx" 等

（绝对路径是拿不到的，浏览器会剥掉。这正是要反查的原因。）
"""

import sys

import httpx

BASE = "http://127.0.0.1:8000"
ok = True


def check(cond: bool, label: str, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


with httpx.Client(base_url=BASE, timeout=120.0) as c:
    print("=== 1. 名字 + 结构 → 唯一定位 ===")
    r = c.post(
        "/api/files/locate",
        json={
            "name": "MyAgent",
            "samples": ["apps/web/src/App.tsx", "services/api/app/main.py"],
        },
    )
    check(r.status_code == 200, f"POST /api/files/locate → {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        print(f"        扫描 {d['scanned']} 个目录，找到 {len(d['candidates'])} 个候选")
        for cand in d["candidates"][:5]:
            print(f"          {cand['path']}  (吻合 {cand['matched']})")
        check(len(d["candidates"]) >= 1, "找到了 MyAgent")
        if d["candidates"]:
            # 结构吻合的必须排第一 —— 这是"用结构确认名字"的判据
            check(
                d["candidates"][0]["matched"] >= 1,
                "排在首位的是**结构吻合**的那一个",
                f"matched={d['candidates'][0]['matched']}",
            )
            check(
                "MyAgent" in d["candidates"][0]["path"],
                "路径里含 MyAgent",
            )

    print("\n=== 2. 只有名字、无结构：仍应找到，但可能多个 ===")
    r = c.post("/api/files/locate", json={"name": "MyAgent", "samples": []})
    if r.status_code == 200:
        d = r.json()
        check(len(d["candidates"]) >= 1, f"无 samples 时也找到了 {len(d['candidates'])} 个")

    print("\n=== 3. 不存在的名字：给可操作的提示，而不是空 ===")
    r = c.post("/api/files/locate", json={"name": "这个文件夹肯定不存在xyz123", "samples": []})
    check(r.status_code == 200, f"不存在的名字 → {r.status_code}（不是 500）")
    if r.status_code == 200:
        d = r.json()
        check(d["candidates"] == [], "候选为空")
        check(bool(d["hint"]), "给了提示", d["hint"][:60])

    print("\n=== 4. 同名但结构不符：不应被误选 ===")
    # 故意给一个不存在的相对路径 —— 有 samples 时只收真的对得上的
    r = c.post(
        "/api/files/locate",
        json={"name": "MyAgent", "samples": ["不存在的目录/不存在.txt"]},
    )
    if r.status_code == 200:
        d = r.json()
        check(
            all(cand["matched"] > 0 for cand in d["candidates"]),
            f"返回的候选都真的吻合（{len(d['candidates'])} 个）",
        )

    print("\n=== 5. 空名字被拒 ===")
    r = c.post("/api/files/locate", json={"name": "", "samples": []})
    check(r.status_code == 422, f"空名字 → {r.status_code}")

print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
