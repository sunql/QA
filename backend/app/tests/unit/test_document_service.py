"""Phase 5.1 document_service 单测（TDD 顺序）。

覆盖：DocumentCatalog CRUD + DocumentEntityRelation CRUD。

集成测试在 test_document_catalog_api.py 用真实 PG 跑。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.domain.enums import (
    DocumentSecurityLevel,
    DocumentStatus,
    DocumentType,
    DocEntityRelationType,
    EntityType,
)
from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.models import DocumentCatalog
from app.domain.schemas import (
    DocEntityRelationCreate,
    DocumentCreate,
    DocumentUpdate,
)
from app.services.document_service import DocumentService


@dataclass
class MockCurrentUser:
    """Mock CurrentUser for unit tests."""
    userId: str = "test-user"
    departments: tuple[str, ...] | None = ("测试部",)


class TestDocumentService:
    """DocumentCatalog CRUD。"""

    _actor = MockCurrentUser()

    def _make_svc(self) -> DocumentService:
        return DocumentService()

    def _doc_dto(self, **overrides) -> DocumentCreate:
        kw = {
            "document_id": "DOC-2026-001",
            "document_name": "供应商框架协议",
            "document_type": DocumentType.CONTRACT,
            "version": "v1.0",
            "owner": "采购部",
            "effective_date": None,
            "security_level": DocumentSecurityLevel.L2,
            "storage_url": None,
            "content_hash": None,
        }
        kw.update(overrides)
        return DocumentCreate(**kw)

    # --- listDocuments ---

    @pytest.mark.asyncio
    async def test_list_returns_empty_on_fresh_db(self, dbSession):
        svc = self._make_svc()
        result = await svc.listDocuments(dbSession)
        assert result == []

    @pytest.mark.asyncio
    async def test_list_respects_limit_offset(self, dbSession):
        svc = self._make_svc()
        for i in range(5):
            await svc.createDocument(dbSession, self._doc_dto(document_id=f"DOC-{i}"), self._actor)
        page = await svc.listDocuments(dbSession, limit=2, offset=1)
        assert len(page) == 2

    @pytest.mark.asyncio
    async def test_list_filters_by_type(self, dbSession):
        svc = self._make_svc()
        await svc.createDocument(dbSession, self._doc_dto(
            document_id="DOC-CONTRACT", document_type=DocumentType.CONTRACT), self._actor)
        await svc.createDocument(dbSession, self._doc_dto(
            document_id="DOC-8D", document_type=DocumentType.REPORT_8D), self._actor)
        result = await svc.listDocuments(dbSession, document_type="8D_REPORT")
        assert len(result) == 1
        assert result[0].document_id == "DOC-8D"

    @pytest.mark.asyncio
    async def test_list_filters_by_security_level(self, dbSession):
        svc = self._make_svc()
        await svc.createDocument(dbSession, self._doc_dto(
            document_id="DOC-L1", security_level=DocumentSecurityLevel.L1), self._actor)
        await svc.createDocument(dbSession, self._doc_dto(
            document_id="DOC-L3", security_level=DocumentSecurityLevel.L3), self._actor)
        result = await svc.listDocuments(dbSession, security_level="L3")
        assert len(result) == 1
        assert result[0].document_id == "DOC-L3"

    # --- createDocument ---

    @pytest.mark.asyncio
    async def test_createDocument_persists_and_returns(self, dbSession):
        svc = self._make_svc()
        dto = self._doc_dto()
        doc = await svc.createDocument(dbSession, dto, self._actor)
        assert doc.id is not None
        assert doc.document_id == "DOC-2026-001"
        assert doc.document_name == "供应商框架协议"
        assert doc.security_level == DocumentSecurityLevel.L2
        assert doc.status == DocumentStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_createDocument_duplicate_id_raises_ConflictError(self, dbSession):
        svc = self._make_svc()
        await svc.createDocument(dbSession, self._doc_dto(document_id="DOC-DUP"), self._actor)
        with pytest.raises(ConflictError):
            await svc.createDocument(dbSession, self._doc_dto(document_id="DOC-DUP"), self._actor)

    # --- getDocument ---

    @pytest.mark.asyncio
    async def test_getDocument_returns_doc(self, dbSession):
        svc = self._make_svc()
        created = await svc.createDocument(dbSession, self._doc_dto(), self._actor)
        fetched = await svc.getDocument(dbSession, created.id)
        assert fetched.id == created.id
        assert fetched.document_name == "供应商框架协议"

    @pytest.mark.asyncio
    async def test_getDocument_not_found_raises(self, dbSession):
        svc = self._make_svc()
        with pytest.raises(NotFoundError):
            await svc.getDocument(dbSession, 99999)

    # --- updateDocument ---

    @pytest.mark.asyncio
    async def test_updateDocument_updates_fields(self, dbSession):
        svc = self._make_svc()
        doc = await svc.createDocument(dbSession, self._doc_dto(), self._actor)
        updated = await svc.updateDocument(
            dbSession, doc.id, DocumentUpdate(document_name="新名称", version="v2.0"), self._actor
        )
        assert updated.document_name == "新名称"
        assert updated.version == "v2.0"

    @pytest.mark.asyncio
    async def test_updateDocument_none_fields_unchanged(self, dbSession):
        svc = self._make_svc()
        doc = await svc.createDocument(dbSession, self._doc_dto(), self._actor)
        await svc.updateDocument(dbSession, doc.id, DocumentUpdate(status=DocumentStatus.ARCHIVED), self._actor)
        fetched = await svc.getDocument(dbSession, doc.id)
        assert fetched.document_name == "供应商框架协议"  # unchanged
        assert fetched.status == DocumentStatus.ARCHIVED

    # --- deleteDocument ---

    @pytest.mark.asyncio
    async def test_deleteDocument_removes_row(self, dbSession):
        svc = self._make_svc()
        doc = await svc.createDocument(dbSession, self._doc_dto(), self._actor)
        await svc.deleteDocument(dbSession, doc.id, self._actor)
        with pytest.raises(NotFoundError):
            await svc.getDocument(dbSession, doc.id)


class TestDocumentEntityRelationService:
    """DocumentEntityRelation CRUD。"""

    _actor = MockCurrentUser()

    def _make_svc(self) -> DocumentService:
        return DocumentService()

    async def _setup_doc(self, dbSession, doc_id: str = "DOC-REL-001") -> DocumentCatalog:
        svc = self._make_svc()
        dto = DocumentCreate(
            document_id=doc_id,
            document_name="测试合同",
            document_type=DocumentType.CONTRACT,
        )
        return await svc.createDocument(dbSession, dto, self._actor)

    def _rel_dto(self, document_id: str, **overrides) -> DocEntityRelationCreate:
        kw = {
            "document_id": document_id,
            "entity_type": EntityType.SUPPLIER,
            "entity_key": 100001,
            "relation_type": DocEntityRelationType.CONTRACT,
        }
        kw.update(overrides)
        return DocEntityRelationCreate(**kw)

    # --- createRelation ---

    @pytest.mark.asyncio
    async def test_createRelation_persists(self, dbSession):
        doc = await self._setup_doc(dbSession)
        svc = self._make_svc()
        rel = await svc.createRelation(dbSession, self._rel_dto(doc.document_id))
        assert rel.id is not None
        assert rel.entity_type == "SUPPLIER"
        assert rel.entity_key == 100001

    @pytest.mark.asyncio
    async def test_createRelation_duplicate_raises_ConflictError(self, dbSession):
        doc = await self._setup_doc(dbSession)
        svc = self._make_svc()
        await svc.createRelation(dbSession, self._rel_dto(doc.document_id))
        with pytest.raises(ConflictError):
            await svc.createRelation(dbSession, self._rel_dto(doc.document_id))

    # --- listRelations ---

    @pytest.mark.asyncio
    async def test_listRelations_filters_by_document(self, dbSession):
        doc = await self._setup_doc(dbSession)
        svc = self._make_svc()
        await svc.createRelation(dbSession, self._rel_dto(doc.document_id, entity_key=100002))
        await svc.createRelation(dbSession, self._rel_dto(doc.document_id, entity_key=100003))
        result = await svc.listRelations(dbSession, document_id=doc.document_id)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_listRelations_filters_by_entity(self, dbSession):
        doc = await self._setup_doc(dbSession)
        svc = self._make_svc()
        await svc.createRelation(dbSession, self._rel_dto(doc.document_id, entity_key=200001))
        await svc.createRelation(dbSession, self._rel_dto(doc.document_id, entity_key=200002))
        result = await svc.listRelations(dbSession, entity_key=200001)
        assert len(result) == 1

    # --- deleteRelation ---

    @pytest.mark.asyncio
    async def test_deleteRelation_removes_row(self, dbSession):
        doc = await self._setup_doc(dbSession)
        svc = self._make_svc()
        rel = await svc.createRelation(dbSession, self._rel_dto(doc.document_id))
        await svc.deleteRelation(dbSession, rel.id)
        with pytest.raises(NotFoundError):
            await svc.getRelation(dbSession, rel.id)
