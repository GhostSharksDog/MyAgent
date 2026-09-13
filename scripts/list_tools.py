"""列出当前注册的所有工具及其参数结构。

用途：调试提示词时快速确认"模型看到的工具描述"到底长什么样。
工具描述写得含糊是 Agent 表现差的首要原因，所以需要能一眼看到它。

用法::

    python scripts/list_tools.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))

# 中文 Windows 控制台默认 GBK，不重设会在打印中文时抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from app.tools.builtin import build_default_registry  # noqa: E402


def main() -> int:
    registry = build_default_registry()
    schemas = registry.schemas()

    print(f"已注册 {len(schemas)} 个工具\n")
    print("=" * 72)

    for schema in schemas:
        fn = schema["function"]
        params = fn["parameters"]
        required = set(params.get("required", []))

        print(f"\n▸ {fn['name']}")
        print(f"  描述: {fn['description']}")
        print(f"  超时: {registry.get(fn['name']).timeout}s")  # type: ignore[union-attr]

        props = params.get("properties", {})
        if not props:
            print("  参数: （无）")
        else:
            print("  参数:")
            for name, meta in props.items():
                tag = "必填" if name in required else "可选"
                desc = str(meta.get("description", ""))[:70]
                default = meta.get("default")
                default_text = f" 默认={default!r}" if default is not None else ""
                print(f"    - {name} [{tag}]{default_text}")
                print(f"        {desc}")

        print("-" * 72)

    print("\n提示：这些描述就是模型看到的全部信息。模型调错工具，")
    print("      第一件事应该是回来读这里，而不是急着换模型。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
