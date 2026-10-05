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
from pathlib import Path

from app.core.config import PROJECT_ROOT
from app.rag.loaders import (
    DocType,
    LoadedDocument,
    load_document,
    normalize_text,
    supported_suffixes,
)

logger = logging.getLogger(__name__)

DATA_DIR = PROJECT_ROOT / "data"
SEED_DIR = PROJECT_ROOT / "services" / "api" / "seed"


def build_corpus(
    *,
    include_resume: bool = False,
    include_jobs: bool = False,
    use_sample_resume: bool = False,
    extra_paths: list[str] | None = None,
) -> list[LoadedDocument]:
    """收集知识库文档。

    【默认值从 True 改成 False —— 这是 P6 最重要的一处修正】

    原来的默认是「简历 + 岗位库」都加载，于是用户装好项目、什么都没配，
    一打开就发现**自己的简历已经在知识库里了**。用户的原话是
    "居然已经有我的简历内容了" —— 那个"居然"就是问题所在：
    **一个通用助手不该默认把用户的私人文件读进索引。**

    数据源应该是用户**显式声明**的，不是我们猜的。
    所以现在默认什么都不加载，语料由 `AGENT_CORPUS_PATHS` 指定；
    求职相关的两份数据保留为可选（用 jobhunt profile 或显式开启）。

    这个改动的意义不只是"少加两份文档"：它把
    **"系统替用户决定读什么"变成了"用户决定读什么"**。

    Args:
        include_resume / include_jobs: 求职场景的两份内置数据（默认关）。
        use_sample_resume: 用可提交的 `seed/resume.sample.md` 代替真实简历。
            **CI 与评测基准必须用它** —— 否则流水线会依赖一个被 gitignore 的
            私有文件而永远跑不起来；也让评测在别人 clone 后依然可用。
        extra_paths: 用户显式声明的文档路径（文件或目录）。
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

    # 用户显式声明的路径。**一个一个加载并逐个容错** ——
    # 一个路径写错不该让整个知识库建不起来（那是"因为一个文件打不开，
    # 所有文档都搜不了"的不合理后果）。
    for raw in extra_paths or []:
        try:
            docs.extend(_load_user_path(raw))
        except Exception as exc:
            logger.warning("加载知识库路径 %r 失败，已跳过：%s", raw, exc)

    logger.info(
        "语料构建完成：%d 个文档（%s）",
        len(docs),
        "、".join(f"{d.source}({d.char_count}字)" for d in docs[:3])
        + ("..." if len(docs) > 3 else ""),
    )
    return docs


def _load_user_path(raw: str) -> list[LoadedDocument]:
    """加载用户声明的一个文件或目录。

    【为什么要支持目录 + 递归】
    用户的真实用法是"把我的手记文件夹加进知识库"，而不是逐文件配置。
    只支持文件的话，加十个文档就要写十行配置 —— 那种设计会让人放弃用它。

    【为什么要过滤】
    一个目录里可能有 `.git`、`node_modules`、二进制文件。
    全读进去既慢又脏，而且 `node_modules` 一个目录就能把索引撑爆。
    """
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p = p.resolve()

    if not p.exists():
        raise FileNotFoundError(f"路径不存在：{raw}")

    if p.is_file():
        return [_load_any(p)]

    # 目录：递归收集文本文件，跳过噪音目录与二进制
    skip_dirs = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".pnpm-store",
    }
    # 【白名单从加载器派生，不手写第二份】
    # 之前这里手写了一份，里面写了 `.json` 而加载器不支持它 ——
    # 用户加了 jobs.json，加载被容错吞掉，界面显示已添加、知识库却是空的。
    # **能派生的清单就不要手写第二份**，手写的那份迟早会漂移。
    text_suffixes = supported_suffixes()

    out: list[LoadedDocument] = []
    for f in sorted(p.rglob("*")):
        if not f.is_file():
            continue
        if any(part in skip_dirs for part in f.parts):
            continue
        if f.suffix.lower() not in text_suffixes:
            continue
        # 单文件上限：防止某个巨大的日志文件把索引撑爆
        if f.stat().st_size > 1_000_000:
            logger.debug("跳过过大的文件：%s", f)
            continue
        try:
            out.append(_load_any(f))
        except Exception as exc:
            logger.debug("跳过 %s：%s", f, exc)
    return out


def _load_any(path: Path) -> LoadedDocument:
    """统一入口 —— 用户声明的文档一律按 NOTE 处理。

    【为什么不按后缀猜 doc_type】
    `DocType`（RESUME / JD / NOTE）原本是为求职场景设计的分类，
    而用户加进来的东西是"我的手记""项目文档""参考手册"——
    硬套成 RESUME 或 JD 只会让检索时的 scope 过滤变得莫名其妙。

    全部归到 NOTE（通用文档）是诚实的：我们确实不知道它是什么，
    而**猜错分类比不分类更糟** —— 用户按 scope=resume 过滤时会
    意外命中一堆自己的笔记。
    """
    return load_document(path, DocType.NOTE)


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
