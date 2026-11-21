"""文档预览渲染（需求 4「通用查看方式」的服务端实现）。

**一个端点覆盖全部格式**：``GET /api/v1/documents/{id}/preview`` 返回一段
自包含 HTML（内联 CSS），前端只需 iframe 打开。

按 ``preview_kind`` 分派（与 ``document_preview_kinds`` 同源）：

===========  ==========================================================
类别          渲染
===========  ==========================================================
``TEXT``      ``<pre>`` 转义（保行、保留空格）
``MARKDOWN``  markdown 渲染 + HTML 消毒
``HTML``      剥脚本后 iframe sandbox 包裹（**禁止外链图片**）
``PDF``       **不渲染**——返回原件字节让浏览器原生查看器打开（保真 100%）
``OFFICE``    服务端用已有解析库渲染结构（docx 标题/段落/表格、xlsx 每 sheet、
              pptx 每页）
``IMAGE``     ``<img>``（原件字节）
===========  ==========================================================

**为什么 PDF 例外**：PDF 的价值就是版式，转一遍必丢；而浏览器原生 PDF
查看器零成本、零依赖、100% 保真。这是唯一该走「原件直出」的格式。

**为什么 Office 在服务端渲染**：前端要装 mammoth/xlsx/pdf.js（+1MB 打包体积，
PDF.js 还需 worker 跨域配置），而服务端**已有** python-docx / openpyxl /
python-pptx（都是既有依赖），预览是只读展示、不需要前端交互能力。
"""

from __future__ import annotations

import html as htmlLib
from dataclasses import dataclass
from typing import Any

from app.services.document_preview_kinds import (
    KIND_HTML,
    KIND_IMAGE,
    KIND_MARKDOWN,
    KIND_OFFICE,
    KIND_TEXT,
)
from app.services.html_sanitizer import escapeHtml, sanitizeHtml, sanitizeMarkdown

#: 预览页的基础样式（内联，避免额外请求；CSP 允许 inline style）。
_PREVIEW_CSS = """
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
 margin:0;padding:24px;line-height:1.7;color:#1f2329;background:#fff}
.doc-title{font-size:18px;font-weight:600;margin:0 0 4px}
.doc-meta{font-size:12px;color:#8f959e;margin:0 0 20px;
 padding-bottom:12px;border-bottom:1px solid #e5e6eb}
pre{white-space:pre-wrap;word-break:break-word;background:#f7f8fa;
 padding:16px;border-radius:6px;margin:0}
table{border-collapse:collapse;margin:12px 0;width:100%;font-size:13px}
th,td{border:1px solid #e5e6eb;padding:6px 10px;text-align:left;
 vertical-align:top}
th{background:#f7f8fa;font-weight:600}
.sheet{margin:20px 0}
.sheet-name{font-weight:600;margin:12px 0 6px;font-size:15px}
.slide{border:1px solid #e5e6eb;border-radius:6px;padding:14px;margin:12px 0}
.slide-title{font-weight:600;margin-bottom:8px}
.slide-text{margin:4px 0;white-space:pre-wrap}
.img-wrap{text-align:center;padding:16px}
.img-wrap img{max-width:100%;height:auto}
.md h1,.md h2,.md h3{margin:20px 0 10px}
.md pre{overflow:auto}
.md code{background:#f2f3f5;padding:1px 4px;border-radius:3px}
iframe{width:100%;height:70vh;border:1px solid #e5e6eb;border-radius:6px}
"""

#: 预览渲染的规模上限（防止 19MB Excel / 十万行表把响应撑爆）。
#: 超限时截断并**明示**「已截断」——静默截断会让用户以为数据只有这些。
MAX_PREVIEW_ROWS = 2000
MAX_PREVIEW_COLS = 50

#: 包裹用户 HTML 的 iframe sandbox（禁止脚本/同源/表单/顶层导航）。
_HTML_SANDBOX = "allow-popups allow-popups-to-escape-sandbox"


@dataclass(frozen=True)
class PreviewResult:
    """预览渲染结果。"""

    html: str
    #: True = 需要前端改用原件字节（PDF/IMAGE 走这条路）
    useSourceBytes: bool = False


