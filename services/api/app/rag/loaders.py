"""RAG 数据接入层：把各种格式的文档变成干净的纯文本。

【为什么单独成层】
RAG 的效果上限由数据质量决定。文档解析这一步做不好，后面无论换多好的
embedding 模型、多贵的 rerank 都救不回来——因为"垃圾进，垃圾出"。

常见的解析坑（按踩坑频率排序）：
1. **PDF 是排版格式，不是文本格式**。它只记录"某个字画在某个坐标"，
   没有段落、没有换行语义。双栏简历尤其容易被解析成"左右两栏交错"的乱序文本。
2. **换行符不一定是段落边界**。PDF 里视觉上的每一行都可能产生一个 \n，
   直接按 \n 切分会把一句话切成七八块。
3. **空白字符污染**。PDF 解析常产生大量连续空格和空行，会浪费 token。
4. **扫描件没有文字层**。纯图片 PDF 用任何文本提取器都拿不到内容，必须走 OCR。

本模块的处理策略：先提取，再**规范化空白**，并保留可检测的行结构信息，
让上层切分器能做出更聪明的判断。
"""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class DocType(StrEnum):
    """文档类型。不同类型在切分和检索时策略不同。"""

    RESUME = "resume"  # 简历：按章节切，结构性强
    JD = "jd"  # 岗位描述：按条目切
    NOTE = "note"  # 笔记/其他：通用递归切分


class LoadedDocument(BaseModel):
    """加载后的文档。text 是规范化后的纯文本，可直接进入切分流程。"""

    source: str = Field(description="来源标识（通常是文件名）")
    doc_type: DocType = DocType.NOTE
    text: str
    char_count: int = 0
    page_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    # 从源头带下来的结构化元数据（如岗位的公司/城市/薪资）。
    # 会一路传到 Chunk.metadata，供检索时做元数据过滤与结果展示。
    metadata_hint: dict[str, str] = Field(default_factory=dict)

    def model_post_init(self, _ctx: object) -> None:
        if not self.char_count:
            self.char_count = len(self.text)


class LoadError(RuntimeError):
    """文档加载失败。比通用异常更好捕获，也便于上层给出可读提示。"""


# ============================================================
# 文本规范化
# ============================================================
def normalize_text(raw: str) -> str:
    """把解析出的原始文本整理成干净形式。

    做五件事，每件都针对一个具体的解析问题：
    1. **剥掉 HTML 注释**。摄取脚本会在文件头写入来源信息；如果让注释留在正文里，
       它会变成一块可被检索到的"内容" —— 元数据污染检索是很隐蔽的质量问题
       （检索出一条只有 `<!-- 来源: xxx -->` 的块，用户和模型都会被误导）。
    2. 统一换行符（PDF 解析可能混入 \\r\\n 或单独的 \\r）
    3. 去掉行尾空白（PDF 常在行尾留大量空格）
    4. 压缩连续空格（保留单个空格，不破坏词间距）
    5. 压缩 3 个以上连续空行到 2 个（段间距），保留段落感
    """
    text = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ============================================================
# 各格式加载器
# ============================================================
def load_pdf(path: Path) -> tuple[str, int, list[str]]:
    """提取 PDF 文本。返回 (文本, 页数, 警告列表)。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise LoadError("需要安装 pypdf：pip install pypdf") from exc

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise LoadError(f"无法打开 PDF（可能已加密或损坏）：{exc}") from exc

    warnings: list[str] = []
    if reader.is_encrypted:
        raise LoadError("PDF 已加密，无法提取文本")

    pages: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            pages.append(page.extract_text() or "")
        except Exception as exc:
            warnings.append(f"第 {i} 页提取失败：{exc}")
            pages.append("")

    text = "\n\n".join(pages)

    # 扫描件检测：有页面但几乎没文字 → 是图片 PDF，必须走 OCR
    if len(pages) > 0 and len(text.strip()) < 20 * len(pages):
        warnings.append(
            "提取到的文字极少，该 PDF 很可能是扫描件（图片）。"
            "需要 OCR 才能提取，请改用文字版 PDF 或先做 OCR。"
        )

    return text, len(reader.pages), warnings


def load_docx(path: Path) -> tuple[str, int, list[str]]:
    """提取 Word 文档文本（含段落与表格）。"""
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise LoadError("需要安装 python-docx：pip install python-docx") from exc

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise LoadError(f"无法打开 DOCX：{exc}") from exc

    parts: list[str] = [p.text for p in document.paragraphs if p.text.strip()]

    # 简历里的技能、经历常用表格排版，不提取表格会丢大量信息
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    return "\n".join(parts), 0, []


def load_plain(path: Path) -> tuple[str, int, list[str]]:
    """读取纯文本 / Markdown。"""
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return path.read_text(encoding=encoding), 0, []
        except UnicodeDecodeError:
            continue
    raise LoadError(f"无法识别 {path.name} 的文本编码（尝试过 utf-8 / gbk）")


_SUPPORTED: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",
    ".md": "plain",
    ".txt": "plain",
    ".markdown": "plain",
}


def load_document(path: Path | str, doc_type: DocType | None = None) -> LoadedDocument:
    """统一的加载入口，按扩展名分发。

    Args:
        path: 文档路径
        doc_type: 显式指定类型；不传则根据文件名猜测（含"简历"→RESUME，含"jd"→JD）
    """
    p = Path(path)
    if not p.exists():
        raise LoadError(f"文件不存在：{p}")
    if not p.is_file():
        raise LoadError(f"不是文件：{p}")

    kind = _SUPPORTED.get(p.suffix.lower())
    if kind is None:
        raise LoadError(f"不支持的格式 {p.suffix!r}。当前支持：{', '.join(sorted(_SUPPORTED))}")

    loaders = {"pdf": load_pdf, "docx": load_docx, "plain": load_plain}
    raw, pages, warnings = loaders[kind](p)
    text = normalize_text(raw)

    if not text:
        raise LoadError(f"{p.name} 提取到的文本为空（可能是扫描件或空文档）")

    if doc_type is None:
        name = p.name.lower()
        if "简历" in p.name or "resume" in name or "cv" in name:
            doc_type = DocType.RESUME
        elif "jd" in name or "岗位" in p.name or "job" in name:
            doc_type = DocType.JD
        else:
            doc_type = DocType.NOTE

    logger.info("已加载 %s：%d 字符，%d 页，格式 %s", p.name, len(text), pages, kind)

    return LoadedDocument(
        source=p.name,
        doc_type=doc_type,
        text=text,
        page_count=pages,
        warnings=warnings,
    )
