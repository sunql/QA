"""会话问答导出 PDF 渲染服务（reportlab platypus）。

与 ``session_history_service`` 解耦：本服务只接收一个 ``ChatExportPayload``
（不可变 dataclass），将其渲染为 PDF 字节，不接触 DB / ORM / FastAPI。

实现要点：
- 中文字体走 ``pdf_fonts.ensureCjkFont()``：优先微软雅黑（TrueType，嵌入子集，
  任何阅读器都能显示），缺字体文件时回落 reportlab 内置 STSong-Light。
- Markdown 解析：``markdown`` 库的 python-markdown API 把内容转 HTML；
  我们再用 ``html.parser`` 拆分为 block 节点（标题/段落/列表/引用/代码块/表格），
  每个 block 转 platypus Flowable。直接 ``md.convert(text)`` 返回的 HTML
  给 Paragraph 渲染时不支持表格/列表/嵌套结构。
- 图表区（0105 起）：**不再一律是占位框**。分三条路：
  1. 有前端回传的 PNG → 直接嵌 ``Image``（像素与前端的图一致）；
  2. ``table`` / ``kpi`` → 由本服务从 ``chart_option`` **原生画**（表格/指标块），
     不吃图片 —— 前端截图失败时这两种仍然出得来；
  3. 其余 kind 且无 PNG → 保留灰色占位框（存量消息 / 截图失败时的兜底）。
  服务端不重画 ECharts：那会是第二套渲染器，与前端必然长得不一样，还引入
  重跑 SQL 的性能与权限风险（同 ``evaluation_report_export_service`` 的判断）。
- 代码块（SQL）等宽字体用 Courier。
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
from io import BytesIO
from typing import Any

import markdown
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image as PdfImage,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import HRFlowable

from app.domain.enums import ChartType
from app.services.chat_chart_persist import _PERSIST_MAX_TABLE_ROWS
from app.services.pdf_fonts import ensureCjkFont

logger = logging.getLogger(__name__)


_MONO_FONT_NAME = "Courier"

# 图表位图在 PDF 里能占的正文框 = 正文可用宽/高 × 此比例。与占位框同宽（0.7），让
# 同一份 PDF 里各轮的图视觉尺度一致；**宽高两轴都用它**，所以是「框」而不是「宽」——
# 只夹宽度会让窄高图的高度溢出版面。
_CHART_IMAGE_BOX_RATIO = 0.7

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


@dataclass(frozen=True)
class ExportStepChart:
    """多步报告里的一步图（2026-10-31，分节插图）。

    分节块只带 ``stepIndex``（图负载不复制，同实时/回放的取舍）；PDF 渲染时按
    stepIndex 到这里取图与结构。``chart_image`` 由前端按 ``(messageId, stepIndex)``
    回传后挂上。
    """

    step_index: int
    chart_type: str | None
    chart_option: dict | None = None
    chart_image: bytes | None = None


@dataclass(frozen=True)
class ChatExportTurn:
    """一对 user + assistant 问答轮次（PDF 中作为一个 KeepTogether 块）。"""

    user_content: str
    user_time: datetime | None
    assistant_content: str
    assistant_time: datetime | None
    # assistant 行的 SessionMessage 主键：把前端回传的位图按 id 对到轮次上。
    # None 表示该轮无对应消息（如「仅有 user 行」的降级轮）。
    message_id: int | None = None
    sql: str | None = None
    chart_type: str | None = None
    # 0105：该轮的图表渲染负载（服务端结构，不含颜色）。table/kpi 直接从这里画。
    chart_option: dict | None = None
    # 0105：前端离屏渲出的 PNG 原始字节（不含 data: 前缀）。仅 ECharts 类 kind 有；
    # table/kpi 不走图片，故为 None 时那两类照样出图。
    chart_image: bytes | None = None
    model_name: str | None = None
    tokens_used: int | None = None
    cost: Decimal | None = None
    # 2026-10-31（分节插图）：多步最终回答的分节块（buildReportSections 现算）。
    # 非 None 时 PDF 按块顺序排版（文字 → 该步图），此时顶层的
    # chart_type/chart_option 不再单画（图都在分节里，再画一次就重复了）。
    report_sections: list[dict] | None = None
    # 2026-10-31：分节块引用的步骤图（按 step_index 取）。无分节时为空。
    step_charts: tuple[ExportStepChart, ...] = ()


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
        self._cjk_font = ensureCjkFont()
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
            fontName=self._cjk_font,
            fontSize=10,
            leading=14,
            wordWrap="CJK",
        )
        title = ParagraphStyle(
            name="DocTitle",
            parent=base["Title"],
            fontName=self._cjk_font,
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
            story.extend(self._build_turn(idx, turn))
        return story

    def _build_turn(self, idx: int, turn: ChatExportTurn) -> list[Any]:
        """一轮 → flowables 列表。

        无 ``report_sections``（单步问答 / 存量消息）整轮 ``KeepTogether``，与 0105
        行为完全一致；有分节时拆开（见 ``_build_sectioned_turn``）。
        """
        if turn.report_sections:
            return self._build_sectioned_turn(idx, turn)
        flowables: list[Any] = []
        flowables.extend(self._question_flowables(idx, turn))
        # 助手回答
        flowables.extend(self._answer_header_flowables(idx, turn))
        flowables.extend(self._markdown_to_flowables(turn.assistant_content))
        # SQL
        if turn.sql:
            flowables.append(Spacer(1, 4))
            flowables.append(Paragraph("<b>SQL：</b>", self._styles["body"]))
            flowables.append(self._sql_flowable(turn.sql))
        # 图表（0105）：真图 > 原生 table/kpi > 占位框，三档降级见 _chart_flowables
        if turn.chart_type:
            flowables.append(Spacer(1, 4))
            flowables.extend(self._chart_flowables(turn))
        flowables.extend(self._turnFooter_flowables(turn))
        return [KeepTogether(flowables)]

    def _build_sectioned_turn(self, idx: int, turn: ChatExportTurn) -> list[Any]:
        """多步报告：问答头一起、正文与图按分节块顺序流式排版。

        **不整轮 KeepTogether**：多步报告常跨好几页，整轮裹在一起会把整块推到
        下一页；单块超过一页时 reportlab 直接抛 ``LayoutError``（整份导出 500）。
        这里只把「用户问题 + 助手标题」保持在一起，其余交给平台分页。
        """
        flowables: list[Any] = [
            KeepTogether(
                self._question_flowables(idx, turn)
                + self._answer_header_flowables(idx, turn)
            )
        ]
        step_charts = {chart.step_index: chart for chart in turn.step_charts}
        for block in turn.report_sections or []:
            kind = block.get("kind")
            if kind == "text":
                content = block.get("content")
                flowables.extend(
                    self._markdown_to_flowables(content if isinstance(content, str) else "")
                )
            elif kind == "chart":
                flowables.extend(self._section_chart_flowables(block, step_charts))
        # 顶层 SQL：多步回答的 SQL 仍是整轮属性，放在正文之后（分节里没有 SQL 块）
        if turn.sql:
            flowables.append(Spacer(1, 4))
            flowables.append(Paragraph("<b>SQL：</b>", self._styles["body"]))
            flowables.append(self._sql_flowable(turn.sql))
        flowables.extend(self._turnFooter_flowables(turn))
        return flowables

    def _section_chart_flowables(
        self, block: dict, step_charts: dict[int, ExportStepChart]
    ) -> list[Any]:
        """一个 chart 分节块 → 图 flowables（缺图/无图就占位，绝不让块消失）。"""
        step_index = block.get("stepIndex")
        chart = step_charts.get(step_index) if isinstance(step_index, int) else None
        title = block.get("title")
        out: list[Any] = []
        if isinstance(title, str) and title.strip():
            out.append(Paragraph(self._escape_cell(title), self._styles["chart_caption"]))
        if chart is None:
            # 分节块由同一把「有图」的尺生成，走到这里说明数据不一致（老快照/
            # 前端算错）—— 占位比静默丢掉一节更诚实。
            out.append(self._chart_placeholder_flowable("chart"))
            return out
        out.extend(
            self._chart_flowables_for(
                chart.chart_type, chart.chart_option, chart.chart_image
            )
        )
        return out

    def _question_flowables(self, idx: int, turn: ChatExportTurn) -> list[Any]:
        user_time = turn.user_time.strftime("%Y-%m-%d %H:%M:%S") if turn.user_time else "—"
        return [
            Paragraph(f"#{idx} · 用户", self._styles["role_user"]),
            Paragraph(f"<font color='#666666'>{user_time}</font>", self._styles["meta"]),
            *self._markdown_to_flowables(turn.user_content),
        ]

    def _answer_header_flowables(self, idx: int, turn: ChatExportTurn) -> list[Any]:
        asst_time = turn.assistant_time.strftime("%Y-%m-%d %H:%M:%S") if turn.assistant_time else "—"
        return [
            Spacer(1, 6),
            Paragraph(f"#{idx} · 助手", self._styles["role_assistant"]),
            Paragraph(f"<font color='#666666'>{asst_time}</font>", self._styles["meta"]),
        ]

    def _turnFooter_flowables(self, turn: ChatExportTurn) -> list[Any]:
        """元信息（模型/Tokens/成本）+ 轮次分隔线。"""
        meta_bits: list[str] = []
        if turn.model_name:
            meta_bits.append(f"模型: {turn.model_name}")
        if turn.tokens_used is not None:
            meta_bits.append(f"Tokens: {turn.tokens_used}")
        if turn.cost is not None:
            meta_bits.append(f"成本: ${turn.cost:.6f}")
        flowables: list[Any] = []
        if meta_bits:
            flowables.append(Spacer(1, 4))
            flowables.append(Paragraph(" · ".join(meta_bits), self._styles["meta"]))
        flowables.append(Spacer(1, 10))
        flowables.append(HRFlowable(width="100%", thickness=0.3, color=colors.lightgrey))
        return flowables

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
            ("FONT", (0, 0), (-1, 0), self._cjk_font, 8),
            ("FONT", (0, 1), (-1, -1), self._cjk_font, 8),
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

    def _chart_flowables(self, turn: ChatExportTurn) -> list:
        """该轮回答的图表区 flowables（三档降级）。

        1. **有前端回传的 PNG** → 直接嵌 ``Image``。前端已经用 ECharts 渲过这张图，
           回传的就是同一张，像素级一致。
        2. **table / kpi** → 从 ``chart_option`` 原生画。这两类本质是文字，服务端
           画得出来，**不吃图片** —— 前端截图失败时它们照样出得来。
        3. **其余 kind 且无 PNG** → 灰色占位框（存量消息 / 截图失败的兜底）。

        ``table`` / ``kpi`` 刻意排在图片之前：官方渲染比位图更清晰、可选中，
        且不受截图分辨率与暗色主题影响。
        """
        return self._chart_flowables_for(
            turn.chart_type, turn.chart_option, turn.chart_image
        )

    def _chart_flowables_for(
        self,
        chart_type: str | None,
        chart_option: dict | None,
        chart_image: bytes | None,
    ) -> list:
        """三档降级的具体分派；顶层图与分节图共用同一套（口径必须一致）。"""
        if chart_type == ChartType.TABLE.value:
            table = self._table_from_chart_option(chart_option)
            if table:
                return table
        if chart_type == ChartType.KPI.value:
            kpi = self._kpi_flowable(chart_option)
            if kpi:
                return kpi
        if chart_image:
            image = self._chart_image_flowable(chart_image)
            if image is not None:
                return [image]
        return [self._chart_placeholder_flowable(chart_type or "chart")]

    def _chart_image_flowable(self, imageBytes: bytes) -> PdfImage | None:
        """PNG 字节 → 等比缩放到正文框内的 ``Image``；解不出图返回 None（降级占位）。

        只可能是 PNG（controller 已按 ``data:image/png;base64,`` 前缀 + 字节/像素
        上限校验过），这里再挡一道：**解不开的字节会让 reportlab 在 build 时抛异常，
        把整份导出的 PDF 一起带走** —— 一张图换一份报告的错配。

        **长宽都要夹**。只夹宽度时，一张又窄又高的图（100×4000）宽度合规、高度却
        溢出页面，reportlab 会在 ``doc.build()`` 里抛 ``LayoutError`` —— 那已经出了
        本函数的 try，整份导出变 500 而不是按设计降级成占位框。
        """
        try:
            image = PdfImage(BytesIO(imageBytes))
            box_width = (self._PAGE_WIDTH - 2 * self._MARGIN) * _CHART_IMAGE_BOX_RATIO
            box_height = (self._PAGE_HEIGHT - 2 * self._MARGIN) * _CHART_IMAGE_BOX_RATIO
            # drawWidth/drawHeight 是 platypus Image 的公开可变属性（构造后、wrap 前
            # 设置）。取两个方向所需比例的**较小者**才同时满足；用 min(…, 1.0) 保证
            # 小图不被放大（放大只会糊）。
            scale = min(box_width / image.drawWidth, box_height / image.drawHeight, 1.0)
            if scale < 1.0:
                image.drawWidth *= scale
                image.drawHeight *= scale
            return image
        except Exception:
            logger.warning("导出图表图片解码失败，降级为占位框", exc_info=True)
            return None

    def _table_from_chart_option(self, chartOption: dict | None) -> list:
        """``{"columns", "rows"}`` 负载 → 带表头的表格（+ 截断说明）；无从画起返回 []。

        行数在这里**再截一道**：顶层列落库时已被 ``boundedChartOption`` 截到
        ``_PERSIST_MAX_TABLE_ROWS``，但 ``steps_snapshot`` 的历史数据可能没截过，
        而 ``build()`` 在 async 端点里同步执行 —— 十万行表格排版期间整个进程的
        事件循环被占住。截断后如实标注，不假装是全部。
        """
        if not isinstance(chartOption, dict):
            return []
        columns = chartOption.get("columns")
        rows = chartOption.get("rows")
        if not isinstance(columns, list) or not columns:
            return []
        dictRows = [row for row in rows or [] if isinstance(row, dict)]
        keptRows = dictRows[:_PERSIST_MAX_TABLE_ROWS]
        body = [[self._escape_cell(col) for col in columns]]
        for row in keptRows:
            body.append([self._escape_cell(row.get(col)) for col in columns])
        if len(body) == 1:
            return []
        flowables: list = [self._build_table_flowable(body)]
        if chartOption.get("truncated") or len(dictRows) > len(keptRows):
            flowables.append(Paragraph(
                f"（表格仅显示前 {len(body) - 1} 行，完整结果请回看会话）",
                self._styles["chart_caption"],
            ))
        return flowables

    def _kpi_flowable(self, chartOption: dict | None) -> list:
        """``{"kpi": {label, value, unit, delta}}`` 负载 → 指标块（label + 大号数值）。

        与表格同源：KPI 不是 ECharts，服务端直接排版比位图清晰。
        """
        kpi = chartOption.get("kpi") if isinstance(chartOption, dict) else None
        if not isinstance(kpi, dict):
            return []
        label = kpi.get("label")
        value = kpi.get("value")
        if value is None and not label:
            return []
        unit = kpi.get("unit") or ""
        value_text = "—" if value is None else f"{value:g}"
        flowables: list = [Paragraph(
            f"<b>{self._escape_cell(label) or '指标'}</b>："
            f"<font size=14>{self._escape_cell(value_text)}</font>{self._escape_cell(unit)}",
            self._styles["body"],
        )]
        delta = kpi.get("delta")
        if delta is not None:
            flowables.append(Paragraph(
                f"<font color='#666666'>变化: {self._escape_cell(f'{delta:+g}')}</font>",
                self._styles["chart_caption"],
            ))
        return flowables

    @staticmethod
    def _escape_cell(value: Any) -> str:
        """图表负载里的裸值 → 可进 Paragraph 的安全文本。

        值来自业务库（供应商名、物料描述等），**必须转义**：不转义的话一个含
        ``<b>`` 的名称就能污染整段排版，含非法标签时 reportlab 会直接抛异常。
        """
        if value is None:
            return ""
        return html.escape(str(value), quote=False)

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