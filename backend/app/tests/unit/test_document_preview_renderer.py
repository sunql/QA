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


def test_brokenOfficeFallsBackToText() -> None:
    """坏文件 ⇒ 退回纯文本，**不抛异常**（预览不该让整个页面挂）。"""
    result = renderOffice(b"not-a-real-docx", "a.docx")

    assert "<pre>" in result.html


def test_renderPreviewDispatchesByKind() -> None:
    markdown = renderPreview(b"# h", previewKind="MARKDOWN", filename="a.md")
    assert "<h1>" in markdown.html

    unknown = renderPreview(b"plain", previewKind="WEIRD", filename="a.bin")
    assert "<pre>" in unknown.html