def _shell(title: str, bodyHtml: str) -> str:
    """把内容包进自包含的 HTML 页。"""
    safeTitle = escapeHtml(title)
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{safeTitle}</title>"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<style>{_PREVIEW_CSS}</style></head><body>"
        f"<div class=\"doc-title\">{safeTitle}</div>"
        f"<div class=\"doc-meta\">{len(bodyHtml)} 字符</div>"
        f"{bodyHtml}</body></html>"
    )


def renderText(content: bytes) -> PreviewResult:
    """纯文本 → ``<pre>``（转义，保行）。"""
    text = content.decode("utf-8", errors="replace")
    return PreviewResult(html=_shell("文档预览", f"<pre>{escapeHtml(text)}</pre>"))


def renderMarkdown(content: bytes) -> PreviewResult:
    """Markdown → 渲染 + 消毒。"""
    text = content.decode("utf-8", errors="replace")
    body = f"<div class=\"md\">{sanitizeMarkdown(text)}</div>"
    return PreviewResult(html=_shell("文档预览", body))


def renderHtml(content: bytes) -> PreviewResult:
    """用户 HTML → 消毒 + iframe sandbox 包裹。

    **外链图片禁止**（跟踪像素 / 视觉 SSRF）⇒ 已在消毒器里剥掉 ``<img>``。
    """
    text = content.decode("utf-8", errors="replace")
    safe = sanitizeHtml(text)
    body = (
        f"<iframe sandbox=\"{_HTML_SANDBOX}\" srcdoc=\"{htmlLib.escape(safe, quote=True)}\">"
        "</iframe>"
    )
    return PreviewResult(html=_shell("文档预览", body))


def renderImage(content: bytes, filename: str) -> PreviewResult:
    """图片 → ``<img>``（用 data URI 内联，避免二次请求与鉴权绕过）。"""
    import base64

    from app.services.document_preview_kinds import mimeFor

    mime = mimeFor(None, filename)
    encoded = base64.b64encode(content).decode("ascii")
    body = (
        f"<div class=\"img-wrap\"><img alt=\"{escapeHtml(filename)}\" "
        f"src=\"data:{mime};base64,{encoded}\"></div>"
    )
    return PreviewResult(html=_shell(filename, body))


# ---------------------------------------------------------------------------
# Office：docx / xlsx / pptx（用既有解析库，服务端渲染结构）
# ---------------------------------------------------------------------------


def _renderDocx(content: bytes) -> PreviewResult:
    """docx → 标题层级 + 段落 + 表格。"""
    import io

    from docx import Document

    document = Document(io.BytesIO(content))
    parts: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name or "") if paragraph.style else ""
        if style.startswith("Heading"):
            digits = "".join(ch for ch in style if ch.isdigit())
            level = min(int(digits or "1"), 6)
            parts.append(f"<h{level}>{escapeHtml(text)}</h{level}>")
        else:
            parts.append(f"<p>{escapeHtml(text)}</p>")
    for table in document.tables:
        parts.append(_renderTable(table))
    return PreviewResult(html=_shell("文档预览", "".join(parts)))


def _renderTable(table: Any) -> str:
    """表格 → HTML（全部转义）。"""
    rows: list[str] = []
    for row in table.rows:
        cells = "".join(f"<td>{escapeHtml(cell.text)}</td>" for cell in row.cells)
        rows.append(f"<tr>{cells}</tr>")
    if not rows:
        return ""
    return "<table>" + "".join(rows) + "</table>"


def _renderXlsx(content: bytes) -> PreviewResult:
    """xlsx → 每个 sheet 一张表。"""
    import io

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        parts.append(f"<div class=\"sheet-name\">{escapeHtml(sheet.title)}</div>")
        rows: list[str] = []
        truncated = False
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= MAX_PREVIEW_ROWS:
                truncated = True
                break
            cells = "".join(
                f"<td>{escapeHtml('' if value is None else str(value))}</td>"
                for value in row[:MAX_PREVIEW_COLS]
            )
            rows.append(f"<tr>{cells}</tr>")
        if rows:
            parts.append("<table>" + "".join(rows) + "</table>")
        if truncated:
            parts.append(_truncatedNote())
    return PreviewResult(html=_shell("文档预览", "".join(parts)))


def _truncatedNote() -> str:
    return (
        f"<p class=\"truncated\">仅预览前 {MAX_PREVIEW_ROWS} 行；"
        "完整内容请下载原文。</p>"
    )


