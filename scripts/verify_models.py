"""端到端演练：多模型流程 + 恢复原状。

【为什么用 httpx 而不是 PowerShell 的 Invoke-RestMethod】
第一次跑这个演练时 POST 一直报 405、中文标签还变成了 `??-????` ——
两个都是 PowerShell 5.1 的问题：它的 `-Body` 按 ANSI 编码发出去
（中文变乱码），而那个辅助函数的构造方式又让请求出了岔子。
接口本身是好的（`/openapi.json` 里 `/api/models` 明确有 GET,POST）。

这提醒一件事：**用不称手的客户端验证服务端，会把工具的问题当成服务的问题。**
所以这个演练用和前端同一族的客户端（httpx）来做。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
BACKUP = Path(__file__).resolve().parent.parent / ".env.verify-backup"

ok = True


def check(cond: bool, label: str, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        ok = False


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


print("=" * 70)
print("多模型端到端演练")
print("=" * 70)

shutil.copy2(ENV_FILE, BACKUP)
before = digest(ENV_FILE)
print(f"\n已备份 .env（sha256 {before[:12]}…）")

with httpx.Client(base_url=BASE, timeout=30.0) as c:
    # 记下演练前的**生效配置**。它才是"有没有被搞坏"的判据 —— 见最后一段。
    original = c.get("/api/settings").json()["llm"]
    print(f"演练前生效配置：model={original['model']} base_url={original['base_url']}")

    print("\n=== 1. 把当前配置存为模型（界面抄不出密钥，必须由服务端代劳）===")
    r = c.post("/api/models/import-current", json={"label": "原始配置（演练）"})
    check(
        r.status_code == 200,
        f"POST /api/models/import-current → {r.status_code}",
        r.text[:80],
    )
    body = r.json()
    check(len(body["models"]) == 1, "清单里多了一条")
    check(body["current_unsaved"] is False, "不再提示「当前配置未保存」")
    check(
        body["models"][0]["label"] == "原始配置（演练）",
        "中文标签原样存下来了",
        body["models"][0]["label"],
    )
    check(
        body["models"][0]["active"] is True,
        "它被标为「当前使用」（比对地址+模型名+密钥算出来的）",
    )
    original_id = body["models"][0]["id"]

    print("\n=== 2. 添加一个供应商（用户要的「配好后显示多了一个模型」）===")
    r = c.post(
        "/api/models",
        json={
            "label": "本地 Ollama",
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen2.5:7b",
            "api_key": "local-key",
            "temperature": 0.7,
        },
    )
    check(r.status_code == 200, f"POST /api/models → {r.status_code}", r.text[:80])
    body = r.json()
    check(len(body["models"]) == 2, "清单从 1 条变成 2 条")
    new = next(m for m in body["models"] if m["label"] == "本地 Ollama")
    check(new["active"] is False, "新加的还没被激活（要再点一次切换）")
    check(new["temperature"] == 0.7, "温度按模型存下来了")
    check("local-key" not in json.dumps(body, ensure_ascii=False), "响应里没有密钥原文")
    # 只断言"掩码不含原文"，不断言掩码长什么样：短密钥会被**整体**打码
    # （`local-key` → `*********`），这是对的 —— 9 个字符露出头尾就等于泄漏。
    # 断言格式会把一个正确的行为判成失败。
    check(
        new["api_key_masked"] != "local-key" and "local" not in new["api_key_masked"],
        "密钥只以掩码形式返回",
        new["api_key_masked"],
    )

    print("\n=== 3. 切换（写 .env + 重建 Agent 全栈）===")
    r = c.post(f"/api/models/{new['id']}/activate")
    check(r.status_code == 200, f"POST activate → {r.status_code}", r.text[:80])
    active = next(m for m in r.json()["models"] if m["active"])
    check(active["label"] == "本地 Ollama", "激活的是新模型", active["label"])

    env_text = ENV_FILE.read_text(encoding="utf-8")
    check("LLM_MODEL=qwen2.5:7b" in env_text, ".env 里的模型名被改了")
    check("LLM_BASE_URL=http://127.0.0.1:11434/v1" in env_text, ".env 里的地址被改了")
    check("LLM_TEMPERATURE=0.7" in env_text, ".env 里的温度被改了")

    health = c.get("/healthz").json()
    check(
        health["model"] == "qwen2.5:7b",
        "**服务的自报模型跟着变了**（无需重启）",
        health["model"],
    )

    print("\n=== 4. 切换回来 ===")
    r = c.post(f"/api/models/{original_id}/activate")
    check(r.status_code == 200, f"切回 → {r.status_code}")
    check(c.get("/healthz").json()["model"] != "qwen2.5:7b", "切回后模型也变回去了")

    print("\n=== 5. 清理演练条目 ===")
    for m in c.get("/api/models").json()["models"]:
        c.delete(f"/api/models/{m['id']}")
    check(len(c.get("/api/models").json()["models"]) == 0, "清单已清空")

    print("\n=== 6. 是否回到演练前的状态 ===")
    now = c.get("/api/settings").json()["llm"]
    check(now["model"] == original["model"], "生效模型名一致", now["model"])
    check(now["base_url"] == original["base_url"], "生效地址一致", now["base_url"])
    check(
        c.get("/healthz").json()["model"] == original["model"],
        "服务自报的模型也回到原值",
    )

# ---- .env 文件本身 ----
#
# 【为什么判据是"生效配置"（上面）而不是"文件哈希"（这里）】
# `_write_env` 会**规范化整个文件**：换行统一成 LF、把原本省略的键显式写出来
# （`LLM_TEMPERATURE` 这类之前不在文件里的，激活一次就落进去了）。
# 所以"切出去再切回来"能让生效值完全一致，而文件字节并不相同 ——
# 拿哈希当判据会把一个正确的行为判成失败（第一次跑就是这样）。
#
# 至于"别留下痕迹"：这是演练脚本，所以最后仍然从备份还原文件，
# 让 .env 逐字节回到原样。
after = digest(ENV_FILE)
print("\n=== .env 文件 ===")
if after == before:
    print("  逐字节一致（本次写入恰好没引入格式变化）")
else:
    print(
        f"  有格式差异（{before[:12]} → {after[:12]}）—— 写入器会规范化文件，已知行为"
    )
    shutil.copy2(BACKUP, ENV_FILE)
    check(digest(ENV_FILE) == before, "已从备份还原为逐字节一致")

BACKUP.unlink(missing_ok=True)
print("\n" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
