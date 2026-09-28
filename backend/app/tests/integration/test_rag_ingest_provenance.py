"""RAG 上传链路的溯源端到端（P0 溯源地基）。

真实 PostgreSQL + 完整 API 链路：POST /documents/upload → rag_service →
document_catalog。MinIO 用假客户端（patch 点与 test_wiki_import_catalog
一致），Milvus 的 insert 用替身捕获 —— 定位符在 Milvus 侧的落库与回读由
test_milvus_document_fields 负责，本文件只验 rag_service 这层的接线。
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from sqlalchemy import text


def _writtenBytes(call) -> bytes | None:
    """从 ``put_object`` 的调用里取出被写入的字节。

    不假设对象流是第几个位置参数：``putSourceObject`` 对 SDK 的调用形状是
    Task 4 单测的契约，这里只关心「写进去的是不是原始字节」。

    直接写 ``content in call.args`` 是**不成立**的 —— 实现传的是
    ``io.BytesIO(content)``，与裸 bytes 做身份/相等比较永远为 False，
    断言会无条件失败（已实测）。
    """
    for value in (*call.args, *call.kwargs.values()):
        if hasattr(value, "getvalue"):
            return value.getvalue()
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
    return None


class TestRagUploadProvenance:
    @pytest.mark.asyncio
    async def test_upload_persists_source_bytes_and_locators(
        self, client, dbSession, fakeMinio, mockEmbeddingService
    ) -> None:
        # Arrange
        # 夹具用 Markdown 而不是 .txt：TXT/MD 都没有页码，.txt 更连章节都没有，
        # 那样「定位符接进去了」这条就只能退化成断言 None，等于没验。
        # 带 `#` 章节的 MD 能同时验到 section_name 的精确透传与段号。
        content = (
            "# 第一章 供应商准入\n\n"
            "第一段：供应商准入需注册资本不少于一千万。\n\n"
            "第二段：质量协议每年复核一次。"
        ).encode("utf-8")
        files = {"file": ("provenance.md", content, "text/markdown")}

        # Act
        with patch(
            "app.services.rag_service._getEmbeddingService",
            return_value=mockEmbeddingService,
        ), patch(
            "app.services.rag_service.insertDocumentChunks"
        ) as mockInsert:
            resp = await client.post(
                "/api/v1/documents/upload",
                params={"documentType": "CONTRACT", "securityLevel": "L1"},
                files=files,
            )

        # Assert 1：源文件真的写进了对象存储，且写的是原始字节
        assert resp.status_code == 201, resp.text
        assert fakeMinio.put_object.call_count == 1
        putArgs = fakeMinio.put_object.call_args
        written = _writtenBytes(putArgs)
        assert written is not None, "put_object 没收到文件内容流"
        assert written == content, "写入对象存储的不是原始字节"

        body = resp.json()
        assert body["storage_url"].startswith("s3://")

        # Assert 2：catalog 落的是真实 url + 64 位摘要（不是 milvus://N_chunks）
        assert re.fullmatch(r"[0-9a-f]{64}", body["content_hash"])
        rows = await dbSession.execute(
            text(
                "SELECT storage_url, content_hash FROM document_catalog "
                "WHERE document_id = :docId"
            ),
            {"docId": body["document_id"]},
        )
        row = rows.one()
        assert row.storage_url == body["storage_url"]
        assert row.content_hash == body["content_hash"]
        assert not row.storage_url.startswith("milvus://")

        # Assert 3：定位符接进了 Milvus 记录
        # 按 Markdown 的真实语义断言：有章节、有段号、**没有页码**（只有 PDF 有页）。
        # 哨兵（-1 / ""）是 insertDocumentChunks 的职责，而它在本用例里被替身接管，
        # 所以这里拿到的是 chunk.metadata 的原样值 —— 那正是本层要验的接线。
        record = mockInsert.call_args.args[0][0]
        assert record["section_name"] == "第一章 供应商准入"
        assert record["paragraph_no"] == 1
        assert record["page_number"] is None