def _renderXls(content: bytes) -> PreviewResult:
    """**老式 .xls（OLE2 二进制）** → 每个 sheet 一张表（走 xlrd 2.x）。

    ⚠️ 这个分支是必需的不是可选的：`.xls` 是**二进制**，若不单独渲染而落到
    文本兜底，字节会被当 UTF-8 解码 ⇒ **页面上全是乱码**（2026-11-22 实测：
    用户上传 19MB 的 .xls，预览乱码）。
    """
    import io

    import xlrd

    workbook = xlrd.open_workbook(file_contents=content)
    parts: list[str] = []
    for sheet in workbook.sheets():
        parts.append(f"<div class=\"sheet-name\">{escapeHtml(sheet.name)}</div>")
        rows: list[str] = []
        truncated = False
        for rowIndex in range(min(sheet.nrows, MAX_PREVIEW_ROWS)):
            cells = "".join(
                f"<td>{escapeHtml(_xlsCellText(sheet.cell_value(rowIndex, colIndex)))}</td>"
                for colIndex in range(min(sheet.ncols, MAX_PREVIEW_COLS))
            )
            rows.append(f"<tr>{cells}</tr>")
        if sheet.nrows > MAX_PREVIEW_ROWS:
            truncated = True
        if rows:
            parts.append("<table>" + "".join(rows) + "</table>")
        if truncated:
            parts.append(_truncatedNote())
    return PreviewResult(html=_shell("文档预览", "".join(parts)))


def _xlsCellText(value: Any) -> str:
    """xlrd 的 cell 值 → 展示文本（浮点整数去掉 '.0'，None ⇒ 空）。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _renderPptx(content: bytes) -> PreviewResult:
    """pptx → 每页一个块（标题 + 文本要点）。"""
    import io

    from pptx import Presentation

    presentation = Presentation(io.BytesIO(content))
    parts: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        blocks: list[str] = [f"<div class=\"slide\">"]
        blocks.append(f"<div class=\"slide-title\">第 {index} 页</div>")
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            text = (shape.text_frame.text or "").strip()
            if not text:
                continue
            isTitle = shape == slide.shapes.title
            if isTitle:
                blocks.append(f"<div class=\"slide-title\">{escapeHtml(text)}</div>")
            else:
                blocks.append(f"<div class=\"slide-text\">{escapeHtml(text)}</div>")
        blocks.append("</div>")
        parts.append("".join(blocks))
    return PreviewResult(html=_shell("文档预览", "".join(parts)))


def renderOffice(content: bytes, filename: str) -> PreviewResult:
    """Office → 按扩展名分派渲染（失败 ⇒ 退回纯文本，不 500）。"""
    from app.services.document_preview_kinds import extensionOf

    extension = extensionOf(filename)
    try:
        if extension == "docx":
            return _renderDocx(content)
        if extension in ("xlsx", "xlsm"):
            return _renderXlsx(content)
        if extension == "xls":
            return _renderXls(content)
        if extension == "pptx":
            return _renderPptx(content)
    except Exception:  # noqa: BLE001 —— 渲染失败给可行动提示，不让预览整个挂掉
        return _unsupportedPreview(filename)
    # 未知 Office 扩展名（如旧 .doc/.ppt）：**不能**按文本读二进制（会乱码）
    return _unsupportedPreview(filename)


def _unsupportedPreview(filename: str) -> PreviewResult:
    """无法在线渲染时的可行动提示（**绝不**把二进制当文本吐出来）。"""
    body = (
        "<div class=\"unsupported\">"
        f"<p>当前格式（{escapeHtml(filename)}）暂不支持在线预览。</p>"
        "<p>请使用「原文」按钮下载后，用对应的办公软件打开。</p>"
        "</div>"
    )
    return PreviewResult(html=_shell("文档预览", body))


def renderPreview(
    content: bytes, *, previewKind: str, filename: str
) -> PreviewResult:
    """按类别渲染（**总入口**）。未知类别 ⇒ 纯文本（最保守）。"""
    if previewKind == KIND_MARKDOWN:
        return renderMarkdown(content)
    if previewKind == KIND_HTML:
        return renderHtml(content)
    if previewKind == KIND_IMAGE:
        return renderImage(content, filename)
    if previewKind == KIND_OFFICE:
        return renderOffice(content, filename)
    if previewKind == KIND_TEXT:
        return renderText(content)
    return renderText(content)
