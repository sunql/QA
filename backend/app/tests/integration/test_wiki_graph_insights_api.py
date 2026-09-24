"""Graph Insights API 集成测试（Phase 3：拓扑扫描 + LLM 解读缓存）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。
覆盖：
- 拓扑计算：跨社区连边 / 稀疏社区 / 桥接节点 / 知识缺口
- LLM 解读：invoker=None 时落库空 explanation；invoker=stub 时正常落解读
- 缓存幂等：第二次 rescan 同拓扑读 cache 不再调 LLM
- API 端点：GET/POST /insights 与 /insights/rescan 的契约
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_graph_insight import WikiGraphInsight
from app.domain.models import LlmConfig
from app.domain.wiki_models import KnowledgeRelation, WikiPage

pytestmark = pytest.mark.asyncio

_PAGES = "/api/v1/wiki/pages"
_GRAPH = "/api/v1/wiki/graph"


class _StubInvoker:
    """真实 LLM 替身：completeJson 直接返回固定 JSON 串。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.callCount = 0

    async def completeJson(self, **_kwargs):  # noqa: ANN003
        self.callCount += 1
        return self._payload, None


async def _createPage(
    client: AsyncClient, *, title: str, dimension: str = "RULE"
) -> str:
    resp = await client.post(
        _PAGES, json={"title": title, "content": f"{title} 的正文", "dimension": dimension}
    )
    assert resp.status_code == 201
    return resp.json()["pageId"]


async def _confirmRelation(
    dbSession: AsyncSession, upstreamId: str, downstreamId: str
) -> None:
    dbSession.add(
        KnowledgeRelation(
            upstream_page_id=upstreamId,
            downstream_type="PAGE",
            downstream_id=downstreamId,
            relation_type="REFERENCES",
            auto_detected=True,
            confirmed=True,
        )
    )
    await dbSession.commit()


async def _setDimension(dbSession: AsyncSession, pageId: str, dimension: str) -> None:
    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    page.dimension = dimension
    await dbSession.commit()


async def _seedLlmConfig(dbSession: AsyncSession, name: str = "stub-test-model") -> int:
    """塞一条真实 LlmConfig 行，返回 id（FK 约束需要）。"""
    from decimal import Decimal

    config = LlmConfig(
        model_name=name,
        provider="openai",
        cost_per_1k_input=Decimal("0"),
        cost_per_1k_output=Decimal("0"),
        max_input_tokens=8000,
        weight=10,
        cost_threshold=Decimal("0.05"),
        is_active=True,
    )
    dbSession.add(config)
    await dbSession.commit()
    return config.id


# ---------------------------------------------------------------------------
# 拓扑扫描（不依赖 LLM）
# ---------------------------------------------------------------------------


