"""通用预览渲染的守卫（需求 4）。

判据要点：
1. **PDF 不渲染**（走原件直出，浏览器原生查看器）⇒ renderer 对 PDF 不产出 HTML；
2. Office → 服务端渲染**结构**（docx 标题/段落、xlsx 每个 sheet、pptx 每页）；
3. 用户 HTML → iframe **sandbox** 包裹 + 脚本已剥；
4. Markdown → 渲染后仍经过消毒；
5. 渲染失败（坏文件）⇒ **退回纯文本**，不 500。
"""

from __future__ import annotations

import io

from app.services.document_preview_renderer import (
    renderHtml,
    renderMarkdown,
    renderOffice,
    renderPreview,
    renderText,
)


def test_textPreviewEscapesAndKeepsLines() -> None:
    result = renderText(b"line1\nline2 <b>")

    assert "<pre>" in result.html
    assert "line1\nline2" in result.html
    assert "&lt;b&gt;" in result.html


def test_markdownPreviewRendersAndSanitizes() -> None:
    result = renderMarkdown("# 标题\n\n正文\n\n<script>alert(1)</script>".encode())

    assert "<h1>" in result.html
    assert "正文" in result.html
    assert "<script" not in result.html


def test_htmlPreviewIsSandboxedAndScriptFree() -> None:
    result = renderHtml(b"<p>ok</p><script>alert(1)</script>")

    assert "<iframe" in result.html
    assert "sandbox=" in result.html
    # 脚本内容不得出现在 srcdoc 里
    assert "alert(1)" not in result.html


def test_officeDocxKeepsHeadingsAndTables() -> None:
    from docx import Document

    document = Document()
    document.add_heading("第一章", level=1)
    document.add_paragraph("正文段落")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "表头"
    buffer = io.BytesIO()
    document.save(buffer)

    result = renderOffice(buffer.getvalue(), "a.docx")

    assert "<h1>第一章</h1>" in result.html
    assert "正文段落" in result.html
    assert "<table>" in result.html
    assert "表头" in result.html


def test_officeXlsxKeepsEverySheet() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "报价"
    sheet.append(["供应商", "价格"])
    workbook.create_sheet("第二表")
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = renderOffice(buffer.getvalue(), "a.xlsx")

    assert "报价" in result.html
    assert "第二表" in result.html
    assert "供应商" in result.html


def test_officePptxKeepsEachSlide() -> None:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "封面标题"
    box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    box.text_frame.text = "要点一"
    buffer = io.BytesIO()
    presentation.save(buffer)

    result = renderOffice(buffer.getvalue(), "a.pptx")

    assert "封面标题" in result.html
    assert "要点一" in result.html
    assert "第 1 页" in result.html


def test_brokenOfficeShowsActionableHintInsteadOfGarbage() -> None:
    """坏文件 ⇒ 可行动提示，**不能**把二进制当文本吐出来。

    2026-11-22 实测：.xls 落到文本兜底后，19MB 二进制被当 UTF-8 解码 ⇒
    页面全是乱码。渲染失败/格式不支持时要给「请下载原文」的指引。
    """
    result = renderOffice(b"not-a-real-docx", "a.docx")

    assert "暂不支持在线预览" in result.html
    assert "下载" in result.html
    assert "<pre>" not in result.html


def test_xlsIsRenderedAsTableNotGarbage() -> None:
    """.xls（OLE2 二进制）必须走 xlrd 渲染，**不能**当文本解码。"""
    import xlwt  # noqa: F401  # 仅当依赖存在时用于生成样本

    workbook = xlwt.Workbook()
    sheet = workbook.add_sheet("报价")
    sheet.write(0, 0, "供应商")
    sheet.write(0, 1, "单价")
    sheet.write(1, 0, "B024")
    sheet.write(1, 1, 12.5)
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = renderOffice(buffer.getvalue(), "a.xls")

    assert "<table>" in result.html
    assert "报价" in result.html
    assert "供应商" in result.html
    assert "B024" in result.html
    # 浮点整数不显示 .0
    assert ">12.5<" in result.html


def test_unknownBinaryOfficeIsNotDecodedAsText() -> None:
    """旧 .doc（解析器不支持、渲染器不认）⇒ 提示，而不是乱码文本。"""
    result = renderOffice(b"\xd0\xcf\x11\xe0binary", "old.doc")

    assert "暂不支持在线预览" in result.html


def test_renderPreviewDispatchesByKind() -> None:
    markdown = renderPreview(b"# h", previewKind="MARKDOWN", filename="a.md")
    assert "<h1>" in markdown.html

    unknown = renderPreview(b"plain", previewKind="WEIRD", filename="a.bin")
    assert "<pre>" in unknown.html


# ---------------------------------------------------------------------------
# 多 sheet 导航（2026-11-22 用户反馈「怎么看多个 sheet」）
# ---------------------------------------------------------------------------


def test_multipleSheetsGetNavigationAndAnchors() -> None:
    """多个 sheet ⇒ 目录导航 + 每个 sheet 一个 ``<section id="sheet-N">``。

    ⚠️ 必须**纯 CSS**（预览页 CSP 禁脚本）：把用户内容渲染出的页面开脚本，
    等于把 XSS 面重新打开。
    """
    from openpyxl import Workbook

    workbook = Workbook()
    first = workbook.active
    first.title = "报价"
    first.append(["供应商", "单价"])
    second = workbook.create_sheet("历史价")
    second.append(["2025", "10.5"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = renderOffice(buffer.getvalue(), "a.xlsx")

    # 目录：两个 sheet 各一个锚点链接
    assert 'class="sheet-nav"' in result.html
    assert 'href="#sheet-1"' in result.html
    assert 'href="#sheet-2"' in result.html
    # 每个 sheet 一个 section
    assert 'id="sheet-1"' in result.html
    assert 'id="sheet-2"' in result.html
    # 两个 sheet 的内容都在（切换靠 CSS，不是砍掉）
    assert "供应商" in result.html
    assert "2025" in result.html
    # 标题里带 sheet 数
    assert "2 个工作表" in result.html


def test_singleSheetStillHasNavigation() -> None:
    """单 sheet 也给目录（行为一致，且用户知道只有 1 个）。"""
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.title = "唯一表"
    workbook.active.append(["x"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = renderOffice(buffer.getvalue(), "a.xlsx")

    assert "1 个工作表" in result.html
    assert 'href="#sheet-1"' in result.html


def test_sheetNavigationIsCssOnlyNoScript() -> None:
    """导航必须是 CSS 实现（`:target`），页面里**不能有 script**。"""
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["x"])
    workbook.create_sheet("二")
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = renderOffice(buffer.getvalue(), "a.xlsx")

    assert ":target" in result.html
    assert "<script" not in result.html
