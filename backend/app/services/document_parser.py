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
    """解析 PDF：每页按空行切段，段号在页内重新计数。

    策略：
    1. pypdf — 优先（原生文本 PDF 最快）
    2. pdfplumber — pypdf 文本过少时的 fallback
    3. pdftoppm + pytesseract OCR — 扫描件/图片型 PDF 的终极 fallback

    每层独立尝试：用本层结果覆盖 blocks，最终用累积 chars 判定。
    任意一层抛出异常则静默降级，不影响后续层。
    """
    blocks: list[TextBlock] = []
    total_chars = 0

    # Step 1: pypdf
    try:
        blocks = _parsePdfWithPypdf(content)
        total_chars = sum(len(b.text) for b in blocks)
    except Exception:
        pass

    # Step 2: pdfplumber（对部分文字型 PDF 效果更好）
    if total_chars < 20:
        try:
            pdfplumber_blocks = _parsePdfWithPdfplumber(content)
            # pdfplumber 可能与 pypdf 有重叠内容，用字符数多的那个
            pdfplumber_chars = sum(len(b.text) for b in pdfplumber_blocks)
            if pdfplumber_chars > total_chars:
                blocks = pdfplumber_blocks
                total_chars = pdfplumber_chars
        except Exception:
            pass

    # Step 3: OCR fallback（扫描件/图片型 PDF）
    if total_chars < 20:
        try:
            ocr_blocks = _parsePdfWithOcr(content)
            ocr_chars = sum(len(b.text) for b in ocr_blocks)
            if ocr_chars > total_chars:
                blocks = ocr_blocks
                total_chars = ocr_chars
        except Exception:
            pass

    if total_chars < 20:
        raise DocumentParserError(
            "PDF 解析完成但未提取到足够文本，可能为扫描件或图片型 PDF"
        )
    return blocks


def _parsePdfWithPypdf(content: bytes) -> list[TextBlock]:
    """用 pypdf 解析 PDF。"""
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


def _parsePdfWithPdfplumber(content: bytes) -> list[TextBlock]:
    """用 pdfplumber 解析 PDF（pypdf fallback，处理部分文字型 PDF 更鲁棒）。"""
    import io
    import pdfplumber
    blocks: list[TextBlock] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for pageIdx, page in enumerate(pdf.pages):
            pageText = (page.extract_text() or "").strip()
            if not pageText:
                continue
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


def _parsePdfWithOcr(content: bytes) -> list[TextBlock]:
    """用 pdftoppm + pytesseract 对 PDF 页面做 OCR（处理扫描件/图片型 PDF）。

    依赖：poppler-utils（pdftoppm）、tesseract-ocr、tesseract-ocr-chi-sim。
    """
    import io
    import os
    import shutil
    import subprocess
    import tempfile

    # Render PDF pages to JPEG images using pdftoppm
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "doc.pdf")
        img_prefix = os.path.join(tmpdir, "page")
        with open(pdf_path, "wb") as f:
            f.write(content)
        # -r 150: 150 DPI; -jpeg: output JPEG; -f 1 -l N: first to last page
        result = subprocess.run(
            ["pdftoppm", "-r", "150", "-jpeg", "-f", "1", pdf_path, img_prefix],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise DocumentParserError(f"pdftoppm failed: {result.stderr}")

        # Collect rendered pages
        pages_dir = os.path.join(tmpdir, "pages")
        os.makedirs(pages_dir, exist_ok=True)
        for fname in sorted(os.listdir(tmpdir)):
            if fname.startswith("page-") and fname.endswith(".jpg"):
                shutil.move(os.path.join(tmpdir, fname), pages_dir)

        page_files = sorted(os.listdir(pages_dir))
        if not page_files:
            raise DocumentParserError("pdftoppm produced no page images")

        # OCR each page with pytesseract
        import pytesseract
        blocks: list[TextBlock] = []
        for pageIdx, fname in enumerate(page_files):
            img_path = os.path.join(pages_dir, fname)
            text = pytesseract.image_to_string(img_path, lang="chi_sim+eng", timeout=60)
            pageText = text.strip()
            if not pageText:
                continue
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
