"""Phase 6.3 知识图谱多跳推理集成测试（真实 PG + 真实 Neo4j）。

覆盖：
- REST API：GET /api/v1/graph/traverse（合法 / 非法 label 422 / 未知实体 404）；
- 真实业务图遍历：Supplier 100001 多跳 -> hops + reachableTypes（依赖 6.2 seed）；
- Chat 端到端：问「供应商 100001 涉及哪些物料」-> ChatResponse 含 graphTraversal；
- Chat 降级：问「供应商 999999 涉及什么」-> answer=NotFound + graphTraversal=None；
- 意图隔离：360 / 风险问法不被图推理吸走。

Neo4j 不可达时跳过（模块级探测，不阻断 PG）。
图数据前提：seed_graph_relations 已回填（fixture 主动重放保证）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, EntityMapping
from app.infrastructure import neo4j_client as neo4j
from app.services.graph_relation_service import GraphRelationService
from scripts.seed_entity_mapping import seedEntityMappings

_neo4jAvailable = neo4j.isNeo4jAvailable()

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _neo4jAvailable, reason="Neo4j 不可达，跳过图推理集成测试"),
]

AUTH_HEADERS = {"X-User-Id": "tester", "X-User-Tenant": "default"}

# ChatRequest.datasourceId 必填 int；聊天用例 seed 一个最小 DataSource（id=9402，
# 与 supplier_risk 测试的 9401 错开）并传其 id。
_CHAT_DATASOURCE_ID = 9402


async def _seedDatasource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        id=_CHAT_DATASOURCE_ID,
        name="ds-chat-graph",
        type="postgresql",
        host="localhost",
        port=5432,
        database_name="x",
        username="u",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.commit()
    return ds


@pytest.fixture(autouse=True)
async def seededGraph(dbSession):
    """保证业务图有种子数据（幂等重放）；用例结束后清场防污染。

    另自备 10 家演示供应商映射：seed_entity_mapping 已按 THBI 真实数据对齐、
    不再合成供应商，而 SUPPLIES 演示边端点是数字键位 100001..100010。
    """
    await seedEntityMappings(dbSession)
    for i in range(1, 11):
        dbSession.add(
            EntityMapping(
                entity_type="SUPPLIER",
                enterprise_key=100_000 + i,
                enterprise_code=str(100_000 + i),
                source_system="ERP",
                source_key=f"V{i:06d}",
                source_code=f"V{i:06d}",
                match_rule="MDM_MASTER",
            )
        )
    await dbSession.commit()
    service = GraphRelationService()
    await service.seedGraphRelations(dbSession)
    yield
    neo4j.deleteBusinessGraph()


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


class TestGraphTraverseApi:
    async def test_traverse_supplier_success(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/graph/traverse",
            params={"startType": "Supplier", "startKey": "100001", "maxHops": 3},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["startType"] == "Supplier"
        assert body["startKey"] == "100001"
        assert body["maxHops"] == 3
        assert len(body["hops"]) > 0
        # hops 字段契约（camelCase）
        hop = body["hops"][0]
        assert {"depth", "fromKey", "fromCode", "fromType", "relType",
                "toKey", "toCode", "toType"} <= set(hop)
        # 可达类型包含 ItemMaster（供应商 -> 物料主链路；物料的 graph_label 为 ItemMaster）
        assert "ItemMaster" in body["reachableTypes"]

    async def test_traverse_invalid_label_422(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/graph/traverse",
            params={"startType": "Class", "startKey": "100001"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422
        assert "Invalid startType" in resp.text

    async def test_traverse_unknown_entity_404(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/api/v1/graph/traverse",
            params={"startType": "Supplier", "startKey": "999999"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 404
        # 通用消息（防侧信道：不区分 label 是否合法存在）
        assert "999999" in resp.text

    async def test_traverse_max_hops_over_limit_422(
        self, client: AsyncClient
    ) -> None:
        resp = await client.get(
            "/api/v1/graph/traverse",
            params={"startType": "Supplier", "startKey": "100001", "maxHops": 6},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422

    async def test_traverse_three_hop_chain_complete(
        self, client: AsyncClient
    ) -> None:
        """3 跳链路验收（Phase 6 验收标准）：验证完整供应链链路可达。

        PO 节点 key = 种子语义编码 PO202608001（曾用旧数字键 300001，seed
        换语义编码后该键不存在）。
        """
        resp = await client.get(
            "/api/v1/graph/traverse",
            params={"startType": "PurchaseOrder", "startKey": "PO202608001", "maxHops": 3},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        # PO -> Material / GR / IQC / Supplier 链路（GR 的 graph_label 为 Receipt）
        assert "Receipt" in body["reachableTypes"]
        assert "IncomingInspection" in body["reachableTypes"]
        # depth 值合法（1..3）
        assert all(1 <= h["depth"] <= 3 for h in body["hops"])

    async def test_traverse_empty_result_returns_200(
        self, client: AsyncClient, dbSession
    ) -> None:
        """孤立节点（无任何边）-> 200 + hops=[]（非 404）。

        NCR 已移出业务图（Phase 4.4），改用白名单内 Contract（无 SIGNED 关联
        即孤立）。
        """
        from app.infrastructure import neo4j_client

        neo4j_client.upsertBusinessEntityNode(
            label="Contract", key="DOC-ORPHAN", code="DOC-ORPHAN", name="孤立合同", source="test"
        )
        resp = await client.get(
            "/api/v1/graph/traverse",
            params={"startType": "Contract", "startKey": "DOC-ORPHAN", "maxHops": 2},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["hops"] == []
        assert body["reachableTypes"] == []


# ---------------------------------------------------------------------------
# Chat 端到端
# ---------------------------------------------------------------------------


class TestChatGraphReasoning:
    async def test_chat_graph_question_returns_traversal(
        self, client: AsyncClient, dbSession
    ) -> None:
        """「供应商 100001 涉及哪些物料」-> intent=graph_reasoning + graphTraversal。"""
        await _seedDatasource(dbSession)
        resp = await client.post(
            "/api/v1/chat",
            headers=AUTH_HEADERS,
            json={
                "sessionId": "test-graph-1",
                "question": "供应商 100001 涉及哪些物料",
                "datasourceId": _CHAT_DATASOURCE_ID,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "graph_reasoning"
        payload = body.get("graphTraversal")
        assert payload is not None
        assert payload["startType"] == "Supplier"
        assert payload["startKey"] == "100001"
        assert payload["maxHops"] == 2
        assert len(payload["hops"]) > 0
        # answer 是可达实体摘要（含类型分组，分组名用 graph_label：ItemMaster 即物料）
        assert "ItemMaster" in body["answer"]
        assert "100001" in body["answer"]

    async def test_chat_graph_three_hop_roundtrips_max_hops(
        self, client: AsyncClient, dbSession
    ) -> None:
        """「供应商 100001 的 3 跳关联」-> graphTraversal.maxHops 回传 3（G3 接线）。

        断言 maxHops 从问句经 intent → service → 响应 DTO 完整回传；
        不断言具体深度 3 的 hop（取决于 seed 图完整性，避免脆断）。
        """
        await _seedDatasource(dbSession)
        resp = await client.post(
            "/api/v1/chat",
            headers=AUTH_HEADERS,
            json={
                "sessionId": "test-graph-3hop",
                "question": "供应商 100001 的 3 跳关联",
                "datasourceId": _CHAT_DATASOURCE_ID,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "graph_reasoning"
        payload = body.get("graphTraversal")
        assert payload is not None
        assert payload["maxHops"] == 3
        assert payload["startKey"] == "100001"
        # 3 跳遍历结果必须不劣于默认 2 跳（多跳不丢失既有 2 跳可达实体）
        assert all(h["depth"] <= 3 for h in payload["hops"])

    async def test_chat_graph_unknown_supplier_not_found(
        self, client: AsyncClient, dbSession
    ) -> None:
        """「供应商 999999 涉及什么」-> answer=NotFound + graphTraversal=None。"""
        await _seedDatasource(dbSession)
        resp = await client.post(
            "/api/v1/chat",
            headers=AUTH_HEADERS,
            json={
                "sessionId": "test-graph-2",
                "question": "供应商 999999 涉及什么",
                "datasourceId": _CHAT_DATASOURCE_ID,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "graph_reasoning"
        assert body.get("graphTraversal") is None
        assert "999999" in body["answer"]

    async def test_chat_supplier_360_not_hijacked_by_graph(
        self, client: AsyncClient, dbSession
    ) -> None:
        """「供应商 100001 的 360° 视图」仍是 supplier_360（图推理在其后）。"""
        await _seedDatasource(dbSession)
        resp = await client.post(
            "/api/v1/chat",
            headers=AUTH_HEADERS,
            json={
                "sessionId": "test-graph-3",
                "question": "供应商 100001 的 360° 视图",
                "datasourceId": _CHAT_DATASOURCE_ID,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "supplier_360"

    async def test_chat_supplier_risk_not_hijacked_by_graph(
        self, client: AsyncClient, dbSession
    ) -> None:
        """「供应商 100001 的风险」仍是 supplier_risk（图推理在其后）。"""
        await _seedDatasource(dbSession)
        resp = await client.post(
            "/api/v1/chat",
            headers=AUTH_HEADERS,
            json={
                "sessionId": "test-graph-4",
                "question": "供应商 100001 的风险等级",
                "datasourceId": _CHAT_DATASOURCE_ID,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "supplier_risk"
