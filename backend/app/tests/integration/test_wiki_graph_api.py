"""知识图谱分析 API 集成测试（Phase 2：4-Signal 相关性 + Louvain 社区）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。
覆盖：
- 相关性：显式关系 / 共享证据源 / 同维度亲和 / 无信号零分
- 社区检测：两个连通簇 → 两个社区；孤立页不产生单点社区
- 重算幂等：同图两次重算结果一致（seed 固定）
- 可视化载荷：截断标记、孤立页过滤、社区归属回填
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import (
    Evidence,
    KnowledgeClaim,
    KnowledgeCommunity,
    KnowledgeRelation,
)

pytestmark = pytest.mark.asyncio

_PAGES = "/api/v1/wiki/pages"
_GRAPH = "/api/v1/wiki/graph"


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
    """直接落一条「已确认」Page↔Page 关系（绕过审核流，测图算法不测审核）。"""
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


async def _seedSharedSource(
    dbSession: AsyncSession, pageId: str, sourceId: str
) -> None:
    """给某页落一条引用指定外部来源的 claim + evidence（信号 2 原料）。"""
    claim = KnowledgeClaim(page_id=pageId, claim_text="某事实", claim_type="FACT")
    dbSession.add(claim)
    await dbSession.flush()
    dbSession.add(
        Evidence(claim_id=claim.id, source_type="DOCUMENT", source_id=sourceId)
    )
    await dbSession.commit()


# ---------------------------------------------------------------------------
# 相关性（4-Signal）
# ---------------------------------------------------------------------------


async def test_relevance_direct_link_and_affinity(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """已确认关系 + 同维度 → directLink + typeAffinity，无共享源无 AA。"""
    pageA = await _createPage(client, title="供应商准入")
    pageB = await _createPage(client, title="供应商分级")
    await _confirmRelation(dbSession, pageA, pageB)

    resp = await client.get(f"{_GRAPH}/relevance", params={"pageA": pageA, "pageB": pageB})

    assert resp.status_code == 200
    body = resp.json()
    assert body["directLink"] is True
    assert body["sourceOverlap"] is False
    assert body["typeAffinity"] is True
    # 3.0（直连）+ 1.0（同维度）+ 0（AA：A-B 各自的唯一邻居就是对方，无共同邻居）
    assert body["total"] == pytest.approx(4.0)


async def test_relevance_source_overlap(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """两页引用同一外部来源 → sourceOverlap=True，权重 ×4 为最强信号。"""
    pageA = await _createPage(client, title="收货规则", dimension="PROCESS")
    pageB = await _createPage(client, title="质检规则", dimension="RULE")
    await _seedSharedSource(dbSession, pageA, "DOC-2026-001")
    await _seedSharedSource(dbSession, pageB, "DOC-2026-001")

    resp = await client.get(f"{_GRAPH}/relevance", params={"pageA": pageA, "pageB": pageB})

    body = resp.json()
    assert body["sourceOverlap"] is True
    assert body["typeAffinity"] is False  # 维度不同
    assert body["total"] == pytest.approx(4.0)


async def test_relevance_adamic_adar_via_common_neighbor(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """A-C、B-C 两条确认边 → A 与 B 有共同邻居 C，AA > 0。"""
    pageA = await _createPage(client, title="页A", dimension="CONCEPT")
    pageB = await _createPage(client, title="页B", dimension="CONCEPT")
    pageC = await _createPage(client, title="页C", dimension="CONCEPT")
    await _confirmRelation(dbSession, pageA, pageC)
    await _confirmRelation(dbSession, pageB, pageC)

    resp = await client.get(f"{_GRAPH}/relevance", params={"pageA": pageA, "pageB": pageB})

    body = resp.json()
    assert body["adamicAdar"] > 0
    assert body["directLink"] is False
    # 1.5×AA + 1.0（同维度）
    assert body["total"] == pytest.approx(1.5 * body["adamicAdar"] + 1.0)


async def test_relevance_no_signals_is_zero(
    client: AsyncClient,
) -> None:
    """无关系、无共享源、不同维度 → 全零。"""
    pageA = await _createPage(client, title="孤立页甲", dimension="RULE")
    pageB = await _createPage(client, title="孤立页乙", dimension="FAQ")

    resp = await client.get(f"{_GRAPH}/relevance", params={"pageA": pageA, "pageB": pageB})

    assert resp.status_code == 200
    assert resp.json()["total"] == 0.0


async def test_relevance_unknown_page_is_zero(client: AsyncClient) -> None:
    """不存在的节点不报错，按零分处理。"""
    pageA = await _createPage(client, title="存在的页")
    resp = await client.get(
        f"{_GRAPH}/relevance", params={"pageA": pageA, "pageB": "NO-SUCH-PAGE"}
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0.0


async def test_relevance_ignores_unconfirmed_relation(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """未确认候选关系不构成 directLink —— 审核机制在图层的体现。"""
    pageA = await _createPage(client, title="待审关系甲")
    pageB = await _createPage(client, title="待审关系乙")
    dbSession.add(
        KnowledgeRelation(
            upstream_page_id=pageA,
            downstream_type="PAGE",
            downstream_id=pageB,
            relation_type="REFERENCES",
            auto_detected=True,
            confirmed=False,  # 待审核，不入图
        )
    )
    await dbSession.commit()

    resp = await client.get(f"{_GRAPH}/relevance", params={"pageA": pageA, "pageB": pageB})

    assert resp.json()["directLink"] is False


# ---------------------------------------------------------------------------
# 社区检测
# ---------------------------------------------------------------------------


async def test_recompute_detects_two_clusters(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """两个互不连通的簇 → 两个社区；孤立页不形成单点社区。"""
    cluster1 = [await _createPage(client, title=f"簇一-{i}") for i in range(3)]
    cluster2 = [await _createPage(client, title=f"簇二-{i}") for i in range(3)]
    await _createPage(client, title="完全孤立页")  # 不应出现在任何社区
    for i in range(2):
        await _confirmRelation(dbSession, cluster1[i], cluster1[i + 1])
        await _confirmRelation(dbSession, cluster2[i], cluster2[i + 1])

    resp = await client.post(f"{_GRAPH}/communities/recompute")

    assert resp.status_code == 200
    assert resp.json()["communityCount"] == 2
    assert resp.json()["memberPageCount"] == 6

    listResp = await client.get(f"{_GRAPH}/communities")
    communities = listResp.json()
    assert len(communities) == 2
    # 链式 3 节点社区：2 条边 / 3 条可能边 = 0.667
    assert all(c["pageCount"] == 3 for c in communities)
    assert all(c["cohesionScore"] == pytest.approx(0.667, abs=0.001) for c in communities)


async def test_recompute_is_idempotent(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同图两次重算：结果一致（seed 固定），且旧社区被全量替换不叠加。"""
    pages = [await _createPage(client, title=f"幂等-{i}") for i in range(2)]
    await _confirmRelation(dbSession, pages[0], pages[1])

    first = (await client.post(f"{_GRAPH}/communities/recompute")).json()
    second = (await client.post(f"{_GRAPH}/communities/recompute")).json()

    assert first == second
    count = (
        await dbSession.execute(select(KnowledgeCommunity))
    ).scalars().all()
    assert len(count) == first["communityCount"]


