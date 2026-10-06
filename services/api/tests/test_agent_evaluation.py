"""任务评测必须识别错误答案、预算退出、未知用量及缺失记录。"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest
from app.evaluation.offline import FixtureResponse, run_offline
from app.evaluation.tasks import (
    AnswerCheck,
    RunBundle,
    TaskCase,
    TaskSuite,
    TrialRecord,
    grade_bundle,
    grade_trial,
    render_report,
)
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
SEED = ROOT / "services/api/seed/agent_eval"


@pytest.fixture
def suite() -> TaskSuite:
    return TaskSuite.load(SEED / "tasks.json")


@pytest.fixture
def fixtures() -> dict[str, FixtureResponse]:
    raw = json.loads((SEED / "fixtures.json").read_text(encoding="utf-8"))
    return {key: FixtureResponse.model_validate(value) for key, value in raw.items()}


def record(answer: dict | str | None = None) -> TrialRecord:
    return TrialRecord(
        task_id="calc-01",
        mode="react",
        elapsed_seconds=0.1,
        llm_calls=2,
        events=[
            {
                "type": "tool_call",
                "tool_name": "calculator",
                "tool_args": {"expression": "(128+64)*3"},
            },
            {"type": "tool_result", "tool_name": "calculator", "tool_ok": True, "content": "576"},
            {
                "type": "final",
                "content": answer
                if isinstance(answer, str)
                else json.dumps(answer or {"value": 576}),
            },
            {
                "type": "done",
                "stopped_reason": "finished",
                "usage_complete": True,
                "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
            },
        ],
    )


def bundle_for(suite: TaskSuite, trials: list[TrialRecord]) -> RunBundle:
    return RunBundle(
        suite_id=suite.id,
        suite_sha256=suite.sha256,
        source="recorded",
        model="test-record",
        modes=["react"],
        task_ids=["calc-01"],
        records=trials,
    )


async def test_public_suite_runs_all_modes_with_actual_tools(suite, fixtures):
    assert Counter(t.category for t in suite.tasks) == {
        "calculation": 8,
        "extraction": 8,
        "planning": 7,
        "comparison": 7,
    }
    assert set(fixtures) == {t.id for t in suite.tasks}
    with (
        patch("httpx.AsyncClient.send", side_effect=AssertionError("不得联网")),
        patch("app.core.config.get_settings", side_effect=AssertionError("不得读取全局配置")),
        patch(
            "app.core.config.AgentSettings.settings_customise_sources",
            side_effect=AssertionError("不得读取环境或 .env"),
        ),
    ):
        runs = await run_offline(suite, fixtures, modes=["react", "plan", "multi"])
    report = grade_bundle(suite, runs)
    assert report["observed_trials"] == report["expected_trials"] == 90
    assert report["passed"] and report["source"] == "synthetic"
    assert report["synthetic"] is True
    assert len(report["groups"]) == 15
    for row in report["groups"]:
        assert row["passed"] == row["expected"]
        assert row["usage_complete"]
        assert row["known_total_tokens"] == row["llm_calls"] * 30
    # Child tool calls really happened even though outer Plan/Multi SSE omits them.
    for mode in ["plan", "multi"]:
        trial = next(t for t in runs.records if t.mode == mode and t.task_id == "extract-01")
        assert not any(e["type"] == "tool_call" for e in trial.events)
        assert len(trial.tool_observations) == 2
        assert all(o.ok and "成员A" in o.content for o in trial.tool_observations)
    assert "合成输出仅检验" in render_report(report)


@pytest.mark.parametrize("value", [575, True, "576", None])
def test_wrong_numeric_answer_is_rejected(suite, value):
    result = grade_trial(suite.tasks[0], record({"value": value}))
    assert not result["passed"]
    assert not next(c for c in result["checks"] if c["name"] == "number:value")["passed"]


@pytest.mark.parametrize(
    "text",
    [
        "声称已完成",
        '```json\n{"value":576}\n```',
        '{"value":575,"value":576}',
        '{"value":NaN}',
        '{"value":Infinity}',
        "[576]",
    ],
)
def test_invalid_json_cannot_pass(suite, text):
    result = grade_trial(suite.tasks[0], record(text))
    assert not result["passed"]
    assert not next(c for c in result["checks"] if c["name"] == "answer_json")["passed"]


@pytest.mark.parametrize("reason", ["token_budget", "timeout", "error", "cancelled", "max_steps"])
def test_partial_correct_answer_is_not_task_success(suite, reason):
    run = record()
    run.events[-1]["stopped_reason"] = reason
    result = grade_trial(suite.tasks[0], run)
    assert not result["passed"] and not result["finished"]
    assert next(c for c in result["checks"] if c["name"] == "number:value")["passed"]


@pytest.mark.parametrize("fault", ["absent", "duplicate", "trailing", "missing_reason"])
def test_terminal_contract_is_checked(suite, fault):
    run = record()
    if fault == "absent":
        run.events.pop()
    elif fault == "duplicate":
        run.events.append(copy.deepcopy(run.events[-1]))
    elif fault == "trailing":
        run.events.append({"type": "token", "content": "late"})
    else:
        run.events[-1].pop("stopped_reason")
    assert not grade_trial(suite.tasks[0], run)["passed"]


@pytest.mark.parametrize("fault", ["missing", "incomplete", "boolean", "inconsistent"])
def test_unknown_usage_is_not_zero_cost(suite, fault):
    run = record()
    if fault == "missing":
        run.events[-1].pop("usage")
    elif fault == "incomplete":
        run.events[-1].pop("usage_complete")
    elif fault == "boolean":
        run.events[-1]["usage"]["total_tokens"] = True
    else:
        run.events[-1]["usage"]["total_tokens"] = 61
    run.llm_calls = None
    report = grade_bundle(suite, bundle_for(suite, [run]))
    row = report["groups"][0]
    assert not row["usage_complete"] and row["llm_calls"] is None
    assert row["known_total_tokens"] == (60 if fault == "incomplete" else None)
    assert "未知" in render_report(report)


@pytest.mark.parametrize("fault", ["failed", "missing_call", "missing_result"])
def test_claiming_tools_were_used_is_insufficient(suite, fault):
    run = record()
    if fault == "failed":
        run.events[1]["tool_ok"] = False
    elif fault == "missing_call":
        run.events.pop(0)
    else:
        run.events.pop(1)
    assert not grade_trial(suite.tasks[0], run)["passed"]


def test_missing_trials_are_not_silently_excluded(suite):
    bundle = bundle_for(suite, [record()])
    bundle.repetitions = 2
    report = grade_bundle(suite, bundle)
    assert not report["passed"]
    assert report["groups"][0]["pass_rate"] == 0.5
    assert report["groups"][0]["finished_rate"] == 0.5
    assert report["missing"] == [{"task_id": "calc-01", "mode": "react", "attempt": 2}]


def test_duplicate_records_and_changed_suite_are_rejected(suite):
    with pytest.raises(ValidationError, match="重复"):
        bundle_for(suite, [record(), record()])
    bundle = bundle_for(suite, [record()])
    changed = suite.model_copy(deep=True)
    changed.tasks[0].checks[0].expected = 577
    with pytest.raises(ValueError, match="摘要"):
        grade_bundle(changed, bundle)


@pytest.mark.parametrize(
    "kind,expected,actual",
    [
        ("unordered", ["A", "B"], ["A", "A"]),
        ("before", ["A", "B"], ["B", "A"]),
        ("sum_lte", 4, [2, 3]),
        ("sum_lte", 4, []),
        ("all_gte", 1, [1, 0]),
        ("all_gte", 1, [True, 1]),
        ("length", 4, [1, 1, 1]),
        ("contains", "sample.md", ["other.md"]),
        ("equals", None, 0),
    ],
)
def test_structured_goals_reject_wrong_outcomes(kind, expected, actual):
    task = TaskCase(
        id="case-01",
        category="planning",
        prompt="public",
        checks=[AnswerCheck(kind=kind, path=["value"], expected=expected)],
    )
    assert not grade_trial(task, record({"value": actual}))["passed"]


def test_planning_accepts_alternative_valid_allocation(suite):
    task = next(t for t in suite.tasks if t.id == "plan-02")
    run = record(
        {
            "steps": ["确认范围", "整理初稿", "核验来源", "发布文档"],
            "days": [2, 1, 1, 1],
            "citations": ["sample.md"],
        }
    )
    run.events[0]["tool_name"] = run.events[1]["tool_name"] = "read_file"
    assert grade_trial(task, run)["passed"]


@pytest.mark.parametrize(
    "path",
    ["../private.md", "C:/private.md", "/private.md", r"..\private.md", ".env", "x/.hidden.md"],
)
def test_task_assets_cannot_escape_temporary_workspace(path):
    with pytest.raises(ValidationError, match="相对路径"):
        TaskCase(
            id="case-01",
            category="extraction",
            prompt="public",
            assets={path: "x"},
            checks=[AnswerCheck(kind="equals", path=["x"], expected=1)],
        )


async def test_bad_fixture_and_failed_child_tool_are_detected(suite, fixtures):
    fixtures["calc-01"].answer = {"value": 575}
    fixtures["extract-01"].calls[0].arguments = {"path": "../private.md"}
    runs = await run_offline(
        suite, fixtures, modes=["react", "plan", "multi"], task_ids=["calc-01", "extract-01"]
    )
    report = grade_bundle(suite, runs)
    assert not report["passed"]
    assert all(not t["passed"] for t in report["trials"])
    assert all(
        not o.ok for r in runs.records if r.task_id == "extract-01" for o in r.tool_observations
    )


async def test_repeat_runs_have_new_budgets_and_no_fixture_fallback(suite, fixtures):
    runs = await run_offline(
        suite, fixtures, modes=["plan", "multi"], task_ids=["calc-01"], repetitions=2
    )
    assert len(runs.records) == 4 and grade_bundle(suite, runs)["passed"]
    assert {r.events[-1]["usage"]["total_tokens"] for r in runs.records} == {180}
    fixtures.pop("calc-01")
    with pytest.raises(ValueError, match="缺失"):
        await run_offline(suite, fixtures, modes=["react"], task_ids=["calc-01"])


def test_cli_produces_regradable_report_and_nonzero_failure(tmp_path, suite):
    first = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/eval_agent.py"),
            "--offline",
            "--task",
            "calc-01",
            "--output-dir",
            str(tmp_path / "first"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert first.returncode == 0, first.stderr
    report = json.loads((tmp_path / "first/report.json").read_text(encoding="utf-8"))
    assert report["observed_trials"] == 3 and report["source"] == "synthetic"
    assert report["task_ids"] == ["calc-01"] and report["suite_task_count"] == 30
    runs = RunBundle.model_validate_json(
        (tmp_path / "first/records.json").read_text(encoding="utf-8")
    )
    runs.records.pop()
    bad = tmp_path / "partial.json"
    bad.write_text(runs.model_dump_json(), encoding="utf-8")
    second = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/eval_agent.py"),
            "--records",
            str(bad),
            "--output-dir",
            str(tmp_path / "second"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert second.returncode == 1, second.stderr
    assert "缺失" in (tmp_path / "second/report.md").read_text(encoding="utf-8")
    assert grade_bundle(suite, runs)["groups"][-1]["pass_rate"] == 0


@pytest.mark.parametrize(
    "args", [["--live"], ["--repetitions", "0"], ["--modes", "react", "react"]]
)
def test_cli_rejects_live_and_invalid_selections(tmp_path, args):
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/eval_agent.py"), *args, "--output-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert run.returncode == 2
    assert not (tmp_path / "records.json").exists()


@pytest.mark.parametrize("fault", ["invalid_fixtures", "output_is_file"])
def test_cli_reports_actionable_input_and_output_errors(tmp_path, fault):
    destination = tmp_path / "output"
    args = ["--task", "calc-01", "--output-dir", str(destination)]
    if fault == "invalid_fixtures":
        bad = tmp_path / "bad.json"
        bad.write_text("[]", encoding="utf-8")
        args.extend(["--fixtures", str(bad)])
    else:
        destination.write_text("keep existing file", encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/eval_agent.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert run.returncode == 2
    assert "Traceback" not in run.stderr
    if fault == "output_is_file":
        assert "--output-dir" in run.stderr
        assert destination.read_text(encoding="utf-8") == "keep existing file"
