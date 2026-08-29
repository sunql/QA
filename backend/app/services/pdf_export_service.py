"""会话问答导出 PDF 渲染服务（reportlab platypus）。

与 ``session_history_service`` 解耦：本服务只接收一个 ``ChatExportPayload``
（不可变 dataclass），将其渲染为 PDF 字节，不接触 DB / ORM / FastAPI。

实现要点：
- 中文字体用 reportlab 内置 STSong-Light CID 字体（CIDFont，无需字体文件，
  任何装了 reportlab 的环境即可用；劣势：不支持斜体/粗体变体）。
- Markdown 解析：``markdown`` 库的 python-markdown API 把内容转 HTML；
  我们再用 ``html.parser`` 拆分为 block 节点（标题/段落/列表/引用/代码块/表格），
  每个 block 转 platypus Flowable。直接 ``md.convert(text)`` 返回的 HTML
  给 Paragraph 渲染时不支持表格/列表/嵌套结构。
- 图表区采用「灰色占位框 + 类型标签」策略：图表对象未持久化，
  不能在服务端重渲染（保留位置 + 信息密度即可，避免重新跑 SQL 的性能与权限风险）。
- 代码块（SQL）等宽字体用 Courier。
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

import markdown
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import HRFlowable

logger = logging.getLogger(__name__)


# 中文 CID 字体（reportlab 内置）；不支持粗体/斜体变体
_CJK_FONT_NAME = "STSong-Light"
_MONO_FONT_NAME = "Courier"

# 支持的 HTML block 标签集合（其它降级为段落）
_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "blockquote", "pre", "table", "hr"}

# 安全审查 MEDIUM-4：白名单 inline 标签 + 白名单属性。reportlab Paragraph 支持
# 的内联标签有限，仅放行 <b>/<i>/<br/>；<font> 仅放行 color/face 属性；其它
# 标签（含 <a href>）一律剥除，避免钓鱼或误导链接。
_WHITELIST_INLINE_TAGS = {"b", "i", "br", "font"}
_WHITELIST_FONT_ATTRS = {"color", "face"}
_BLOCK_TAGS_ALLOWED = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li",
                       "blockquote", "pre", "code", "table", "thead", "tbody", "tr",
                       "th", "td", "hr"}
_WHITELIST_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "blockquote", "pre", "table", "hr"}


def _sanitize_inline_html(html_str: str) -> str:
    """白名单 block + inline 标签与属性，其它标签全部剥除。

    解析 HTML 后只输出 block 标签（白名单）+ 选定的 inline 子集；危险标签
    （如 <script>, <iframe>, <a href javascript:...>）转为纯文本。
    """
    # 用一个简单 HTMLParser 重建；非白名单标签全部 escape 为文本
    class _Sanitizer(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.out: list[str] = []
            self._skip_depth = 0  # 处于非白名单标签内时 1+

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if self._skip_depth > 0:
                # 嵌套在剥除标签内的子标签也剥除
                if tag in _BLOCK_TAGS_ALLOWED or tag in _WHITELIST_INLINE_TAGS or tag not in (
                    _WHITELIST_INLINE_TAGS | _BLOCK_TAGS_ALLOWED
                ):
                    self._skip_depth += 1
                    return
            if tag == "br":
                self.out.append("<br/>")
                return
            if tag in _BLOCK_TAGS_ALLOWED:
                self.out.append(f"<{tag}>")
                return
            if tag in _WHITELIST_INLINE_TAGS:
                if tag == "font":
                    attrs_kept = [
                        (k, v) for k, v in attrs if k in _WHITELIST_FONT_ATTRS and v is not None
                    ]
                    attr_str = "".join(
                        f' {k}="{html.escape(v, quote=True)}"' for k, v in attrs_kept
                    )
                    self.out.append(f"<font{attr_str}>")
                elif tag == "b":
                    self.out.append("<b>")
                elif tag == "i":
                    self.out.append("<i>")
                return
            # 非白名单标签（含 script/style/iframe/a）— 跳过其子树
            if tag not in (_WHITELIST_INLINE_TAGS | _BLOCK_TAGS_ALLOWED):
                self._skip_depth += 1

        def handle_endtag(self, tag: str) -> None:
            if tag in _BLOCK_TAGS_ALLOWED:
                # 配对简单约定：未配对的 end tag 一律忽略
                # （HTMLParser 不会自动平衡）
                self.out.append(f"</{tag}>")
                return
            if tag in _WHITELIST_INLINE_TAGS:
                if tag == "font":
                    self.out.append("</font>")
                elif tag == "b":
                    self.out.append("</b>")
                elif tag == "i":
                    self.out.append("</i>")
                return
            if tag not in (_WHITELIST_INLINE_TAGS | _BLOCK_TAGS_ALLOWED):
                # 关闭剥除标签 — 仅在跳过栈非空时弹出
                if self._skip_depth > 0:
                    self._skip_depth -= 1

        def handle_data(self, data: str) -> None:
            if self._skip_depth == 0:
                self.out.append(html.escape(data, quote=False))

    s = _Sanitizer()
    try:
        s.feed(html_str)
        s.close()
    except Exception:
        logger.warning("HTML sanitize 失败，降级为 escape 全文")
        return html.escape(html_str)
    return "".join(s.out)


def _register_cjk_font_once() -> None:
    """注册内置中文字体；reportlab 要求显式 registerFont。
    重复注册会抛 ValueError，因此通过模块属性幂等。
    """
    if getattr(_register_cjk_font_once, "_done", False):
        return
    pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT_NAME))
    _register_cjk_font_once._done = True  # type: ignore[attr-defined]


@dataclass(frozen=True)
class ChatExportTurn:
    """一对 user + assistant 问答轮次（PDF 中作为一个 KeepTogether 块）。"""

    user_content: str
    user_time: datetime | None
    assistant_content: str
    assistant_time: datetime | None
    sql: str | None = None
    chart_type: str | None = None
    model_name: str | None = None
    tokens_used: int | None = None
    cost: Decimal | None = None


@dataclass(frozen=True)
class ChatExportPayload:
    """导出 PDF 的完整输入（service 层取数 → 渲染层消费，二者通过此 DTO 解耦）。"""

    session_id: str
    title: str
    generated_at: datetime
    turns: list[ChatExportTurn] = field(default_factory=list)


# 简易 block 抽取器：扫 markdown→HTML 输出，按 _BLOCK_TAGS 切分为 (tag, inner_html) 列表
class _BlockExtractor(HTMLParser):
    """把 HTML 流拆成 block 节点；inline 标签（<b>/<i>/<code>）保留在 inner HTML 里。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []  # [(tag, inner_html)]
        self._stack: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._stack.append((tag, []))
        else:
            # inline 标签：作为原始标签重新写出，由 reportlab Paragraph 解析
            attr_str = "".join(
                f' {k}="{html.escape(v, quote=True)}"' for k, v in attrs if v is not None
            )
            if self._stack:
                self._stack[-1][1].append(f"<{tag}{attr_str}>")
            else:
                # block 外 inline：作为单独段落处理
                self._stack.append(("p", [f"<{tag}{attr_str}>"]))

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS and self._stack and self._stack[-1][0] == tag:
            block_tag, parts = self._stack.pop()
            self.blocks.append((block_tag, "".join(parts)))
        else:
            # inline 标签闭合
            if self._stack:
                self._stack[-1][1].append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1][1].append(html.escape(data, quote=False))


