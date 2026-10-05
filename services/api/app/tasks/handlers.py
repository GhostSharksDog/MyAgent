"""任务处理器：真正干活的地方。

【本文件最重要的一条纪律：CPU 密集的活必须丢线程池】

把任务"拆到后台"是目的。但如果 worker 在事件循环里直接跑那段 CPU 密集代码，
事件循环照样被阻塞 —— 拆了等于没拆，只是把阻塞点从请求处理挪到了 worker。

所以这里的模式统一是：

    await ctx.report(10, "读语料")          # 上报进度（异步、非阻塞）
    stats = await asyncio.to_thread(work)   # 真正干活（线程池）
    await ctx.report(100, "完成")

判断标准很简单：**这段代码里有没有 await？没有就丢线程池。**
纯 CPU 计算、同步 IO（读文件、压缩、解析）都属于此类。

【为什么 reindex 是最典型的一个任务】
知识库重建要读全部文档 → 切分 → 拟合 TF-IDF → 建 BM25 倒排表。
当前语料下是几百毫秒，看起来无所谓；语料涨到几千块时就是几秒到几十秒。
期间所有 HTTP 请求（包括别人的对话流式响应）都会停摆。
这是"不拆会疼在哪"的具体答案 —— 不是架构洁癖，是明确的可用性问题。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from app.tasks.models import TaskType
from app.tasks.queue import TaskContext, TaskQueue

logger = logging.getLogger(__name__)


# ============================================================
# 重建检索索引
# ============================================================
def _rebuild_index() -> dict[str, Any]:
    """同步的索引重建。**这段代码会被丢进线程池执行。**

    它必须保持为纯同步函数，没有任何 await ——
    只有这样才能被 `asyncio.to_thread` 整体搬到别的线程，
    事件循环在它运行期间保持可调度。
    """
    from app.rag.factory import get_shared_retriever, reset_shared_retriever

    started = time.perf_counter()

    # 先清掉共享实例，确保重新构建而不是复用旧索引。
    # 顺序不能反：若先 get 再 reset，get 会把旧索引填进缓存。
    reset_shared_retriever()
    retriever = get_shared_retriever()
    stats = retriever.stats()

    return {
        "chunk_count": stats.get("chunk_count", 0),
        "dim": stats.get("dim", 0),
        "total_chars": stats.get("total_chars", 0),
        "by_doc_type": stats.get("by_doc_type", {}),
        "mode": stats.get("mode"),
        "reranker": stats.get("reranker"),
        "bm25_vocab": (stats.get("bm25") or {}).get("vocab", 0),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


async def handle_reindex(ctx: TaskContext) -> dict[str, Any]:
    """重建检索索引。"""
    await ctx.report(10, "读取语料并切分")
    # 整个重建过程丢进线程池 —— 期间事件循环继续服务其他请求
    stats = await asyncio.to_thread(_rebuild_index)
    await ctx.report(90, f"索引完成：{stats['chunk_count']} 块")

    if stats["chunk_count"] == 0:
        # 语料为空不是"成功但结果为空"，而是明确的问题：
        # 报成成功会让用户以为索引建好了，然后困惑于检索为什么没结果
        #
        # 【提示必须指向**当前默认形态下真的有效**的做法】
        # 默认（general）profile 不会自动加载 data/resume.md ——
        # 数据源由 AGENT_CORPUS_PATHS 显式声明（见 rag/corpus.py 的说明）。
        # 原来的提示只教用户"放简历 / 跑 ingest.py"，而这两件事在通用形态下
        # 都不改变知识库内容：用户照做、重试、再失败，且**看不出哪里错了**。
        # 一条无效的指引比没有指引更糟 —— 它会让人以为是自己操作得不对。
        raise RuntimeError(
            "语料为空，索引未建立。知识库的数据源需要显式声明："
            "设置 AGENT_CORPUS_PATHS=<文件或目录路径> 后重试；"
            "或用 AGENT_PROFILE=jobhunt 切到求职形态（会加载 data/resume.md 与 seed/jobs.json）"
        )

    await ctx.report(100, "重建完成")
    return stats


# ============================================================
# 解析简历文件
# ============================================================
def _ingest_document(source: str, doc_type: str | None) -> dict[str, Any]:
    """同步的文档解析。同样会被丢进线程池。"""
    from app.rag.loaders import DocType, load_document

    path = Path(source)
    doc = load_document(path, DocType(doc_type) if doc_type else None)

    # 输出路径按类型决定，与 scripts/ingest.py 保持一致
    from app.core.config import PROJECT_ROOT

    target_name = {
        DocType.RESUME: "resume.md",
        DocType.JD: "target_jd.md",
        DocType.NOTE: "note.md",
    }[doc.doc_type]
    out_path = PROJECT_ROOT / "data" / target_name
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header = f"<!-- 来源: {doc.source} | 类型: {doc.doc_type} | 由任务队列生成 -->\n\n"
    out_path.write_text(header + doc.text + "\n", encoding="utf-8", newline="\n")

    return {
        "source": doc.source,
        "doc_type": str(doc.doc_type),
        "char_count": doc.char_count,
        "page_count": doc.page_count,
        "warnings": doc.warnings,
        "output": str(out_path),
    }


async def handle_ingest_resume(ctx: TaskContext) -> dict[str, Any]:
    """解析简历/文档并写入 data/ 目录。"""
    source = str(ctx.arg("source", "") or "")
    if not source:
        raise ValueError("缺少 source 参数（要解析的文件路径）")

    # 【这里刻意**没有**先做 path.exists() 检查】
    # 那是一个阻塞的文件系统调用，放在异步函数里会被 ruff 的 ASYNC240 抓出来 ——
    # 而且它本身是冗余的：`load_document` 已经在 `_ingest_document`
    # 内部（线程池中）检查并抛出信息清晰的 LoadError。
    # 去掉重复检查 = 少一处可能前后不一致的逻辑，也少一次事件循环上的阻塞。
    await ctx.report(20, f"解析 {Path(source).name}")
    result = await asyncio.to_thread(_ingest_document, source, ctx.arg("doc_type"))  # type: ignore[arg-type]

    await ctx.report(80, "已写入 data/ 目录，准备重建索引")
    # 解析完顺手重建索引：否则检索到的还是旧语料，
    # 用户会看到"文件传上去了但搜不到"这种自相矛盾的现象
    stats = await asyncio.to_thread(_rebuild_index)
    await ctx.report(100, "完成")

    return {**result, "index": stats}


# ============================================================
# 批量岗位匹配
# ============================================================
async def handle_batch_match(ctx: TaskContext) -> dict[str, Any]:
    """对多个岗位做匹配度打分。

    这个处理器与上面两个的关键差别：它是**异步 IO 密集**而不是 CPU 密集的。
    每次匹配都是一次 LLM 调用（网络等待），所以不能丢线程池 ——
    丢线程池只会浪费线程，正确做法是并发发起（asyncio.gather）。

    **两类任务用两种不同的处理方式**，这是本文件最值得注意的对比：
      - CPU 密集 → asyncio.to_thread
      - IO 密集  → asyncio.gather 并发
    搞反了要么阻塞事件循环，要么白白浪费线程。
    """
    from app.core.config import get_settings
    from app.llm.client import LLMClient
    from app.llm.types import ChatMessage

    job_ids = list(ctx.arg("job_ids", []) or [])
    resume_text = str(ctx.arg("resume_text", "") or "")
    if not job_ids or not resume_text:
        raise ValueError("缺少 job_ids 或 resume_text 参数")

    settings = get_settings()
    if not settings.llm.is_configured:
        raise RuntimeError("未配置 LLM_API_KEY，无法执行匹配分析")

    from app.tools.builtin import _search_jobs

    jobs: list[dict[str, Any]] = []
    for job_id in job_ids:
        from app.tools.builtin import SearchJobsParams

        result = await asyncio.to_thread(  # 读岗位库是同步 IO
            _search_jobs, SearchJobsParams(keyword=job_id, limit=1)
        )
        if result.ok:
            jobs.append({"job_id": job_id, "text": result.content})

    if not jobs:
        raise ValueError(f"未找到任何岗位：{job_ids}")

    client = LLMClient(settings.llm)
    semaphore = asyncio.Semaphore(4)  # 限制并发，避免触发限流

    async def score_one(job: dict[str, Any], index: int) -> dict[str, Any]:
        async with semaphore:
            prompt = (
                f"简历：\n{resume_text[:2000]}\n\n岗位：\n{job['text'][:1500]}\n\n"
                f"请给出这个候选人与该岗位的匹配度（0-100 的整数）以及一句话理由。"
                f'严格只输出 JSON：{{"score": 整数, "reason": "理由"}}'
            )
            response = await client.chat(
                [ChatMessage.user(prompt)], response_format={"type": "json_object"}
            )
            await ctx.report(
                min(95, 10 + int(85 * (index + 1) / len(jobs))), f"已评估 {index + 1}/{len(jobs)}"
            )
            return {
                "job_id": job["job_id"],
                "raw": (response.message.content or "")[:300],
                "tokens": response.usage.total_tokens,
            }

    try:
        # 并发发起：总耗时 ≈ 最慢的一次，而不是所有次之和
        results = await asyncio.gather(
            *(score_one(job, i) for i, job in enumerate(jobs)), return_exceptions=True
        )
    finally:
        await client.aclose()

    succeeded = [r for r in results if not isinstance(r, Exception)]
    failed = [str(r) for r in results if isinstance(r, Exception)]
    if failed:
        logger.warning("批匹配有 %d 个岗位失败：%s", len(failed), failed[:3])

    return {
        "total": len(jobs),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "results": succeeded,
        "total_tokens": sum(r.get("tokens", 0) for r in succeeded),
    }


# ============================================================
# 注册
# ============================================================
HANDLERS = {
    TaskType.REINDEX: handle_reindex,
    TaskType.INGEST_RESUME: handle_ingest_resume,
    TaskType.BATCH_MATCH: handle_batch_match,
}


def register_default_handlers(queue: TaskQueue) -> list[str]:
    """把全部默认处理器注册到队列上。返回已注册的类型列表。"""
    for task_type, handler in HANDLERS.items():
        queue.register(str(task_type), handler)
    return queue.known_types()
