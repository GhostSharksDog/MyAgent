"""RAG 检索增强生成层。

模块划分（按数据流顺序）：

    loaders    文档 → 纯文本（PDF/DOCX/文本，含扫描件检测与质量评估）
    chunker    纯文本 → 带元数据的语义块
    tokenizer  文本 → 词元（中英混合：CJK 单字+双字，拉丁整串保留）
    embedder   文本块 → 稠密向量（TF-IDF 基线，可替换为语义 embedding）
    store      向量存储与相似度检索
    bm25       稀疏检索打分（Okapi BM25）
    fusion     多路排名融合（RRF）
    rerank     精排（特征式 / LLM listwise）
    retriever  两段式管线：宽召回 → RRF 融合 → 精排 → 上下文组装
    evaluate   Recall@k / Precision@k / MRR / NDCG@k，按难度分层
    corpus     数据源装配（简历 + 岗位库 + 笔记）

两段式检索是这一层的核心结构：

    向量检索 ─┐
              ├─ RRF 融合 ─→ 重排 ─→ top-k
    BM25    ─┘
    宽而全                     准而窄

第一段要求快且全（可用可预计算的结构），第二段要求准（能做查询-文档交互）。
"""

from app.rag.bm25 import BM25
from app.rag.chunker import Chunk, ChunkStrategy, chunk_document, chunk_documents
from app.rag.corpus import build_corpus
from app.rag.embedder import Embedder, TfidfEmbedder
from app.rag.evaluate import EvalReport, EvalSet, evaluate
from app.rag.fusion import fuse_rankings, reciprocal_rank_fusion
from app.rag.loaders import DocType, LoadedDocument, LoadError, load_document, normalize_text
from app.rag.rerank import LexicalReranker, LLMReranker, NoOpReranker, Reranker
from app.rag.retriever import RetrievalMode, Retriever
from app.rag.store import SearchHit, VectorStore
from app.rag.tokenizer import tokenize, tokenize_query, tokenize_query_filtered

__all__ = [
    "BM25",
    "Chunk",
    "ChunkStrategy",
    "DocType",
    "Embedder",
    "EvalReport",
    "EvalSet",
    "LLMReranker",
    "LexicalReranker",
    "LoadError",
    "LoadedDocument",
    "NoOpReranker",
    "Reranker",
    "RetrievalMode",
    "Retriever",
    "SearchHit",
    "TfidfEmbedder",
    "VectorStore",
    "build_corpus",
    "chunk_document",
    "chunk_documents",
    "evaluate",
    "fuse_rankings",
    "load_document",
    "normalize_text",
    "reciprocal_rank_fusion",
    "tokenize",
    "tokenize_query",
    "tokenize_query_filtered",
]
