"""显式导入旧 JSON；不读取配置，不扫描目录，保留原文件并创建备份。"""

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "api"))
from app.agent.memory import Fact
from app.agent.sqlite_memory import SqliteLongTermMemory


def migrate(source: Path, destination: Path, max_facts: int = 200):
    source, destination = source.resolve(), destination.resolve()
    if source == destination:
        raise ValueError("原文件与目标数据库不能相同")
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("旧记忆必须是 JSON 数组")
    facts = [Fact.model_validate(item) for item in raw]
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    backup = source.with_name(source.name + ".backup-" + stamp)
    shutil.copy2(source, backup)
    if destination.exists():
        with (
            sqlite3.connect(destination) as old,
            sqlite3.connect(str(destination) + ".backup-" + stamp) as copied,
        ):
            old.backup(copied)
    memory = SqliteLongTermMemory(destination, max_facts)
    added = 0

    def merge(existing):
        nonlocal added
        known = {f.text.casefold() for f in existing}
        for fact in facts:
            if fact.text.casefold() not in known:
                existing.append(fact)
                known.add(fact.text.casefold())
                added += 1
        existing.sort(key=lambda fact: (fact.ts, fact.id))
        return bool(added)

    memory._change(merge)
    return {
        "source_backup": str(backup),
        "imported": added,
        "retained": len(memory),
        "destination": str(destination),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--max-facts", type=int, default=200)
    args = parser.parse_args()
    if args.max_facts < 1:
        parser.error("max-facts 必须为正")
    print(
        json.dumps(
            migrate(args.source, args.destination, args.max_facts),
            ensure_ascii=False,
            indent=2,
        )
    )
