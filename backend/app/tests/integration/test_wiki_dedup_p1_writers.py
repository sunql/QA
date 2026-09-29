"""feat-wiki-dedup P1 — 四个写路径诚实报 content_hash 冲突（真实 PG + 完整 API 链路）。

迁移 0061 给 ``document_catalog(content_hash)`` 加了唯一索引
``uq_document_catalog_content_hash``。本文件钉死四个写路径在撞这把锁时的**可观察
行为**（4xx + 报真实冲突来源），而不是让 IntegrityError 裸冒成 500 或吞成错误消息：

- 1a ``POST /documents`` 创建：content_hash 冲突 → 409，报唯一索引名，不复用
  「编号重复」错误。
- 1b ``PUT /documents/{id}`` 更新：content_hash 冲突 → 409，报唯一索引名。
- 1c ``POST /wiki/import/preview-file`` 登记：同 hash 幂等 → 两次都 200，同一行。
- 1d ``POST /documents/upload`` RAG 入库：content_hash 冲突在 MinIO/Milvus 写入
  **之前**拦截 → 409，报**已存在**的文档编号，且不落任何孤儿对象/向量。
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

from httpx import AsyncClient

_BASE = "/api/v1"

# 64 位小写十六进制占位（content_hash 列 String(64)，这里只关心长度与唯一性，
# 不关心是否为真实文件摘要）。
_HASH_A = "a" * 64
_HASH_B = "b" * 64

# 两个唯一索引的约束名，用于断言「报的是哪把锁」。
_UQ_DOCUMENT_CONTENT_HASH = "uq_document_catalog_content_hash"


async def test_create_content_hash_conflict_reports_index_not_document_id(
    client: AsyncClient,
) -> None:
    """1a：同一 content_hash 换 document_id 再建 → 409 且报内容重复，不是「编号重复」。"""
    payload_a = {
        "documentId": "DOC-HASH-A",
        "documentName": "甲",
        "documentType": "CONTRACT",
        "contentHash": _HASH_A,
    }
    resp_a = await client.post(f"{_BASE}/documents", json=payload_a)
    assert resp_a.status_code == 201, resp_a.text

    payload_b = {
        "documentId": "DOC-HASH-B",  # 编号不同，撞的是 content_hash 唯一索引
        "documentName": "乙",
        "documentType": "CONTRACT",
        "contentHash": _HASH_A,
    }
    resp_b = await client.post(f"{_BASE}/documents", json=payload_b)

    assert resp_b.status_code == 409, resp_b.text
    body = resp_b.json()
    # 报真实冲突来源：唯一索引名 + 内容重复语义
    assert _UQ_DOCUMENT_CONTENT_HASH in body["error"]
    assert "文档内容重复" in body["error"]
    # 绝不复用 document_id 的「编号重复」错误（否则运维会去换编号而非换文件）
    assert "文档编号" not in body["error"]


async def test_create_document_id_conflict_still_reports_document_duplicate(
    client: AsyncClient,
) -> None:
    """回归：编号重复仍走「编号重复」错误，与内容重复严格区分。"""
    payload = {
        "documentId": "DOC-ID-DUP",
        "documentName": "甲",
        "documentType": "CONTRACT",
        "contentHash": _HASH_A,
    }
    assert (await client.post(f"{_BASE}/documents", json=payload)).status_code == 201
    resp_dup = await client.post(f"{_BASE}/documents", json=payload)

    assert resp_dup.status_code == 409
    assert "文档编号" in resp_dup.json()["error"]


async def test_update_content_hash_conflict_reports_index(
    client: AsyncClient,
) -> None:
    """1b：PUT 把 content_hash 改成已存在的值 → 409 且报内容重复。"""
    resp_a = await client.post(
        f"{_BASE}/documents",
        json={
            "documentId": "DOC-UPD-A",
            "documentName": "甲",
            "documentType": "CONTRACT",
            "contentHash": _HASH_A,
        },
    )
    assert resp_a.status_code == 201, resp_a.text

    resp_b = await client.post(
        f"{_BASE}/documents",
        json={
            "documentId": "DOC-UPD-B",
            "documentName": "乙",
            "documentType": "CONTRACT",
            "contentHash": _HASH_B,
        },
    )
    assert resp_b.status_code == 201, resp_b.text
    doc_b_id = resp_b.json()["id"]

    upd = await client.put(
        f"{_BASE}/documents/{doc_b_id}", json={"contentHash": _HASH_A}
    )

    assert upd.status_code == 409, upd.text
    body = upd.json()
    assert _UQ_DOCUMENT_CONTENT_HASH in body["error"]
    assert "文档内容重复" in body["error"]


async def test_registrar_upsert_same_hash_returns_same_row(
    client: AsyncClient, fakeMinio
) -> None:
    """1c：同一文件两次登记 → 两次都 200，catalog 仍是同一行（幂等）。

    ``upsertByContentHash`` 从 SELECT-then-INSERT 改为 ``INSERT ... ON CONFLICT
    DO NOTHING``，成功语义不变：同 hash 返回既有行，不新增第二行。
    """
    body = "# 同一份\n\n同样的内容".encode("utf-8")
    expected_hash = hashlib.sha256(body).hexdigest()
    files = {"file": ("same.md", body, "text/plain")}

    first = await client.post(f"{_BASE}/wiki/import/preview-file", files=files)
    assert first.status_code == 200, first.text

    docs_after_first = (await client.get(f"{_BASE}/documents")).json()
    assert len(docs_after_first) == 1
    first_id = docs_after_first[0]["id"]

    second = await client.post(f"{_BASE}/wiki/import/preview-file", files=files)
    assert second.status_code == 200, second.text

    docs_after_second = (await client.get(f"{_BASE}/documents")).json()
    assert len(docs_after_second) == 1, "同 hash 重复登记产生了第二行 catalog"
    assert docs_after_second[0]["id"] == first_id, "幂等登记必须返回既有行"
    # 登记的是**文件字节**摘要，且 document_id 带 wiki 前缀
    assert docs_after_second[0]["contentHash"] == expected_hash
    assert docs_after_second[0]["documentId"].startswith("DOC-WIKI-")


async def test_upload_duplicate_content_rejected_before_writes(
    client: AsyncClient, fakeMinio, mockEmbeddingService
) -> None:
    """1d：同一文件再上传 → 409 在 MinIO/Milvus 写入前拦截，报已存在文档编号。

    关键断言：第二次上传不调用 putSourceObject / insertDocumentChunks
    （否则留下孤儿源对象与 chunk 向量），且消息点名的是**第一次**那行的
    document_id，不是新造的 DOC-<uuid>。
    """
    content = b"Supplier 100001 has OTD rate 98%."
    files = {"file": ("contract.txt", content, "text/plain")}

    with patch(
        "app.services.rag_service._getEmbeddingService",
        return_value=mockEmbeddingService,
    ), patch(
        "app.services.rag_service.insertDocumentChunks",
    ) as mock_insert:
        first = await client.post(
            f"{_BASE}/documents/upload",
            params={"documentType": "CONTRACT", "securityLevel": "L1"},
            files=files,
        )
        assert first.status_code == 201, first.text
        first_doc_id = first.json()["document_id"]
        assert mock_insert.call_count == 1

        second = await client.post(
            f"{_BASE}/documents/upload",
            params={"documentType": "CONTRACT", "securityLevel": "L1"},
            files=files,
        )

    assert second.status_code == 409, second.text
    body = second.json()
    # 消息点名的是**已存在**的文档，且提到唯一索引（内容重复语义）
    assert first_doc_id in body["error"]
    assert _UQ_DOCUMENT_CONTENT_HASH in body["error"]

    # 第二次上传不落任何孤儿：MinIO 只写了一次、Milvus 只写了一次
    assert fakeMinio.put_object.call_count == 1
    assert mock_insert.call_count == 1
