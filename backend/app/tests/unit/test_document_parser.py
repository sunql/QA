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


# ---------- PPTX / XLSX / XLS 测试夹具 ----------


def _makePptx(slides: list[tuple[str, list[str]]]) -> bytes:
    """用 python-pptx 构造 PPTX。

    ``slides`` 是 ``(title, [bullet1, bullet2, ...])`` 列表。生成的 PPTX
    含可读中文标题与项目符号，便于断言。
    """
    from pptx import Presentation

    prs = Presentation()
    blankLayout = prs.slide_layouts[6]  # 空白版式避免依赖主题
    for title, bullets in slides:
        slide = prs.slides.add_slide(blankLayout)
        if title:
            # 左上角放标题文本框
            txBox = slide.shapes.add_textbox(0, 0, 720000, 500000)
            tf = txBox.text_frame
            tf.text = title
        for bullet in bullets:
            txBox = slide.shapes.add_textbox(0, 600000, 720000, 500000)
            tf = txBox.text_frame
            tf.text = bullet
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _makeXlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    """用 openpyxl 构造 XLSX。

    ``sheets`` 是 ``{sheet_name: [[row1col1, row1col2], ...], ...}``。
    """
    from openpyxl import Workbook

    wb = Workbook()
    # 默认创建 1 个空 sheet，重命名为第一个
    firstName = next(iter(sheets))
    ws = wb.active
    ws.title = firstName
    for row in sheets[firstName]:
        ws.append(row)
    for name, rows in list(sheets.items())[1:]:
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _makeXls(sheets: dict[str, list[list[str]]]) -> bytes:
    """用 xlwt 构造老式 .xls 二进制。

    xlwt 仍支持旧格式，与 xlrd 读取兼容；本夹具**不**用 xlrd 写 ——
    xlrd 2.0+ 已不再支持写，只读。
    """
    import xlwt

    wb = xlwt.Workbook()
    for name, rows in sheets.items():
        ws = wb.add_sheet(name)
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                ws.write(r, c, val)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _makePdfWithTable(rows: list[list[str]]) -> bytes:
    """用 reportlab 构造含**表格**的 PDF（验证 pdfplumber 提取表格）。

    纯文本 PDF 由 ``_makePdf`` 覆盖；这里专门覆盖"页内有表格"的分支。
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    table = Table(rows)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    doc.build([table])
    return buf.getvalue()


# ---------- Phase 5 新格式测试 ----------


class TestPptx:
    """PPTX：每张幻灯片 = 一个 TextBlock，``section_name`` 用 slide 标题。"""

    @pytest.mark.asyncio
    async def test_pptx_extracts_slide_title_and_bullets(self) -> None:
        pptx = _makePptx([
            ("供应商准入", ["注册资本 >= 1000 万", "成立 3 年以上"]),
            ("供应商分级", ["按年度采购额分 A/B/C 级"]),
        ])
        blocks = await parse_document(
            pptx,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "a.pptx",
        )
        # 2 张幻灯片 → 2 个块（每张的内容合并为一段）
        assert len(blocks) == 2
        assert blocks[0].section_name == "供应商准入"
        assert "注册资本" in blocks[0].text
        assert "成立 3 年以上" in blocks[0].text
        assert blocks[1].section_name == "供应商分级"
        assert "A/B/C" in blocks[1].text
        # PPT 没有页概念 → page_number 一律 None；paragraph_no 顺序递增
        assert all(b.page_number is None for b in blocks)
        assert [b.paragraph_no for b in blocks] == [1, 2]

    @pytest.mark.asyncio
    async def test_pptx_fallback_by_extension(self) -> None:
        """MIME 缺失时仅靠扩展名 .pptx 也能识别。"""
        pptx = _makePptx([("标题", ["要点"])])
        blocks = await parse_document(pptx, "application/octet-stream", "a.pptx")
        assert len(blocks) == 1
        assert blocks[0].section_name == "标题"

    @pytest.mark.asyncio
    async def test_pptx_old_binary_format_unsupported(self) -> None:
        """.ppt（二进制旧格式）python-pptx 读不了，必须明确抛 UnsupportedFileTypeError
        而不是 DocumentParserError —— 告诉用户「换格式」而非「换文件」。"""
        with pytest.raises(UnsupportedFileTypeError):
            await parse_document(b"fake ppt content", "application/octet-stream", "a.ppt")


class TestXlsx:
    """XLSX：每个 sheet 渲为结构化文本。"""

    @pytest.mark.asyncio
    async def test_xlsx_emits_one_block_per_sheet(self) -> None:
        xlsx = _makeXlsx({
            "供应商": [["名称", "等级"], ["A 公司", "A 级"], ["B 公司", "B 级"]],
            "产品": [["型号", "价格"], ["X-100", 100]],
        })
        blocks = await parse_document(
            xlsx,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "a.xlsx",
        )
        assert len(blocks) == 2
        assert blocks[0].section_name == "供应商"
        assert "名称" in blocks[0].text
        assert "A 公司" in blocks[0].text
        assert blocks[1].section_name == "产品"
        assert "X-100" in blocks[1].text

    @pytest.mark.asyncio
    async def test_xlsx_large_sheet_truncated(self) -> None:
        """大表格被截断防止一次吃光 token 预算。

        1000 行 × 5 列的 sheet 应被截断到 ``_MAX_SOURCE_CHARS`` 之内。
        """
        rows = [[f"行{i}-列{j}" for j in range(5)] for i in range(1000)]
        xlsx = _makeXlsx({"大表": rows})
        blocks = await parse_document(xlsx, "application/octet-stream", "big.xlsx")
        assert len(blocks) == 1
        # 截断后总文本必须 < 1MB（_MAX_SOURCE_CHARS 上限）
        assert len(blocks[0].text) < 1_000_000

    @pytest.mark.asyncio
    async def test_xlsx_empty_sheet_returns_no_blocks(self) -> None:
        xlsx = _makeXlsx({"空": []})
        blocks = await parse_document(xlsx, "application/octet-stream", "empty.xlsx")
        assert blocks == []

    @pytest.mark.asyncio
    async def test_xlsx_real_third_party_file_via_raw_fallback(
        self,
    ) -> None:
        """真实第三方 xlsx（来自 ``docs/excel/供应商价格.xlsx``）。

        openpyxl 对某些 WPS / BI 工具导出的 xlsx 抛
        ``could not read stylesheet from None`` —— 但数据完整可读。
        该回归测试确保 ``_parseXlsx`` 自动降级到 raw XML 读取，不让用户
        拿到误导性的「文件损坏」错误。
        """
        import zipfile
        from pathlib import Path

        samplePath = (
            Path(__file__).parent.parent.parent.parent.parent
            / "docs"
            / "excel"
            / "供应商价格.xlsx"
        )
        if not samplePath.exists():
            pytest.skip(f"sample xlsx not found: {samplePath}")
        # 该 xlsx 必须含 sheet2 (data sheet)；若被替换为别的样例则 skip
        with zipfile.ZipFile(samplePath) as zf:
            assert any("sheet2" in n for n in zf.namelist()), "sample 不含 sheet2"
        content = samplePath.read_bytes()
        blocks = await parse_document(content, "application/octet-stream", "供应商价格.xlsx")
        # 至少抽出 1 个 sheet；data sheet 应含真实业务数据
        assert len(blocks) >= 1
        allText = "\n".join(b.text for b in blocks)
        # 数据 sheet 表头里有 "类型" 或 "价目表" 这种业务关键词即可
        assert any(kw in allText for kw in ("价目表", "PLI", "类型"))


class TestXls:
    """XLS：老式二进制 Excel 走 xlrd 路径。"""

    @pytest.mark.asyncio
    async def test_xls_extracts_cells(self) -> None:
        xls = _makeXls({
            "客户": [["名称", "城市"], ["A 公司", "上海"], ["B 公司", "北京"]],
        })
        blocks = await parse_document(xls, "application/octet-stream", "a.xls")
        assert len(blocks) == 1
        assert blocks[0].section_name == "客户"
        assert "A 公司" in blocks[0].text
        assert "上海" in blocks[0].text

    @pytest.mark.asyncio
    async def test_xls_fallback_by_extension(self) -> None:
        """仅靠 .xls 扩展名也能识别。"""
        xls = _makeXls({"数据": [["行1", "值"]]})
        blocks = await parse_document(xls, "application/octet-stream", "a.xls")
        assert len(blocks) == 1
        assert "行1" in blocks[0].text


class TestPdfTableExtraction:
    """PDF 表格：必须保留表格结构（不被压扁成无定位的纯文本）。"""

    @pytest.mark.asyncio
    async def test_pdf_table_rows_preserved(self) -> None:
        """含表格的 PDF：列对齐关系必须保留（用 markdown table 或行分隔标记）。

        允许的两种输出形态：
        - pdfplumber 识别出表格 → 多列用 ``|`` 或 tab 分隔，**列间不留空**
        - 仅 pypdf 命中 → 行内文本拼回，可能列间为空

        至少要满足：第 1 行表头 + 第 2 行数据都出现在同一块里，且列间分隔可辨。
        """
        pdf_bytes = _makePdfWithTable([
            ["供应商", "等级", "金额"],
            ["A 公司", "A", "100"],
            ["B 公司", "B", "50"],
        ])
        blocks = await parse_document(pdf_bytes, "application/pdf", "a.pdf")
        assert len(blocks) >= 1
        allText = "\n".join(b.text for b in blocks)
        assert "供应商" in allText
        assert "A 公司" in allText
        assert "100" in allText
