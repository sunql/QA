"""预览类别的**单一事实源**守卫（0163 的元数据推断口径）。

判据要点：
1. **MIME 优先于扩展名**（扩展名可伪造，MIME 是上传时的真实声明）；
2. PDF 不转（走原件直出），Office 归一类（统一服务端渲染）；
3. 认不出来的格式 ⇒ ``TEXT``（最保守：按纯文本渲染**不会执行脚本**）；
4. 中文/大小写扩展名、大小写 MIME 都要归一。
"""

from __future__ import annotations

import pytest

from app.services.document_preview_kinds import (
    KIND_HTML,
    KIND_IMAGE,
    KIND_MARKDOWN,
    KIND_OFFICE,
    KIND_PDF,
    KIND_TEXT,
    PREVIEW_KINDS,
    extensionOf,
    isInlineViewable,
    kindFor,
    mimeFor,
)


@pytest.mark.parametrize(
    ("mime", "filename", "expected"),
    [
        ("application/pdf", "a.pdf", KIND_PDF),
        ("text/markdown", "a.md", KIND_MARKDOWN),
        ("text/html", "a.html", KIND_HTML),
        ("image/svg+xml", "a.svg", KIND_HTML),
        ("text/plain", "a.txt", KIND_TEXT),
        ("text/csv", "a.csv", KIND_TEXT),
        ("image/png", "a.png", KIND_IMAGE),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "a.docx",
            KIND_OFFICE,
        ),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "a.xlsx",
            KIND_OFFICE,
        ),
        (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "a.pptx",
            KIND_OFFICE,
        ),
    ],
)
def test_kindFor_knownCombinations(mime: str, filename: str, expected: str) -> None:
    assert kindFor(mime, filename) == expected


def test_kindFor_mimeWinsOverExtension() -> None:
    """扩展名可伪造（把 .html 改名成 .pdf）⇒ MIME 优先。"""
    assert kindFor("text/html", "fake.pdf") == KIND_HTML


def test_kindFor_fallsBackToExtensionWhenMimeUnknown() -> None:
    assert kindFor("application/octet-stream", "a.pdf") == KIND_PDF
    assert kindFor(None, "a.docx") == KIND_OFFICE


def test_kindFor_unknownFallsBackToText() -> None:
    """认不出来 ⇒ TEXT（按纯文本渲染不会执行脚本）。"""
    assert kindFor(None, "a.bin") == KIND_TEXT
    assert kindFor("application/x-weird", "a.weird") == KIND_TEXT


def test_kindFor_isCaseAndParamInsensitive() -> None:
    assert kindFor("TEXT/PLAIN; charset=utf-8", "A.TXT") == KIND_TEXT
    assert extensionOf("报告.PDF") == "pdf"


def test_mimeFor_prefersRealMime() -> None:
    assert mimeFor("text/markdown", "a.md") == "text/markdown"
    assert mimeFor(None, "a.pdf") == "application/pdf"
    assert mimeFor("application/octet-stream", "a.pdf") == "application/pdf"
    assert mimeFor(None, "a.unknown") == "application/octet-stream"


def test_isInlineViewable() -> None:
    assert isInlineViewable("application/pdf", "a.pdf") is True
    assert isInlineViewable("text/plain", "a.txt") is True
    assert isInlineViewable(None, "a.png") is True
    # Office 不能内联（浏览器打不开）⇒ 只能下载
    assert isInlineViewable(None, "a.docx") is False


def test_previewKinds_areAllProducible() -> None:
    """白名单与推断结果一致（迁移 0163 的 CHECK 约束同源）。"""
    samples = ["a.txt", "a.md", "a.html", "a.pdf", "a.docx", "a.png", "a.bin"]
    for filename in samples:
        assert kindFor(None, filename) in PREVIEW_KINDS
