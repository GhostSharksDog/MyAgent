"""公开留出集 CLI 的端到端隔离，不为已看过的排序结果设置分数门槛。"""

from __future__ import annotations

import json
import runpy
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.rag.answer_audit import EvidenceBundle, generation_messages
from app.rag.chunker import Chunk
from app.rag.evaluate import fingerprint
from app.rag.holdout import HOLDOUT_FREEZE_SHA256

ROOT = Path(__file__).resolve().parents[3]


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("公开质量评测读取了配置、私人语料或网络")


@pytest.fixture
def isolated_entrypoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[dict[str, Any], dict[str, Any], Path, Path]:
    from app.core import config
    from app.rag import corpus

    holdout, general = tmp_path / "holdout", tmp_path / "general"
    shutil.copytree(ROOT / "services/api/seed/rag_holdout", holdout)
    shutil.copytree(ROOT / "services/api/seed/rag_general", general)
    (holdout / "documents/private-unlisted.md").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    (holdout / ".env").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    (holdout / "notes").mkdir()
    (holdout / "notes/private-notes.md").write_text("PRIVATE_SENTINEL", encoding="utf-8")

    retrieval = runpy.run_path(str(ROOT / "scripts/eval_rag.py"))
    answers = runpy.run_path(str(ROOT / "scripts/eval_rag_answers.py"))
    retrieval_globals = retrieval["main"].__globals__
    answers_globals = answers["main"].__globals__
    monkeypatch.setitem(retrieval_globals, "HOLDOUT_BENCHMARK", holdout)
    monkeypatch.setitem(retrieval_globals, "GENERAL_BENCHMARK", general)
    monkeypatch.setitem(retrieval_globals, "get_settings", forbidden)
    monkeypatch.setitem(retrieval_globals, "build_corpus", forbidden)
    monkeypatch.setitem(answers_globals, "HOLDOUT", holdout)
    monkeypatch.setitem(answers_globals, "GENERAL", general)
    monkeypatch.setattr(config, "get_settings", forbidden)
    monkeypatch.setattr(corpus, "build_corpus", forbidden)
    monkeypatch.setattr(corpus, "_load_notes", forbidden)
    monkeypatch.setattr(corpus, "_load_user_path", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    original = Path.open

    def guarded(path: Path, *args: Any, **kwargs: Any):
        if path.name in {".env", "private-unlisted.md", "private-notes.md"}:
            forbidden()
        if path.resolve().is_relative_to(ROOT / "data"):
            forbidden()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    return retrieval, answers, holdout, general


@pytest.mark.parametrize("mode", ["validate", "run", "compare"])
def test_holdout_retrieval_modes_are_strictly_public_and_offline(
    isolated_entrypoints: tuple[dict[str, Any], dict[str, Any], Path, Path],
    tmp_path: Path,
    mode: str,
) -> None:
    retrieval, _, _, _ = isolated_entrypoints
    output = tmp_path / "retrieval-report.json"
    arguments = [f"--{mode}", "--dataset", "holdout", "--rerank", "coverage"]
    if mode != "validate":
        arguments += ["--diagnostics", "--json-out", str(output)]
    assert retrieval["main"](arguments) == 0
    if mode == "validate":
        assert not output.exists()
        return
    payload = json.loads(output.read_text(encoding="utf-8"))
    reports = payload if mode == "compare" else [payload]
    assert len(reports) == (5 if mode == "compare" else 1)
    assert all(len(report["per_query"]) == 32 for report in reports)
    assert all(report["metrics"]["count"] == 24 for report in reports)
    assert all(report["abstention_metrics"]["count"] == 8 for report in reports)
    assert all(report["provenance"]["source"] == "holdout-public" for report in reports)
    assert all(report["provenance"]["freeze_sha256"] == HOLDOUT_FREEZE_SHA256 for report in reports)
    assert all(report["parameters"]["diagnostics"] for report in reports)
    assert "PRIVATE_SENTINEL" not in output.read_text(encoding="utf-8")
    if mode == "run":
        assert reports[0]["parameters"]["rerank"] == "coverage"
        assert reports[0]["reranker"] == "coverage"
    else:
        assert {report["reranker"] for report in reports} == {"none", "lexical"}


@pytest.mark.parametrize(
    "paid",
    [
        ["--with-llm"],
        ["--with-rewrite"],
        ["--rerank", "llm"],
        ["--rewrite", "multi_query"],
        ["--rewrite", "hyde"],
    ],
)
def test_holdout_paid_flags_fail_before_loading_any_retrieval_input(
    isolated_entrypoints: tuple[dict[str, Any], dict[str, Any], Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    paid: list[str],
) -> None:
    retrieval, _, _, _ = isolated_entrypoints
    monkeypatch.setitem(retrieval["main"].__globals__, "build_retriever", forbidden)
    with pytest.raises(SystemExit) as exc:
        retrieval["main"](["--run", "--dataset", "holdout", *paid])
    assert exc.value.code == 2


@pytest.mark.parametrize("mode", ["validate", "run", "compare"])
def test_retrieval_rejects_invalid_holdout_freeze_in_all_modes(
    isolated_entrypoints: tuple[dict[str, Any], dict[str, Any], Path, Path],
    tmp_path: Path,
    mode: str,
) -> None:
    retrieval, _, holdout, _ = isolated_entrypoints
    freeze = holdout / "freeze.json"
    freeze.write_bytes(freeze.read_bytes() + b"\n")
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(SystemExit) as exc:
        retrieval["main"]([f"--{mode}", "--dataset", "holdout", "--json-out", str(output)])
    assert exc.value.code == 2
    assert not output.exists()


def _export_holdout(answers: dict[str, Any], tmp_path: Path) -> tuple[Path, Path]:
    bundle, records = tmp_path / "bundle.json", tmp_path / "records.json"
    assert (
        answers["main"](
            [
                "--export",
                "--dataset",
                "holdout",
                "--rerank",
                "coverage",
                "--bundle-out",
                str(bundle),
                "--template-out",
                str(records),
            ]
        )
        == 0
    )
    return bundle, records


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value))
    return set()


