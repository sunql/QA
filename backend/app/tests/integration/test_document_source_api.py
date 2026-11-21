"""原文阅览端点 `GET /api/v1/documents/{id}/source`（真实 PG + 假 MinIO）。

判据要点（按处置动作分档）：
1. **正常**：返回原件字节 + 正确 Content-Type + 中文文件名（RFC 5987）；
2. **403**：密级不在 clearance 内 ⇒ **不许**返回字节（原文比检索片段敏感）；
3. **404**：文档没有源文件（storage_url 空 / 对象已被删）；
4. **503**：对象存储故障（**不是**「文件没了」）；
5. **下载模式**：`disposition=attachment` ⇒ Content-Disposition 是 attachment。
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterator
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from app.domain.enums import DocumentSecurityLevel

_CLIENT = "app.infrastructure.object_storage._getClient"
_SOURCE_PATH = "/api/v1/documents/{id}/source"


class _FakeMinio:
    """最小 MinIO 替身：只实现本用例需要的 stat/get。"""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.getError: Exception | None = None
        self.putCalls: list[dict[str, Any]] = []

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def make_bucket(self, bucket: str) -> None:
        return None

    def put_object(self, bucket, objectName, data, length=None, content_type=None):
        content = data.read() if hasattr(data, "read") else bytes(data)
        self.objects[(bucket, objectName)] = content
        self.putCalls.append(
            {"bucket": bucket, "objectName": objectName, "content": content}
        )

    def get_object(self, bucket: str, objectName: str):
        if self.getError is not None:
            raise self.getError
        if (bucket, objectName) not in self.objects:
            raise RuntimeError("NoSuchKey: not found")
        outer = self

        class _Resp:
            def read(self) -> bytes:
                return outer.objects[(bucket, objectName)]

            def close(self) -> None:
                pass

            def release_conn(self) -> None:
                pass

        return _Resp()

    def stat_object(self, bucket: str, objectName: str):
        data = self.objects[(bucket, objectName)]
        return type("Stat", (), {"size": len(data)})()


@pytest.fixture(autouse=True)
def fakeMinio() -> Iterator[_FakeMinio]:
    fake = _FakeMinio()
    with patch(_CLIENT, return_value=fake):
        yield fake


async def _createDocument(
    client: AsyncClient,
    dbSession: Any,
    fakeMinio: _FakeMinio,
    *,
    content: bytes = "## 采购制度\n\n注册资本须 >= 1000 万。".encode("utf-8"),
    filename: str = "采购与供应商控制程序.md",
    mime: str = "text/markdown",
    securityLevel: str = "L1",
) -> dict[str, Any]:
    """造一份「catalog 行 + MinIO 对象」。

    **不经上传接口**：上传会真调 embedding（本地服务离线 ⇒ 422），而本用例
    要测的是**读取端点**，不该被向量化链路绑住。
    """
    import hashlib

    from app.domain.models import DocumentCatalog

    contentHash = hashlib.sha256(content).hexdigest()
    objectName = f"sources/{contentHash[:2]}/{contentHash}/{filename}"
    fakeMinio.objects[("qa-knowledge-sources", objectName)] = content

    row = DocumentCatalog(
        document_id=f"DOC-SRC-{contentHash[:8].upper()}",
        document_name=filename,
        document_type="OTHER",
        version="v1.0",
        status="ACTIVE",
        owner=None,
        security_level=securityLevel,
        storage_url=f"s3://qa-knowledge-sources/{objectName}",
        content_hash=contentHash,
        mime_type=mime,
        file_size=len(content),
        preview_kind="MARKDOWN",
    )
    dbSession.add(row)
    await dbSession.commit()
    await dbSession.refresh(row)
    return {
        "id": row.id,
        "documentId": row.document_id,
        "mimeType": row.mime_type,
        "fileSize": row.file_size,
        "previewKind": row.preview_kind,
        "vectorizedAt": None,
    }


async def test_source_returns_original_bytes(client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio) -> None:
    """正常路径：字节、Content-Type、中文文件名都正确。"""
    body = b"%PDF-1.7 hello world"
    created = await _createDocument(client, dbSession, fakeMinio, content=body)

    resp = await client.get(_SOURCE_PATH.format(id=created["id"]))

    assert resp.status_code == 200, resp.text
    assert resp.content == body
    assert resp.headers["content-type"].startswith("text/markdown")
    # 中文文件名：RFC 5987
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].startswith("inline")
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_source_attachment_disposition(client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio) -> None:
    """下载模式 ⇒ attachment。"""
    created = await _createDocument(client, dbSession, fakeMinio)

    resp = await client.get(
        _SOURCE_PATH.format(id=created["id"]), params={"disposition": "attachment"}
    )

    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")


async def test_source_denied_for_l3_document(client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio) -> None:
    """密级不够 ⇒ 403，**且不返回任何字节**。

    原文是整份文件（比检索片段敏感得多）⇒ ACL 必须先于读字节。
    """
    created = await _createDocument(client, dbSession, fakeMinio, securityLevel="L3")

    resp = await client.get(_SOURCE_PATH.format(id=created["id"]))

    assert resp.status_code == 403, resp.text
    assert "注册资本" not in resp.text


async def test_source_404_when_no_source_file(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """catalog 行存在但没有源文件 ⇒ 404（不是 500）。"""
    from sqlalchemy import update

    from app.domain.models import DocumentCatalog

    created = await _createDocument(client, dbSession, fakeMinio)
    await dbSession.execute(
        update(DocumentCatalog)
        .where(DocumentCatalog.id == created["id"])
        .values(storage_url=None)
    )
    await dbSession.commit()

    resp = await client.get(_SOURCE_PATH.format(id=created["id"]))

    assert resp.status_code == 404, resp.text


async def test_source_503_when_object_storage_fails(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """存储故障 ⇒ 503（基础设施问题，不是「文件没了」）。"""
    from app.infrastructure.object_storage import ObjectStorageError

    created = await _createDocument(client, dbSession, fakeMinio)
    fakeMinio.getError = ObjectStorageError("connection refused")

    resp = await client.get(_SOURCE_PATH.format(id=created["id"]))

    assert resp.status_code == 503, resp.text


async def test_source_404_for_unknown_document(client: AsyncClient) -> None:
    resp = await client.get(_SOURCE_PATH.format(id=999999))

    assert resp.status_code == 404, resp.text


async def test_uploaded_document_has_preview_metadata(
    client: AsyncClient, mockEmbeddingService: Any
) -> None:
    """0163 元数据在**上传时**就写齐（mime / size / 预览类别 / 向量化状态）。

    走真实上传链路（解析 → MinIO → catalog → 向量化），embedding 用替身。
    """
    with patch(
        "app.services.rag_service._getEmbeddingService",
        return_value=mockEmbeddingService,
    ):
        body = "## 标题\n\n正文内容".encode("utf-8")
        resp = await client.post(
            "/api/v1/documents/upload",
            files={"file": ("规则.md", body, "text/markdown")},
            data={"documentType": "OTHER", "securityLevel": "L1"},
        )

    assert resp.status_code == 201, resp.text
    # 上传端点返回的是**裸 dict**（snake_case，不经 CamelModel）⇒ 按其契约断言
    data = resp.json()
    assert data["mime_type"] == "text/markdown"
    assert data["file_size"] == len(body)
    assert data["preview_kind"] == "MARKDOWN"
    # 上传路径本身会向量化 ⇒ vectorized_at 有值（目录页据此显示「已向量化」）
    assert data["vectorized_at"] is not None
