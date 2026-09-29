"""M3 置信度 4 级 API 集成测试（v3.1 任务 B4）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）：
- GET /wiki/pages/{pageId}/claims 响应带 confidenceLevel / refuseReason
- 冲突前置真实触发 REFUSE（knowledge_conflict 未解决 + page_ids @>）
- PATCH /wiki/compile/claims/{claimId} 响应同上
- 真实 claim 链路（page + claim + evidence）服务层注入 historySnapshot
  返回 HIGH 且 breakdown 可解释（生产 v1 传 None → 冷启动上限 MEDIUM，
  这是 verdict 6 的刻意设计）
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_learning_models import KnowledgeConflict
from app.domain.wiki_models import Evidence, KnowledgeClaim
from app.services.confidence_service import (
    ConfidenceHistorySnapshot,
    calculateClaimConfidence,
)

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/wiki/pages"


async def _createPage(client: AsyncClient, *, pageId: str) -> str:
    resp = await client.post(
        _BASE, json={"pageId": pageId, "title": f"t-{pageId}", "content": "注册资本 >= 1000 万"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["pageId"]


async def _seedClaim(
    dbSession: AsyncSession,
    *,
    pageId: str,
    withTriple: bool = True,
    evidenceSourceId: str | None = "DOC-1",
) -> int:
    claim = KnowledgeClaim(
        page_id=pageId,
        claim_text="供应商注册资本不低于 1000 万",
        subject_id="SUPPLIER-1" if withTriple else None,
        predicate="注册资本" if withTriple else None,
        object_value="1000万" if withTriple else None,
        object_type="STATISTIC" if withTriple else None,
    )
    dbSession.add(claim)
    await dbSession.flush()
    if evidenceSourceId is not None:
        dbSession.add(
            Evidence(claim_id=claim.id, source_type="DOCUMENT", source_id=evidenceSourceId)
        )
        await dbSession.flush()
    await dbSession.commit()
    return claim.id


# ---------------------------------------------------------------------------
# 读路径：GET claims
# ---------------------------------------------------------------------------


async def test_list_claims_returns_confidence_level(client: AsyncClient, dbSession: AsyncSession) -> None:
    """完整证据 + 三元组 + 无冲突（冷启动无历史）→ MEDIUM，无拒绝原因。"""
    pageId = "PAGE-B4-MEDIUM"
    await _createPage(client, pageId=pageId)
    await _seedClaim(dbSession, pageId=pageId)

    resp = await client.get(f"{_BASE}/{pageId}/claims")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    claim = items[0]
    assert claim["confidenceLevel"] == "MEDIUM"
    assert claim["refuseReason"] is None


async def test_list_claims_refuse_when_no_evidence(client: AsyncClient, dbSession: AsyncSession) -> None:
    """无证据 → REFUSE + 具体原因（含表级时效未评估注记）。"""
    pageId = "PAGE-B4-NOEV"
    await _createPage(client, pageId=pageId)
    await _seedClaim(dbSession, pageId=pageId, evidenceSourceId=None)

    resp = await client.get(f"{_BASE}/{pageId}/claims")
    items = resp.json()
    assert items[0]["confidenceLevel"] == "REFUSE"
    reason = items[0]["refuseReason"]
    assert reason is not None and "证据" in reason
    assert "表级时效未评估" in reason


async def test_list_claims_refuse_when_open_conflict(client: AsyncClient, dbSession: AsyncSession) -> None:
    """未解决冲突（page_ids @> pageId）→ REFUSE；处置后不再拒绝。"""
    pageId = "PAGE-B4-CONFLICT"
    await _createPage(client, pageId=pageId)
    await _seedClaim(dbSession, pageId=pageId)

    dbSession.add(
        KnowledgeConflict(
            conflict_type="CONTRADICTION",
            page_ids=[pageId, "PAGE-OTHER"],
            severity="HIGH",
        )
    )
    await dbSession.commit()

    resp = await client.get(f"{_BASE}/{pageId}/claims")
    items = resp.json()
    assert items[0]["confidenceLevel"] == "REFUSE"
    reason = items[0]["refuseReason"]
    assert reason is not None and "冲突" in reason

    # 处置冲突（resolved_at 置位）→ 前置不再命中
    conflict = (
        await dbSession.execute(
            KnowledgeConflict.__table__.select().where(
                KnowledgeConflict.conflict_type == "CONTRADICTION"
            )
        )
    ).first()
    await dbSession.execute(
        KnowledgeConflict.__table__.update()
        .where(KnowledgeConflict.id == conflict.id)
        .values(resolved_at=conflict.auto_detected_at)
    )
    await dbSession.commit()

    resp = await client.get(f"{_BASE}/{pageId}/claims")
    items = resp.json()
    assert items[0]["confidenceLevel"] != "REFUSE"
    assert items[0]["refuseReason"] is None


# ---------------------------------------------------------------------------
# 服务层编排：真实 claim 链路注入 historySnapshot → HIGH + breakdown
# ---------------------------------------------------------------------------


async def test_calculate_claim_confidence_high_with_breakdown(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """page + claim + evidence 真实落库，注入历史快照 → HIGH，breakdown 全项可解释。"""
    pageId = "PAGE-B4-HIGH"
    await _createPage(client, pageId=pageId)
    claimId = await _seedClaim(dbSession, pageId=pageId)

    from sqlalchemy import select

    claim = (
        await dbSession.execute(select(KnowledgeClaim).where(KnowledgeClaim.id == claimId))
    ).scalar_one()

    result = await calculateClaimConfidence(
        dbSession,
        claim,
        historySnapshot=ConfidenceHistorySnapshot(usageCount=25, accuracy=0.95),
    )
    assert result.level.value == "HIGH"
    assert result.score == 3
    breakdown = result.breakdown
    assert breakdown.hasEvidence is True
    assert breakdown.isStale is False
    assert breakdown.hasOpenConflict is False
    assert breakdown.hasCompleteEvidence is True
    assert breakdown.rulesMatched is True
    assert breakdown.hasSufficientHistory is True
    assert breakdown.hasHighHistoricalAccuracy is True


# ---------------------------------------------------------------------------
# PATCH /wiki/compile/claims/{claimId}
# ---------------------------------------------------------------------------


async def test_patch_compile_claim_returns_confidence(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    pageId = "PAGE-B4-PATCH"
    await _createPage(client, pageId=pageId)
    claimId = await _seedClaim(dbSession, pageId=pageId)

    resp = await client.patch(
        f"/api/v1/wiki/compile/claims/{claimId}", json={"claimText": "修订后的断言"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 修订后三元组被标 triple_stale → 数据未更新前置命中 → REFUSE
    assert body["confidenceLevel"] == "REFUSE"
    assert body["refuseReason"] is not None and "数据未更新" in body["refuseReason"]
    assert body["tripleStale"] is True
