"""Phase 5.2 document_parser 单测（TDD RED）。

RED: 先写测试（FAIL）→ 实现（GREEN）→ 重构（IMPROVE）。
"""

from __future__ import annotations

import pytest

from app.services.document_parser import (
    DocumentParserError,
    parse_document,
)


class TestParseDocument:
    """按文件类型分发解析。"""

    @pytest.mark.asyncio
    async def test_plain_text_utf8(self) -> None:
        content = "这是一段测试文本。\n第二行。"
        result = await parse_document(content.encode("utf-8"), "text/plain", "readme.txt")
        assert result == "这是一段测试文本。\n第二行。"

    @pytest.mark.asyncio
    async def test_markdown_file(self) -> None:
        content = "# 标题\n\n正文内容。"
        result = await parse_document(content.encode("utf-8"), "text/plain", "doc.md")
        assert result == "# 标题\n\n正文内容。"

    @pytest.mark.asyncio
    async def test_unsupported_type_raises(self) -> None:
        with pytest.raises(DocumentParserError):
            await parse_document(b"\x00\x01", "application/octet-stream", "file.bin")

    @pytest.mark.asyncio
    async def test_pdf_parsing(self) -> None:
        """PDF 解析：pypdf 必装后必须真跑（之前 importorskip 在没装时静默跳过 = 实际未覆盖）。"""
        # 用已知有效的最小 PDF（单页无内容）
        minimal_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>\nendobj\n"
            b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>\nendobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>\nendobj\n"
            b"xref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n0000000058 00000 n\n0000000115 00000 n\n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n194\n%%EOF"
        )
        result = await parse_document(minimal_pdf, "application/pdf", "test.pdf")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_docx_parsing(self) -> None:
        """DOCX 解析：python-docx 必装后必须真跑。"""
        import zipfile, io

        docx_bytes = _make_minimal_docx("Test paragraph content.")
        result = await parse_document(
            docx_bytes,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "test.docx",
        )
        assert "Test paragraph content." in result

    @pytest.mark.asyncio
    async def test_pdf_fallback_by_extension(self) -> None:
        """扩展名可推断类型，即使 MIME type 未知。"""
        content = b"dummy pdf content"
        with pytest.raises(DocumentParserError):
            await parse_document(content, "application/octet-stream", "file.pdf")


def _make_minimal_docx(text: str) -> bytes:
    """构造只含一段文本的最小 DOCX。"""
    import zipfile, io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # [Content_Types].xml
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        # _rels/.rels
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        # word/document.xml
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            f"<w:p><w:r><w:t>{escaped}</w:t></w:r></w:p>"
            "</w:body>"
            "</w:document>",
        )
    buf.seek(0)
    return buf.read()
