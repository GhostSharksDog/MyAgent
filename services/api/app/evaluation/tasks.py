"""可验证的任务目标、运行记录和评分。评分不调用模型、不访问任务文件。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path, PurePosixPath
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Mode = Literal["react", "plan", "multi"]
Category = Literal["calculation", "extraction", "planning", "comparison"]
Source = Literal["synthetic", "live", "recorded"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AnswerCheck(StrictModel):
    kind: Literal[
        "equals", "number", "unordered", "contains", "before", "sum_lte", "all_gte", "length"
    ]
    path: list[str | int] = Field(min_length=1)
    expected: Any
    tolerance: float = Field(default=1e-6, ge=0)

    @model_validator(mode="after")
    def validate_expected(self) -> AnswerCheck:
        if self.kind in {"number", "sum_lte", "all_gte"} and not _number(self.expected):
            raise ValueError("数值评分需要有限数值 expected")
        if self.kind == "length" and (type(self.expected) is not int or self.expected < 0):
            raise ValueError("length 需要非负整数 expected")
        if self.kind in {"unordered", "before"} and not isinstance(self.expected, list):
            raise ValueError("列表评分需要列表 expected")
        if self.kind == "before" and len(self.expected) != 2:
            raise ValueError("before 必须声明两个不同的步骤")
        if self.kind == "before" and self.expected[0] == self.expected[1]:
            raise ValueError("before 的两个步骤必须不同")
        return self


def _number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class TaskCase(StrictModel):
    id: str = Field(pattern=r"^[a-z]+-[0-9]{2}$")
    category: Category
    prompt: str = Field(min_length=1)
    assets: dict[str, str] = Field(default_factory=dict)
    checks: list[AnswerCheck] = Field(min_length=1)
    required_tools: list[Literal["calculator", "read_file"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_assets(self) -> TaskCase:
        for name in self.assets:
            path = PurePosixPath(name)
            if (
                not name
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or ":" in name
                or any(part.startswith(".") for part in path.parts)
                or path.suffix not in {".md", ".txt", ".csv", ".json"}
            ):
                raise ValueError("任务文件必须是相对路径的公开文本，不允许隐藏文件或越界")
        return self


class TaskSuite(StrictModel):
    id: str
    version: int = Field(ge=1)
    description: str
    tasks: list[TaskCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> TaskSuite:
        if len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError("任务 ID 重复")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical(self.model_dump()).encode("utf-8")).hexdigest()

    @classmethod
    def load(cls, path: Path) -> TaskSuite:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class ToolObservation(StrictModel):
    tool_name: str
    arguments: dict[str, Any]
    ok: bool
    content: str
    duration_ms: int = Field(ge=0)


class TrialRecord(StrictModel):
    task_id: str
    mode: Mode
    attempt: int = Field(default=1, ge=1)
    elapsed_seconds: float = Field(ge=0)
    llm_calls: int | None = Field(default=None, ge=0)
    events: list[dict[str, Any]] = Field(min_length=1)
    tool_observations: list[ToolObservation] = Field(default_factory=list)


class RunBundle(StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    suite_id: str
    suite_sha256: str
    source: Source
    model: str = Field(min_length=1)
    modes: list[Mode] = Field(min_length=1)
    task_ids: list[str] = Field(min_length=1)
    repetitions: int = Field(default=1, ge=1)
    records: list[TrialRecord]

    @model_validator(mode="after")
    def unique_records(self) -> RunBundle:
        if len(set(self.modes)) != len(self.modes) or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("模式或任务选择重复")
        keys = [(r.task_id, r.mode, r.attempt) for r in self.records]
        if len(set(keys)) != len(keys):
            raise ValueError("运行记录重复，不能重复计分")
        if any(
            r.mode not in self.modes
            or r.task_id not in self.task_ids
            or r.attempt > self.repetitions
            for r in self.records
        ):
            raise ValueError("运行记录不属于声明的任务／模式／重复次数")
        return self


def _value_at(answer: Any, path: list[str | int]) -> Any:
    for segment in path:
        if isinstance(segment, int) and type(segment) is int:
            if not isinstance(answer, list) or segment < 0:
                raise KeyError(segment)
        elif not isinstance(answer, dict):
            raise KeyError(segment)
        answer = answer[segment]
    return answer


def _matches(check: AnswerCheck, actual: Any) -> bool:
    expected = check.expected
    if check.kind == "equals":
        return _canonical(actual) == _canonical(expected)
    if check.kind == "number":
        return _number(actual) and math.isclose(
            actual, expected, rel_tol=0, abs_tol=check.tolerance
        )
    if check.kind == "unordered":
        return isinstance(actual, list) and Counter(map(_canonical, actual)) == Counter(
            map(_canonical, expected)
        )
    if check.kind == "contains":
        return isinstance(actual, (list, str)) and expected in actual
    if check.kind == "before":
        return (
            isinstance(actual, list)
            and all(item in actual for item in expected)
            and actual.index(expected[0]) < actual.index(expected[1])
        )
    if check.kind == "length":
        return isinstance(actual, list) and len(actual) == expected
    if check.kind == "all_gte":
        return (
            isinstance(actual, list)
            and bool(actual)
            and all(_number(v) and v >= expected - check.tolerance for v in actual)
        )
    return (
        isinstance(actual, list)
        and bool(actual)
        and all(_number(v) for v in actual)
        and sum(actual) <= expected + check.tolerance
    )


def grade_trial(task: TaskCase, record: TrialRecord) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    events = record.events
    done = [e for e in events if e.get("type") == "done"]
    terminal = done[0] if len(done) == 1 else {}
    contract = (
        len(done) == 1 and events[-1].get("type") == "done" and bool(terminal.get("stopped_reason"))
    )
    add("terminal_contract", contract, "需恰好一个位于末尾、明确原因的 done")
    finished = contract and terminal.get("stopped_reason") == "finished"
    add("finished", finished, str(terminal.get("stopped_reason", "missing")))
    finals = [e for e in events if e.get("type") == "final"]
    answer: Any = None
    try:
        if len(finals) != 1 or not isinstance(finals[0].get("content"), str):
            raise ValueError("需恰好一个最终答案")

        def reject_constant(value: str) -> None:
            raise ValueError("JSON 不允许 " + value)

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("JSON 字段重复")
                result[key] = value
            return result

        answer = json.loads(
            finals[0]["content"], parse_constant=reject_constant, object_pairs_hook=unique_object
        )
        if not isinstance(answer, dict):
            raise ValueError("答案需为 JSON 对象")
        add("answer_json", True)
    except (ValueError, TypeError):
        add("answer_json", False, "最终答案不是有效的 JSON 对象")
    for check in task.checks:
        name = check.kind + ":" + "/".join(map(str, check.path))
        try:
            actual = _value_at(answer, check.path)
            add(
                name,
                _matches(check, actual),
                f"expected={_canonical(check.expected)}; actual={_canonical(actual)}",
            )
        except (KeyError, IndexError, TypeError, ValueError):
            add(name, False, "缺少目标字段或字段类型错误")
    for tool in task.required_tools:
        if record.tool_observations:
            called = any(o.tool_name == tool for o in record.tool_observations)
            succeeded = any(o.tool_name == tool and o.ok for o in record.tool_observations)
        else:
            called = any(
                e.get("type") == "tool_call" and e.get("tool_name") == tool for e in events
            )
            succeeded = any(
                e.get("type") == "tool_result"
                and e.get("tool_name") == tool
                and e.get("tool_ok") is True
                for e in events
            )
        add("tool:" + tool, called and succeeded, "需有实际请求与成功工具结果")
    usage = terminal.get("usage")
    has_usage = isinstance(usage, dict) and all(
        type(usage.get(k)) is int and usage[k] >= 0
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    if has_usage and usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
        has_usage = False
    complete = contract and has_usage and terminal.get("usage_complete") is True
    return {
        "task_id": task.id,
        "category": task.category,
        "mode": record.mode,
        "attempt": record.attempt,
        "passed": all(c["passed"] for c in checks),
        "finished": finished,
        "stopped_reason": terminal.get("stopped_reason", "missing"),
        "elapsed_seconds": record.elapsed_seconds,
        "llm_calls": record.llm_calls,
        "tool_calls": len(record.tool_observations)
        if record.tool_observations
        else sum(e.get("type") == "tool_call" for e in events),
        "tool_evidence_source": "registry" if record.tool_observations else "events",
        "usage": usage if has_usage else None,
        "usage_complete": bool(complete),
        "checks": checks,
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def grade_bundle(suite: TaskSuite, bundle: RunBundle) -> dict[str, Any]:
    if bundle.suite_id != suite.id or bundle.suite_sha256 != suite.sha256:
        raise ValueError("任务集版本／摘要不匹配，不能把不同评分规则的成绩混在一起")
    tasks = {task.id: task for task in suite.tasks}
    if set(bundle.task_ids) - tasks.keys():
        raise ValueError("记录包含未知任务")
    trials = [grade_trial(tasks[r.task_id], r) for r in bundle.records]
    groups: list[dict[str, Any]] = []
    for mode in bundle.modes:
        for category in ["all", *sorted({tasks[t].category for t in bundle.task_ids})]:
            selected = [
                t
                for t in trials
                if t["mode"] == mode and (category == "all" or t["category"] == category)
            ]
            count = (
                sum(category == "all" or tasks[t].category == category for t in bundle.task_ids)
                * bundle.repetitions
            )
            seconds = [t["elapsed_seconds"] for t in selected]
            known = [t["usage"] for t in selected if t["usage"] is not None]
            groups.append(
                {
                    "mode": mode,
                    "category": category,
                    "expected": count,
                    "observed": len(selected),
                    "passed": sum(t["passed"] for t in selected),
                    "pass_rate": sum(t["passed"] for t in selected) / count,
                    "finished_rate": sum(t["finished"] for t in selected) / count,
                    "mean_seconds": mean(seconds) if seconds else None,
                    "p50_seconds": _percentile(seconds, 0.5),
                    "p95_seconds": _percentile(seconds, 0.95),
                    "tool_calls": sum(t["tool_calls"] for t in selected),
                    "llm_calls": sum(t["llm_calls"] for t in selected)
                    if selected and all(t["llm_calls"] is not None for t in selected)
                    else None,
                    "known_total_tokens": sum(u["total_tokens"] for u in known) if known else None,
                    "usage_complete_count": sum(t["usage_complete"] for t in selected),
                    "usage_complete": len(selected) == count
                    and all(t["usage_complete"] for t in selected),
                }
            )
    expected = len(bundle.task_ids) * len(bundle.modes) * bundle.repetitions
    keys = {(r.task_id, r.mode, r.attempt) for r in bundle.records}
    missing = [
        {"task_id": task, "mode": mode, "attempt": attempt}
        for task in bundle.task_ids
        for mode in bundle.modes
        for attempt in range(1, bundle.repetitions + 1)
        if (task, mode, attempt) not in keys
    ]
    return {
        "schema_version": 1,
        "suite_id": suite.id,
        "suite_sha256": suite.sha256,
        "source": bundle.source,
        "model": bundle.model,
        "task_ids": bundle.task_ids,
        "suite_task_count": len(suite.tasks),
        "modes": bundle.modes,
        "repetitions": bundle.repetitions,
        "synthetic": bundle.source == "synthetic",
        "note": "合成输出仅检验执行与评分链路，耗时与 Usage 不能作为模型性能／成本"
        if bundle.source == "synthetic"
        else "来源由输入记录声明；结果只适用于此任务集和模型，不代表生产正确率",
        "expected_trials": expected,
        "observed_trials": len(trials),
        "passed": len(trials) == expected and all(t["passed"] for t in trials),
        "groups": groups,
        "trials": trials,
        "missing": missing,
    }


def render_report(report: dict[str, Any]) -> str:
    """可分享的摘要；完整事件与逐项评分另存 JSON。"""
    lines = [
        "# Agent 任务评测报告",
        "",
        report["note"],
        "",
        f"来源：{report['source']}；模型：{report['model']}。",
        f"任务集：{report['suite_id']}；SHA-256：`{report['suite_sha256']}`。",
        f"任务选择：{len(report['task_ids'])}/{report['suite_task_count']}；模式：{', '.join(report['modes'])}；每题重复：{report['repetitions']}。",
        f"记录覆盖：{report['observed_trials']}/{report['expected_trials']}；检查整体通过：{report['passed']}。",
        "",
        "| 模式 | 通过/应运行 | 目标通过率 | 正常结束率 | 平均耗时(s) | 模型调用 | 工具调用 | 已知 token | 用量完整 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]

    def show(value: Any) -> str:
        return (
            "未知" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)
        )

    for row in report["groups"]:
        if row["category"] == "all":
            lines.append(
                f"| {row['mode']} | {row['passed']}/{row['expected']} | {row['pass_rate']:.1%} | {row['finished_rate']:.1%} | {show(row['mean_seconds'])} | {show(row['llm_calls'])} | {row['tool_calls']} | {show(row['known_total_tokens'])} | {row['usage_complete']} |"
            )
    lines += [
        "",
        "用量缺失时只报告已知下界；无已知用量显示未知，不据零值推断免费。",
        "",
        "## 失败与缺失",
        "",
    ]
    failures = [t for t in report["trials"] if not t["passed"]]
    for trial in failures:
        names = ", ".join(c["name"] for c in trial["checks"] if not c["passed"])
        lines.append(f"- {trial['task_id']} / {trial['mode']} / #{trial['attempt']}：{names}")
    for item in report["missing"]:
        lines.append(f"- 缺失：{item['task_id']} / {item['mode']} / #{item['attempt']}")
    if not failures and not report["missing"]:
        lines.append("无失败或缺失记录。")
    return "\n".join(lines) + "\n"
