"""RAG 检索服务的独立进程入口。

【为什么要把检索拆成独立服务 —— 三个具体理由，不是架构洁癖】

1. **资源画像完全不同**。agent 服务是 **IO 密集**（绝大部分时间在等模型返回），
   RAG 服务是 **CPU 密集**（TF-IDF/BM25/重建索引都是纯计算）。
   放在同一个进程里，重建索引那几秒会直接抢走 agent 的 CPU ——
   表现为"别人正在对话时页面卡住"。这是真实发生过的现象（见任务队列的实测：
   reindex 耗时 1894ms，同期请求的 P95 会被显著拉高）。

2. **伸缩比不同**。对话高峰需要更多 agent 副本，索引重建/批量检索高峰需要更多
   RAG 副本。绑在一个 Deployment 里就只能一起扩，代价是白花一倍的资源。

3. **故障隔离**。RAG 服务崩溃（索引损坏、内存不足）不该让整个对话服务不可用。
   拆开之后 agent 可以降级到"本轮不检索"，而不是整体 500。

【为什么现在用"同一代码库 + 独立入口"而不是独立仓库/独立包】

拆分的第一步不该是"把代码搬走" —— 那会同时引入部署复杂度与代码同步问题，
一旦出问题，你无法判断是"拆分本身有问题"还是"搬运时漏了什么"。

正确顺序是：**先让它在独立进程里跑起来（进程边界）→ 再让它走网络调用
（接口边界）→ 最后才搬代码（仓库边界）**。
本模块完成的是第二步：同一个镜像、不同入口、通过 HTTP 通信。

代价很明确：两边共享 `app.rag` 的代码，还不是真正的独立部署单元。
这是**已知的技术债**，升级路径写在这里，避免以后有人误以为已经拆干净了。

启动：
    uvicorn app.rag_service.main:app --host 0.0.0.0 --port 8001 --app-dir services/api
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.logging import setup_logging
from app.core.telemetry import METRICS, get_trace_id, set_trace_id
from app.rag.corpus import EMPTY_CORPUS_HINT
from app.rag.factory import get_shared_retriever, reset_shared_retriever
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ============================================================
# 接口模型
# ============================================================
class RetrieveRequest(BaseModel):
    """检索请求。

    【接口设计原则：粗粒度，不要细粒度】
    这里**刻意**不暴露 encode / chunk / bm25_score 这类细粒度操作，
    只提供一个"给查询、还结果"的粗接口。

    原因是网络边界的每一次调用都是一次往返。如果按"chunk → encode → search"
    拆成三个接口，一次检索就是 3 次网络往返 —— 延迟翻三倍，
    而且中间状态要在网络上传输（向量比文本大得多）。

    **跨进程接口应当传输"业务意图"而不是"实现步骤"。**
    这是分布式设计里最常被违反、代价也最大的一条。
    """

    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=50)
    # 元数据过滤：scope 用字符串而不是 DocType 枚举，
    # 这样调用方不需要与 RAG 服务的内部枚举保持同步
    doc_types: list[str] | None = None
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    max_chars: int = Field(default=4000, gt=0)


class RetrieveHit(BaseModel):
    chunk_id: str
    doc_id: str
    doc_type: str
    section: str
    citation: str
    text: str
    score: float
    rank: int


class RetrieveResponse(BaseModel):
    hits: list[RetrieveHit] = Field(default_factory=list)
    trace_id: str = ""
    elapsed_ms: int = 0


class ReindexResponse(BaseModel):
    chunk_count: int
    dim: int
    total_chars: int
    elapsed_ms: int


# ============================================================
# 应用
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger.info("RAG 检索服务启动")
    # 不在启动时建索引：语料可能还没准备好，而且启动不该被数据准备拖慢。
    # 第一次检索请求会触发懒加载（见 rag/factory.py）。
    yield
    logger.info("RAG 检索服务退出")


app = FastAPI(
    title="Legacy RAG Service",
    description="独立的检索服务：向量 + BM25 混合召回、重排、索引重建",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """沿用上游 trace id。

    网关 → agent → rag 三段链路能串成一条，靠的就是每个服务都
    **接受并透传**上游的 X-Trace-Id。任一环节自己生成新 id，链路就断了。
    """
    set_trace_id(request.headers.get("X-Trace-Id"))
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Trace-Id"] = get_trace_id()
    METRICS.observe("legacy_rag_request_ms", (time.perf_counter() - started) * 1000)
    return response


@app.get("/healthz", summary="健康检查")
async def healthz() -> dict[str, object]:
    retriever = get_shared_retriever()
    stats = retriever.stats()
    return {
        "status": "ok",
        "service": "legacy-rag",
        "chunks": stats.get("chunk_count", 0),
        "mode": stats.get("mode"),
        "reranker": stats.get("reranker"),
    }


@app.post("/retrieve", response_model=RetrieveResponse, summary="检索")
async def retrieve(payload: RetrieveRequest) -> RetrieveResponse:
    started = time.perf_counter()
    retriever = get_shared_retriever()

    if len(retriever.chunks) == 0:
        # 空语料不是 500：它是一个**可预期的业务状态**。
        # 返回 200 + 空结果会让调用方以为是"没找到"，而实际上是"还没准备数据"——
        # 两者的处理方式完全不同（后者需要引导用户去准备数据）。
        raise HTTPException(
            status_code=503,
            detail=EMPTY_CORPUS_HINT,
        )

    hits = await retriever.aretrieve(
        payload.query,
        k=payload.k,
        doc_types=payload.doc_types,
        min_score=payload.min_score,
    )

    return RetrieveResponse(
        hits=[
            RetrieveHit(
                chunk_id=h.chunk.id,
                doc_id=h.chunk.doc_id,
                doc_type=str(h.chunk.doc_type),
                section=h.chunk.section,
                citation=h.chunk.citation,
                text=h.chunk.text,
                score=h.score,
                rank=h.rank,
            )
            for h in hits
        ],
        trace_id=get_trace_id(),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


@app.post("/context", summary="检索并组装上下文")
async def context(payload: RetrieveRequest) -> dict[str, str]:
    """直接返回渲染好的上下文文本。

    为什么单独一个端点：agent 侧真正需要的就是"可塞进提示词的一段文本"。
    让它自己去拼 hits 会让引用格式与字符预算的规则散落在两处，
    迟早不一致。**规则的归属应该跟着数据走。**
    """
    started = time.perf_counter()
    retriever = get_shared_retriever()
    if len(retriever.chunks) == 0:
        raise HTTPException(status_code=503, detail=EMPTY_CORPUS_HINT)

    text = await retriever.aretrieve_context(
        payload.query,
        k=payload.k,
        doc_types=payload.doc_types,
        min_score=payload.min_score,
        max_chars=payload.max_chars,
    )
    return {
        "context": text,
        "trace_id": get_trace_id(),
        "elapsed_ms": str(int((time.perf_counter() - started) * 1000)),
    }


@app.post("/reindex", response_model=ReindexResponse, summary="重建索引")
async def reindex() -> ReindexResponse:
    """重建索引。

    【为什么这里用同步接口而不是走任务队列】
    它是 CPU 密集且可能耗时的操作，按理该走异步任务。但 RAG 服务本身
    就是**专门用来跑这类活的进程** —— 在这里同步执行不会阻塞 agent 服务，
    也就没有了"阻塞请求路径"的问题。

    加任务队列反而复杂：调用方要轮询、要处理任务记录、
    还要跨服务传任务 id。**拆服务已经解决了资源隔离问题，
    就不该再叠一层异步** —— 每一层抽象都要有它解决的问题。
    """
    started = time.perf_counter()
    reset_shared_retriever()
    retriever = get_shared_retriever()
    stats = retriever.stats()

    if stats.get("chunk_count", 0) == 0:
        raise HTTPException(
            status_code=422,
            detail=EMPTY_CORPUS_HINT,
        )

    return ReindexResponse(
        chunk_count=stats.get("chunk_count", 0),
        dim=stats.get("dim", 0),
        total_chars=stats.get("total_chars", 0),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


@app.get("/metrics", summary="Prometheus 指标")
async def metrics() -> Response:
    return Response(
        content=METRICS.render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