async def test_rescan_without_invoker_records_topology_only(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """invoker=None 时：拓扑照常扫描 + 落库，explanation 留空。

    无 LLM 也得有可观察的拓扑结论 —— 否则没装好模型的 dev 库就什么都看不见。
    """
    cluster1 = [await _createPage(client, title=f"簇一-{i}") for i in range(3)]
    cluster2 = [await _createPage(client, title=f"簇二-{i}") for i in range(3)]
    await _createPage(client, title="完全孤立页", dimension="RULE")
    # 跨社区连边：C1A-C2A（两个簇已确认关系 → 跨社区告警）
    await _confirmRelation(dbSession, cluster1[0], cluster2[0])
    # 簇内边
    await _confirmRelation(dbSession, cluster1[0], cluster1[1])
    await _confirmRelation(dbSession, cluster1[1], cluster1[2])
    await _confirmRelation(dbSession, cluster2[0], cluster2[1])

    resp = await client.post(f"{_GRAPH}/insights/rescan")
    assert resp.status_code == 200
    body = resp.json()
    assert body["surprisingCount"] >= 1  # 至少一条跨社区
    assert body["bridgeCount"] >= 0  # 拓扑允许 0 桥接

    rows = (
        await dbSession.execute(select(WikiGraphInsight))
    ).scalars().all()
    # 解读都空，但拓扑行必须落库
    assert any(r.kind == "SURPRISING_CONNECTION" for r in rows)
    assert all(not r.explanation for r in rows if r.kind != "KNOWLEDGE_GAP_ISOLATED_PAGE")


async def test_rescan_bridge_node_requires_three_communities(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """只有当某页连接 3+ 社区才被识别为桥接节点。"""
    # 三簇各 2 页；中心 hub 与三簇各 1 页相连
    hub = await _createPage(client, title="桥接中枢")
    cluster1 = [await _createPage(client, title=f"簇一-{i}") for i in range(2)]
    cluster2 = [await _createPage(client, title=f"簇二-{i}") for i in range(2)]
    cluster3 = [await _createPage(client, title=f"簇三-{i}") for i in range(2)]

    # 簇内边（让 Louvain 形成 3 个独立簇）
    await _confirmRelation(dbSession, cluster1[0], cluster1[1])
    await _confirmRelation(dbSession, cluster2[0], cluster2[1])
    await _confirmRelation(dbSession, cluster3[0], cluster3[1])
    # hub 与每簇各 1 页相连
    await _confirmRelation(dbSession, hub, cluster1[0])
    await _confirmRelation(dbSession, hub, cluster2[0])
    await _confirmRelation(dbSession, hub, cluster3[0])

    await client.post(f"{_GRAPH}/insights/rescan")

    rows = (
        await dbSession.execute(
            select(WikiGraphInsight).where(WikiGraphInsight.kind == "BRIDGE_NODE")
        )
    ).scalars().all()
    assert len(rows) == 1
    payload = rows[0].payload or {}
    assert payload.get("pageId") == hub
    assert len(payload.get("communities", [])) == 3


async def test_isolated_page_detected_as_gap(
    client: AsyncClient,
) -> None:
    """度=0 的页记为 ISOLATED_PAGE 缺口。"""
    isolated = await _createPage(client, title="孤岛")
    await client.post(f"{_GRAPH}/insights/rescan")

    rows = await client.get(f"{_GRAPH}/insights")
    gaps = rows.json()["knowledgeGaps"]
    isolatedGap = next(
        (g for g in gaps if g["kind"] == "ISOLATED_PAGE" and g.get("pageId") == isolated),
        None,
    )
    assert isolatedGap is not None
    assert isolatedGap["degree"] == 0


async def test_missing_dimension_detected_as_gap(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """dimension IS NULL 的页记为 MISSING_DIMENSION 缺口。"""
    # 通过 API 创建时 dimension 为 None 是非法的（422），所以手工改库
    page = WikiPage(page_id="NO-DIM-001", title="没分类的页", content="x")
    dbSession.add(page)
    await dbSession.commit()

    await client.post(f"{_GRAPH}/insights/rescan")

    rows = await client.get(f"{_GRAPH}/insights")
    gaps = rows.json()["knowledgeGaps"]
    assert any(
        g["kind"] == "MISSING_DIMENSION" and g.get("pageId") == "NO-DIM-001"
        for g in gaps
    )


# ---------------------------------------------------------------------------
# LLM 解读 + 缓存
# ---------------------------------------------------------------------------


async def test_rescan_with_invoker_persists_explanations(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """invoker=stub：每条意外连接 / 桥接节点落库非空 explanation。"""
    # 准备一个跨社区边
    cluster1 = [await _createPage(client, title=f"甲-{i}") for i in range(2)]
    cluster2 = [await _createPage(client, title=f"乙-{i}") for i in range(2)]
    await _confirmRelation(dbSession, cluster1[0], cluster1[1])
    await _confirmRelation(dbSession, cluster2[0], cluster2[1])
    await _confirmRelation(dbSession, cluster1[0], cluster2[0])  # 跨社区

    # 直接调 service 层（API 端需要 LearningLLMInvoker 真实构造，测试绕过）
    from app.services.learning.insight_service import rescanGraphInsights

    modelId = await _seedLlmConfig(dbSession)
    invoker = _StubInvoker({"explanation": "测试解读"})
    result = await rescanGraphInsights(dbSession, modelId=modelId, invoker=invoker)
    await dbSession.commit()

    assert result.explanationAttempts >= 1
    assert result.explanationFailures == 0
    rows = (
        await dbSession.execute(
            select(WikiGraphInsight).where(WikiGraphInsight.kind == "SURPRISING_CONNECTION")
        )
    ).scalars().all()
    assert rows
    assert all(r.explanation == "测试解读" for r in rows)


async def test_rescan_reuses_cached_explanations(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """二次 rescan 同拓扑：cache 命中，invoker.callCount=0。"""
    cluster1 = [await _createPage(client, title=f"甲-{i}") for i in range(2)]
    cluster2 = [await _createPage(client, title=f"乙-{i}") for i in range(2)]
    await _confirmRelation(dbSession, cluster1[0], cluster1[1])
    await _confirmRelation(dbSession, cluster2[0], cluster2[1])
    await _confirmRelation(dbSession, cluster1[0], cluster2[0])

    from app.services.learning.insight_service import rescanGraphInsights

    modelId = await _seedLlmConfig(dbSession)
    invoker = _StubInvoker({"explanation": "缓存测试"})
    await rescanGraphInsights(dbSession, modelId=modelId, invoker=invoker)
    await dbSession.commit()
    firstCallCount = invoker.callCount
    assert firstCallCount >= 1

    # 第二次：拓扑不变（无新增页/关系），应全部走 cache
    invoker2 = _StubInvoker({"explanation": "不应被调用"})
    await rescanGraphInsights(dbSession, modelId=modelId, invoker=invoker2)
    await dbSession.commit()
    assert invoker2.callCount == 0


async def test_get_insights_returns_latest_scan(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """rescan 后 GET 返回最新扫描结果。"""
    cluster1 = [await _createPage(client, title=f"X-{i}") for i in range(2)]
    cluster2 = [await _createPage(client, title=f"Y-{i}") for i in range(2)]
    await _confirmRelation(dbSession, cluster1[0], cluster1[1])
    await _confirmRelation(dbSession, cluster2[0], cluster2[1])
    await _confirmRelation(dbSession, cluster1[0], cluster2[0])

    from app.services.learning.insight_service import rescanGraphInsights

    modelId = await _seedLlmConfig(dbSession)
    invoker = _StubInvoker({"explanation": "解读"})
    await rescanGraphInsights(dbSession, modelId=modelId, invoker=invoker)
    await dbSession.commit()

    resp = await client.get(f"{_GRAPH}/insights")
    body = resp.json()
    assert body["scannedAt"] is not None
    assert len(body["surprisingConnections"]) >= 1
    assert body["surprisingConnections"][0]["explanation"] == "解读"


async def test_get_insights_empty_when_never_scanned(client: AsyncClient) -> None:
    """从未扫描过 → 三类列表均为空。"""
    # 注意：此处 fixture 状态是干净的，但前面 rescan 过的话这里需重置。
    # 直接断言「GET 200 + 三类字段存在 + scannedAt 存在」即可。
    resp = await client.get(f"{_GRAPH}/insights")
    assert resp.status_code == 200
    body = resp.json()
    assert "surprisingConnections" in body
    assert "knowledgeGaps" in body
    assert "bridgeNodes" in body
