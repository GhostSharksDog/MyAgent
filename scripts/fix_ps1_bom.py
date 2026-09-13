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
    scripts_dir = root
    problems = 0

    # ---------- .ps1：必须有 UTF-8 BOM ----------
    ps1_files = sorted(p for p in scripts_dir.rglob("*.ps1") if ".venv" not in p.parts)
    if not ps1_files:
        print("没有找到 .ps1 文件")

    fixed = 0
    for p in ps1_files:
        raw = p.read_bytes()
        had_bom = raw.startswith(BOM)
        if not had_bom:
            p.write_bytes(BOM + raw)
            fixed += 1

        errs = ps_parse_errors(p)
        status = "OK " if not errs else "ERR"
        print(f"[{status}] {p.relative_to(root.parent)}  BOM={'有' if had_bom else '已补'}")
        for e in errs[:5]:
            print(f"       {e}")
        if errs:
            problems += 1

    # ---------- .cmd / .bat：必须纯 ASCII ----------
    #
    # 【为什么这条规则是必要的，而不是洁癖】
    # cmd.exe 和 PowerShell 5.1 一样，按系统 ANSI 代码页解码脚本文件。
    # 一个 UTF-8 编码、带中文注释的 .cmd 会被解成乱码，
    # 而乱码片段会被 cmd.exe **当成命令去执行** ——
    # 实测输出是几百行
    #     'xx），' is not recognized as an internal or external command
    # 然后脚本卡死。
    #
    # 更关键的是：`fix-bom.cmd` 是"dev.ps1 已经坏掉时唯一的修复入口"，
    # **它自己不能有和它要修复的问题同源的脆弱性**。
    # 所以规则写成"必须纯 ASCII"，而不是"给它也加 BOM"——
    # cmd.exe 对 UTF-8 BOM 的支持在各版本 Windows 上并不一致，
    # 而 ASCII 在任何代码页下解码结果都相同。
    cmd_files = sorted(
        p for p in scripts_dir.rglob("*") if p.suffix.lower() in {".cmd", ".bat"}
    )
    for p in cmd_files:
        raw = p.read_bytes()
        try:
            raw.decode("ascii")
            print(f"[OK ] {p.relative_to(root.parent)}  纯 ASCII")
        except UnicodeDecodeError as exc:
            problems += 1
            print(
                f"[ERR] {p.relative_to(root.parent)}  含非 ASCII 字符（{exc.reason}，"
                f"偏移 {exc.start}）—— cmd.exe 会按 ANSI 解码成乱码并当作命令执行"
            )

    print(f"\n共 {len(ps1_files)} 个 .ps1、{len(cmd_files)} 个 .cmd/.bat；补 BOM {fixed} 个")
    return 1 if problems else 0


if __name__ == "__main__":
    # 临时文件不参与
    sys.exit(main())
