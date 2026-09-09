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

import pytest
from httpx import AsyncClient

from app.infrastructure import neo4j_client as neo4j

_neo4jAvailable = neo4j.isNeo4jAvailable()

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _neo4jAvailable, reason="Neo4j 不可达，跳过本体图集成测试"),
]


def _wipeOntologyNodes() -> None:
    """清空本体图节点（含其全部边）；不触碰 BusinessEntity 业务子图。"""
    with neo4j.getDriver().session() as session:
        for label in ("Class", "Property", "Metric"):
            session.run(f"MATCH (n:{label}) DETACH DELETE n")


@pytest.fixture(autouse=True)
async def cleanOntologyGraph() -> None:
    _wipeOntologyNodes()
    yield
    _wipeOntologyNodes()


async def _createClass(client: AsyncClient, name: str, table: str) -> int:
    resp = await client.post(
        "/api/v1/ontology/classes",
        json={"className": name, "sourceTable": table},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _edgesOf(client: None, srcId: int, relType: str) -> list[dict]:
    """返回源类节点 relType 指向目标的出边。"""
    del client
    return [
        e
        for e in neo4j.getNodeRelationships("Class", srcId)
        if e["relType"] == relType
    ]


class TestOntologyRelationGraph:
    async def test_create_relation_writes_class_edge(
        self, client: AsyncClient
    ) -> None:
        srcId = await _createClass(client, "PRECEIPT", "PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")

        resp = await client.post(
            "/api/v1/ontology/relations",
            json={
                "sourceClassId": srcId,
                "targetClassId": tgtId,
                "relationType": "SUPPLIES",
            },
        )
        assert resp.status_code == 201

        edges = _edgesOf(client, srcId, "SUPPLIES")
        assert len(edges) == 1
        assert edges[0]["targetId"] == tgtId

    async def test_delete_relation_removes_class_edge(
        self, client: AsyncClient
    ) -> None:
        srcId = await _createClass(client, "PRECEIPT", "PRECEIPT")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")

        created = await client.post(
            "/api/v1/ontology/relations",
            json={
                "sourceClassId": srcId,
                "targetClassId": tgtId,
                "relationType": "CONTAINS",
            },
        )
        relId = created.json()["id"]
        assert _edgesOf(client, srcId, "CONTAINS")

        resp = await client.delete(f"/api/v1/ontology/relations/{relId}")
        assert resp.status_code == 204
        assert _edgesOf(client, srcId, "CONTAINS") == []

    async def test_create_join_writes_and_delete_removes_join_edge(
        self, client: AsyncClient
    ) -> None:
        srcId = await _createClass(client, "PORDER", "PORDER")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")

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
        assert len(_edgesOf(client, srcId, "JOIN")) == 1

        deleted = await client.delete(f"/api/v1/ontology/joins/{joinId}")
        assert deleted.status_code == 204
        assert _edgesOf(client, srcId, "JOIN") == []

    async def test_backfill_syncs_join_and_reference_edges_idempotent(
        self, client: AsyncClient
    ) -> None:
        srcId = await _createClass(client, "PORDER", "PORDER")
        tgtId = await _createClass(client, "BPARTNER", "BPARTNER")

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
        # 既有 join：createJoin 现写入 JOIN 边，故先建后删该边，模拟本特性落地前
        # 历史 join 的「PG 有行、图无 JOIN 边」状态 —— backfill 的修复对象。
        await client.post(
            "/api/v1/ontology/joins",
            json={
                "sourceClassId": srcId,
                "sourceColumns": ["BPRNUM_0"],
                "targetClassId": tgtId,
                "targetColumns": ["BPRNUM_0"],
            },
        )
        neo4j.deleteClassJoin(srcId, tgtId)
        # 基线：图内无 JOIN / REFERENCES 边，等待 backfill 补
        assert _edgesOf(client, srcId, "JOIN") == []

        first = await client.post("/api/v1/ontology/relations/backfill")
        assert first.status_code == 200
        assert first.json() == {"syncedJoins": 1, "backfilledReferences": 1}
        assert len(_edgesOf(client, srcId, "JOIN")) == 1
        propEdges = [
            e
            for e in neo4j.getNodeRelationships("Property", propId)
            if e["relType"] == "REFERENCES"
        ]
        assert len(propEdges) == 1
        assert propEdges[0]["targetId"] == tgtId

        # 幂等：重复 backfill 无新增补全（ref 已设）→ backfilledReferences=0，边不翻倍
        second = await client.post("/api/v1/ontology/relations/backfill")
        assert second.status_code == 200
        assert second.json() == {"syncedJoins": 1, "backfilledReferences": 0}
        assert len(_edgesOf(client, srcId, "JOIN")) == 1
        assert len(_edgesOf(client, srcId, "SUPPLIES")) == 0
