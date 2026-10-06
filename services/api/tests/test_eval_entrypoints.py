"""公开评测入口不能依赖或偷读开发者的私人数据。"""

from __future__ import annotations

import argparse
import runpy
from pathlib import Path
from typing import Any

import pytest
from app.core.config import AgentSettings, get_settings
from app.rag import corpus

ROOT = Path(__file__).resolve().parents[3]


def _eval_args() -> argparse.Namespace:
    return argparse.Namespace(
        sample=True,
        strategy="section",
        size=500,
        overlap=80,
        min_size=120,
        mode="hybrid",
        rerank="lexical",
        rrf_k=60,
    )


def test_sample_eval_ignores_private_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    module = runpy.run_path(str(ROOT / "scripts" / "eval_rag.py"))
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "agent",
        AgentSettings(
            _env_file=None,
            profile="general",
            corpus_paths="private-must-not-be-read",
        ),
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("公开评测读取了私人数据源")

    monkeypatch.setattr(corpus, "_load_notes", forbidden)
    monkeypatch.setattr(corpus, "_load_user_path", forbidden)
    retriever = module["build_retriever"](_eval_args())
    assert retriever.chunks
    assert {str(c.doc_type) for c in retriever.chunks} == {"resume", "jd"}
    assert module["resolve_eval_set"](True) == ROOT / "services/api/seed/eval_set.json"


def test_normal_eval_respects_declared_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    module = runpy.run_path(str(ROOT / "scripts" / "eval_rag.py"))
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "agent",
        AgentSettings(
            _env_file=None,
            profile="general",
            corpus_paths="explicit-notes.md",
        ),
    )
    seen: dict[str, Any] = {}

    def capture(**kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        return []

    monkeypatch.setitem(module["build_retriever"].__globals__, "build_corpus", capture)
    args = _eval_args()
    args.sample = False
    module["build_retriever"](args)
    assert seen["extra_paths"] == ["explicit-notes.md"]
    assert seen["include_resume"] is False
    assert seen["include_jobs"] is False


@pytest.mark.parametrize(
    "expression,flag,skipped",
    [
        ("", False, True),
        ("not slow", False, True),
        ("not live", False, True),
        ("live", False, False),
        ("live and not slow", True, False),
    ],
)
def test_live_tests_require_explicit_opt_in(expression: str, flag: bool, skipped: bool) -> None:
    from tests.conftest import pytest_collection_modifyitems

    class Config:
        def getoption(self, name: str) -> Any:
            return flag if name == "--run-live" else expression

    class Item:
        def __init__(self, live: bool) -> None:
            self.live = live
            self.markers: list[Any] = []

        def get_closest_marker(self, name: str) -> object | None:
            return object() if self.live else None

        def add_marker(self, marker: Any) -> None:
            self.markers.append(marker)

    live, offline = Item(True), Item(False)
    pytest_collection_modifyitems(Config(), [live, offline])  # type: ignore[arg-type]
    assert bool(live.markers) is skipped
    assert not offline.markers
