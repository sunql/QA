"""本体语义关系（ontology_relation）API 集成测试（真实 PostgreSQL + 完整 API 链路）。

验证 HTTP 契约（camelCase JSON）：
- GET    /api/v1/ontology/relations
- POST   /api/v1/ontology/relations
- DELETE /api/v1/ontology/relations/{id}
- POST   /api/v1/ontology/relations/backfill   （一键补关系：join 入图 + 补 ref_class_id）

覆盖：创建（201 + 字段回显）、列表、重复三元组（422）、自环（422）、非法类型（422）、
源/目标类不存在（404）、删除（204/404）、backfill 计数 + ref_class_id 落库。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditLog, OntologyProperty

pytestmark = pytest.mark.asyncio


async def _createClass(client: AsyncClient, name: str, table: str) -> int:
    resp = await client.post(
        "/api/v1/ontology/classes",
        json={"className": name, "sourceTable": table},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _relationPayload(srcId: int, tgtId: int, *, relType: str = "SUPPLIES") -> dict:
    return {
        "sourceClassId": srcId,
        "targetClassId": tgtId,
        "relationType": relType,
        "description": "供应商向采购方供货",
    }


class TestOntologyRelationApi:
    async def test_list_relations_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/ontology/relations")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_and_list_relation(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")

        resp = await client.post(
            "/api/v1/ontology/relations",
            json=_relationPayload(srcId, tgtId, relType="CONTAINS"),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] > 0
        assert body["sourceClassId"] == srcId
        assert body["targetClassId"] == tgtId
        assert body["relationType"] == "CONTAINS"
        assert body["description"] == "供应商向采购方供货"
        assert body["id"] > 0

        listing = await client.get("/api/v1/ontology/relations")
        assert listing.status_code == 200
        assert len(listing.json()) == 1
        assert listing.json()[0]["relationType"] == "CONTAINS"

    async def test_create_duplicate_relation_conflict(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")

        payload = _relationPayload(srcId, tgtId)
        first = await client.post("/api/v1/ontology/relations", json=payload)
        assert first.status_code == 201
        dup = await client.post("/api/v1/ontology/relations", json=payload)
        assert dup.status_code == 422

    async def test_create_self_relation_rejected(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "PRECEIPT")
        payload = _relationPayload(srcId, srcId)
        resp = await client.post("/api/v1/ontology/relations", json=payload)
        assert resp.status_code == 422

    async def test_create_invalid_relation_type_rejected(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        payload = _relationPayload(srcId, tgtId, relType="NOT_A_TYPE")
        resp = await client.post("/api/v1/ontology/relations", json=payload)
        assert resp.status_code == 422

    async def test_create_relation_extra_field_rejected(self, client: AsyncClient) -> None:
        """extra='forbid'：未知字段在 DTO 边界直接 422，杜绝 mass-assignment。"""
        srcId = await _createClass(client, "PRECEIPT", "PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        payload = _relationPayload(srcId, tgtId)
        payload["sneakyField"] = True
        resp = await client.post("/api/v1/ontology/relations", json=payload)
        assert resp.status_code == 422

    async def test_create_relation_description_too_long(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        payload = _relationPayload(srcId, tgtId)
        payload["description"] = "x" * 1001
        resp = await client.post("/api/v1/ontology/relations", json=payload)
        assert resp.status_code == 422

    async def test_create_source_class_missing_not_found(self, client: AsyncClient) -> None:
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")
        payload = _relationPayload(999999, tgtId)
        resp = await client.post("/api/v1/ontology/relations", json=payload)
        assert resp.status_code == 404

    async def test_create_target_class_missing_not_found(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "PRECEIPT")
        payload = _relationPayload(srcId, 999999)
        resp = await client.post("/api/v1/ontology/relations", json=payload)
        assert resp.status_code == 404

    async def test_delete_relation_and_nonexistent(self, client: AsyncClient) -> None:
        srcId = await _createClass(client, "PRECEIPT", "PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")

        created = await client.post(
            "/api/v1/ontology/relations", json=_relationPayload(srcId, tgtId)
        )
        relId = created.json()["id"]

        resp = await client.delete(f"/api/v1/ontology/relations/{relId}")
        assert resp.status_code == 204
        listing = await client.get("/api/v1/ontology/relations")
        assert listing.json() == []

        missing = await client.delete("/api/v1/ontology/relations/999999")
        assert missing.status_code == 404

    async def test_backfill_backfills_ref_and_syncs_joins(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """一键补关系：X3 命名外键补 ref_class_id + 已有 join 全量同步计数。"""
        srcId = await _createClass(client, "PORDER", "PORDER")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")

        # PORDER.BPRNUM_0 按 SAGE_X3_REFERENCE_MAP 指向 BPARTNER（is_foreign_key，ref 为空）
        await client.post(
            "/api/v1/ontology/properties",
            json={
                "classId": srcId,
                "propertyName": "BPRNUM_0",
                "dataType": "STRING",
                "sourceColumn": "BPRNUM_0",
                "isForeignKey": True,
            },
        )
        # 无关外键列（不在 X3 注册表内）应保持 NULL
        await client.post(
            "/api/v1/ontology/properties",
            json={
                "classId": srcId,
                "propertyName": "CUSTOM_FK",
                "dataType": "STRING",
                "sourceColumn": "CUSTOM_FK",
                "isForeignKey": True,
            },
        )
        # 已有一条 join 边（供 join 入图同步计数）
        join = await client.post(
            "/api/v1/ontology/joins",
            json={
                "sourceClassId": srcId,
                "sourceColumns": ["BPRNUM_0"],
                "targetClassId": tgtId,
                "targetColumns": ["BPRNUM_0"],
            },
        )
        assert join.status_code == 201

        resp = await client.post("/api/v1/ontology/relations/backfill")
        assert resp.status_code == 200
        body = resp.json()
        assert body["syncedJoins"] == 1
        assert body["backfilledReferences"] == 1

        # BPRNUM_0 的 ref_class_id 已补为 BPARTNER 类；CUSTOM_FK 保持 NULL
        await dbSession.commit()
        rows = (
            await dbSession.execute(
                select(OntologyProperty).where(OntologyProperty.class_id == srcId)
            )
        ).scalars().all()
        byCol = {r.source_column: r.ref_class_id for r in rows}
        bprRow = next(r for r in rows if r.source_column == "BPRNUM_0")
        assert byCol["BPRNUM_0"] == tgtId
        assert byCol["CUSTOM_FK"] is None

        # 归属：真正被补全的外键各落一条 ONTOLOGY_PROPERTY UPDATE 审计（who/what）
        auditRows = (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "ONTOLOGY_PROPERTY",
                    AuditLog.action == "UPDATE",
                    AuditLog.entity_id == bprRow.id,
                )
            )
        ).scalars().all()
        assert len(auditRows) == 1
        assert (auditRows[0].after_json or {}).get("ref_class_id") == tgtId
        assert auditRows[0].actor  # 非空，可追溯到触发 backfill 的用户

    async def test_backfill_skips_tombstoned_target_class(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """墓碑类（deleteClass 软删，valid_to 已设）不能作为补引用目标。"""
        srcId = await _createClass(client, "PORDER", "PORDER")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")

        # PORDER.BPRNUM_0 指向 BPARTNER（X3 ref 命中）
        await client.post(
            "/api/v1/ontology/properties",
            json={
                "classId": srcId,
                "propertyName": "BPRNUM_0",
                "dataType": "STRING",
                "sourceColumn": "BPRNUM_0",
                "isForeignKey": True,
            },
        )
        # 软删 BPARTNER → 不再是合法 ref 目标（createRelation/getClass 同样拒绝）
        removed = await client.delete(f"/api/v1/ontology/classes/{tgtId}")
        assert removed.status_code == 204

        resp = await client.post("/api/v1/ontology/relations/backfill")
        assert resp.status_code == 200
        assert resp.json()["backfilledReferences"] == 0

        # BPRNUM_0 的 ref_class_id 保持 NULL，不指向墓碑
        await dbSession.commit()
        row = (
            await dbSession.execute(
                select(OntologyProperty).where(
                    OntologyProperty.class_id == srcId,
                    OntologyProperty.source_column == "BPRNUM_0",
                )
            )
        ).scalar_one()
        assert row.ref_class_id is None
