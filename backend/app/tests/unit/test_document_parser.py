"""document_parser 定位符单测（P0 溯源地基）。"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.services.document_parser import (
    DocumentParserError,
    TextBlock,
    UnsupportedFileTypeError,
    parse_document,
)


class TestPlainText:
    """MD / TXT：段落 + 章节识别。"""

    @pytest.mark.asyncio
    async def test_plain_text_utf8(self) -> None:
        blocks = await parse_document(b"hello world", "text/plain", "a.txt")
        assert len(blocks) == 1
        assert blocks[0].text == "hello world"
        assert blocks[0].page_number is None
        assert blocks[0].paragraph_no == 1
        assert blocks[0].section_name is None

    @pytest.mark.asyncio
    async def test_markdown_sections(self) -> None:
        md = "# 第一章\n\n第一段内容。\n\n## 1.1 小节\n\n第二段内容。"
        blocks = await parse_document(md.encode("utf-8"), "text/markdown", "a.md")
        # 标题本身也保留为块（不丢文本），且携带它开启的 section
        assert [b.text for b in blocks] == ["# 第一章", "第一段内容。", "## 1.1 小节", "第二段内容。"]
        assert blocks[0].section_name == "第一章"
        assert blocks[1].section_name == "第一章"
        assert blocks[2].section_name == "1.1 小节"
        assert blocks[3].section_name == "1.1 小节"
        assert [b.paragraph_no for b in blocks] == [1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_blank_lines_separate_paragraphs(self) -> None:
        blocks = await parse_document(b"a\n\n\nb", "text/plain", "a.txt")
        assert [b.text for b in blocks] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_list(self) -> None:
        blocks = await parse_document(b"   \n\n  ", "text/plain", "a.txt")
        assert blocks == []


class TestUnsupported:
    """不支持的类型与损坏文件必须可区分。"""

    @pytest.mark.asyncio
    async def test_unsupported_type_raises(self) -> None:
        with pytest.raises(UnsupportedFileTypeError):
            await parse_document(b"x", "application/x-msdownload", "a.exe")

    @pytest.mark.asyncio
    async def test_corrupt_pdf_raises_document_parser_error(self) -> None:
        with pytest.raises(DocumentParserError) as excinfo:
            await parse_document(b"not a pdf", "application/pdf", "a.pdf")
        assert not isinstance(excinfo.value, UnsupportedFileTypeError)


class TestPdf:
    """PDF：每页一段块，段号在页内重新计数。"""

    @pytest.mark.asyncio
    async def test_pdf_page_numbers_are_1_based_and_continuous(self) -> None:
        pdf_bytes = _makePdf(["第一页内容", "第二页内容"])
        blocks = await parse_document(pdf_bytes, "application/pdf", "a.pdf")
        assert [b.page_number for b in blocks] == [1, 2]
        assert [b.paragraph_no for b in blocks] == [1, 1]
        assert [b.text for b in blocks] == ["第一页内容", "第二页内容"]

    @pytest.mark.asyncio
    async def test_pdf_fallback_by_extension(self) -> None:
        pdf_bytes = _makePdf(["内容"])
        blocks = await parse_document(pdf_bytes, "application/octet-stream", "a.pdf")
        assert len(blocks) == 1
        assert blocks[0].page_number == 1


class TestDocx:
    """DOCX：段号在文档内递增，Heading 样式开章节。"""

    @pytest.mark.asyncio
    async def test_docx_paragraph_numbers(self) -> None:
        docx_bytes = _makeDocx(["第一章", "正文一", "正文二"])
        blocks = await parse_document(
            docx_bytes,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "a.docx",
        )
        assert [b.text for b in blocks] == ["第一章", "正文一", "正文二"]
        assert [b.paragraph_no for b in blocks] == [1, 2, 3]
        assert all(b.page_number is None for b in blocks)

    @pytest.mark.asyncio
    async def test_docx_heading_sets_section(self) -> None:
        docx_bytes = _makeDocx(["概述", "正文内容"], headingIndexes={0})
        blocks = await parse_document(docx_bytes, "application/octet-stream", "a.docx")
        assert blocks[0].section_name == "概述"
        assert blocks[1].section_name == "概述"

    @pytest.mark.asyncio
    async def test_docx_style_id_heading_without_styles_part(self) -> None:
        """真实 Word 文件的样式解析依赖 styles.xml；本夹具不含该部件，
        python-docx 会把所有段落都报成 Normal。因此 heading 判定不能只看
        ``para.style.name``，必须直读 ``w:pStyle``（见 _parseDocx）。"""
        docx_bytes = _makeDocx(["第一章", "正文"], headingIndexes={0})
        blocks = await parse_document(docx_bytes, "application/octet-stream", "a.docx")
        assert blocks[0].section_name == "第一章"


class TestTextBlockImmutability:
    """TextBlock 必须是 frozen（项目不可变数据约束）。"""

    def test_textblock_is_frozen(self) -> None:
        block = TextBlock(text="x", page_number=None, section_name=None, paragraph_no=1)
        with pytest.raises(Exception):
            block.text = "y"  # type: ignore[misc]


# ---------- 测试夹具：构最小 PDF / DOCX ----------


def _makePdf(pageTexts: list[str]) -> bytes:
    """用 reportlab 生成多页 PDF（每页一行文字）。

    不用手搓 PDF 字节：xref 偏移量极易写错，而且写错之后 pypdf 读到的是
    **0 页**而非报错 —— 测试会以「空结果」的形式静默通过。reportlab 是
    pyproject.toml 里已声明的正式依赖。

    ⚠️ **必须显式指定 CJK 字体。** reportlab 默认的 Helvetica 编不了中文，
    它不报错，而是把每个汉字替换成一个豆腐块 —— pypdf 抽出来是 `■■■■■`。
    于是「夹具画中文、断言比中文」两个动作互相矛盾，任何 `_parsePdf` 实现
    都过不了这条用例，而失败信息还完全指向不到字体上。
    `STSong-Light` 是 reportlab 自带的 CID 字体，不需要额外依赖或字体文件。
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for text in pageTexts:
        # 每页都要重设：showPage() 会重置字体状态，只在循环外设一次不够。
        c.setFont("STSong-Light", 14)
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def _makeDocx(paragraphs: list[str], headingIndexes: set[int] | None = None) -> bytes:
    """手工构造最小 DOCX（OOXML 包）。

    **不含 word/styles.xml** —— 这是有意的：真实 Word 文件一定带该部件，
    但若 _parseDocx 依赖 ``para.style.name`` 判标题，本夹具会因样式解析失
    败把每段都报成 Normal，从而把这个错误依赖暴露出来。直读 ``w:pStyle``
    则在有/无 styles.xml 两种情况下都成立。
    """
    headingIndexes = headingIndexes or set()
    bodyParts: list[str] = []
    for i, text in enumerate(paragraphs):
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        style = (
            '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            if i in headingIndexes
            else ""
        )
        bodyParts.append(
            f'<w:p>{style}<w:r><w:t xml:space="preserve">{escaped}</w:t></w:r></w:p>'
        )
    documentXml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(bodyParts)}</w:body></w:document>"
    )
    contentTypes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", contentTypes)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", documentXml)
    return buf.getvalue()
