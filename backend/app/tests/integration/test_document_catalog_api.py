"""文档目录 API 集成测试（真实 PostgreSQL + 完整 API 链路，Phase 5.1）。

验证 HTTP 契约（camelCase JSON）：
- GET    /api/v1/documents                         列表（过滤：type / securityLevel / status）
- GET    /api/v1/documents/{id}                   详情
- POST   /api/v1/documents                        创建（201）
- PUT    /api/v1/documents/{id}                   更新
- DELETE /api/v1/documents/{id}                    删除（204）

- GET    /api/v1/documents/relations              关联列表（过滤：documentId / entityType / entityKey）
- GET    /api/v1/documents/relations/{id}          关联详情
- POST   /api/v1/documents/relations              创建关联（201）
- DELETE /api/v1/documents/relations/{id}           删除关联（204）

测试在真实 PG 5433 上运行（qa_metadata_test 数据库）。
"""

from __future__ import annotations


class TestDocumentCatalogApi:
    """Document Catalog CRUD。"""

    async def test_list_empty(self, client) -> None:
        resp = await client.get("/api/v1/documents")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_get_roundtrip(self, client) -> None:
        # CREATE
        payload = {
            "documentId": "DOC-2026-001",
            "documentName": "供应商框架协议",
            "documentType": "CONTRACT",
            "version": "v1.0",
            "owner": "采购部",
            "securityLevel": "L2",
        }
        resp = await client.post("/api/v1/documents", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["documentId"] == "DOC-2026-001"
        assert body["documentName"] == "供应商框架协议"
        assert body["documentType"] == "CONTRACT"
        assert body["status"] == "ACTIVE"
        assert body["id"] > 0

        doc_id = body["id"]

        # GET
        detail = await client.get(f"/api/v1/documents/{doc_id}")
        assert detail.status_code == 200
        assert detail.json()["id"] == doc_id
        assert detail.json()["documentName"] == "供应商框架协议"

    async def test_create_duplicate_returns_409(self, client) -> None:
        payload = {
            "documentId": "DOC-DUP",
            "documentName": "重复测试",
            "documentType": "CONTRACT",
        }
        resp1 = await client.post("/api/v1/documents", json=payload)
        assert resp1.status_code == 201
        resp2 = await client.post("/api/v1/documents", json=payload)
        assert resp2.status_code == 409

    async def test_list_filter_by_type(self, client) -> None:
        await client.post("/api/v1/documents", json={
            "documentId": "DOC-CONTRACT", "documentName": "合同", "documentType": "CONTRACT",
        })
        await client.post("/api/v1/documents", json={
            "documentId": "DOC-8D", "documentName": "8D报告", "documentType": "8D_REPORT",
        })
        resp = await client.get("/api/v1/documents?documentType=8D_REPORT")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["documentId"] == "DOC-8D"

    async def test_list_filter_by_security_level(self, client) -> None:
        await client.post("/api/v1/documents", json={
            "documentId": "DOC-L1", "documentName": "L1文档", "documentType": "CONTRACT",
            "securityLevel": "L1",
        })
        await client.post("/api/v1/documents", json={
            "documentId": "DOC-L3", "documentName": "L3文档", "documentType": "CONTRACT",
            "securityLevel": "L3",
        })
        resp = await client.get("/api/v1/documents?securityLevel=L3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["documentId"] == "DOC-L3"

    async def test_update_document(self, client) -> None:
        create = await client.post("/api/v1/documents", json={
            "documentId": "DOC-UPD", "documentName": "原始名称", "documentType": "CONTRACT",
        })
        doc_id = create.json()["id"]

        upd = await client.put(f"/api/v1/documents/{doc_id}", json={"documentName": "新名称", "version": "v2.0"})
        assert upd.status_code == 200, upd.text
        assert upd.json()["documentName"] == "新名称"
        assert upd.json()["version"] == "v2.0"
        # 未改字段不变
        assert upd.json()["documentId"] == "DOC-UPD"

    async def test_delete_document(self, client) -> None:
        create = await client.post("/api/v1/documents", json={
            "documentId": "DOC-DEL", "documentName": "待删除", "documentType": "CONTRACT",
        })
        doc_id = create.json()["id"]
        resp = await client.delete(f"/api/v1/documents/{doc_id}")
        assert resp.status_code == 204
        # 已删除，查不到了
        get_resp = await client.get(f"/api/v1/documents/{doc_id}")
        assert get_resp.status_code == 404


class TestDocumentEntityRelationApi:
    """Document-Entity Relation CRUD。"""

    async def _create_doc(self, client, doc_id: str = "DOC-REL-001") -> int:
        resp = await client.post("/api/v1/documents", json={
            "documentId": doc_id,
            "documentName": f"测试文档-{doc_id}",
            "documentType": "CONTRACT",
        })
        assert resp.status_code == 201
        return resp.json()["id"]

    async def test_create_get_relation(self, client) -> None:
        doc_id_str = "DOC-R-001"
        await self._create_doc(client, doc_id_str)
        payload = {
            "documentId": doc_id_str,
            "entityType": "SUPPLIER",
            "entityKey": "100001",
            "relationType": "CONTRACT",
        }
        resp = await client.post("/api/v1/documents/relations", json=payload)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["documentId"] == doc_id_str
        assert body["entityType"] == "SUPPLIER"
        assert body["entityKey"] == "100001"
        assert body["relationType"] == "CONTRACT"
        assert body["id"] > 0

        rel_id = body["id"]
        detail = await client.get(f"/api/v1/documents/relations/{rel_id}")
        assert detail.status_code == 200
        assert detail.json()["id"] == rel_id

    async def test_create_duplicate_relation_returns_409(self, client) -> None:
        doc_id_str = "DOC-R-DUP"
        await self._create_doc(client, doc_id_str)
        payload = {
            "documentId": doc_id_str,
            "entityType": "SUPPLIER",
            "entityKey": "100001",
            "relationType": "CONTRACT",
        }
        resp1 = await client.post("/api/v1/documents/relations", json=payload)
        assert resp1.status_code == 201
        resp2 = await client.post("/api/v1/documents/relations", json=payload)
        assert resp2.status_code == 409

    async def test_list_relations_by_document(self, client) -> None:
        doc_id_str = "DOC-R-LIST"
        await self._create_doc(client, doc_id_str)
        await client.post("/api/v1/documents/relations", json={
            "documentId": doc_id_str, "entityType": "SUPPLIER", "entityKey": "200001", "relationType": "CONTRACT",
        })
        await client.post("/api/v1/documents/relations", json={
            "documentId": doc_id_str, "entityType": "SUPPLIER", "entityKey": "200002", "relationType": "CONTRACT",
        })
        resp = await client.get(f"/api/v1/documents/relations?documentId={doc_id_str}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    async def test_list_relations_by_entity_key(self, client) -> None:
        doc_id_str = "DOC-R-EKEY"
        await self._create_doc(client, doc_id_str)
        await client.post("/api/v1/documents/relations", json={
            "documentId": doc_id_str, "entityType": "SUPPLIER", "entityKey": "300001", "relationType": "CONTRACT",
        })
        await client.post("/api/v1/documents/relations", json={
            "documentId": doc_id_str, "entityType": "SUPPLIER", "entityKey": "300002", "relationType": "CONTRACT",
        })
        resp = await client.get("/api/v1/documents/relations?entityKey=300001")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_delete_relation(self, client) -> None:
        doc_id_str = "DOC-R-DEL"
        await self._create_doc(client, doc_id_str)
        create = await client.post("/api/v1/documents/relations", json={
            "documentId": doc_id_str, "entityType": "SUPPLIER", "entityKey": "400001", "relationType": "CONTRACT",
        })
        rel_id = create.json()["id"]
        resp = await client.delete(f"/api/v1/documents/relations/{rel_id}")
        assert resp.status_code == 204
        # 已删除
        get_resp = await client.get(f"/api/v1/documents/relations/{rel_id}")
        assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# 枚举契约回归：前端下拉的所有 documentType / relationType 必须被后端 schema 接受。
# 防止前端新增分类时漏改后端 Pydantic 枚举导致 422。
# 真实 PG 上的端到端断言：POST 返回 201 + 回读 body[type] == 提交值。
# ---------------------------------------------------------------------------

import pytest


# 与 frontend/src/types/document.ts 保持一致（前端下拉全集）
ALL_DOCUMENT_TYPES: tuple[str, ...] = (
    "CONTRACT", "8D_REPORT", "AUDIT_REPORT", "SPEC", "SOP",
    "QUALITY_AGREEMENT", "INSPECTION_SPEC", "REMEDIATION_REPORT",
    "PURCHASE_SPEC", "MEETING_MINUTES", "SAFETY_SHEET",
    "QUALITY", "OTHER",
)

ALL_RELATION_TYPES: tuple[str, ...] = (
    "CONTRACT", "8D_REPORT", "AUDIT_REPORT", "SPEC", "SOP",
    "QUALITY_AGREEMENT", "INSPECTION_SPEC", "REMEDIATION_REPORT", "OTHER",
)


@pytest.mark.parametrize("doc_type", ALL_DOCUMENT_TYPES)
async def test_all_document_types_accepted_by_api(client, doc_type: str) -> None:
    """每个前端下拉值都必须能 POST /api/v1/documents 成功（防后端 enum 漂移）。"""
    payload = {
        "documentId": f"DOC-T-{doc_type}",
        "documentName": f"测试-{doc_type}",
        "documentType": doc_type,
    }
    resp = await client.post("/api/v1/documents", json=payload)
    assert resp.status_code == 201, f"{doc_type} 应被接受，实际 {resp.status_code}: {resp.text}"
    assert resp.json()["documentType"] == doc_type


@pytest.mark.parametrize("rel_type", ALL_RELATION_TYPES)
async def test_all_relation_types_accepted_by_api(client, rel_type: str) -> None:
    """每个前端下拉值都必须能 POST /api/v1/documents/relations 成功。"""
    doc_id_str = f"DOC-RT-{rel_type}"
    # 复用 doc 工厂：建一个 CONTRACT 类型 doc（任何类型都行，只为承载 relation）
    doc_resp = await client.post("/api/v1/documents", json={
        "documentId": doc_id_str,
        "documentName": f"承载-{rel_type}",
        "documentType": "CONTRACT",
    })
    assert doc_resp.status_code == 201, doc_resp.text

    payload = {
        "documentId": doc_id_str,
        "entityType": "SUPPLIER",
        # 用 rel_type 后缀确保每个 parametrize 用不同 key（key 长度受 String(100) 限制）
        "entityKey": f"999-{rel_type[:80]}",
        "relationType": rel_type,
    }
    resp = await client.post("/api/v1/documents/relations", json=payload)
    assert resp.status_code == 201, f"{rel_type} 应被接受，实际 {resp.status_code}: {resp.text}"
    assert resp.json()["relationType"] == rel_type

