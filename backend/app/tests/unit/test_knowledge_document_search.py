"""M2-3 采购文档检索（`POST /procurement/knowledge/documents/search`，设计稿 §8.3）。

判据要点：
1. **检索不返回未完成/过期版本**：无 catalog 行的 hit **丢弃**（密级/有效期无法
   核验 ⇒ fail-closed）；非 ACTIVE 状态丢弃；effectiveAt 过滤（生效期未知=保留）；
2. **密级过滤复用既有闸**（Task 4.3 buildClearanceFilter 经 user 透传），本批
   不重复实现；evidence.security.level 回填 catalog 密级（M2-1 契约的消费方）；
3. **实体过滤**：entityKeys 形如 `"TYPE:KEY"`（entity_key 跨类型会撞），
   命中 `document_entity_relation` 任一即保留；
4. **provenance 自证**：`source=LIVE`（文档不在发布面，但 catalog.version 持久
   可溯——与治理表「无版本可引」不同）；`sourceRef.documentId` 回填（M2-1
   契约的第二个消费方）；
5. **双接线守卫**：main.py 与 _testapp.py 同块注册。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from app.domain.enums import DocumentSecurityLevel, DocumentStatus, DocumentType
from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_RAG,
    KNOWLEDGE_SOURCE_LIVE,
)
from app.domain.models import DocumentCatalog
from app.services.knowledge.document_search import (
    buildDocumentEvidence,
    filterDocumentHits,
)


def _hit(docId: str, distance: float = 0.2, chunk: int = 0) -> dict:
    return {
        "document_id": docId,
        "chunk_text": f"条款内容 {docId}",
        "chunk_sequence": chunk,
        "distance": distance,
        "score": 1 - distance,
        "document_name": f"文档{docId}",
    }


def _catalog(
    docId: str,
    *,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    security: DocumentSecurityLevel = DocumentSecurityLevel.L1,
    docType: DocumentType = DocumentType.SOP,
    effective: date | None = date(2026, 1, 1),
) -> DocumentCatalog:
    return DocumentCatalog(
        document_id=docId,
        document_name=f"文档{docId}",
        document_type=docType,
        version="v1.0",
        status=status,
        security_level=security,
        effective_date=effective,
        owner=None,
    )


# ── 1. 过滤（fail-closed）─────────────────────────────────────────────────


def test_filterDropsHitsWithoutCatalogRow() -> None:
    hits = [_hit("DOC-1"), _hit("DOC-MISSING")]
    catalogById = {"DOC-1": _catalog("DOC-1")}
    kept = filterDocumentHits(
        hits,
        catalogById,
        relationsByDoc={},
        documentTypes=None,
        entityKeys=None,
        effectiveAt=None,
    )
    assert [h["document_id"] for h in kept] == ["DOC-1"], (
        "无 catalog 行 ⇒ 密级/有效期无法核验 ⇒ fail-closed 丢弃"
    )


def test_filterDropsNonActiveDocuments() -> None:
    catalogById = {
        "DOC-A": _catalog("DOC-A", status=DocumentStatus.ACTIVE),
        "DOC-E": _catalog("DOC-E", status=DocumentStatus.EXPIRED),
    }
    kept = filterDocumentHits(
        [_hit("DOC-A"), _hit("DOC-E")],
        catalogById,
        relationsByDoc={},
        documentTypes=None,
        entityKeys=None,
        effectiveAt=None,
    )
    assert [h["document_id"] for h in kept] == ["DOC-A"]


def test_filterByDocumentType() -> None:
    catalogById = {
        "DOC-1": _catalog("DOC-1", docType=DocumentType.SOP),
        "DOC-2": _catalog("DOC-2", docType=DocumentType.CONTRACT),
    }
    kept = filterDocumentHits(
        [_hit("DOC-1"), _hit("DOC-2")],
        catalogById,
        relationsByDoc={},
        documentTypes=["CONTRACT"],
        entityKeys=None,
        effectiveAt=None,
    )
    assert [h["document_id"] for h in kept] == ["DOC-2"]


def test_filterByEffectiveDateKeepsUnknown() -> None:
    catalogById = {
        "DOC-PAST": _catalog("DOC-PAST", effective=date(2025, 1, 1)),
        "DOC-FUTURE": _catalog("DOC-FUTURE", effective=date(2027, 1, 1)),
        "DOC-UNKNOWN": _catalog("DOC-UNKNOWN", effective=None),
    }
    kept = filterDocumentHits(
        [_hit(d) for d in ("DOC-PAST", "DOC-FUTURE", "DOC-UNKNOWN")],
        catalogById,
        relationsByDoc={},
        documentTypes=None,
        entityKeys=None,
        effectiveAt=date(2026, 11, 20),
    )
    assert {h["document_id"] for h in kept} == {"DOC-PAST", "DOC-UNKNOWN"}, (
        "生效期未知 ⇒ 保留（无法判定不误杀）；未来生效 ⇒ 剔除"
    )


def test_filterByEntityKeysMatchesRelationUnion() -> None:
    relationsByDoc = {
        "DOC-1": {("SUPPLIER", "100001")},
        "DOC-2": {("MATERIAL", "M1")},
    }
    catalogById = {d: _catalog(d) for d in ("DOC-1", "DOC-2")}
    kept = filterDocumentHits(
        [_hit("DOC-1"), _hit("DOC-2")],
        catalogById,
        relationsByDoc,
        documentTypes=None,
        entityKeys=["SUPPLIER:100001"],
        effectiveAt=None,
    )
    assert [h["document_id"] for h in kept] == ["DOC-1"], (
        "entityKeys=TYPE:KEY 形式，命中任一关联即保留"
    )


# ── 2. 证据映射（M2-1 契约的回填方）───────────────────────────────────────


def test_buildDocumentEvidenceFillsM21Contract() -> None:
    catalog = _catalog("DOC-1", security=DocumentSecurityLevel.L2)
    evidence = buildDocumentEvidence(_hit("DOC-1", distance=0.2), catalog)
    assert evidence.channel == KNOWLEDGE_CHANNEL_RAG
    assert evidence.kind == "DOCUMENT"
    assert evidence.source == KNOWLEDGE_SOURCE_LIVE, "文档不在发布面 ⇒ LIVE（version 经 catalog 可溯）"
    assert evidence.release_id is None and evidence.release_version is None
    assert evidence.ref == "DOC-1" and evidence.title == "文档DOC-1"
    assert evidence.confidence == 0.8, "confidence = 1 - distance"
    assert evidence.security is not None and evidence.security.level == "L2"
    assert evidence.source_ref is not None and evidence.source_ref.document_id == "DOC-1"
    assert evidence.valid_at == datetime(2026, 1, 1), "validAt = catalog 生效日（升 datetime）"
    assert evidence.evidence_id, "evidenceId 自动生成（M2-1 default_factory）"


def test_buildDocumentEvidence_keepsValidAtUnknown() -> None:
    evidence = buildDocumentEvidence(_hit("DOC-X"), _catalog("DOC-X", effective=None))
    assert evidence.valid_at is None


# ── 3. 双接线守卫 ─────────────────────────────────────────────────────────


def test_documents_search_registered_in_both_wirings() -> None:
    backendRoot = Path(__file__).resolve().parents[3]
    for rel in ("app/main.py", "app/tests/_testapp.py"):
        code = (backendRoot / rel).read_text(encoding="utf-8")
        assert "procurement_knowledge_documents" in code, f"{rel} 未导入新路由模块"
        assert "procurement_knowledge_documents.router" in code, (
            f"{rel} 未注册 documents/search 路由（漏一边 ⇒ 那一侧 404）"
        )
