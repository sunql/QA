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
from app.services.document_preview_kinds import kindFor

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
        # 预览类别按**真实规则**推断（不写死）⇒ 与生产入库路径一致
        preview_kind=kindFor(mime, filename),
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


# ---------------------------------------------------------------------------
# 通用预览（需求 4）
# ---------------------------------------------------------------------------


async def test_preview_renders_markdown_as_html(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """Markdown ⇒ 自包含 HTML（渲染 + 消毒）。"""
    created = await _createDocument(
        client, dbSession, fakeMinio, content=b"# Title\n\nbody"
    )

    resp = await client.get(f"/api/v1/documents/{created['id']}/preview")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert "<h1>Title</h1>" in resp.text
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "Content-Security-Policy" in resp.headers


async def test_preview_strips_scripts_from_user_html(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """用户 HTML ⇒ 脚本不得出现在预览页里。"""
    created = await _createDocument(
        client,
        dbSession,
        fakeMinio,
        content=b"<p>ok</p><script>alert(1)</script>",
        filename="x.html",
        mime="text/html",
    )

    resp = await client.get(f"/api/v1/documents/{created['id']}/preview")

    assert resp.status_code == 200
    assert "<script" not in resp.text
    assert "alert(1)" not in resp.text
    assert "sandbox=" in resp.text


async def test_preview_redirects_pdf_to_source(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """PDF 不转（保真）⇒ 302 指到原件（浏览器原生查看器）。"""
    created = await _createDocument(
        client,
        dbSession,
        fakeMinio,
        content=b"%PDF-1.4 x",
        filename="a.pdf",
        mime="application/pdf",
    )

    resp = await client.get(
        f"/api/v1/documents/{created['id']}/preview", follow_redirects=False
    )

    assert resp.status_code == 302
    assert resp.headers["location"].endswith(f"/source")


async def test_preview_denied_for_l3_document(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """密级不够 ⇒ 403（预览与下载同一套闸）。"""
    created = await _createDocument(
        client, dbSession, fakeMinio, securityLevel="L3"
    )

    resp = await client.get(f"/api/v1/documents/{created['id']}/preview")

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 向量化（需求 3：wiki 导入的文件也能进 document_embeddings）
# ---------------------------------------------------------------------------


async def test_vectorize_readsStoredSourceAndWritesVectors(
    client: AsyncClient,
    dbSession: Any,
    fakeMinio: _FakeMinio,
    mockEmbeddingService: Any,
) -> None:
    """读回**已存的源文件** → 切块 → 写向量 ⇒ 不必重新上传。"""
    from sqlalchemy import select

    from app.domain.models import DocumentCatalog

    created = await _createDocument(
        client,
        dbSession,
        fakeMinio,
        content="## 制度\n\n第一条 注册资本 1000 万。\n\n第二条 交付延迟 30 天。".encode("utf-8"),
    )

    with patch(
        "app.services.rag_service._getEmbeddingService",
        return_value=mockEmbeddingService,
    ):
        resp = await client.post(f"/api/v1/documents/{created['id']}/vectorize")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["documentId"] == created["documentId"]
    assert body["chunks"] >= 1
    assert body["status"] == "vectorized"

    # catalog 的向量化时间已记（目录页据此显示「已向量化」）
    row = (
        await dbSession.execute(
            select(DocumentCatalog).where(DocumentCatalog.id == created["id"])
        )
    ).scalars().first()
    assert row.vectorized_at is not None


async def test_vectorize_is_idempotent(
    client: AsyncClient,
    dbSession: Any,
    fakeMinio: _FakeMinio,
    mockEmbeddingService: Any,
) -> None:
    """重复调用不产生重复 chunk（先删旧向量再写）。"""
    with patch(
        "app.services.rag_service._getEmbeddingService",
        return_value=mockEmbeddingService,
    ):
        created = await _createDocument(
            client,
            dbSession,
            fakeMinio,
            content="## 制度\n\n一条内容。".encode("utf-8"),
        )
        first = await client.post(f"/api/v1/documents/{created['id']}/vectorize")
        second = await client.post(f"/api/v1/documents/{created['id']}/vectorize")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["chunks"] == second.json()["chunks"]
    assert second.json()["replaced"] is True


async def test_vectorize_denied_for_non_owner(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """非 owner 部门 ⇒ 403（向量化消耗额度，不能任意用户触发）。"""
    from sqlalchemy import update

    from app.domain.models import DocumentCatalog

    created = await _createDocument(client, dbSession, fakeMinio)
    await dbSession.execute(
        update(DocumentCatalog)
        .where(DocumentCatalog.id == created["id"])
        .values(owner="finance-dept")
    )
    await dbSession.commit()

    resp = await client.post(
        f"/api/v1/documents/{created['id']}/vectorize",
        # header 只能是 ASCII（httpx 不编码非 ASCII header 值）
        headers={
            "X-User-Id": "someone",
            "X-User-Roles": "analyst",
            "X-User-Departments": "sales-dept",
        },
    )

    assert resp.status_code == 403, resp.text


async def test_vectorize_404_when_no_source(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """没有源文件 ⇒ 422（不是 500）。"""
    from sqlalchemy import update

    from app.domain.models import DocumentCatalog

    created = await _createDocument(client, dbSession, fakeMinio)
    await dbSession.execute(
        update(DocumentCatalog)
        .where(DocumentCatalog.id == created["id"])
        .values(storage_url=None)
    )
    await dbSession.commit()

    resp = await client.post(f"/api/v1/documents/{created['id']}/vectorize")

    assert resp.status_code == 422, resp.text


async def test_source_accepts_business_code(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """业务编号（DOC-xxx）也能取原件——采购知识证据里存的就是它。"""
    created = await _createDocument(client, dbSession, fakeMinio)

    resp = await client.get(
        f"/api/v1/documents/{created['documentId']}/source"
    )

    assert resp.status_code == 200, resp.text
    assert resp.content


async def test_preview_accepts_business_code(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    created = await _createDocument(client, dbSession, fakeMinio)

    resp = await client.get(
        f"/api/v1/documents/{created['documentId']}/preview"
    )

    assert resp.status_code == 200, resp.text
    assert "<html" in resp.text


async def test_source_unknown_business_code_is_404(
    client: AsyncClient,
) -> None:
    resp = await client.get("/api/v1/documents/DOC-NOT-EXIST/source")

    assert resp.status_code == 404, resp.text
