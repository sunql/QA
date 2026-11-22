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
from types import SimpleNamespace
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
def ensureMilvusDatabase() -> None:
    """确保 CI 的 Milvus 隔离库存在（幂等）。

    隔离库是**惰性创建**的（只在首次用到本体清理夹具时建），而 CI 分片按
    「排序后文件下标 mod N」切分 ⇒ 新增 integration 文件会把一部分文件挪到
    别的分片；落到一个「还没有任何文件建过库」的分片时，直接撞
    ``database not found[qa_ci]``，在闸门上表现为**净新增失败**（假回归）。

    本文件测向量化，必须自己确保库存在（与 ``test_milvus_document_fields``
    同一约定）。
    """
    from app.tests.milvus_isolation import ensureIsolatedDatabaseForTest

    ensureIsolatedDatabaseForTest()


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


# ---------------------------------------------------------------------------
# Office → PDF 预览（保真）
# ---------------------------------------------------------------------------


async def test_officePreviewReturnsPdfWhenConverterAvailable(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio, monkeypatch
) -> None:
    """OFFICE + 转换可用 ⇒ 直接返回 **application/pdf**（浏览器原生渲染，保真）。"""
    from app.services import document_office_pdf as officePdf

    async def _fakeConvert(content, filename, **kwargs):
        return b"%PDF-1.7 converted"

    monkeypatch.setattr(officePdf, "convertToPdfAsync", _fakeConvert)
    monkeypatch.delenv("OFFICE_PDF_PREVIEW", raising=False)

    created = await _createDocument(
        client, dbSession, fakeMinio, filename="a.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    resp = await client.get(f"/api/v1/documents/{created['id']}/preview")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content == b"%PDF-1.7 converted"
    # 下载名是 .pdf（不是原 xlsx）
    assert ".pdf" in resp.headers["content-disposition"]


async def test_officePreviewFallsBackToHtmlWhenConverterFails(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio, monkeypatch
) -> None:
    """转换不可用 ⇒ **退回 HTML 渲染**（降级，不 500、不空白）。"""
    from app.services import document_office_pdf as officePdf

    async def _fail(content, filename, **kwargs):
        return None

    monkeypatch.setattr(officePdf, "convertToPdfAsync", _fail)

    created = await _createDocument(
        client, dbSession, fakeMinio, filename="a.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    resp = await client.get(f"/api/v1/documents/{created['id']}/preview")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")


async def test_officePreviewRespectsDisabledFlag(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio, monkeypatch
) -> None:
    """`OFFICE_PDF_PREVIEW=0` ⇒ 不转换，直接 HTML。"""
    from app.services import document_office_pdf as officePdf

    async def _shouldNotRun(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("开关关闭时不得调用转换器")

    monkeypatch.setattr(officePdf, "convertToPdfAsync", _shouldNotRun)
    monkeypatch.setenv("OFFICE_PDF_PREVIEW", "0")

    created = await _createDocument(
        client, dbSession, fakeMinio, filename="a.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    resp = await client.get(f"/api/v1/documents/{created['id']}/preview")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


# ---------------------------------------------------------------------------
# 文档关联目标（新建关联的可搜索下拉，2026-11-22 用户反馈）
# ---------------------------------------------------------------------------


async def test_relationTargets_ontologyClass(client: AsyncClient, dbSession: Any) -> None:
    """本体类候选：按类名/别名搜索，key 存**类名**（可读、可引用）。"""
    from app.domain.models import OntologyClass

    dbSession.add(
        OntologyClass(class_name="DIM_SUPPLIER", class_alias="供应商主数据")
    )
    await dbSession.commit()

    resp = await client.get(
        "/api/v1/documents/relations/targets",
        params={"entityType": "ONTOLOGY_CLASS", "q": "供应商"},
    )

    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert rows and rows[0]["key"] == "DIM_SUPPLIER"
    assert "供应商" in rows[0]["label"]


async def test_relationTargets_metric(client: AsyncClient, dbSession: Any) -> None:
    from app.domain.models import OntologyMetric

    dbSession.add(OntologyMetric(metric_name="到货及时率", formula="1"))
    await dbSession.commit()

    resp = await client.get(
        "/api/v1/documents/relations/targets",
        params={"entityType": "ONTOLOGY_METRIC", "q": "及时"},
    )

    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert rows[0]["key"] == "到货及时率"


async def test_relationTargets_propertyUsesClassDotName(
    client: AsyncClient, dbSession: Any
) -> None:
    """属性 key = 类名.属性名（脱离类名的属性名无法唯一定位）。"""
    from app.domain.models import OntologyClass, OntologyProperty

    klass = OntologyClass(class_name="DIM_SUPPLIER", class_alias="供应商")
    dbSession.add(klass)
    await dbSession.commit()
    await dbSession.refresh(klass)
    dbSession.add(
        OntologyProperty(
            class_id=klass.id,
            property_name="SUPPLIER_NAME",
            property_alias="供应商名称",
            data_type="string",  # NOT NULL
        )
    )
    await dbSession.commit()

    resp = await client.get(
        "/api/v1/documents/relations/targets",
        params={"entityType": "ONTOLOGY_PROPERTY", "q": "SUPPLIER_NAME"},
    )

    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert rows[0]["key"] == "DIM_SUPPLIER.SUPPLIER_NAME"


async def test_relationTargets_emptyQueryBrowses(client: AsyncClient, dbSession: Any) -> None:
    """空关键词也要能取到候选（让用户先浏览，不必先想关键词）。"""
    from app.domain.models import OntologyClass

    dbSession.add(OntologyClass(class_name="DIM_SUPPLIER"))
    await dbSession.commit()

    resp = await client.get(
        "/api/v1/documents/relations/targets",
        params={"entityType": "ONTOLOGY_CLASS"},
    )

    assert resp.status_code == 200
    assert len(resp.json()) >= 1


async def test_relationTargets_rejectsUnknownType(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/documents/relations/targets",
        params={"entityType": "NOT_A_CODE"},
    )

    assert resp.status_code == 422, resp.text


async def test_createRelation_acceptsKnowledgeEntityType(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """关联到本体类（不是业务对象码）必须能建——这是用户的核心诉求。"""
    created = await _createDocument(client, dbSession, fakeMinio)

    resp = await client.post(
        "/api/v1/documents/relations",
        json={
            "documentId": created["documentId"],
            "entityType": "ONTOLOGY_CLASS",
            "entityKey": "DIM_SUPPLIER",
            "relationType": "SPEC",
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["entityType"] == "ONTOLOGY_CLASS"
    assert body["entityKey"] == "DIM_SUPPLIER"


# ---------------------------------------------------------------------------
# 关联修改（2026-11-22 用户反馈：建了不能改）
# ---------------------------------------------------------------------------


async def _createRelation(client: AsyncClient, documentId: str, **overrides):
    payload = {
        "documentId": documentId,
        "entityType": "ONTOLOGY_CLASS",
        "entityKey": "DIM_SUPPLIER",
        "relationType": "SPEC",
        **overrides,
    }
    resp = await client.post("/api/v1/documents/relations", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_updateRelation_changesTargetAndType(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    created = await _createDocument(client, dbSession, fakeMinio)
    rel = await _createRelation(client, created["documentId"])

    resp = await client.put(
        f"/api/v1/documents/relations/{rel['id']}",
        json={
            "entityType": "ONTOLOGY_METRIC",
            "entityKey": "到货及时率",
            "relationType": "8D_REPORT",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entityType"] == "ONTOLOGY_METRIC"
    assert body["entityKey"] == "到货及时率"
    assert body["relationType"] == "8D_REPORT"
    assert body["documentId"] == created["documentId"]


async def test_updateRelation_partialKeepsOtherFields(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    created = await _createDocument(client, dbSession, fakeMinio)
    rel = await _createRelation(client, created["documentId"])

    resp = await client.put(
        f"/api/v1/documents/relations/{rel['id']}", json={"relationType": "SOP"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["relationType"] == "SOP"
    # 只改了类型，目标不动
    assert body["entityType"] == "ONTOLOGY_CLASS"
    assert body["entityKey"] == "DIM_SUPPLIER"


async def test_updateRelation_conflictWhenDuplicating(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """改成已存在的组合 ⇒ 409（不能静默产生重复行）。"""
    created = await _createDocument(client, dbSession, fakeMinio)
    first = await _createRelation(client, created["documentId"])
    second = await _createRelation(
        client, created["documentId"], entityKey="DIM_ITEM", relationType="SOP"
    )

    resp = await client.put(
        f"/api/v1/documents/relations/{second['id']}",
        json={"entityKey": "DIM_SUPPLIER", "relationType": "SPEC"},
    )

    assert resp.status_code == 409, resp.text
    assert first["id"] != second["id"]


async def test_updateRelation_404ForUnknown(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/v1/documents/relations/999999", json={"relationType": "SOP"}
    )

    assert resp.status_code == 404, resp.text


async def test_documentEvidenceCarriesRelatedEntities(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio, mockEmbeddingService: Any
) -> None:
    """**关联要能进 LLM**：文档检索的证据必须带 `relatedEntities`。

    2026-11-22 实测：关联此前只存不用——AIChat/Agent 路径没人传 entityKeys，
    关联对模型完全不可见。现在证据回填关联对象，Agent/Copilot 的答案里能看到
    「这份文档讲的是哪个对象」。
    """
    import hashlib

    from sqlalchemy import select

    from app.domain.models import DocumentCatalog

    content = "供应商准入制度：注册资本不得低于 1000 万。".encode("utf-8")
    created = await _createDocument(
        client, dbSession, fakeMinio, content=content, filename="准入制度.md",
        mime="text/markdown",
    )
    await _createRelation(client, created["documentId"])

    # 让文档有向量（检索需要 chunk）
    with patch(
        "app.services.rag_service._getEmbeddingService",
        return_value=mockEmbeddingService,
    ):
        resp = await client.post(
            f"/api/v1/documents/{created['id']}/vectorize"
        )
    assert resp.status_code == 200, resp.text

    # 直接走服务层（searchFn 是该模块文档规定的注入点）：
    # 知识检索 HTTP 路由受 enableKnowledgeFacade 开关控制，与本用例要验证的
    # 「关联是否回填到证据」无关。
    from app.services.knowledge.document_search import searchProcurementDocuments

    async def _fakeSearch(query, *, user, session, top_k):
        return [
            {
                "document_id": created["documentId"],
                "chunk_text": content.decode("utf-8"),
                "chunk_sequence": 0,
                "distance": 0.1,
            }
        ]

    actor = SimpleNamespace(
        userId="admin", roles=("admin",), departments=(), dbUserId=1
    )
    rows = await searchProcurementDocuments(
        dbSession, actor, query="供应商准入 注册资本", topK=3, searchFn=_fakeSearch
    )

    assert rows, "应有检索命中"
    related = rows[0].related_entities
    assert any(
        item.type == "ONTOLOGY_CLASS" and item.code == "DIM_SUPPLIER"
        for item in related
    ), f"证据未回填关联：{related}"


# ---------------------------------------------------------------------------
# AI 上下文人工开关（0165，用户裁定：人工而非自动判罚）
# ---------------------------------------------------------------------------


async def test_ai_context_defaults_enabled(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """新文档默认「可用于 AI 上下文」。"""
    created = await _createDocument(client, dbSession, fakeMinio)

    detail = await client.get(f"/api/v1/documents/{created['id']}")

    assert detail.status_code == 200, detail.text
    assert detail.json()["aiContextEnabled"] is True


async def test_ai_context_canBeTurnedOffAndBackOn(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio
) -> None:
    """人工开关：关掉附原因、再打开。"""
    created = await _createDocument(client, dbSession, fakeMinio)

    off = await client.put(
        f"/api/v1/documents/{created['id']}",
        json={"aiContextEnabled": False, "aiContextNote": "文本疑似乱码"},
    )
    assert off.status_code == 200, off.text
    assert off.json()["aiContextEnabled"] is False
    assert off.json()["aiContextNote"] == "文本疑似乱码"

    on = await client.put(
        f"/api/v1/documents/{created['id']}",
        json={"aiContextEnabled": True, "aiContextNote": None},
    )
    assert on.status_code == 200, on.text
    assert on.json()["aiContextEnabled"] is True


async def test_disabledDocumentIsExcludedFromRetrieval(
    client: AsyncClient, dbSession: Any, fakeMinio: _FakeMinio, mockEmbeddingService: Any
) -> None:
    """关掉开关的文档**不参与检索**（人工决定生效于检索面）。"""
    from types import SimpleNamespace

    from app.services.knowledge.document_search import searchProcurementDocuments

    content = "供应商准入制度：注册资本不得低于 1000 万。".encode("utf-8")
    created = await _createDocument(
        client, dbSession, fakeMinio, content=content, filename="准入制度.md",
        mime="text/markdown",
    )

    async def _fakeSearch(query, *, user, session, top_k):
        return [
            {
                "document_id": created["documentId"],
                "chunk_text": content.decode("utf-8"),
                "chunk_sequence": 0,
                "distance": 0.1,
            }
        ]

    actor = SimpleNamespace(userId="admin", roles=("admin",), departments=(), dbUserId=1)

    # 开关开 ⇒ 能检索到
    rows = await searchProcurementDocuments(
        dbSession, actor, query="供应商准入", topK=3, searchFn=_fakeSearch
    )
    assert rows, "开启时应能检索到"

    # 关掉后再检索 ⇒ 空
    await client.put(
        f"/api/v1/documents/{created['id']}",
        json={"aiContextEnabled": False, "aiContextNote": "测试关闭"},
    )
    rows = await searchProcurementDocuments(
        dbSession, actor, query="供应商准入", topK=3, searchFn=_fakeSearch
    )
    assert rows == [], "关闭后不得参与检索"
