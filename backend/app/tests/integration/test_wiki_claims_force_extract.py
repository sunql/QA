"""事实原子抽取（机制 6）force 重抽集成测试。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。
覆盖：
- 无 modelId → SKIPPED（不调 LLM，不删旧 claims）
- 已有 claims + 无 force → ALREADY_DONE（幂等保护）
- 已有 claims + force → 删除旧 claims/evidence 后重抽（SUCCEEDED）
- force 但 LLM 返回无效 → INVALID，旧 claims 原样保留（先有新再有删）
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import Evidence, KnowledgeClaim
from app.services.learning.claim_extractor import ClaimExtractor

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/wiki/pages"


class _StubInvoker:
    """真实 LLM 的替身：completeJson 直接返回固定 payload，不发起网络调用。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def completeJson(self, **_kwargs):  # noqa: ANN003
        return self._payload, None


_VALID_PAYLOAD = {
    "claims": [
        {
            "claim_text": "供应商注册资本不低于 1000 万",
            "claim_type": "RULE",
            "evidence_quote": "注册资本 >= 1000 万",
        }
    ]
}


async def _createPage(client: AsyncClient) -> dict:
    resp = await client.post(
        _BASE, json={"title": "供应商准入规则", "content": "注册资本 >= 1000 万"}
    )
    assert resp.status_code == 201
    return resp.json()


async def _seedClaim(
    dbSession: AsyncSession, pageId: str, *, text: str = "旧事实原子"
) -> int:
    """落一条 claim + 一条 evidence，返回 claim id。

    刻意返回原始 id 而非 ORM 对象：commit 后实例过期，测试里再访问
    ``claim.id`` 会跨 greenlet 边界触发同步 IO（MissingGreenlet）。
    """
    claim = KnowledgeClaim(page_id=pageId, claim_text=text, claim_type="RULE")
    dbSession.add(claim)
    await dbSession.flush()
    claimId = claim.id
    dbSession.add(
        Evidence(claim_id=claimId, source_type="WIKI_PAGE", source_id=pageId)
    )
    await dbSession.commit()
    return claimId


async def test_extract_skipped_without_model_id(client: AsyncClient) -> None:
    """modelId 为空 → SKIPPED，不产生任何 claims。"""
    page = await _createPage(client)
    resp = await client.post(f"{_BASE}/{page['pageId']}/claims/extract", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "SKIPPED"


async def test_extract_already_done_when_claims_exist(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """已有 claims 且未指定 force → ALREADY_DONE，旧 claims 原样保留。

    modelId 传一个不存在 id 也不会触发 LLM：幂等检查在调用之前短路。
    """
    page = await _createPage(client)
    pageId = page["pageId"]
    oldClaimId = await _seedClaim(dbSession, pageId)

    resp = await client.post(
        f"{_BASE}/{pageId}/claims/extract", json={"modelId": 999999}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ALREADY_DONE"
    dbSession.expire_all()
    remaining = (
        await dbSession.execute(
            select(KnowledgeClaim).where(KnowledgeClaim.page_id == pageId)
        )
    ).scalars().all()
    assert [c.id for c in remaining] == [oldClaimId]


async def test_force_without_model_id_skipped_and_preserves_claims(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """force=true 但无 modelId → 仍 SKIPPED，且**不删除**旧 claims。

    invoker 为空的短路在删除之前，保证「没模型可用」永远不会造成数据丢失。
    """
    page = await _createPage(client)
    pageId = page["pageId"]
    oldClaimId = await _seedClaim(dbSession, pageId)

    resp = await client.post(
        f"{_BASE}/{pageId}/claims/extract", json={"force": True}
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "SKIPPED"
    dbSession.expire_all()
    remaining = (
        await dbSession.execute(
            select(KnowledgeClaim.id).where(KnowledgeClaim.page_id == pageId)
        )
    ).scalars().all()
    assert remaining == [oldClaimId]


async def test_force_reextract_replaces_claims_and_evidence(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """force=true + 可用 invoker → 旧 claims/evidence 全清，写入新抽取结果。"""
    page = await _createPage(client)
    pageId = page["pageId"]
    oldClaimId = await _seedClaim(dbSession, pageId)

    result = await ClaimExtractor().extractForPage(
        dbSession, pageId, invoker=_StubInvoker(_VALID_PAYLOAD), force=True
    )
    await dbSession.commit()

    assert result.status == "SUCCEEDED"
    assert result.claimCount == 1
    dbSession.expire_all()
    claims = (
        await dbSession.execute(
            select(KnowledgeClaim).where(KnowledgeClaim.page_id == pageId)
        )
    ).scalars().all()
    assert len(claims) == 1
    assert claims[0].id != oldClaimId
    assert claims[0].claim_text == "供应商注册资本不低于 1000 万"
    # 旧 claim 的 evidence 依赖 DB 级联清理
    evidences = (
        await dbSession.execute(
            select(Evidence).where(Evidence.claim_id == oldClaimId)
        )
    ).scalars().all()
    assert list(evidences) == []


async def test_force_reextract_invalid_preserves_old_claims(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """force=true 但 LLM 输出无有效 claims → INVALID，旧 claims 原样保留。

    刻意设计：先拿到有效新结果再删旧数据，LLM 失败/无效输出不构成丢数据的理由。
    """
    page = await _createPage(client)
    pageId = page["pageId"]
    oldClaimId = await _seedClaim(dbSession, pageId)

    result = await ClaimExtractor().extractForPage(
        dbSession, pageId, invoker=_StubInvoker({"claims": []}), force=True
    )
    await dbSession.commit()

    assert result.status == "INVALID"
    dbSession.expire_all()
    remaining = (
        await dbSession.execute(
            select(KnowledgeClaim.id).where(KnowledgeClaim.page_id == pageId)
        )
    ).scalars().all()
    assert remaining == [oldClaimId]


async def test_no_force_preserves_claims_with_stub_invoker(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """对照组：force=false + 已有 claims + 可用 invoker → ALREADY_DONE，不删不增。"""
    page = await _createPage(client)
    pageId = page["pageId"]
    oldClaimId = await _seedClaim(dbSession, pageId)

    result = await ClaimExtractor().extractForPage(
        dbSession, pageId, invoker=_StubInvoker(_VALID_PAYLOAD), force=False
    )

    assert result.status == "ALREADY_DONE"
    dbSession.expire_all()
    remaining = (
        await dbSession.execute(
            select(KnowledgeClaim.id).where(KnowledgeClaim.page_id == pageId)
        )
    ).scalars().all()
    assert remaining == [oldClaimId]
