"""HTML 消毒（预览用户上传文件时的**安全闸**）。

为什么必须消毒：预览就是把用户上传的 HTML/Markdown **渲染成页面**发给浏览器。
用户文件里的 ``<script>`` / ``onerror=`` / ``javascript:`` URL 会直接在
本系统的域下执行 ⇒ 偷 token、改数据、冒充管理员。

**用 bleach 而不是手写**：消毒器是高危代码，手写必漏（属性名大小写、
命名空间、畸形标签、编码绕过…）。bleach 是标准实现。

三条硬规则（与设计稿一致）：
1. **禁止外链图片**（``<img src=http://…>``）——防跟踪像素/视觉 SSRF；
2. **链接允许**，但强制 ``rel="noopener noreferrer"``（防 tabnabbing）；
3. ``<iframe>`` / ``<object>`` / ``<embed>`` / ``<script>`` **一律剥掉**
   （``<iframe>`` 尤其危险：能加载任意站点并伪装成我们的页面）。
"""

from __future__ import annotations

import re

import bleach
from bleach import Cleaner

#: 允许的标签（白名单；不在表里的一律剥掉，但**保留其文本内容**）。
ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p", "br", "hr", "div", "span",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "dl", "dt", "dd",
        "strong", "b", "em", "i", "u", "s", "strike", "sub", "sup",
        "blockquote", "pre", "code", "kbd", "samp",
        "table", "thead", "tbody", "tfoot", "tr", "th", "td",
        "caption", "col", "colgroup",
        "a", "img",
        "figure", "figcaption", "details", "summary", "mark",
    }
)

#: 允许的属性（按标签）。
ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "*": ["class", "id", "title", "dir", "lang"],
    "a": ["href", "target", "rel"],
    "img": ["src", "alt", "width", "height"],
    "td": ["colspan", "rowspan", "align"],
    "th": ["colspan", "rowspan", "align", "scope"],
    "ol": ["start"],
    "details": ["open"],
}

#: 允许的 URL 协议（``javascript:`` / ``data:`` 一律拒）。
ALLOWED_PROTOCOLS: frozenset[str] = frozenset({"http", "https", "mailto"})

#: 允许的 CSS（属性级）。空 ⇒ 不允许 style 属性（防 `expression()` 之类）。
ALLOWED_CSS: frozenset[str] = frozenset()


def _cleaner(*, allowImages: bool = False) -> Cleaner:
    """构造消毒器。

    ``allowImages=False``（默认，用于用户上传的 HTML/Markdown）：
    **禁止外链图片**——外链图片是跟踪像素与视觉 SSRF 的载体。
    """
    attributes = dict(ALLOWED_ATTRIBUTES)
    if not allowImages:
        attributes.pop("img", None)
        attributes["*"] = [a for a in attributes.get("*", []) if a != "src"]
    tags = set(ALLOWED_TAGS)
    if not allowImages:
        tags.discard("img")
    return Cleaner(
        tags=tags,
        attributes=attributes,
        protocols=list(ALLOWED_PROTOCOLS),
        css_sanitizer=None,
        strip=True,
        strip_comments=True,
    )


#: 整块删除（**连内容一起**）的标签：它们的内容从来不是可显示内容。
#: bleach 的 ``strip=True`` 只剥标签、**保留文本** ⇒ ``<script>alert(1)</script>``
#: 会变成可见的 "alert(1)"（无害但难看，且让人以为消毒没生效）。
_BLOCK_TAGS_RE = re.compile(
    r"<\s*(script|style|noscript|template)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def sanitizeHtml(html: str) -> str:
    """HTML → 安全 HTML（剥脚本/iframe/外链图片；链接加 noopener）。"""
    withoutBlocks = _BLOCK_TAGS_RE.sub("", html)
    cleaned = _cleaner().clean(withoutBlocks)
    return _hardenLinks(cleaned)


def sanitizeMarkdown(markdownText: str) -> str:
    """Markdown → 安全 HTML。

    ``markdown`` 库默认**透传原始 HTML**（这是它的既定行为）⇒ 必须消毒，
    否则 Markdown 里的 ``<script>`` 会原样进页面。
    """
    import markdown as markdownLib

    rendered = markdownLib.markdown(
        markdownText,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html",
    )
    return sanitizeHtml(rendered)


def _hardenLinks(html: str) -> str:
    """给 ``<a target=_blank>`` 补 ``rel="noopener noreferrer"``。

    没这一步 ⇒ 被点开的链接能用 ``window.opener`` 反向操纵我们的页面
    （tabnabbing）。
    """
    import re

    def _fix(match: re.Match[str]) -> str:
        tag = match.group(0)
        if "rel=" in tag:
            return tag
        return tag[:-1] + ' rel="noopener noreferrer">'

    return re.sub(r"<a\b[^>]*target=\"_blank\"[^>]*>", _fix, html)


def escapeHtml(text: str) -> str:
    """纯文本 → HTML 转义（TEXT 类预览用；也是 Office 渲染的基础）。

    **用标准库 html.escape，不用 bleach**：bleach 的 ``strip=True`` 会把
    ``<b>`` 当标签**吃掉**（连同内容）⇒ 纯文本里的 "1 < 2" 会被吞成 "1 "。
    纯文本要的是「转义成实体」，不是「按标签解析」。
    """
    import html as htmlLib

    return htmlLib.escape(text, quote=True)
