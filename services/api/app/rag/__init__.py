"""RAG 检索增强生成层。

模块划分（按数据流顺序）：
    loaders    文档 → 纯文本（PDF/DOCX/文本，含扫描件检测）
    chunker    纯文本 → 带元数据的语义块      [P2 待实现]
    embedder   文本块 → 向量                  [P2 待实现]
    store      向量存储与相似度检索            [P2 待实现]
    retriever  查询 → 召回 → （重排）→ 上下文  [P2 待实现]
    evaluate   检索质量指标与消融实验          [P2 待实现]
"""

from app.rag.loaders import DocType, LoadedDocument, LoadError, load_document, normalize_text

__all__ = [
    "DocType",
    "LoadError",
    "LoadedDocument",
    "load_document",
    "normalize_text",
]
