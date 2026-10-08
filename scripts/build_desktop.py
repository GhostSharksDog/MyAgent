"""发行构建：使用独立环境和白名单资源，输出 ZIP、校验值与构建清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from patch_desktop_commander import patch

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = "24.20.0"
NODE_SHA256 = "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--node",
        type=Path,
        required=True,
        help="已安装的 Node 24.20.0 node.exe；按官方 SHASUMS 校验",
    )
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("使用独立 Python 3.12 构建环境")
    assets = ROOT / "data" / "desktop-assets"
    assets.mkdir(parents=True, exist_ok=True)
    web = assets / "web"
    if web.exists():
        assert web.resolve().is_relative_to((ROOT / "data").resolve())
        shutil.rmtree(web)
    shutil.copytree(ROOT / "apps" / "web" / "dist", web)
    seed = assets / "seed"
    seed.mkdir(exist_ok=True)
    for name in ("jobs.json", "resume.sample.md"):
        shutil.copy2(ROOT / "services" / "api" / "seed" / name, seed / name)
    node = args.node.resolve()
    version = subprocess.check_output([str(node), "--version"], text=True).strip()
    if version != "v" + NODE_VERSION:
        raise SystemExit("Node 版本不匹配：" + version)
    checksum_path = ROOT / "data" / "node-shasums.txt"
    if not checksum_path.exists():
        urllib.request.urlretrieve(
            f"https://nodejs.org/dist/v{NODE_VERSION}/SHASUMS256.txt", checksum_path
        )
    expected = next(
        line.split()[0]
        for line in checksum_path.read_text().splitlines()
        if line.endswith("win-x64/node.exe")
    )
    actual = hashlib.sha256(node.read_bytes()).hexdigest()
    if actual != expected or actual != NODE_SHA256:
        raise SystemExit("Node 文件不符合官方 SHA-256，构建停止")
    (assets / "node").mkdir(exist_ok=True)
    shutil.copy2(node, assets / "node" / "node.exe")
    node_license = ROOT / "data" / "node-license.txt"
    if not node_license.exists():
        urllib.request.urlretrieve(
            f"https://raw.githubusercontent.com/nodejs/node/v{NODE_VERSION}/LICENSE",
            node_license,
        )
    shutil.copy2(node_license, assets / "node" / "LICENSE")
    mcp = assets / "mcp"
    if mcp.exists():
        # Only this verified build-owned directory may be removed.
        assert mcp.resolve().is_relative_to((ROOT / "data").resolve())
        shutil.rmtree(mcp)
    mcp.mkdir()
    source = ROOT / "scripts" / "desktop-node"
    shutil.copy2(source / "package.json", mcp / "package.json")
    shutil.copy2(source / "package-lock.json", mcp / "package-lock.json")
    npm = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    subprocess.run(
        [str(node), str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=mcp,
        check=True,
    )
    patch_record = patch(mcp)
    licenses = assets / "licenses"
    licenses.mkdir(exist_ok=True)
    python_license = ROOT / "data" / "python-license.txt"
    if not python_license.exists():
        installed = Path(sys.base_prefix) / "LICENSE_PYTHON.txt"
        if installed.is_file():
            shutil.copy2(installed, python_license)
        else:
            urllib.request.urlretrieve(
                f"https://raw.githubusercontent.com/python/cpython/v{sys.version.split()[0]}/LICENSE",
                python_license,
            )
    shutil.copy2(python_license, licenses / "PYTHON-LICENSE.txt")
    import importlib.metadata

    for distribution in importlib.metadata.distributions():
        for file in distribution.files or []:
            name = Path(str(file)).name.lower()
            if (
                name.startswith(("license", "copying", "notice"))
                and ".." not in file.parts
            ):
                location = distribution.locate_file(file)
                if location.is_file():
                    target = licenses / distribution.metadata["Name"] / str(file)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(location, target)
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (256, 256), (247, 246, 242, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 20, 236, 236), radius=56, fill=(85, 113, 83, 255))
    draw.line((90, 72, 90, 177, 174, 177), fill="white", width=24)
    image.save(
        assets / "Legacy.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)],
    )
    if args.prepare_only:
        return
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            "data/desktop-dist",
            "--workpath",
            "data/desktop-build",
            "scripts/desktop.spec",
        ],
        cwd=ROOT,
        check=True,
    )
    output = ROOT / "data" / "desktop-dist" / "Legacy"
    # Node and its modules are foreign runtime resources, not Python libraries.
    # Copy them after COLLECT so PyInstaller never rewrites or classifies their DLLs.
    for name in ("node", "mcp", "licenses"):
        target = output / "_internal" / name
        if target.exists():
            assert target.resolve().is_relative_to(output.resolve())
            shutil.rmtree(target)
        shutil.copytree(assets / name, target)
    # Runtime resources only; ignore user config, seed and local databases.
    manifest = {
        "node": version,
        "node_sha256": actual,
        "pyinstaller": "6.22.3",
        "python": sys.version.split()[0],
        "patches": patch_record,
        "files": sorted(
            p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()
        ),
    }
    (output / "build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    shutil.copy2(ROOT / "docs" / "desktop-quickstart.txt", output / "使用说明.txt")
    release = ROOT / "data" / "releases"
    release.mkdir(exist_ok=True)
    archive = release / "Legacy-Windows-x64.zip"
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as bundle:
        for path in output.rglob("*"):
            if path.is_file():
                bundle.write(path, "Legacy/" + path.relative_to(output).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(
        digest + "  " + archive.name + "\n", encoding="ascii"
    )
    print(f"ZIP: {archive}\nSHA-256: {digest}")


if __name__ == "__main__":
    main()
