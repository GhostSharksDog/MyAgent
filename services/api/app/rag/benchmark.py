"""只读公开 RAG 基准：显式清单加载，不扫描用户目录或读取配置。"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.rag.evaluate import EvalSet, fingerprint
from app.rag.loaders import DocType, LoadedDocument, load_document


class BenchmarkDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    domain: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("file")
    @classmethod
    def public_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or "\\" in value
            or ":" in value
            or len(path.parts) != 2
            or path.parts[0] != "documents"
            or path.suffix != ".md"
            or any(part.startswith(".") for part in path.parts)
        ):
            raise ValueError("公开文档路径必须为 documents/<文件名>.md")
        return value


class BenchmarkManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str
    version: int = Field(ge=1)
    provenance: str
    documents: list[BenchmarkDocument] = Field(min_length=1)
    eval_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def load_benchmark(root: Path) -> tuple[list[LoadedDocument], EvalSet, dict[str, object]]:
    """仅打开 manifest 中列出的公开文件；内容漂移、越界或缺文件明确失败。"""
    root = root.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.resolve().is_relative_to(root):
        raise ValueError("公开清单路径越界")
    manifest = BenchmarkManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    paths = [item.file for item in manifest.documents]
    if len(paths) != len(set(paths)):
        raise ValueError("公开文档清单含重复路径")
    query_path = root / "eval_set.json"
    if not query_path.resolve().is_relative_to(root):
        raise ValueError("公开评测集路径越界")
    query_bytes = query_path.read_bytes()
    if hashlib.sha256(query_bytes).hexdigest() != manifest.eval_set_sha256:
        raise ValueError("评测集摘要不符；请核对公开标注并更新 manifest")
    eval_set = EvalSet.model_validate_json(query_bytes)
    if eval_set.name != f"{manifest.dataset_id}-v{manifest.version}":
        raise ValueError("评测集名称与 manifest 版本不一致")
    docs: list[LoadedDocument] = []
    for item in manifest.documents:
        path = (root / item.file).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"公开文档路径越界：{item.file}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item.sha256:
            raise ValueError(f"公开文档摘要不符：{item.file}；请核对内容并更新 manifest")
        doc = load_document(path, DocType.NOTE)
        doc.metadata_hint = {"domain": item.domain, "dataset": eval_set.name}
        docs.append(doc)
    return (
        docs,
        eval_set,
        {
            "dataset_id": manifest.dataset_id,
            "version": manifest.version,
            "description": manifest.provenance,
            "manifest_sha256": fingerprint(manifest.model_dump()),
            "documents": [item.model_dump() for item in manifest.documents],
        },
    )