def _html_to_blocks(html_str: str) -> list[tuple[str, str]]:
    """把 markdown→HTML 输出拆为 (tag, inner_html) 列表。"""

    # 安全审查 MEDIUM-4：白名单 inline 标签，避免恶意 HTML（reportlab Paragraph
    # 识别 <font>/<b>/<i>/<br/> 等内联标签，可能被注入 <font color=red> 误导读者）
    # 通过 attribute 白名单 + tag 白名单双重过滤，非白名单标签直接剥除。
    html_str = _sanitize_inline_html(html_str)
    parser = _BlockExtractor()
    try:
        parser.feed(html_str)
        parser.close()
    except Exception:
        logger.warning("HTML 解析失败，降级为段落: %s", html_str[:80])
        return [("p", html.escape(html_str))]
    # 收尾：未闭合的 stack 强制 flush（markdown 转换结果通常闭合完整）
    for tag, parts in parser._stack:  # type: ignore[attr-defined]
        parser.blocks.append((tag, "".join(parts)))
    return parser.blocks


class ChatExportPdfBuilder:
    """把 ChatExportPayload 渲染为 PDF 字节（bytes）。

    单例无状态；build 返回完整 PDF 字节。
    """

    _PAGE_WIDTH, _PAGE_HEIGHT = A4
    _MARGIN = 2 * cm

    def __init__(self) -> None:
        _register_cjk_font_once()
        self._styles = self._build_styles()

    def build(self, payload: ChatExportPayload) -> bytes:
        import io

        buf = io.BytesIO()
        doc = BaseDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=self._MARGIN,
            rightMargin=self._MARGIN,
            topMargin=self._MARGIN,
            bottomMargin=self._MARGIN,
            title=payload.title,
        )
        frame = Frame(
            doc.leftMargin,
            doc.bottomMargin,
            doc.width,
            doc.height,
            id="normal",
            showBoundary=0,
        )
        doc.addPageTemplates([PageTemplate(id="default", frames=[frame])])

        story = self._build_story(payload)
        doc.build(story)
        return buf.getvalue()

    # -------- 私有：构建样式 + flowables --------

    def _build_styles(self) -> dict[str, ParagraphStyle]:
        base = getSampleStyleSheet()
        body = ParagraphStyle(
            name="Body",
            parent=base["Normal"],
            fontName=_CJK_FONT_NAME,
            fontSize=10,
            leading=14,
            wordWrap="CJK",
        )
        title = ParagraphStyle(
            name="DocTitle",
            parent=base["Title"],
            fontName=_CJK_FONT_NAME,
            fontSize=18,
            leading=22,
            spaceAfter=8,
        )
        meta = ParagraphStyle(
            name="Meta",
            parent=body,
            fontSize=9,
            textColor=colors.grey,
            spaceAfter=12,
        )
        role_user = ParagraphStyle(
            name="RoleUser",
            parent=body,
            fontSize=11,
            textColor=colors.HexColor("#1d39c4"),
            spaceBefore=8,
            spaceAfter=4,
        )
        role_assistant = ParagraphStyle(
            name="RoleAssistant",
            parent=body,
            fontSize=11,
            textColor=colors.HexColor("#1f1f1f"),
            spaceBefore=4,
            spaceAfter=4,
        )
        cell = ParagraphStyle(
            name="Cell",
            parent=body,
            fontSize=8,
            leading=10,
        )
        chart_caption = ParagraphStyle(
            name="ChartCaption",
            parent=body,
            fontSize=8,
            textColor=colors.grey,
            alignment=1,  # CENTER
        )
        return {
            "body": body,
            "title": title,
            "meta": meta,
            "role_user": role_user,
            "role_assistant": role_assistant,
            "cell": cell,
            "chart_caption": chart_caption,
        }

    def _build_story(self, payload: ChatExportPayload) -> list:
        story: list[Any] = []
        story.append(Paragraph(payload.title, self._styles["title"]))
        meta_text = (
            f"会话 ID: {payload.session_id}    "
            f"导出时间: {payload.generated_at.strftime('%Y-%m-%d %H:%M:%S')}    "
            f"问答轮数: {len(payload.turns)}"
        )
        story.append(Paragraph(meta_text, self._styles["meta"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
        story.append(Spacer(1, 6))

        if not payload.turns:
            story.append(Paragraph("（该会话暂无任何消息记录）", self._styles["body"]))
            return story

        for idx, turn in enumerate(payload.turns, start=1):
            story.append(self._build_turn(idx, turn))
        return story

    def _build_turn(self, idx: int, turn: ChatExportTurn) -> KeepTogether:
        flowables: list[Any] = []
        # 用户问题
        flowables.append(Paragraph(f"#{idx} · 用户", self._styles["role_user"]))
        user_time = turn.user_time.strftime("%Y-%m-%d %H:%M:%S") if turn.user_time else "—"
        flowables.append(Paragraph(f"<font color='#666666'>{user_time}</font>", self._styles["meta"]))
        flowables.extend(self._markdown_to_flowables(turn.user_content))
        # 助手回答
        flowables.append(Spacer(1, 6))
        flowables.append(Paragraph(f"#{idx} · 助手", self._styles["role_assistant"]))
        asst_time = turn.assistant_time.strftime("%Y-%m-%d %H:%M:%S") if turn.assistant_time else "—"
        flowables.append(Paragraph(f"<font color='#666666'>{asst_time}</font>", self._styles["meta"]))
        flowables.extend(self._markdown_to_flowables(turn.assistant_content))
        # SQL
        if turn.sql:
            flowables.append(Spacer(1, 4))
            flowables.append(Paragraph("<b>SQL：</b>", self._styles["body"]))
            flowables.append(self._sql_flowable(turn.sql))
        # 图表占位（无 chart_option，标记类型 + 占位说明）
        if turn.chart_type:
            flowables.append(Spacer(1, 4))
            flowables.append(self._chart_placeholder_flowable(turn.chart_type))
        # 元信息：模型/Tokens/成本
        meta_bits: list[str] = []
        if turn.model_name:
            meta_bits.append(f"模型: {turn.model_name}")
        if turn.tokens_used is not None:
            meta_bits.append(f"Tokens: {turn.tokens_used}")
        if turn.cost is not None:
            meta_bits.append(f"成本: ${turn.cost:.6f}")
        if meta_bits:
            flowables.append(Spacer(1, 4))
            flowables.append(Paragraph(" · ".join(meta_bits), self._styles["meta"]))
        flowables.append(Spacer(1, 10))
        flowables.append(HRFlowable(width="100%", thickness=0.3, color=colors.lightgrey))
        return KeepTogether(flowables)

    # -------- Markdown → flowables --------

    def _markdown_to_flowables(self, content: str) -> list:
        """把 Markdown 文本转 ReportLab flowables（按 block 切分）。"""
        if not content.strip():
            return [Paragraph("<i>（无内容）</i>", self._styles["body"])]
        try:
            md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists"])
            html_str = md.convert(content)
        except Exception:
            logger.warning("Markdown 解析失败，降级为纯文本: %s", content[:80])
            return [Paragraph(html.escape(content), self._styles["body"])]

        blocks = _html_to_blocks(html_str)
        flowables: list[Any] = []
        for tag, inner in blocks:
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                level = int(tag[1])
                size = {1: 14, 2: 12, 3: 11}.get(level, 10)
                style = ParagraphStyle(
                    name=f"H{level}", parent=self._styles["body"], fontSize=size, leading=size + 4,
                    spaceBefore=4, spaceAfter=2,
                )
                flowables.append(Paragraph(f"<b>{inner}</b>", style))
            elif tag in {"p", "blockquote"}:
                style = self._styles["body"]
                if tag == "blockquote":
                    style = ParagraphStyle(
                        name="Quote", parent=self._styles["body"],
                        leftIndent=12, textColor=colors.HexColor("#555555"),
                    )
                flowables.append(Paragraph(inner, style))
                flowables.append(Spacer(1, 2))
            elif tag == "pre":
                # fenced_code: 通常含 <code> 子节点
                code_inner = self._strip_code_wrapper(inner)
                flowables.append(Preformatted(code_inner, ParagraphStyle(
                    name="Code", parent=self._styles["body"], fontName=_MONO_FONT_NAME,
                    fontSize=8, leading=10, leftIndent=8,
                    backColor=colors.HexColor("#f5f5f5"),
                )))
                flowables.append(Spacer(1, 4))
            elif tag in {"ul", "ol"}:
                items = self._extract_list_items(inner)
                marker = "• " if tag == "ul" else None
                for k, item in enumerate(items):
                    prefix = marker if marker else f"{k + 1}. "
                    flowables.append(Paragraph(f"{prefix}{item}", self._styles["body"]))
                flowables.append(Spacer(1, 4))
            elif tag == "table":
                rows = self._extract_table_rows(inner)
                if rows:
                    flowables.append(self._build_table_flowable(rows))
            elif tag == "hr":
                flowables.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))

        if not flowables:
            return [Paragraph(html.escape(content), self._styles["body"])]
        return flowables

    @staticmethod
    def _strip_code_wrapper(inner_html: str) -> str:
        """移除 <pre><code class="language-x">..</code></pre> 中的 code 标签，保留文本。"""
        # markdown 库会输出 <code class="language-xxx">xxx</code>；保留内容即可
        # 简化：去首尾的 <code...> 与 </code>
        s = inner_html.strip()
        if s.startswith("<code"):
            end = s.find(">")
            if end != -1:
                s = s[end + 1 :]
        if s.endswith("</code>"):
            s = s[: -len("</code>")]
        return html.unescape(s)

    @staticmethod
    def _extract_list_items(inner_html: str) -> list[str]:
        """从 <ul>/<ol> inner HTML 抽取 <li>...</li> 文本。

        简化处理：直接用 HTMLParser 拆 li，不递归嵌套（与单层 LLM 输出对齐）。
        """
        class _LiParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.items: list[str] = []
                self._current: list[str] | None = None

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                if tag == "li":
                    self._current = []

            def handle_endtag(self, tag: str) -> None:
                if tag == "li" and self._current is not None:
                    self.items.append("".join(self._current).strip())
                    self._current = None

            def handle_data(self, data: str) -> None:
                if self._current is not None:
                    self._current.append(html.escape(data, quote=False))

        p = _LiParser()
        p.feed(inner_html)
        p.close()
        return [item for item in p.items if item]

    @staticmethod
    def _extract_table_rows(inner_html: str) -> list[list[str]]:
        """从 <table> inner HTML 抽取 rows（首行为 header）。"""

        class _TableParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.rows: list[list[str]] = []
                self._current_row: list[str] | None = None
                self._current_cell: list[str] | None = None

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                if tag == "tr":
                    self._current_row = []
                elif tag in {"th", "td"} and self._current_row is not None:
                    self._current_cell = []

            def handle_endtag(self, tag: str) -> None:
                if tag in {"th", "td"} and self._current_cell is not None and self._current_row is not None:
                    self._current_row.append("".join(self._current_cell).strip() or " ")
                    self._current_cell = None
                elif tag == "tr" and self._current_row is not None:
                    if self._current_row:
                        self.rows.append(self._current_row)
                    self._current_row = None

            def handle_data(self, data: str) -> None:
                if self._current_cell is not None:
                    self._current_cell.append(html.escape(data, quote=False))

        p = _TableParser()
        p.feed(inner_html)
        p.close()
        # 收尾未闭合的 tr
        if p._current_row:  # type: ignore[attr-defined]
            p.rows.append(p._current_row)  # type: ignore[attr-defined]
        return p.rows

    def _build_table_flowable(self, rows: list[list[str]]) -> Table:
        """把 rows（首行 header）渲染为带边框的 ReportLab Table。"""
        data = [[Paragraph(cell, self._styles["cell"]) for cell in row] for row in rows]
        ncols = max(len(r) for r in data) if data else 0
        if ncols == 0:
            return Table([[""]])
        col_width = (self._PAGE_WIDTH - 2 * self._MARGIN) / ncols
        table = Table(data, colWidths=[col_width] * ncols, repeatRows=1)
        style = TableStyle([
            ("FONT", (0, 0), (-1, 0), _CJK_FONT_NAME, 8),
            ("FONT", (0, 1), (-1, -1), _CJK_FONT_NAME, 8),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f5ff")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#d9d9d9")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e8e8e8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ])
        table.setStyle(style)
        return table

    def _sql_flowable(self, sql: str) -> Preformatted:
        """SQL 代码块（等宽 + 浅灰背景）。"""
        return Preformatted(
            html.escape(sql.rstrip(";")),
            ParagraphStyle(
                name="Sql",
                fontName=_MONO_FONT_NAME,
                fontSize=8,
                leading=10,
                leftIndent=8,
                backColor=colors.HexColor("#f5f5f5"),
                borderColor=colors.HexColor("#e0e0e0"),
                borderWidth=0.5,
                borderPadding=4,
            ),
        )

    def _chart_placeholder_flowable(self, chart_type: str) -> Table:
        """图表占位框：灰色边框 + 类型标签（chart_option 未持久化，无重渲染）。"""
        label = f"图表类型: {chart_type}\n（图表对象未持久化，原始 ECharts option 不可在 PDF 重渲染）"
        para = Paragraph(
            f"<font color='#888888'><i>{html.escape(label).replace(chr(10), '<br/>')}</i></font>",
            self._styles["chart_caption"],
        )
        table = Table([[para]], colWidths=[(self._PAGE_WIDTH - 2 * self._MARGIN) * 0.7])
        table.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#bfbfbf")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafafa")),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        return table