"""从环境变量生成本地 .env 配置文件。

【为什么需要这个脚本】
手工复制 .env.example 再粘贴密钥容易出错，而且 Windows 上还有个隐蔽的坑：
PowerShell 5.1 的 `Get-Content` 默认按系统代码页（中文系统是 GBK）解码文件，
一旦用它处理含中文的 UTF-8 文件再回写，中文就变成乱码，密钥也可能被搞坏。

本脚本全程使用 Python 的显式 UTF-8 读写，不经过 shell 的文本处理。

用法::

    # 从环境变量读取（推荐，密钥不会出现在命令历史里）
    set LLM_API_KEY=sk-xxxx
    python scripts/bootstrap_env.py

    # 已存在 .env 时会被拒绝覆盖，除非显式 --force
    python scripts/bootstrap_env.py --force

【Windows 控制台提示】
中文系统的控制台默认编码是 GBK，直接 print 中文可能抛 UnicodeEncodeError。
本脚本在启动时就重设 stdout 为 UTF-8，避免这个问题。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"

# 环境变量查找顺序：优先项目自己的命名，再兼容各家 SDK 的通用命名
KEY_SOURCES = ("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


def _fail(msg: str) -> int:
    print(f"[x] {msg}", file=sys.stderr)
    return 1


def main() -> int:
    # 中文 Windows 的控制台默认是 GBK，不重设会在打印中文时崩溃
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="生成 Legacy 的 .env 配置")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 .env")
    parser.add_argument("--base-url", default=None, help="覆盖 LLM_BASE_URL")
    parser.add_argument("--model", default=None, help="覆盖 LLM_MODEL")
    args = parser.parse_args()

    if not EXAMPLE.exists():
        return _fail(f"找不到模板文件：{EXAMPLE}")
    if TARGET.exists() and not args.force:
        return _fail(f"{TARGET.name} 已存在。如需重建请加 --force（会覆盖现有内容）")

    api_key = ""
    for name in KEY_SOURCES:
        if value := os.environ.get(name, "").strip():
            api_key = value
            print(f"[+] 从环境变量 {name} 读取到密钥（{len(value)} 字符）")
            break
    if not api_key:
        return _fail(
            f"未找到密钥。请先设置环境变量之一：{', '.join(KEY_SOURCES)}\n"
            f"    PowerShell:  $env:LLM_API_KEY = 'sk-xxxx'"
        )

    if not api_key.startswith("sk-"):
        print(f"[!] 警告：密钥不以 'sk-' 开头，请确认没有粘贴错误（前缀：{api_key[:4]!r}）")

    # 全程显式 UTF-8：绝不让 shell 的默认编码插手
    template = EXAMPLE.read_text(encoding="utf-8")

    content, n_key = re.subn(r"^LLM_API_KEY=.*$", f"LLM_API_KEY={api_key}", template, flags=re.M)
    if n_key != 1:
        return _fail(f"模板中 LLM_API_KEY 行匹配到 {n_key} 处，预期 1 处，模板可能已被改动")
    if args.base_url:
        content = re.sub(r"^LLM_BASE_URL=.*$", f"LLM_BASE_URL={args.base_url}", content, flags=re.M)
    if args.model:
        content = re.sub(r"^LLM_MODEL=.*$", f"LLM_MODEL={args.model}", content, flags=re.M)

    # newline="\n" 保证跨平台一致；encoding="utf-8"（非 utf-8-sig）不写 BOM ——
    # BOM 会让 dotenv 把第一行解析成一个诡异的键名
    TARGET.write_text(content, encoding="utf-8", newline="\n")

    print(f"[+] 已生成 {TARGET}")
    print(f"    文件大小 {TARGET.stat().st_size} 字节")

    # 自检：真的能被解析出来才算成功
    try:
        sys.path.insert(0, str(ROOT / "services" / "api"))
        from app.core.config import get_settings  # noqa: PLC0415

        get_settings.cache_clear()
        s = get_settings()
        ok = s.llm.is_configured
        print(f"[{'OK' if ok else 'x'}] 配置自检：is_configured={ok}  model={s.llm.model}  url={s.llm.base_url}")
        return 0 if ok else 1
    except ImportError:
        print("[i] 跳过自检（依赖尚未安装）")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