# ---------------------------------------------------------------------------
# 可视化载荷
# ---------------------------------------------------------------------------


async def test_graph_view_excludes_isolated_by_default(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """默认不返回孤立页；includeIsolated=true 才带上。"""
    pageA = await _createPage(client, title="有边页甲")
    pageB = await _createPage(client, title="有边页乙")
    isolated = await _createPage(client, title="孤立页丙")
    await _confirmRelation(dbSession, pageA, pageB)

    defaultResp = await client.get(f"{_GRAPH}/view")
    body = defaultResp.json()
    nodeIds = [n["pageId"] for n in body["nodes"]]
    assert pageA in nodeIds and pageB in nodeIds
    assert isolated not in nodeIds
    assert len(body["edges"]) == 1
    assert body["edges"][0]["score"] > 0
    assert body["truncated"] is False

    withIsolated = await client.get(f"{_GRAPH}/view", params={"includeIsolated": True})
    assert isolated in [n["pageId"] for n in withIsolated.json()["nodes"]]


async def test_graph_view_backfills_community_key(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """重算社区后，view 的节点带上 communityKey。"""
    pageA = await _createPage(client, title="归属甲")
    pageB = await _createPage(client, title="归属乙")
    await _confirmRelation(dbSession, pageA, pageB)
    await client.post(f"{_GRAPH}/communities/recompute")

    body = (await client.get(f"{_GRAPH}/view")).json()
    communityKeys = {n["pageId"]: n["communityKey"] for n in body["nodes"]}
    assert communityKeys[pageA] is not None
    assert communityKeys[pageA] == communityKeys[pageB]
