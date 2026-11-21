"""HTML 消毒守卫（预览用户上传文件的安全闸）。

判据要点（每条都是真实攻击面）：
1. ``<script>`` 一律剥掉（含其内容）；
2. ``on*`` 事件属性（onerror/onload/onclick…）剥掉；
3. ``javascript:`` URL 剥掉；
4. **外链图片禁止**（跟踪像素 / 视觉 SSRF）；
5. ``<iframe>`` / ``<object>`` / ``<embed>`` 剥掉；
6. Markdown 渲染出的原始 HTML **也要**消毒（markdown 库默认透传）；
7. ``target=_blank`` 的链接自动补 ``rel=noopener noreferrer``。
"""

from __future__ import annotations

from app.services.html_sanitizer import (
    escapeHtml,
    sanitizeHtml,
    sanitizeMarkdown,
)


def test_scriptTagsAreRemoved() -> None:
    out = sanitizeHtml("<p>你好</p><script>alert(1)</script>")

    assert "<script" not in out
    assert "alert(1)" not in out
    assert "你好" in out


def test_eventHandlerAttributesAreRemoved() -> None:
    out = sanitizeHtml('<img src="x.png" onerror="alert(1)">')

    assert "onerror" not in out


def test_javascriptUrlsAreRemoved() -> None:
    out = sanitizeHtml('<a href="javascript:alert(1)">点</a>')

    assert "javascript:" not in out
    assert "点" in out


def test_externalImagesAreStripped() -> None:
    """外链图片禁止（跟踪像素 / 视觉 SSRF）。"""
    out = sanitizeHtml('<img src="https://tracker.example/pixel.gif">')

    assert "<img" not in out
    assert "tracker.example" not in out


def test_iframeObjectEmbedAreRemoved() -> None:
    for tag in ("iframe", "object", "embed"):
        out = sanitizeHtml(f'<{tag} src="https://evil.example"></{tag}>')
        assert f"<{tag}" not in out


def test_tablesAndFormattingSurvive() -> None:
    out = sanitizeHtml(
        "<table><tr><th>列</th></tr><tr><td><strong>值</strong></td></tr></table>"
    )

    assert "<table>" in out
    assert "<strong>值</strong>" in out


def test_markdownIsSanitizedToo() -> None:
    """markdown 库默认透传原始 HTML ⇒ 必须消毒。"""
    out = sanitizeMarkdown("# 标题\n\n正文\n\n<script>alert(1)</script>")

    assert "<script" not in out
    assert "<h1>" in out
    assert "正文" in out


def test_markdownLinksGetNoopener() -> None:
    out = sanitizeMarkdown('[点我](https://example.com)')

    assert 'href="https://example.com"' in out


def test_blankTargetLinksGetNoopener() -> None:
    out = sanitizeHtml('<a href="https://example.com" target="_blank">x</a>')

    assert 'rel="noopener noreferrer"' in out


def test_escapeHtmlEscapesRatherThanStrips() -> None:
    """纯文本要的是**转义**（实体），不是剥除。

    bleach 的 strip=True 会把 ``<b>`` 连内容一起吃掉 ⇒ 纯文本里的
    "1 < 2" 会被吞成 "1 "。所以这里用标准库 html.escape。
    """
    assert escapeHtml("<b>粗</b>") == "&lt;b&gt;粗&lt;/b&gt;"
    assert escapeHtml("a & b") == "a &amp; b"
    assert escapeHtml("1 < 2") == "1 &lt; 2"
