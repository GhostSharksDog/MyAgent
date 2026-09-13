"""给所有 .ps1 补上 UTF-8 BOM，并用 PS 解析器验证。

【为什么必须这么做】
本机的 PowerShell 是 **5.1**。它在读取 `.ps1` 时，**只有见到 UTF-8 BOM
才会按 UTF-8 解码**；没有 BOM 就按系统 ANSI 代码页（这里 GBK）解码。

GBK 解码 UTF-8 中文时会产生一个隐藏的"字节吞并"效应：
UTF-8 的中文字是 3 字节（E4-E9 | 80-BF | 80-BF）。
GBK 把前两字节当成一个汉字，剩下的第三字节（0x80-0xBF）单独成字 ——
而 0x80-0xBF 在 GBK 里是**合法的首字节**，于是它会继续吞掉**后面那个字节**。
被吞掉的往往是换行（0x0A）或引号（0x22）。

后果是：行号错位、字符串引号断开、语法树崩掉 ——
而报错位置指向的行看起来完全正常（它可能只是一句注释），
排查方向完全被带偏。

【这就是"没有 BOM 的 UTF-8 脚本在本机能跑"这件事有多脆】
HEAD 版本的 dev.ps1 恰好没触发字节对齐问题，所以看起来一直是好的。
但只要改一个中文字、加一行注释，字节配对整体位移，就会突然解析失败 ——
而且失败信息和真正的原因毫无关系。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

BOM = b"\xef\xbb\xbf"


def ps_parse_errors(path: pathlib.Path) -> list[str]:
    """用 PowerShell 自己的解析器检查，而不是靠跑一遍脚本去猜。"""
    script = (
        f"$errs = $null; "
        f"$null = [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path.resolve()}', [ref]$null, [ref]$errs); "
        f"$errs | ForEach-Object {{ $_.Extent.StartLineNumber.ToString() + ':' + $_.Message }}"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def main() -> int:
    root = pathlib.Path(__file__).parent
    files = sorted(p for p in root.rglob("*.ps1") if ".venv" not in p.parts)
    if not files:
        print("没有找到 .ps1 文件")
        return 0

    changed = 0
    for p in files:
        raw = p.read_bytes()
        had_bom = raw.startswith(BOM)
        if not had_bom:
            p.write_bytes(BOM + raw)
            changed += 1

        errs = ps_parse_errors(p)
        status = "OK " if not errs else "ERR"
        print(f"[{status}] {p.relative_to(root.parent)}  "
              f"BOM={'有' if had_bom else '已补'}")
        for e in errs[:5]:
            print(f"       {e}")

    print(f"\n共 {len(files)} 个文件，补 BOM {changed} 个")
    return 0


if __name__ == "__main__":
    # 临时文件不参与
    sys.exit(main())
