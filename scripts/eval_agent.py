"""通用 Agent 任务评测：默认合成 LLM + 真实内核；--records 只离线评分已有记录。

不支持联网执行，不加载 .env 或私人文件；真实模型成绩需另行采集和授权额度。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api"))
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from app.evaluation.offline import FixtureResponse, run_offline
from app.evaluation.tasks import RunBundle, TaskSuite, grade_bundle, render_report

SEED = ROOT / "services" / "api" / "seed" / "agent_eval"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=SEED / "tasks.json")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--offline", action="store_true", help="默认：注入合成模型，不联网"
    )
    source.add_argument("--records", type=Path, help="离线评分已采集的 RunBundle JSON")
    parser.add_argument("--fixtures", type=Path, default=SEED / "fixtures.json")
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["react", "plan", "multi"],
        default=["react", "plan", "multi"],
    )
    parser.add_argument(
        "--task", action="append", help="只运行指定任务，可重复；报告明确显示选择范围"
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "agent-eval")
    args = parser.parse_args(argv)
    if args.records and (
        args.task or args.repetitions != 1 or args.modes != ["react", "plan", "multi"]
    ):
        parser.error(
            "--records 使用记录中的选择范围，不接受 --task／--modes／--repetitions"
        )
    if not 1 <= args.repetitions <= 20:
        parser.error("--repetitions 范围为 1..20")
    try:
        suite = TaskSuite.load(args.suite)
        if args.records:
            bundle = RunBundle.model_validate_json(
                args.records.read_text(encoding="utf-8")
            )
        else:
            fixtures = TypeAdapter(dict[str, FixtureResponse]).validate_json(
                args.fixtures.read_text(encoding="utf-8")
            )
            bundle = asyncio.run(
                run_offline(
                    suite,
                    fixtures,
                    modes=args.modes,
                    task_ids=args.task,
                    repetitions=args.repetitions,
                )
            )
        report = grade_bundle(suite, bundle)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    try:
        write_json(args.output_dir / "records.json", bundle.model_dump(mode="json"))
        write_json(args.output_dir / "report.json", report)
        (args.output_dir / "report.md").write_text(
            render_report(report), encoding="utf-8", newline="\n"
        )
    except (ValueError, OSError) as exc:
        parser.error(
            f"输出报告失败：{exc}；请用 --output-dir 指定可写目录，并确认记录为有效 JSON"
        )
    print(
        f"source={bundle.source}, model={bundle.model}, tasks={len(bundle.task_ids)}, trials={report['observed_trials']}/{report['expected_trials']}"
    )
    print(report["note"])
    for row in report["groups"]:
        if row["category"] == "all":
            print(
                f"{row['mode']}: checks {row['passed']}/{row['expected']}, tools={row['tool_calls']}, calls={row['llm_calls']}, known_tokens={row['known_total_tokens']}, usage_complete={row['usage_complete']}"
            )
    failures = [
        {
            "task_id": t["task_id"],
            "mode": t["mode"],
            "attempt": t["attempt"],
            "failed_checks": [c for c in t["checks"] if not c["passed"]],
        }
        for t in report["trials"]
        if not t["passed"]
    ]
    if failures or report["missing"]:
        print(
            json.dumps(
                {"failures": failures, "missing": report["missing"]},
                ensure_ascii=False,
                indent=2,
            )
        )
    print(
        f"报告：{args.output_dir / 'report.json'}；原始事件：{args.output_dir / 'records.json'}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