def test_holdout_answer_export_score_and_self_test_remain_offline_without_label_leak(
    isolated_entrypoints: tuple[dict[str, Any], dict[str, Any], Path, Path],
    tmp_path: Path,
) -> None:
    _, answers, _, _ = isolated_entrypoints
    bundle, records = _export_holdout(answers, tmp_path)
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    assert len(payload["questions"]) == 32
    assert not (
        _all_keys(payload) & {"gold", "reference_answer", "answerable", "difficulty", "category"}
    )
    assert payload["parameters"]["dataset"] == "holdout"
    assert payload["parameters"]["reranker"] == "coverage"
    assert payload["provenance"]["freeze_sha256"] == HOLDOUT_FREEZE_SHA256
    assert "PRIVATE_SENTINEL" not in bundle.read_text(encoding="utf-8")
    for question in payload["questions"]:
        generation_input = json.dumps(question["generation_messages"], ensure_ascii=False)
        assert question["id"] not in generation_input
        assert all(
            set(message) == {"role", "content"} for message in question["generation_messages"]
        )
    template = json.loads(records.read_text(encoding="utf-8"))
    assert len(template) == 32 and all(
        item["answer"] == "" and item["review"] is None for item in template
    )
    report = tmp_path / "score.json"
    assert (
        answers["main"](
            [
                "--score",
                "--bundle",
                str(bundle),
                "--records",
                str(records),
                "--json-out",
                str(report),
            ]
        )
        == 0
    )
    scored = json.loads(report.read_text(encoding="utf-8"))
    assert scored["counts"]["expected"] == scored["counts"]["empty_answers"] == 32
    assert scored["counts"]["human_correctness_unknown"] == 32
    assert scored["human_metrics"]["accuracy_on_reviewed"] is None
    assert (
        answers["main"](
            [
                "--self-test",
                "--dataset",
                "holdout",
                "--rerank",
                "coverage",
                "--json-out",
                str(report),
            ]
        )
        == 0
    )
    tested = json.loads(report.read_text(encoding="utf-8"))
    assert tested["origin"] == "controlled_fixture_only" and tested["model_requests"] == 0
    assert tested["checks"] and all(item["passed"] for item in tested["checks"])
    assert tested["audit"]["human_metrics"]["accuracy_on_reviewed"] is None


