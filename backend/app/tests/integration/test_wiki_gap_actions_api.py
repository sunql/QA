"""知识缺口操作入口集成测试（Phase 5.5：GraphInsightsPanel 可操作性）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。

覆盖三类 gap 的"预览 → 确认"两步操作：
- MISSING_DIMENSION → /classify/preview → PATCH dimension
- ISOLATED_PAGE    → /relations/suggest → POST relations/discover
- SPARSE_COMMUNITY → /communities/topic-suggest → PATCH community topic
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig
from app.domain.wiki_models import (
    KnowledgeCommunity,
    KnowledgeRelation,
    WikiPage,
)
from app.infrastructure.llm.base_client import LlmResponse

pytestmark = pytest.mark.asyncio

_PAGES = "/api/v1/wiki/pages"
_GRAPH = "/api/v1/wiki/graph"
_COMMUNITIES = f"{_GRAPH}/communities"

_ADMIN_HEADERS = {"X-User-Id": "wiki-gap-admin", "X-User-Roles": "admin"}


@pytest.fixture
async def seededLlmConfig(dbSession: AsyncSession) -> int:
    """在每个测试前种一个 active LlmConfig —— /classify/preview 用
    ``ModelConfigService.list(activeOnly=True)`` 取第一个作为默认模型。
    """
    return await _seedModel(dbSession)


class _StubClassifierClient:
    """Fake LLM client：``complete`` 返回 AutoClassifier 能解析的 JSON。

    LLM 调用链是：AutoClassifier → invoker.completeJson → invoker.complete
    → invoker._invokeOnce → client.complete()。所以我们 stub 的是最底层
    ``client.complete``（返回 ``LlmResponse``），JSON 解析由 invoker 自己
    完成。
    """

    def __init__(self, primary: str = "RULE", confidence: float = 0.9) -> None:
        self.primary = primary
        self.confidence = confidence

    async def complete(self, _messages, **_kwargs) -> LlmResponse:  # noqa: ANN003
        payload = {
            "primary": self.primary,
            "confidence": self.confidence,
            "alternatives": ["POLICY", "DOCUMENT"],
            "reason": f"测试建议：{self.primary}",
        }
        return LlmResponse(
            content=json.dumps(payload, ensure_ascii=False),
            modelName="stub-classifier",
            promptTokens=10,
            completionTokens=5,
            totalTokens=15,
        )


class _StubTopicClient:
    """Fake LLM client：``complete`` 返回 CommunityTopicSuggester 能解析的 JSON。"""

    async def complete(self, _messages, **_kwargs) -> LlmResponse:  # noqa: ANN003
        payload = {"topic": "采购供应商分级管理"}
        return LlmResponse(
            content=json.dumps(payload, ensure_ascii=False),
            modelName="stub-topic",
            promptTokens=20,
            completionTokens=8,
            totalTokens=28,
        )


async def _seedModel(dbSession: AsyncSession) -> int:
    from decimal import Decimal

    config = LlmConfig(
        model_name="gap-test",
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


async def _createPage(
    client: AsyncClient,
    *,
    title: str,
    dimension: str | None = "RULE",
    content: str = "正文内容",
) -> str:
    resp = await client.post(
        _PAGES,
        json={"title": title, "content": content, "dimension": dimension},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["pageId"]


async def _forceNullDimension(dbSession: AsyncSession, pageId: str) -> None:
    """测试夹具：直接把 page.dimension 置为 None，模拟 MISSING_DIMENSION gap。"""
    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    page.dimension = None
    page.auto_classification = None
    await dbSession.commit()


# ---------------------------------------------------------------------------
# MISSING_DIMENSION → /classify/preview
# ---------------------------------------------------------------------------


async def test_classify_preview_returns_suggestion_without_writing(
    client: AsyncClient, dbSession: AsyncSession, seededLlmConfig: int
) -> None:
    """预览端点：调 LLM 返建议，**不**写 wiki_page.dimension。"""
    pageId = await _createPage(client, title="待重分类的规则", dimension="RULE")
    await _forceNullDimension(dbSession, pageId)

    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubClassifierClient(primary="POLICY", confidence=0.85),
    ):
        resp = await client.post(
            f"{_PAGES}/{pageId}/classify/preview",
            headers=_ADMIN_HEADERS,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["primary"] == "POLICY"
    assert body["confidence"] == 0.85
    # alternatives 已过滤 primary：POLICY 在原 stub 里，但解析时与 primary 相同的
    # 项会被剔除（_parseSuggestion ``a != primary``），剩下 DOCUMENT。
    assert "DOCUMENT" in body["alternatives"]

    # 关键不变量：page.dimension 仍是 None
    dbSession.expire_all()
    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    assert page.dimension is None


async def test_classify_preview_404_for_unknown_page(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        f"{_PAGES}/PAGE-DOES-NOT-EXIST/classify/preview",
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 404


async def test_classify_confirm_via_patch_writes_dimension(
    client: AsyncClient, dbSession: AsyncSession, seededLlmConfig: int
) -> None:
    """预览后通过 PATCH 确认：把 dimension 写入 page。"""
    pageId = await _createPage(client, title="采购限价规则", dimension="RULE")
    await _forceNullDimension(dbSession, pageId)

    # 1) 预览拿到建议
    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubClassifierClient(primary="RULE"),
    ):
        preview = await client.post(
            f"{_PAGES}/{pageId}/classify/preview",
            headers=_ADMIN_HEADERS,
        )
    assert preview.status_code == 200

    # 2) 用户确认 → PATCH 写库
    patch_resp = await client.patch(
        f"{_PAGES}/{pageId}",
        json={"dimension": preview.json()["primary"]},
        headers=_ADMIN_HEADERS,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["dimension"] == "RULE"

    # 3) 库内确认
    dbSession.expire_all()
    page = (
        await dbSession.execute(select(WikiPage).where(WikiPage.page_id == pageId))
    ).scalar_one()
    assert page.dimension == "RULE"


# ---------------------------------------------------------------------------
# ISOLATED_PAGE → /relations/suggest
# ---------------------------------------------------------------------------


async def test_relations_suggest_returns_candidates_without_writing(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """预览端点：发现候选 + 不写 knowledge_relation。"""
    # 制造一个引用关系：pageA 的正文中包含 pageB 的标题 → 引用检测
    # 会算出 REFERENCES 候选，但 dryRun 模式不会 commit。
    pageAId = await _createPage(
        client,
        title="采购管理制度",
        content="本制度遵循《供应商管理办法》的总则要求。",
    )
    pageBId = await _createPage(
        client,
        title="供应商管理办法",
        content="（独立文档）",
    )

    resp = await client.post(
        f"{_PAGES}/{pageAId}/relations/suggest",
        json={"model_id": None},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 应该有一条指向 pageB 的 REFERENCES 候选（CamelModel 序列化用驼峰）
    refCandidates = [
        c for c in body["candidates"]
        if c["relationType"] == "REFERENCES"
        and c["downstreamId"] == pageBId
    ]
    assert refCandidates, f"未发现指向 {pageBId} 的引用候选: {body}"
    assert refCandidates[0]["downstreamTitle"] == "供应商管理办法"
    assert refCandidates[0]["confidence"] > 0

    # 关键不变量：knowledge_relation 表里没有新增记录
    from sqlalchemy import func as saFunc
    relCount = (
        await dbSession.execute(
            saFunc.count(KnowledgeRelation.id).select()
        )
    ).scalar_one()
    assert relCount == 0


async def test_relations_suggest_404_for_unknown_page(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        f"{_PAGES}/PAGE-DOES-NOT-EXIST/relations/suggest",
        json={"model_id": None},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 404


async def test_relations_suggest_then_discover_writes_candidate(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """预览后通过 /discover 真正落库。"""
    pageAId = await _createPage(
        client,
        title="采购付款流程",
        content="依《采购付款单据要求》执行。",
    )
    pageBId = await _createPage(
        client,
        title="采购付款单据要求",
        content="（独立文档）",
    )

    # 1) 预览
    preview = await client.post(
        f"{_PAGES}/{pageAId}/relations/suggest",
        json={"model_id": None},
        headers=_ADMIN_HEADERS,
    )
    assert preview.status_code == 200
    assert preview.json()["total"] >= 1

    # 2) 确认落库
    discover = await client.post(
        f"{_PAGES}/{pageAId}/relations/discover",
        json={"model_id": None},
        headers=_ADMIN_HEADERS,
    )
    assert discover.status_code == 200, discover.text
    # 落库结果里应能找到指向 pageB 的关系（CamelModel → JSON 字段驼峰）
    found = [
        r for r in discover.json()["candidates"]
        if r["downstreamId"] == pageBId and r["relationType"] == "REFERENCES"
    ]
    assert found

    # 3) 库内确认
    dbSession.expire_all()
    rows = (
        await dbSession.execute(
            select(KnowledgeRelation).where(
                KnowledgeRelation.upstream_page_id == pageAId
            )
        )
    ).scalars().all()
    target_ids = {r.downstream_id for r in rows}
    assert pageBId in target_ids


# ---------------------------------------------------------------------------
# SPARSE_COMMUNITY → /communities/{key}/topic-suggest + PATCH /communities/{key}
# ---------------------------------------------------------------------------


async def _createCommunityWithPages(
    dbSession: AsyncSession,
    *,
    communityKey: str,
    pageTitles: list[str],
) -> str:
    """手工造一个社区：跳过 Louvain 重算，直接 INSERT 一行
    ``KnowledgeCommunity`` + 若干 ``KnowledgeCommunityMember``。
    """
    from app.domain.wiki_models import (
        KnowledgeCommunity,
        KnowledgeCommunityMember,
        WikiPage,
    )

    community = KnowledgeCommunity(
        community_key=communityKey,
        name=f"社区 {communityKey}",
        cohesion_score=0.1,  # 模拟「稀疏」
        page_count=len(pageTitles),
        top_pages=[],
        topic=None,
    )
    dbSession.add(community)
    await dbSession.flush()

    for title in pageTitles:
        page = WikiPage(
            page_id=f"PAGE-{title}",
            title=title,
            content=f"{title} 正文",
            dimension="RULE",
            status="ACTIVE",
            created_by_user_id=1,
        )
        dbSession.add(page)
        await dbSession.flush()
        dbSession.add(
            KnowledgeCommunityMember(
                community_id=community.id, page_id=page.page_id,
            )
        )
    await dbSession.commit()
    return communityKey


async def test_community_topic_suggest_returns_topic_without_writing(
    client: AsyncClient, dbSession: AsyncSession, seededLlmConfig: int
) -> None:
    """预览端点：调 LLM 返主题，**不**写 community.topic。"""
    from app.domain.wiki_models import KnowledgeCommunity

    await _createCommunityWithPages(
        dbSession,
        communityKey="SPARSE-COMMUNITY-TEST",
        pageTitles=[
            "采购供应商黑名单管理办法",
            "采购供应商评估打分规则",
            "采购供应商分级管理细则",
        ],
    )

    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubTopicClient(),
    ):
        resp = await client.post(
            f"{_COMMUNITIES}/SPARSE-COMMUNITY-TEST/topic-suggest",
            headers=_ADMIN_HEADERS,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["topic"] == "采购供应商分级管理"
    assert body["pageCount"] == 3
    assert "采购供应商黑名单管理办法" in body["pageTitles"]

    # 关键不变量：community.topic 仍是 None（没写库）
    dbSession.expire_all()
    community = (
        await dbSession.execute(
            select(KnowledgeCommunity).where(
                KnowledgeCommunity.community_key == "SPARSE-COMMUNITY-TEST"
            )
        )
    ).scalar_one()
    assert community.topic is None


async def test_community_topic_suggest_404_for_unknown_community(
    client: AsyncClient, seededLlmConfig: int
) -> None:
    resp = await client.post(
        f"{_COMMUNITIES}/DOES-NOT-EXIST/topic-suggest",
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 404


async def test_community_topic_suggest_then_patch_writes_topic(
    client: AsyncClient, dbSession: AsyncSession, seededLlmConfig: int
) -> None:
    """预览后 PATCH 写库。"""
    from app.domain.wiki_models import KnowledgeCommunity

    await _createCommunityWithPages(
        dbSession,
        communityKey="PATCH-COMMUNITY-TEST",
        pageTitles=["采购付款规则", "采购付款时效要求"],
    )

    # 1) 预览
    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubTopicClient(),
    ):
        preview = await client.post(
            f"{_COMMUNITIES}/PATCH-COMMUNITY-TEST/topic-suggest",
            headers=_ADMIN_HEADERS,
        )
    assert preview.status_code == 200

    # 2) PATCH 确认
    patch_resp = await client.patch(
        f"{_COMMUNITIES}/PATCH-COMMUNITY-TEST",
        json={"topic": preview.json()["topic"]},
        headers=_ADMIN_HEADERS,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["community"]["topic"] == "采购供应商分级管理"

    # 3) 库内确认
    dbSession.expire_all()
    community = (
        await dbSession.execute(
            select(KnowledgeCommunity).where(
                KnowledgeCommunity.community_key == "PATCH-COMMUNITY-TEST"
            )
        )
    ).scalar_one()
    assert community.topic == "采购供应商分级管理"


async def test_community_patch_topic_omitted_means_noop(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """PATCH body 不含 topic 字段 → 不动库（区分「不改」与「清空」）。"""
    from app.domain.wiki_models import KnowledgeCommunity

    await _createCommunityWithPages(
        dbSession,
        communityKey="NOOP-COMMUNITY-TEST",
        pageTitles=["规则一", "规则二"],
    )
    # 先手动设个 topic 验证不动
    dbSession.expire_all()
    community = (
        await dbSession.execute(
            select(KnowledgeCommunity).where(
                KnowledgeCommunity.community_key == "NOOP-COMMUNITY-TEST"
            )
        )
    ).scalar_one()
    community.topic = "既有主题"
    await dbSession.commit()

    resp = await client.patch(
        f"{_COMMUNITIES}/NOOP-COMMUNITY-TEST",
        json={},  # 不带 topic
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["community"]["topic"] == "既有主题"


async def test_community_patch_topic_null_clears(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """PATCH {"topic": null} → 清空社区主题。"""
    from app.domain.wiki_models import KnowledgeCommunity

    await _createCommunityWithPages(
        dbSession,
        communityKey="CLEAR-COMMUNITY-TEST",
        pageTitles=["条目"],
    )
    dbSession.expire_all()
    community = (
        await dbSession.execute(
            select(KnowledgeCommunity).where(
                KnowledgeCommunity.community_key == "CLEAR-COMMUNITY-TEST"
            )
        )
    ).scalar_one()
    community.topic = "待清空"
    await dbSession.commit()

    resp = await client.patch(
        f"{_COMMUNITIES}/CLEAR-COMMUNITY-TEST",
        json={"topic": None},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["community"]["topic"] is None
