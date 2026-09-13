"""语料构建：把项目里的各种数据源装成统一的文档集合。

数据源有三类：
  1. `data/resume.md`  —— 用户简历（由 scripts/ingest.py 从 PDF 解析而来，不进版本库）
  2. `seed/jobs.json`  —— 岗位库，每个岗位作为独立文档
  3. `data/notes/*.md` —— 用户自己丢进来的参考资料（可选）

【为什么岗位要拆成独立文档而不是合并成一份】
这直接决定检索质量。合并成一份大文档会有两个问题：
  - 切分后一个块里可能混着两个不同岗位的要求，引用时说不清是哪个岗位
  - 元数据过滤失效——无法实现"只在某个岗位内检索"

一个岗位 = 一个文档 = 可被精确引用和过滤，这是**数据结构设计对检索质量的影响**，
比换模型划算得多。
"""

from __future__ import annotations

import json
import logging

from app.core.config import PROJECT_ROOT
from app.rag.loaders import DocType, LoadedDocument, load_document, normalize_text

logger = logging.getLogger(__name__)

DATA_DIR = PROJECT_ROOT / "data"
SEED_DIR = PROJECT_ROOT / "services" / "api" / "seed"


def build_corpus(
    *,
    include_resume: bool = True,
    include_jobs: bool = True,
    use_sample_resume: bool = False,
) -> list[LoadedDocument]:
    """收集所有可用文档。缺失的数据源被跳过而不是报错——
    这样在只有岗位库、或只有简历的环境下也能跑起来。

    Args:
        use_sample_resume: 用可提交的 `seed/resume.sample.md` 代替用户的真实简历。
            **CI 与评测基准必须用它** —— 否则流水线会依赖一个被 gitignore 的
            私有文件而永远跑不起来；同时也让评测在别人 clone 后依然可用。
    """
    docs: list[LoadedDocument] = []

    if include_resume:
        resume_path = SEED_DIR / "resume.sample.md" if use_sample_resume else DATA_DIR / "resume.md"
        if resume_path.exists():
            try:
                docs.append(load_document(resume_path, DocType.RESUME))
            except Exception as exc:
                logger.warning("简历加载失败，已跳过：%s", exc)
        else:
            logger.warning(
                "未找到 %s —— 请先运行：python scripts/ingest.py <你的简历.pdf> --type resume",
                resume_path.relative_to(PROJECT_ROOT),
            )

    if include_jobs:
        docs.extend(_load_jobs())

    docs.extend(_load_notes())

    logger.info(
        "语料构建完成：%d 个文档（%s）",
        len(docs),
        "、".join(f"{d.source}({d.char_count}字)" for d in docs[:3])
        + ("..." if len(docs) > 3 else ""),
    )
    return docs


def _load_jobs() -> list[LoadedDocument]:
    """把岗位库拆成一个个独立文档。"""
    jobs_file = SEED_DIR / "jobs.json"
    if not jobs_file.exists():
        logger.warning("岗位库缺失：%s", jobs_file)
        return []

    raw = json.loads(jobs_file.read_text(encoding="utf-8"))
    docs: list[LoadedDocument] = []

    for job in raw:
        # 把结构化 JSON 渲染成自然语言——embedding 与 TF-IDF 都只理解文本，
        # 直接塞 JSON 字符串会让 {"title": ...} 这类语法噪声污染特征。
        title = job.get("title", "未知岗位")
        parts = [
            f"岗位名称：{title}",
            f"公司：{job.get('company', '未知')}",
            f"城市：{job.get('city', '未知')}",
            f"薪资：{job.get('salary', '面议')}",
            "",
            "岗位描述：",
            str(job.get("description", "")),
            "",
            "任职要求：",
            str(job.get("requirements", "")),
        ]
        text = normalize_text("\n".join(parts))

        docs.append(
            LoadedDocument(
                source=f"{job.get('id', 'job')} {title}",
                doc_type=DocType.JD,
                text=text,
                char_count=len(text),
                metadata_hint={
                    "job_id": str(job.get("id", "")),
                    "company": str(job.get("company", "")),
                    "city": str(job.get("city", "")),
                    "salary": str(job.get("salary", "")),
                },
            )
        )

    return docs


def _load_notes() -> list[LoadedDocument]:
    """加载 data/notes/ 下的个人参考资料。"""
    notes_dir = DATA_DIR / "notes"
    if not notes_dir.exists():
        return []

    docs: list[LoadedDocument] = []
    for path in sorted(notes_dir.glob("*.md")):
        try:
            docs.append(load_document(path, DocType.NOTE))
        except Exception as exc:
            logger.warning("笔记 %s 加载失败：%s", path.name, exc)
    return docs


def resume_available() -> bool:
    return (DATA_DIR / "resume.md").exists()
