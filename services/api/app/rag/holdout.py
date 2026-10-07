"""冻结的公开合成留出集：只读固定清单，不接触用户配置或额外语料。"""

from __future__ import annotations

import hashlib
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.rag.benchmark import BenchmarkManifest, load_benchmark
from app.rag.chunker import Chunk, ChunkStrategy, chunk_documents
from app.rag.evaluate import EvalSet, fingerprint, validate_labels
from app.rag.loaders import LoadedDocument

# 首次检索实验前固定。重写清单及 freeze.json 不能悄悄成为同一版留出集。
# 这是一致性校验，不是签名或由第三方背书的不可篡改时间戳。
HOLDOUT_FREEZE_SHA256 = "9c1407c126b6a3113b242dca2cc49b45569341acbd4da37ba353a36dd360fc58"
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class HoldoutFreeze(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["legacy-public-holdout-freeze-v1"]
    dataset_id: Literal["legacy-holdout-rag"]
    version: Literal[1]
    frozen_at: datetime
    authorship: Literal["same-ai-author"]
    before_first_experiment: Literal[True]
    limitation: str = Field(min_length=30)
    files: dict[str, Digest]
    development_dataset: str
    development_manifest_sha256: Digest
    development_queries_sha256: Digest

    @field_validator("frozen_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("冻结时间必须包含时区")
        return value


def _public_bytes(root: Path, relative: str) -> bytes:
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"公开冻结文件路径越界：{relative}")
    return target.read_bytes()


def _canonical_query(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def validate_holdout_split(
    documents: list[LoadedDocument],
    suite: EvalSet,
    development_documents: list[LoadedDocument],
    development_suite: EvalSet,
) -> list[str]:
    """检查文件名、规范化全文和查询的精确交集；不宣称语义或作者独立。"""
    problems: list[str] = []
    names = {doc.source.casefold() for doc in documents}
    development_names = {doc.source.casefold() for doc in development_documents}
    if names & development_names:
        problems.append("留出集与开发集存在相同文档名")
    texts = {fingerprint(doc.text) for doc in documents}
    development_texts = {fingerprint(doc.text) for doc in development_documents}
    if texts & development_texts:
        problems.append("留出集与开发集存在相同规范化文档全文")
    queries = {_canonical_query(item.query) for item in suite.queries}
    development_queries = {_canonical_query(item.query) for item in development_suite.queries}
    if queries & development_queries:
        problems.append("留出集与开发集存在相同规范化查询")
    return problems


def validate_holdout_labels(documents: list[LoadedDocument], suite: EvalSet) -> list[str]:
    """先核对原文锚点与预设分组，再验证固定支持的切分参数；不评分排序。"""
    problems: list[str] = []
    if len(documents) != 12 or len({doc.source for doc in documents}) != 12:
        problems.append("冻结留出集必须包含12份不同文档")
    expected = {name: 8 for name in ("direct", "paraphrase", "multi_evidence", "unanswerable")}
    if Counter(item.category for item in suite.queries) != expected:
        problems.append("冻结留出集四类查询必须各8条")
    normalized = [_canonical_query(item.query) for item in suite.queries]
    if len(normalized) != len(set(normalized)) or not all(normalized):
        problems.append("留出集查询为空或存在规范化重复")
    whole = [
        Chunk(id=doc.source, doc_id=doc.source, doc_type=doc.doc_type, text=doc.text, index=0)
        for doc in documents
    ]
    problems.extend(validate_labels(suite, whole))
    for item in suite.queries:
        if not item.id.strip() or not item.reference_answer.strip():
            problems.append("每条留出集查询必须有id和参考说明")
        if item.answerable != (item.category != "unanswerable"):
            problems.append(f"{item.id}: 类别与answerable不一致")
        if any(
            not gold.doc_id or not gold.text_contains or gold.doc_id_contains for gold in item.gold
        ):
            problems.append(f"{item.id}: gold必须使用精确文档名和正文锚点")
        if item.category == "multi_evidence" and len({gold.doc_id for gold in item.gold}) < 2:
            problems.append(f"{item.id}: 多证据必须来自至少两份不同文档")
    for strategy in ChunkStrategy:
        chunks = chunk_documents(documents, strategy=strategy, size=500, overlap=80, min_size=120)
        problems.extend(f"{strategy}: {problem}" for problem in validate_labels(suite, chunks))
    return problems


def load_holdout_benchmark(
    root: Path, *, development_root: Path
) -> tuple[list[LoadedDocument], EvalSet, dict[str, object]]:
    """验证固定字节、标注有效性和开发集隔离；与load_benchmark返回契约相同。"""
    root, development_root = root.resolve(), development_root.resolve()
    freeze_bytes = _public_bytes(root, "freeze.json")
    freeze_digest = hashlib.sha256(freeze_bytes).hexdigest()
    if freeze_digest != HOLDOUT_FREEZE_SHA256:
        raise ValueError("留出集冻结摘要不符；不能重写本版冻结文件，新增数据须显式另建版本")
    freeze = HoldoutFreeze.model_validate_json(freeze_bytes)
    manifest = BenchmarkManifest.model_validate_json(_public_bytes(root, "manifest.json"))
    if (manifest.dataset_id, manifest.version) != (freeze.dataset_id, freeze.version):
        raise ValueError("留出集清单版本与冻结版本不一致")
    expected_files = {"manifest.json", "eval_set.json", "README.md"} | {
        item.file for item in manifest.documents
    }
    if set(freeze.files) != expected_files:
        raise ValueError("冻结文件清单与公开文档清单不一致")
    for name, expected in freeze.files.items():
        if hashlib.sha256(_public_bytes(root, name)).hexdigest() != expected:
            raise ValueError(f"留出集冻结文件摘要不符：{name}；本版本已冻结，不得按实验结果修改")
    development_manifest = _public_bytes(development_root, "manifest.json")
    if hashlib.sha256(development_manifest).hexdigest() != freeze.development_manifest_sha256:
        raise ValueError("开发集清单与留出集冻结时不一致；请显式核对数据版本")
    docs, suite, metadata = load_benchmark(root)
    development_docs, development_suite, _ = load_benchmark(development_root)
    if (
        development_suite.name != freeze.development_dataset
        or fingerprint([item.query for item in development_suite.queries])
        != freeze.development_queries_sha256
    ):
        raise ValueError("开发集查询与留出集冻结时不一致")
    problems = validate_holdout_labels(docs, suite) + validate_holdout_split(
        docs, suite, development_docs, development_suite
    )
    if problems:
        raise ValueError("留出集验证失败：" + "；".join(problems))
    metadata.update(
        source="holdout-public",
        freeze_sha256=freeze_digest,
        frozen_at=freeze.frozen_at.isoformat(),
        authorship=freeze.authorship,
        limitation=freeze.limitation,
        development_dataset=freeze.development_dataset,
        split_checks=["document_names", "normalized_document_text", "normalized_queries"],
    )
    return docs, suite, metadata