@pytest.mark.parametrize("mode", ["export", "score", "self-test"])
def test_answer_modes_reject_invalid_freeze_before_writing_results(
    isolated_entrypoints: tuple[dict[str, Any], dict[str, Any], Path, Path],
    tmp_path: Path,
    mode: str,
) -> None:
    _, answers, holdout, _ = isolated_entrypoints
    arguments = [f"--{mode}", "--dataset", "holdout", "--rerank", "coverage"]
    output = tmp_path / "rejected.json"
    if mode == "score":
        bundle, records = _export_holdout(answers, tmp_path)
        arguments += ["--bundle", str(bundle), "--records", str(records)]
    arguments += ["--bundle-out" if mode == "export" else "--json-out", str(output)]
    freeze = holdout / "freeze.json"
    freeze.write_bytes(freeze.read_bytes() + b"\n")
    with pytest.raises(SystemExit) as exc:
        answers["main"](arguments)
    assert exc.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("paid", [["--with-llm"], ["--with-rewrite"], ["--rerank", "llm"]])
def test_answer_paid_options_cannot_reach_loader(
    isolated_entrypoints: tuple[dict[str, Any], dict[str, Any], Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    paid: list[str],
) -> None:
    _, answers, _, _ = isolated_entrypoints
    monkeypatch.setitem(answers["main"].__globals__, "execute", forbidden)
    with pytest.raises(SystemExit) as exc:
        answers["main"](["--self-test", "--dataset", "holdout", *paid])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "case", ["export_inputs", "self_test_inputs", "export_collision", "score_collision"]
)
def test_answer_mode_inputs_cannot_be_implicitly_read_or_overwritten(
    isolated_entrypoints: tuple[dict[str, Any], dict[str, Any], Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    _, answers, holdout, _ = isolated_entrypoints
    output = tmp_path / "out.json"
    private = holdout / "notes/private-notes.md"
    choices = {
        "export_inputs": ["--export", "--bundle-out", str(output), "--records", str(private)],
        "self_test_inputs": ["--self-test", "--records", str(private)],
        "export_collision": [
            "--export",
            "--bundle-out",
            str(output),
            "--template-out",
            str(output),
        ],
        "score_collision": [
            "--score",
            "--bundle",
            str(output),
            "--records",
            str(private),
            "--json-out",
            str(private),
        ],
    }
    monkeypatch.setitem(answers["main"].__globals__, "execute", forbidden)
    with pytest.raises(SystemExit) as exc:
        answers["main"](["--dataset", "holdout", *choices[case]])
    assert exc.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("mutation", ["private_dataset", "parameter_drift", "source_drift"])
def test_score_cannot_accept_self_consistent_but_forged_public_bundle(
    isolated_entrypoints: tuple[dict[str, Any], dict[str, Any], Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    _, answers, _, _ = isolated_entrypoints
    bundle, records = _export_holdout(answers, tmp_path)
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    if mutation == "private_dataset":
        payload["parameters"]["dataset"] = "private"
    elif mutation == "parameter_drift":
        payload["parameters"]["recall_k"] = 1
    else:
        source = payload["questions"][0]["sources"][0]
        source["chunk"]["text"] += "FORGED_SOURCE"
        payload["questions"][0]["context"] = "\n\n---\n\n".join(
            f"[{item['number']}] 出处：{Chunk.model_validate(item['chunk']).citation}\n{item['chunk']['text']}"
            for item in payload["questions"][0]["sources"]
        )
        question = payload["questions"][0]
        question["generation_messages"] = [
            message.model_dump(mode="json")
            for message in generation_messages(question["query"], question["context"])
        ]
    payload["bundle_id"] = fingerprint(
        {key: value for key, value in payload.items() if key != "bundle_id"}
    )
    bundle.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # 摘要和可见正文结构自洽；必须靠重新加载冻结公开管线拒绝，不能偶然因 JSON 坏掉而绿。
    EvidenceBundle.model_validate_json(bundle.read_bytes())
    original = Path.open

    def guarded(path: Path, *args: Any, **kwargs: Any):
        if path == records:
            raise AssertionError("检索版本尚未通过校验，已经读取答案记录")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    output = tmp_path / "must-not-score.json"
    with pytest.raises(SystemExit) as exc:
        answers["main"](
            [
                "--score",
                "--bundle",
                str(bundle),
                "--records",
                str(records),
                "--json-out",
                str(output),
            ]
        )
    assert exc.value.code == 2
    assert not output.exists()
