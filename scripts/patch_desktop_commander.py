"""只允许对已审核版本/字节应用补丁；不匹配立即停止发行构建。"""

import hashlib
import json
from pathlib import Path

PACKAGE = "@wonderwhy-er/desktop-commander"
ORIGINAL_HASHES = {
    "dist/index.js": "a4145198dc75cd34e7c7452c2054b4cc0d29e199ae1459961a17e4da25a13500",
    "dist/utils/feature-flags.js": "537db6e7988d191dcafce8d5182b32bfb53962497dfcf0bcbb49ffbbcc4b6000",
}


def patch(root: Path):
    package = root / "node_modules" / PACKAGE
    if (
        json.loads((package / "package.json").read_text(encoding="utf-8"))["version"]
        != "0.2.52"
    ):
        raise ValueError("Desktop Commander 版本与补丁不匹配，构建停止")
    changes = {
        "dist/index.js": [
            (
                "            ensureChromeAvailable();",
                "            // Legacy: Chrome is never downloaded at startup.",
            )
        ],
        "dist/utils/feature-flags.js": [],
    }
    evidence = []
    for name, replacements in changes.items():
        path = package / name
        original = path.read_bytes()
        if hashlib.sha256(original).hexdigest() != ORIGINAL_HASHES[name]:
            raise ValueError(f"上游文件字节与已审核版本不一致：{name}，构建停止")
        text = original.decode("utf-8")
        if name.endswith("feature-flags.js"):
            start = text.index("    async initialize() {")
            end = text.index("    /**\n     * Get a flag value", start)
            block = text[start:end]
            if (
                block.count("this.fetchFlags()") != 2
                or "this.refreshInterval.unref();" not in block
            ):
                raise ValueError("远程功能配置初始化已变化，补丁拒绝应用")
            replacements = [
                (
                    block,
                    "    async initialize() {\n        // Legacy: no vendor network or background refresh.\n        await this.loadFromCache();\n        if (this.resolveFreshFetch) this.resolveFreshFetch();\n    }\n",
                )
            ]
        for before, after in replacements:
            if text.count(before) != 1:
                raise ValueError(f"补丁位置不唯一：{name}，构建停止")
            text = text.replace(before, after)
        result = text.encode("utf-8")
        path.write_bytes(result)
        evidence.append(
            {
                "file": name,
                "before_sha256": hashlib.sha256(original).hexdigest(),
                "after_sha256": hashlib.sha256(result).hexdigest(),
            }
        )
    (root / "legacy-patches.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
    )
    return evidence


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    print(json.dumps(patch(parser.parse_args().root), indent=2))
