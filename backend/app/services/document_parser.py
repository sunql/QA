"""Phase 5.2 文档解析器（TDD RED 占位）。

解析 PDF/DOCX/MD 文件，提取纯文本供分块使用。
"""

from __future__ import annotations


class DocumentParserError(Exception):
    """解析失败。"""
    pass


async def parse_document(content: bytes, mime_type: str, filename: str) -> str:
    """解析文档内容为纯文本。

    Args:
        content: 文件字节内容
        mime_type: MIME 类型（如 application/pdf）
        filename: 原始文件名（用于扩展名推断）

    Returns:
        提取的纯文本

    Raises:
        DocumentParserError: 解析失败
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if mime_type == "text/plain" or ext in ("txt", "md", "markdown"):
        return content.decode("utf-8", errors="replace")

    if mime_type == "application/pdf" or ext == "pdf":
        return _parse_pdf(content)

    if mime_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ) or ext in ("docx", "doc"):
        return _parse_docx(content)

    raise DocumentParserError(f"Unsupported file type: {mime_type} ({filename})")


def _parse_pdf(content: bytes) -> str:
    """提取 PDF 文本（简单实现）。"""
    try:
        import pypdf

        import io

        reader = pypdf.PdfReader(io.BytesIO(content))
        texts: list[str] = []
        for page in reader.pages:
            texts.append(page.extract_text() or "")
        return "\n".join(texts)
    except Exception as e:
        raise DocumentParserError(f"PDF parsing failed: {e}") from e


def _parse_docx(content: bytes) -> str:
    """提取 DOCX 文本。"""
    try:
        from docx import Document

        import io

        doc = Document(io.BytesIO(content))
        return "\n".join(para.text for para in doc.paragraphs)
    except Exception as e:
        raise DocumentParserError(f"DOCX parsing failed: {e}") from e
