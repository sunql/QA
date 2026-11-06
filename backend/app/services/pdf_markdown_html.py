"""markdown→HTML 结果的白名单清洗与 block 拆分（PDF 导出用）。

从 ``pdf_export_service`` 拆出：这一段是「把不可信的 HTML 变成可安全交给
reportlab Paragraph 的 block 列表」的纯函数层，与排版无关，单独成文件才好被
单测直接钉住（安全审查 MEDIUM-4 的白名单就在 ``sanitizeInlineHtml`` 里）。

``htmlToBlocks`` 的输出是 ``[(tag, inner_html)]``：tag 取自 ``_BLOCK_TAGS``，
inner HTML 里的 inline 标签已按白名单过滤。
"""

from __future__ import annotations

import html
import logging
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

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


def sanitizeInlineHtml(htmlStr: str) -> str:
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
        s.feed(htmlStr)
        s.close()
    except Exception:
        logger.warning("HTML sanitize 失败，降级为 escape 全文")
        return html.escape(htmlStr)
    return "".join(s.out)


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


def htmlToBlocks(htmlStr: str) -> list[tuple[str, str]]:
    """把 markdown→HTML 输出拆为 (tag, inner_html) 列表。"""

    # 安全审查 MEDIUM-4：白名单 inline 标签，避免恶意 HTML（reportlab Paragraph
    # 识别 <font>/<b>/<i>/<br/> 等内联标签，可能被注入 <font color=red> 误导读者）
    # 通过 attribute 白名单 + tag 白名单双重过滤，非白名单标签直接剥除。
    htmlStr = sanitizeInlineHtml(htmlStr)
    parser = _BlockExtractor()
    try:
        parser.feed(htmlStr)
        parser.close()
    except Exception:
        logger.warning("HTML 解析失败，降级为段落: %s", htmlStr[:80])
        return [("p", html.escape(htmlStr))]
    # 收尾：未闭合的 stack 强制 flush（markdown 转换结果通常闭合完整）
    for tag, parts in parser._stack:  # type: ignore[attr-defined]
        parser.blocks.append((tag, "".join(parts)))
    return parser.blocks
