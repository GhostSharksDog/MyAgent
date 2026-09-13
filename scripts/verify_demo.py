"""验证离线回放：在不依赖任何网络调用的前提下消费一次完整事件流。

【这个脚本要证明的命题】
"断网也能演示"不能靠嘴说。这里的验证方式是**把 LLM 的地址指向一个不可达端口**，
然后看流式端点是否仍然产出完整的事件序列 —— 包括工具调用、token 流、最终答案、用量。

如果回放路径里还有任何一处偷偷调用了模型，这个测试就会超时或报错。

退出码 0 = 离线演示成立。
"""

from __future__ import annotations

import json
import sys
import time

import httpx

API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
QUESTION = "岗位库里有哪些大模型应用方向的岗位？它们分别要求什么技术？请逐条列出并标注出处。"

failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        failures.append(label)
    return ok


def main() -> int:
    print("=" * 66)
    print("离线回放验证（LLM 地址已被指向不可达端口）")
    print("=" * 66)

    with httpx.Client(timeout=60.0) as client:
        print("\n[1] 健康检查")
        health = client.get(f"{API}/healthz").json()
        replay = str(health.get("demo_replay", ""))
        check(replay != "off", f"回放已就绪：{replay[:60]}")

        print("\n[2] 消费流式端点")
        types: dict[str, int] = {}
        final = ""
        usage: dict = {}
        tool_names: list[str] = []
        started = time.perf_counter()

        with client.stream(
            "POST",
            f"{API}/api/chat/stream",
            json={"message": QUESTION, "mode": "react"},
        ) as resp:
            check(resp.status_code == 200, f"HTTP {resp.status_code}")
            data_lines: list[str] = []
            for raw in resp.iter_lines():
                line = raw.rstrip("\r")
                if line == "":
                    if data_lines:
                        try:
                            ev = json.loads("\n".join(data_lines))
                        except json.JSONDecodeError:
                            data_lines = []
                            continue
                        t = ev.get("type", "?")
                        types[t] = types.get(t, 0) + 1
                        if t == "final":
                            final = ev.get("content", "")
                        elif t == "done" and ev.get("usage"):
                            usage = ev["usage"]
                        elif t == "tool_call":
                            tool_names.append(str(ev.get("tool_name") or ev.get("content", "")))
                    data_lines = []
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())

        elapsed = time.perf_counter() - started
        print(f"  耗时 {elapsed:.1f}s")
        print(f"  事件类型分布：{ {k: v for k, v in sorted(types.items())} }")

        print("\n[3] 断言事件流的完整性")

        # 这四个是"演示要展示的东西"，缺任何一个演示就不成立
        check(types.get("step", 0) >= 1, f"有推理步骤事件（{types.get('step', 0)} 个）")
        check(types.get("tool_call", 0) >= 1, f"有工具调用事件（{types.get('tool_call', 0)} 个）")
        check(types.get("tool_result", 0) >= 1, f"有工具结果事件（{types.get('tool_result', 0)} 个）")
        check(types.get("token", 0) >= 50, f"有 token 流（{types.get('token', 0)} 个）—— '逐字出现'靠它")
        check(types.get("final", 0) == 1, "有一个最终答案事件")
        check(types.get("done", 0) == 1, "有一个结束事件")

        print("\n[4] 断言内容质量")
        check(bool(final), f"最终答案非空（{len(final)} 字符）")
        check("出处" in final or "[1]" in final, "答案带出处标注 —— 这是 RAG 可核对的前提")
        check(bool(usage), f"有 token 用量：{usage} —— 成本展示靠它")
        check(not types.get("error"), f"没有 error 事件（{types.get('error', 0)} 个）")

        print("\n[5] 工具调用可见性")
        check(bool(tool_names), f"记录到工具名：{tool_names}")

    print("\n" + "=" * 66)
    if failures:
        print(f"验证失败 {len(failures)} 项：")
        for f in failures:
            print(f"  · {f}")
        print("=" * 66)
        return 1
    print("离线回放成立：不依赖任何网络调用即可产出完整事件流。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
