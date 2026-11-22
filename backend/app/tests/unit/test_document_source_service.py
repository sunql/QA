"""文档源文件读取（原文阅览）的纯逻辑守卫。

判据要点：
1. **storage_url 解析**：`s3://bucket/sources/xx/<hash>/<名>` → 对象名 + 原文件名；
   对象名里的中文是 URL 编码 ⇒ 必须解码（否则读不到对象）。
2. **中文文件名**：`Content-Disposition` 必须带 RFC 5987 的 `filename*=UTF-8''`
   （只给 ASCII `filename=` ⇒ 下载下来是乱码）。
3. **内联 vs 下载**：PDF/文本/图片可内联；Office 只能下载（浏览器打不开）。
4. **「不存在」vs「故障」**：对象不存在 ⇒ None（正常未命中）；
   存储故障 ⇒ 冒泡（API 层转 503），**不能都吞成「文件没了」**。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.document_source_service import (
    buildSourceFile,
    contentDisposition,
    filenameFromStorageUrl,
    objectNameFromStorageUrl,
    readSourceBytes,
)


def _catalog(**overrides):
    values = {
        "document_name": "采购与供应商控制程序.pdf",
        "storage_url": "s3://qa-knowledge-sources/sources/00/abc/采购与供应商控制程序v1.pdf",
        "mime_type": "application/pdf",
        "preview_kind": "PDF",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_objectNameFromStorageUrl() -> None:
    assert (
        objectNameFromStorageUrl("s3://bucket/sources/00/abc/file.pdf")
        == "sources/00/abc/file.pdf"
    )


def test_objectName_decodesUrlEncodedChinese() -> None:
    """对象名里的中文是 URL 编码 ⇒ 必须解码（不解码读不到对象）。"""
    url = "s3://bucket/sources/00/abc/%E9%87%87%E8%B4%AD.pdf"
    assert objectNameFromStorageUrl(url) == "sources/00/abc/采购.pdf"


def test_objectName_rejectsNonS3Urls() -> None:
    assert objectNameFromStorageUrl("/local/path/file.pdf") is None
    assert objectNameFromStorageUrl("s3://bucket") is None
    assert objectNameFromStorageUrl("") is None


def test_filenameFromStorageUrl_fallsBackToCatalogName() -> None:
    assert (
        filenameFromStorageUrl(
            "s3://bucket/sources/00/abc/report.pdf", "fallback.pdf"
        )
        == "report.pdf"
    )
    assert filenameFromStorageUrl("", "fallback.pdf") == "fallback.pdf"


def test_contentDisposition_usesRfc5987ForChinese() -> None:
    header = contentDisposition("采购与供应商控制程序.pdf", inline=True)

    assert header.startswith("inline;")
    assert "filename*=UTF-8''" in header
    # ASCII 回退名：主名退化时用 "document" 占位，但**保留扩展名**
    # （只剩 ".pdf" 会被浏览器当隐藏文件）
    assert 'filename="document.pdf"' in header
    assert 'filename=".pdf"' not in header


def test_contentDisposition_attachmentForDownload() -> None:
    header = contentDisposition("a.pdf", inline=False)

    assert header.startswith("attachment;")


def test_buildSourceFile_resolvesTypeAndInlineFlag() -> None:
    source = buildSourceFile(_catalog(), b"%PDF-1.7")

    assert source.filename == "采购与供应商控制程序v1.pdf"
    assert source.contentType == "application/pdf"
    assert source.previewKind == "PDF"
    assert source.inline is True
    assert source.size == 8


def test_buildSourceFile_officeIsNotInline() -> None:
    """Office 浏览器打不开 ⇒ 只能下载（内联会得到「空白页」）。"""
    source = buildSourceFile(
        _catalog(
            storage_url="s3://b/sources/00/abc/a.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            preview_kind="OFFICE",
        ),
        b"PK",
    )

    assert source.inline is False
    assert source.previewKind == "OFFICE"


@pytest.mark.asyncio
async def test_readSourceBytes_returnsNoneWhenNoStorageUrl() -> None:
    assert await readSourceBytes(None) is None
    assert await readSourceBytes("") is None


@pytest.mark.asyncio
async def test_readSourceBytes_distinguishesMissingFromFailure(monkeypatch) -> None:
    """对象不存在 ⇒ None；存储故障 ⇒ 冒泡（不能都吞成「文件没了」）。"""
    from app.infrastructure.object_storage import ObjectStorageError

    def _raise(error: Exception) -> None:
        raise error

    monkeypatch.setattr(
        "app.infrastructure.object_storage.getSourceObject",
        lambda name: _raise(ObjectStorageError("NoSuchKey: not found")),
    )
    assert await readSourceBytes("s3://b/sources/00/abc/x.pdf") is None

    monkeypatch.setattr(
        "app.infrastructure.object_storage.getSourceObject",
        lambda name: _raise(ObjectStorageError("connection refused")),
    )
    with pytest.raises(ObjectStorageError):
        await readSourceBytes("s3://b/sources/00/abc/x.pdf")
