"""文档解析器：把 PDF/DOCX/MD 解析成带定位信息的文本块。

P0 溯源地基：原实现返回扁平字符串，PDF 的页码与 DOCX 的段落结构在
``"\\n".join(...)`` 处被丢弃，导致下游证据链无出处可填。本实现改为返回
``list[TextBlock]``，把定位符一路带到 Chunk 与 Milvus。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class DocumentParserError(Exception):
    """解析失败。"""
    pass


class UnsupportedFileTypeError(DocumentParserError):
    """文件类型不在支持范围内。

    与「文件损坏」分开：前者换格式即可，后者要换文件。调用方要据此给出
    **不同**的提示，合并成一句会让用户拿着一个损坏的 PDF 反复换扩展名。
    """
    pass


@dataclass(frozen=True)
class TextBlock:
    """带定位信息的最小文本单元。

    定位符语义（各格式不同，下游按此解释）：

    - PDF：``page_number`` 为 1-based 页码，``paragraph_no`` 为**页内**段号
    - DOCX：``paragraph_no`` 为文档内段号，``page_number`` 为 None
    - MD/TXT：``section_name`` 由 ``#`` 标题识别，``paragraph_no`` 为文档内段号
    """

    text: str
    page_number: int | None
    section_name: str | None
    paragraph_no: int | None


# 空行（含仅空白行）分段。PDF 的 extract_text 常在同一段内插换行，
# 按单个 \n 切会把一段切成多块，故只认空行。
_BLANK_LINE_RE = re.compile(r"\n\s*\n")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


async def parse_document(content: bytes, mime_type: str, filename: str) -> list[TextBlock]:
    """解析文档内容为带定位信息的文本块列表。

    Args:
        content: 文件字节内容
        mime_type: MIME 类型（如 application/pdf）
        filename: 原始文件名（用于扩展名推断）

    Returns:
        文本块列表；空文档返回空列表

    Raises:
        UnsupportedFileTypeError: 格式不支持
        DocumentParserError: 文件损坏或解析失败
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if mime_type == "text/plain" or ext in ("txt", "md", "markdown"):
        return _parsePlainText(content.decode("utf-8", errors="replace"))

    if mime_type == "application/pdf" or ext == "pdf":
        return _parsePdf(content)

    # 只认 docx（OOXML 包），**不含**老式二进制 ``.doc``：python-docx 读不了
    # 后者，会抛 ``Package not found`` —— 那是个误导性的「解析失败」，真相是
    # 「这种格式根本不支持」。
    if mime_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ) or ext == "docx":
        return _parseDocx(content)

    raise UnsupportedFileTypeError(f"Unsupported file type: {mime_type} ({filename})")


def _splitParagraphs(text: str) -> list[str]:
    """按空行切段；无空行时整段作为一段。"""
    return [p.strip() for p in _BLANK_LINE_RE.split(text) if p.strip()]


def _parsePlainText(text: str) -> list[TextBlock]:
    """解析 MD/TXT：``#`` 标题开章节，标题本身保留为块。"""
    blocks: list[TextBlock] = []
    section: str | None = None
    paraNo = 0
    for para in _splitParagraphs(text):
        heading = _MD_HEADING_RE.match(para)
        if heading:
            section = heading.group(2)
        paraNo += 1
        blocks.append(
            TextBlock(
                text=para, page_number=None, section_name=section, paragraph_no=paraNo
            )
        )
    return blocks


def _parsePdf(content: bytes) -> list[TextBlock]:
    """解析 PDF：每页按空行切段，段号在页内重新计数。"""
    try:
        import io

        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(content))
        blocks: list[TextBlock] = []
        for pageIdx, page in enumerate(reader.pages):
            pageText = page.extract_text() or ""
            paraNo = 0
            for para in _splitParagraphs(pageText):
                paraNo += 1
                blocks.append(
                    TextBlock(
                        text=para,
                        page_number=pageIdx + 1,
                        section_name=None,
                        paragraph_no=paraNo,
                    )
                )
        return blocks
    except Exception as e:
        raise DocumentParserError(f"PDF parsing failed: {e}") from e


def _isHeadingParagraph(para: object) -> bool:
    """判定段落是否为标题：直读 ``w:pStyle`` 的样式 ID。

    为什么不用 ``para.style.name``：后者要先解析文档的 ``word/styles.xml``
    部件，部件缺失或样式表引用不完整时 python-docx 会把**所有**段落一律报成
    ``Normal``，标题被静默漏掉（已实测）。``w:pStyle/@w:val`` 是样式 ID，
    任何合乎规范的 DOCX 都带，不受样式表能否解析影响。

    用 ``para._p`` 是有意为之：python-docx 没有公开 ``pStyle`` 的访问器，
    这是社区通行的下钻方式。

    已知限制：中文版 Word 用数字样式 ID（``1``/``2``/``3`` 对应标题 1/2/3），
    本实现只认 ``Heading<n>`` 形式，这类文档的 ``section_name`` 会退化为
    None（页码与段号不受影响）。该限制记入变更记录，不在 P0 处理。
    """
    from docx.oxml.ns import qn

    pPr = para._p.find(qn("w:pPr"))  # type: ignore[attr-defined]
    if pPr is None:
        return False
    pStyle = pPr.find(qn("w:pStyle"))
    if pStyle is None:
        return False
    styleId = pStyle.get(qn("w:val")) or ""
    return styleId.lower().startswith("heading")


def _parseDocx(content: bytes) -> list[TextBlock]:
    """解析 DOCX：标题段落开章节，段号在文档内递增。"""
    try:
        import io

        from docx import Document

        doc = Document(io.BytesIO(content))
        blocks: list[TextBlock] = []
        section: str | None = None
        paraNo = 0
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            if _isHeadingParagraph(para):
                section = text
            paraNo += 1
            blocks.append(
                TextBlock(
                    text=text,
                    page_number=None,
                    section_name=section,
                    paragraph_no=paraNo,
                )
            )
        return blocks
    except Exception as e:
        raise DocumentParserError(f"DOCX parsing failed: {e}") from e
