"""本体语义关系 / 关联关系 → Neo4j 类级边集成测试（真实 PG + 真实 Neo4j）。

覆盖第 1 版「关联入图」验收：
- 创建语义关系 → (:Class)-[:SUPPLIES]->(:Class) 边存在；删除后边消失；
- 创建 join → (:Class)-[:JOIN]->(:Class) 边存在；删除后边消失；
- backfill：join 全量入图（JOIN 边）+ X3 外键补 ref_class_id → REFERENCES 边，
  且重复执行幂等（计数与边不翻倍）。

Neo4j 不可达时整文件跳过（模块级探测）；每用例前后清空本体图节点
（Class/Property/Metric，不动 BusinessEntity 子图），保证断言不被前序用例残留污染。
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure import neo4j_client as neo4j
from app.services.id_mapping_service import IdMappingService
from app.tests._neo4j_support import assertAppNeo4jIsIsolated

_neo4jAvailable = neo4j.isNeo4jAvailable()

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _neo4jAvailable, reason="Neo4j 不可达，跳过本体图集成测试"),
]


def _wipeOntologyNodes() -> None:
    """清空本体图节点（含其全部边）；不触碰 BusinessEntity 业务子图。

    本函数走 `neo4j.getDriver()`（读应用配置），与集成夹具的 `TEST_NEO4J_URI`
    是两条路径 —— 2026-10-03 就是在这里用生产 driver 删了线上图谱。故清理前
    强制复查两边指向同一个测试实例（见 `_neo4j_support`）。
    """
    assertAppNeo4jIsIsolated()
    with neo4j.getDriver().session() as session:
        for label in ("Class", "Property", "Metric"):
            session.run(f"MATCH (n:{label}) DETACH DELETE n")


@pytest.fixture(autouse=True)
async def cleanOntologyGraph() -> None:
    _wipeOntologyNodes()
    yield
    _wipeOntologyNodes()


async def _uid(dbSession: AsyncSession, businessObject: str, pgId: int) -> str:
    """PG id → unified_id（图侧一切标识都用它，见 fix-m0-graph-key-consistency）。"""
    row = await IdMappingService().resolveByExternal(dbSession, businessObject, str(pgId))
    assert row is not None, f"{businessObject}:{pgId} 未注册 unified_id"
    return row.unified_id


async def _createClass(
    client: AsyncClient, dbSession: AsyncSession, name: str, table: str
) -> tuple[int, str]:
    """建类，返回 (PG id, unified_id)。

    两个 id 都要：写 PG 的接口（建关系/建 join）收 PG id，而图侧断言与
    `deleteClassJoin` 收 unified_id。
    """
    resp = await client.post(
        "/api/v1/ontology/classes",
        json={"className": name, "sourceTable": table},
    )
    assert resp.status_code == 201
    pgId = resp.json()["id"]
    return pgId, await _uid(dbSession, "CLASS", pgId)


def _edgesOf(unifiedId: str, relType: str) -> list[dict]:
    """返回源类节点 relType 指向目标的出边（按 unified_id 定位节点）。"""
    return [
        e
        for e in neo4j.getNodeRelationships("Class", unifiedId)
        if e["relType"] == relType
    ]


class TestOntologyRelationGraph:
    async def test_create_relation_writes_class_edge(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        srcId, srcUid = await _createClass(client, dbSession, "PRECEIPT", "PRECEIPT")
        tgtId, tgtUid = await _createClass(client, dbSession, "BPARTNER", "BPARTNER")

        resp = await client.post(
            "/api/v1/ontology/relations",
            json={
                "sourceClassId": srcId,
                "targetClassId": tgtId,
                "relationType": "SUPPLIES",
            },
        )
        assert resp.status_code == 201

        edges = _edgesOf(srcUid, "SUPPLIES")
        assert len(edges) == 1
        assert edges[0]["targetUid"] == tgtUid

    async def test_delete_relation_removes_class_edge(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        srcId, srcUid = await _createClass(client, dbSession, "PRECEIPT", "PRECEIPT")
        tgtId, _ = await _createClass(client, dbSession, "BPARTNER", "BPARTNER")

        created = await client.post(
            "/api/v1/ontology/relations",
            json={
                "sourceClassId": srcId,
                "targetClassId": tgtId,
                "relationType": "CONTAINS",
            },
        )
        relId = created.json()["id"]
        assert _edgesOf(srcUid, "CONTAINS")

        resp = await client.delete(f"/api/v1/ontology/relations/{relId}")
        assert resp.status_code == 204
        assert _edgesOf(srcUid, "CONTAINS") == []

    async def test_create_join_writes_and_delete_removes_join_edge(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        srcId, srcUid = await _createClass(client, dbSession, "PORDER", "PORDER")
        tgtId, _ = await _createClass(client, dbSession, "BPARTNER", "BPARTNER")

        created = await client.post(
            "/api/v1/ontology/joins",
            json={
                "sourceClassId": srcId,
                "sourceColumns": ["BPRNUM_0"],
                "targetClassId": tgtId,
                "targetColumns": ["BPRNUM_0"],
            },
        )
        assert created.status_code == 201
        joinId = created.json()["id"]
        assert len(_edgesOf(srcUid, "JOIN")) == 1

        deleted = await client.delete(f"/api/v1/ontology/joins/{joinId}")
        assert deleted.status_code == 204
        assert _edgesOf(srcUid, "JOIN") == []

    async def test_backfill_syncs_join_and_reference_edges_idempotent(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        srcId, srcUid = await _createClass(client, dbSession, "PORDER", "PORDER")
        tgtId, tgtUid = await _createClass(client, dbSession, "BPARTNER", "BPARTNER")

        # 外键属性 BPRNUM_0 -> BPARTNER（is_foreign_key，ref 为空 → 无 REFERENCES 边）
        prop = await client.post(
            "/api/v1/ontology/properties",
            json={
                "classId": srcId,
                "propertyName": "BPRNUM_0",
                "dataType": "STRING",
                "sourceColumn": "BPRNUM_0",
                "isForeignKey": True,
            },
        )
        propId = prop.json()["id"]
        propUid = await _uid(dbSession, "PROPERTY", propId)
        # 既有 join：createJoin 现写入 JOIN 边，故先建后删该边，模拟本特性落地前
        # 历史 join 的「PG 有行、图无 JOIN 边」状态 —— backfill 的修复对象。
        # 注意 deleteClassJoin 收 unified_id（曾误传 PG id，静默 no-op）。
        await client.post(
            "/api/v1/ontology/joins",
            json={
                "sourceClassId": srcId,
                "sourceColumns": ["BPRNUM_0"],
                "targetClassId": tgtId,
                "targetColumns": ["BPRNUM_0"],
            },
        )
        neo4j.deleteClassJoin(srcUid, tgtUid)
        # 基线：图内无 JOIN / REFERENCES 边，等待 backfill 补
        assert _edgesOf(srcUid, "JOIN") == []

        first = await client.post("/api/v1/ontology/relations/backfill")
        assert first.status_code == 200
        assert first.json() == {"syncedJoins": 1, "backfilledReferences": 1}
        assert len(_edgesOf(srcUid, "JOIN")) == 1
        propEdges = [
            e
            for e in neo4j.getNodeRelationships("Property", propUid)
            if e["relType"] == "REFERENCES"
        ]
        assert len(propEdges) == 1
        assert propEdges[0]["targetUid"] == tgtUid

        # 幂等：重复 backfill 无新增补全（ref 已设）→ backfilledReferences=0，边不翻倍
        second = await client.post("/api/v1/ontology/relations/backfill")
        assert second.status_code == 200
        assert second.json() == {"syncedJoins": 1, "backfilledReferences": 0}
        assert len(_edgesOf(srcUid, "JOIN")) == 1
        assert len(_edgesOf(srcUid, "SUPPLIES")) == 0


class TestGraphViewerEndpoints:
    """HTTP 契约：节点主键必须下发，且关系端点按 unified_id 定位。

    这两条是「图浏览整块是死的」的直接回归闸 —— 曾经的症状是列表不下发主键、
    关系端点恒返回 `[]`，两者都以 HTTP 200 静默通过。
    """

    async def test_list_nodes_exposes_unified_id(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        _, uid = await _createClass(client, dbSession, "PRECEIPT", "PRECEIPT")

        resp = await client.get("/api/v1/system/graph/nodes", params={"label": "Class"})

        assert resp.status_code == 200
        node = next(n for n in resp.json() if n.get("unifiedId") == uid)
        assert node["name"] == "PRECEIPT"
        assert "unified_id" not in node, "对外契约是 camelCase，不得漏出蛇形键"

    async def test_relationships_locate_node_by_unified_id(
        self, client: AsyncClient, dbSession: AsyncSession
    ) -> None:
        """unified_id 形如 obj:CLASS:9，含冒号 —— 调用方 encode 后后端必须能解码。"""
        srcId, srcUid = await _createClass(client, dbSession, "PRECEIPT", "PRECEIPT")
        tgtId, tgtUid = await _createClass(client, dbSession, "BPARTNER", "BPARTNER")
        created = await client.post(
            "/api/v1/ontology/relations",
            json={
                "sourceClassId": srcId,
                "targetClassId": tgtId,
                "relationType": "SUPPLIES",
            },
        )
        assert created.status_code == 201

        resp = await client.get(
            f"/api/v1/system/graph/nodes/Class/{quote(srcUid, safe='')}/relationships"
        )

        assert resp.status_code == 200
        assert [(r["relType"], r["targetUid"]) for r in resp.json()] == [
            ("SUPPLIES", tgtUid)
        ]
