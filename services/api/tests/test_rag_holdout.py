"""留出集只验证冻结、标注和隔离；不以排序成绩反向修改样本。"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.rag.benchmark import load_benchmark
from app.rag.chunker import ChunkStrategy, chunk_documents
from app.rag.evaluate import EvalQuery, EvalSet, GoldCondition, validate_labels
from app.rag.holdout import (
    HOLDOUT_FREEZE_SHA256,
    load_holdout_benchmark,
    validate_holdout_labels,
    validate_holdout_split,
)
from app.rag.loaders import LoadedDocument

ROOT = Path(__file__).resolve().parents[3]
HOLDOUT = ROOT / "services/api/seed/rag_holdout"
DEVELOPMENT = ROOT / "services/api/seed/rag_general"


@pytest.fixture
def public_copy(tmp_path: Path) -> tuple[Path, Path]:
    holdout, development = tmp_path / "holdout", tmp_path / "development"
    shutil.copytree(HOLDOUT, holdout)
    shutil.copytree(DEVELOPMENT, development)
    return holdout, development


def test_freeze_and_authorship_are_explicit_without_claiming_human_independence() -> None:
    docs, suite, metadata = load_holdout_benchmark(HOLDOUT, development_root=DEVELOPMENT)
    assert len(docs) == 12 and len(suite.queries) == 32
    assert Counter(item.category for item in suite.queries) == {
        "direct": 8,
        "paraphrase": 8,
        "multi_evidence": 8,
        "unanswerable": 8,
    }
    assert sum(item.answerable for item in suite.queries) == 24
    assert metadata["source"] == "holdout-public"
    assert metadata["authorship"] == "same-ai-author"
    assert "Not independent human labels" in metadata["limitation"]
    assert metadata["freeze_sha256"] == HOLDOUT_FREEZE_SHA256
    assert (
        hashlib.sha256((HOLDOUT / "freeze.json").read_bytes()).hexdigest() == HOLDOUT_FREEZE_SHA256
    )
    assert suite.name == "legacy-holdout-rag-v1"
    assert all(doc.metadata_hint["dataset"] == suite.name for doc in docs)


@pytest.mark.parametrize("strategy", list(ChunkStrategy))
def test_frozen_gold_survives_supported_chunking(strategy: ChunkStrategy) -> None:
    docs, suite, _ = load_holdout_benchmark(HOLDOUT, development_root=DEVELOPMENT)
    chunks = chunk_documents(docs, strategy=strategy, size=500, overlap=80, min_size=120)
    assert not validate_labels(suite, chunks)
    for item in suite.queries:
        if item.category == "multi_evidence":
            assert len({gold.doc_id for gold in item.gold}) >= 2
        if not item.answerable:
            assert not item.gold and item.reference_answer


@pytest.mark.parametrize(
    "relative", ["documents/gear-lending.md", "eval_set.json", "README.md", "manifest.json"]
)
def test_frozen_content_drift_fails_even_before_retrieval(
    public_copy: tuple[Path, Path], relative: str
) -> None:
    holdout, development = public_copy
    path = holdout / relative
    path.write_bytes(path.read_bytes() + b"\n ")
    with pytest.raises(ValueError, match="冻结文件摘要不符"):
        load_holdout_benchmark(holdout, development_root=development)


def test_rewriting_manifest_and_freeze_cannot_silently_change_frozen_version(
    public_copy: tuple[Path, Path],
) -> None:
    holdout, development = public_copy
    document = holdout / "documents/gear-lending.md"
    document.write_bytes(document.read_bytes() + b"\nchanged")
    manifest_path = holdout / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["documents"][0]["sha256"] = hashlib.sha256(document.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    freeze_path = holdout / "freeze.json"
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    freeze["files"]["documents/gear-lending.md"] = hashlib.sha256(document.read_bytes()).hexdigest()
    freeze["files"]["manifest.json"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    freeze_path.write_text(json.dumps(freeze, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="冻结摘要不符"):
        load_holdout_benchmark(holdout, development_root=development)


def test_changed_development_version_requires_explicit_new_split_review(
    public_copy: tuple[Path, Path],
) -> None:
    holdout, development = public_copy
    path = development / "manifest.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="开发集清单"):
        load_holdout_benchmark(holdout, development_root=development)


def test_missing_frozen_document_never_degrades_to_smaller_corpus(
    public_copy: tuple[Path, Path],
) -> None:
    holdout, development = public_copy
    (holdout / "documents/gear-lending.md").unlink()
    with pytest.raises(FileNotFoundError):
        load_holdout_benchmark(holdout, development_root=development)


def test_symlink_resolution_escape_is_rejected_before_opening_external_bytes(
    public_copy: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    holdout, development = public_copy
    original = Path.resolve
    outside = tmp_path / "outside-must-not-read.json"
    freeze_path = holdout / "freeze.json"

    def redirected(path: Path, *args: Any, **kwargs: Any) -> Path:
        return outside if path == freeze_path else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="路径越界"):
        load_holdout_benchmark(holdout, development_root=development)


def test_loader_cannot_read_configuration_unlisted_private_text_or_network(
    public_copy: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import config
    from app.rag import corpus

    holdout, development = public_copy
    (holdout / "documents/private-must-not-open.md").write_text(
        "PRIVATE_SENTINEL", encoding="utf-8"
    )
    (holdout / "notes").mkdir()
    (holdout / "notes/private.md").write_text("PRIVATE_SENTINEL", encoding="utf-8")
    (holdout / ".env").write_text("PRIVATE_SENTINEL", encoding="utf-8")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("留出集读取了配置、私人语料或网络")

    monkeypatch.setattr(config, "get_settings", forbidden)
    monkeypatch.setattr(corpus, "_load_notes", forbidden)
    monkeypatch.setattr(corpus, "_load_user_path", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    original = Path.open

    def guarded(path: Path, *args: Any, **kwargs: Any):
        if path.name in {".env", "private.md", "private-must-not-open.md"}:
            forbidden()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    docs, suite, _ = load_holdout_benchmark(holdout, development_root=development)
    assert len(docs) == 12 and len(suite.queries) == 32
    assert all("PRIVATE_SENTINEL" not in doc.text for doc in docs)
    assert all(doc.source.endswith(".md") for doc in docs)


@pytest.mark.parametrize("collision", ["name", "text", "query"])
def test_split_validation_has_independent_collision_counterexamples(collision: str) -> None:
    doc = LoadedDocument(source="one.md", text="development document")
    held = LoadedDocument(source="different.md", text="new holdout document")
    development = EvalSet(queries=[EvalQuery(query="HOW MUCH time?")])
    holdout = EvalSet(queries=[EvalQuery(query="a new question")])
    expected = {"name": "文档名", "text": "文档全文", "query": "查询"}[collision]
    if collision == "name":
        held.source = "ONE.MD"
    elif collision == "text":
        held.text = doc.text
    else:
        holdout.queries[0].query = "ｈｏｗ　ＭＵＣＨ　time?"
    problems = validate_holdout_split([held], holdout, [doc], development)
    assert len(problems) == 1 and expected in problems[0]


def test_new_frozen_documents_and_queries_are_disjoint_from_development() -> None:
    docs, suite, _ = load_holdout_benchmark(HOLDOUT, development_root=DEVELOPMENT)
    development_docs, development_suite, _ = load_benchmark(DEVELOPMENT)
    assert not validate_holdout_split(docs, suite, development_docs, development_suite)
    assert len({doc.source for doc in docs + development_docs}) == 28
    assert len({item.query for item in suite.queries + development_suite.queries}) == 92


@pytest.mark.parametrize(
    "problem", ["empty_id", "empty_reference", "bad_anchor", "single_doc_multi", "answerable"]
)
def test_label_validation_rejects_bad_labels_even_with_other_valid_gold(problem: str) -> None:
    docs, original, _ = load_holdout_benchmark(HOLDOUT, development_root=DEVELOPMENT)
    suite = original.model_copy(deep=True)
    expected = ""
    if problem == "empty_id":
        suite.queries[0].id = ""
        expected = "必须有id"
    elif problem == "empty_reference":
        suite.queries[0].reference_answer = " "
        expected = "必须有id"
    elif problem == "bad_anchor":
        item = next(item for item in suite.queries if item.category == "multi_evidence")
        item.gold[1].text_contains = "THIS ANCHOR IS ABSENT"
        expected = "gold 2"
    elif problem == "single_doc_multi":
        item = next(item for item in suite.queries if item.category == "multi_evidence")
        item.gold = [
            GoldCondition(doc_id="gear-lending.md", text_contains="最长借期为五个自然日"),
            GoldCondition(doc_id="gear-lending.md", text_contains="最多续借一次"),
        ]
        expected = "两份不同文档"
    else:
        suite.queries[-1].answerable = True
        expected = "answerable不一致"
    assert any(expected in message for message in validate_holdout_labels(docs, suite))
