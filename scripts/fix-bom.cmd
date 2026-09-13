@echo off
REM ============================================================
REM Fix UTF-8 BOM for scripts\*.ps1  (ASCII-ONLY ON PURPOSE)
REM
REM WHY THIS FILE IS PURE ASCII AND WHY IT MUST STAY THAT WAY:
REM
REM This script exists to solve a chicken-and-egg problem.
REM
REM   This machine runs PowerShell 5.1. It only decodes a .ps1 as
REM   UTF-8 when the file starts with a UTF-8 BOM. Without the BOM
REM   it decodes as the system ANSI code page (GBK here).
REM
REM   GBK decoding of UTF-8 Chinese has a byte-swallowing effect:
REM   a 3-byte Chinese char is read as one 2-byte GBK char plus a
REM   lone trailing byte in 0x80-0xBF. That trailing byte is a valid
REM   GBK lead byte, so it swallows the NEXT byte -- often a newline
REM   or a quote. Line numbers shift, quotes break, the parse tree
REM   collapses.
REM
REM   When that happens, scripts\dev.ps1 itself cannot be parsed, so
REM   the auto-fix step INSIDE it never gets a chance to run.
REM
REM So the recovery entry point must not depend on PowerShell, and
REM must not depend on the file being fixed. cmd.exe satisfies both.
REM
REM But cmd.exe has the SAME decoding behavior, and a first version
REM of this file with Chinese comments in UTF-8 exploded into
REM "is not recognized as an internal or external command".
REM
REM Lesson: the recovery tool must not share the fragility it
REM recovers from. Keep this file ASCII-only.
REM
REM Usage (double-click in the repo root, or run from any shell):
REM     scripts\fix-bom.cmd
REM ============================================================

setlocal
cd /d "%~dp0.."

set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
    echo [x] Cannot find %PY%
    echo     Run: scripts\dev.ps1 setup   ^(or create the venv first^)
    exit /b 1
)

"%PY%" scripts\fix_ps1_bom.py
if errorlevel 1 (
    echo [x] Fix failed.
    exit /b 1
)

echo.
echo [OK] Done. scripts\dev.ps1 should now run normally.
endlocal
exit /b 0
