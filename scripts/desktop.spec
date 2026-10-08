# Whitelist only build resources. Never collect repository .env or data/.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
assets = root / "data" / "desktop-assets"
a = Analysis(
    [str(root / "scripts" / "desktop_entry.py")],
    pathex=[str(root / "services" / "api")],
    binaries=[],
    datas=[(str(assets / "web"), "web"), (str(assets / "seed"), "seed")],
    hiddenimports=collect_submodules("app") + ["aiosqlite", "pystray._win32", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets.websockets_impl", "uvicorn.lifespan.on", "sqlalchemy.dialects.sqlite.aiosqlite"],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "mypy", "tiktoken", "uvloop"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Legacy",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, icon=str(assets / "Legacy.ico"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Legacy")
