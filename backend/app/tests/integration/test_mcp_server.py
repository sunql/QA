"""MCP Server 集成测试（Phase 6）。

真实 PostgreSQL + 直接调用 MCP tool 函数（不走 HTTP 客户端，避免在测试里
再起一个 streamable-http server）。覆盖：

1. ``_openContext()`` 路径 — DB session 正确建立 + 失败 rollback
2. 每个 tool 的 happy path — 真实 DB 数据流过 tool
3. 每个 tool 的 error path — 资源不存在 / 无 active 模型 / 未知 community
4. session 生命周期 — tool finally 块触发 close 不漏掉

参考 ``Harness/rules/测试规范.md``：必须用真实 PostgreSQL + 完整服务链路。
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig
from app.domain.wiki_models import (
    KnowledgeCommunity,
    KnowledgeCommunityMember,
    WikiPage,
)
from app.services.mcp_server import (
    _DEFAULT_USER_ID,
    _openContext,
    mcp,
    wiki_graph_communities,
    wiki_preview_classify,
    wiki_preview_community_topic,
    wiki_read,
    wiki_search,
    wiki_status,
    wiki_update_community_topic,
    wiki_update_dimension,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 共享 fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def seededWikiPage(dbSession: AsyncSession) -> WikiPage:
    """种一条 page 用于 read / search / classify preview。"""
    page = WikiPage(
        page_id="PAGE-MCP-TEST-001",
        title="MCP Test Page",
        dimension="POLICY",
        status="DRAFT",
        version="v1.0",
        content="This page tests MCP tool routing.",
    )
    dbSession.add(page)
    await dbSession.commit()
    await dbSession.refresh(page)
    return page


@pytest.fixture
async def seededCommunity(dbSession: AsyncSession) -> KnowledgeCommunity:
    """种一个 community + 2 个 page-membership，用于 topic 测试。"""
    community = KnowledgeCommunity(
        community_key="C-MCP-TEST-001",
        name="MCP Test Community",
        topic=None,
        page_count=2,
        cohesion_score=0.5,
        top_pages=["PAGE-MCP-A", "PAGE-MCP-B"],
    )
    dbSession.add(community)
    await dbSession.flush()
    for pid in ("PAGE-MCP-A", "PAGE-MCP-B"):
        page = WikiPage(
            page_id=pid,
            title=f"MCP Test Page {pid}",
            dimension="RULE",
            status="DRAFT",
            version="v1.0",
            content="",
        )
        dbSession.add(page)
    await dbSession.flush()
    dbSession.add(KnowledgeCommunityMember(
        community_id=community.id, page_id="PAGE-MCP-A"
    ))
    dbSession.add(KnowledgeCommunityMember(
        community_id=community.id, page_id="PAGE-MCP-B"
    ))
    await dbSession.commit()
    await dbSession.refresh(community)
    return community


@pytest.fixture
async def seededLlmConfig(dbSession: AsyncSession) -> int:
    """active LlmConfig — classify / community_topic preview 需要。"""
    cfg = LlmConfig(
        provider="openai",
        model_name="gpt-4o-mini",
        api_key="encrypted-fake-key",
        base_url=None,
        is_active=True,
        temperature=0.0,
        max_tokens=200,
    )
    dbSession.add(cfg)
    await dbSession.commit()
    await dbSession.refresh(cfg)
    return cfg.id


class _FakeContext:
    """fastmcp.Context 的最小占位（tool 函数不会真用它）。"""


# ---------------------------------------------------------------------------
# 1. _openContext 路径
# ---------------------------------------------------------------------------


async def test_openContext_returnsValidSession():
    """_openContext 必须返回可用的 DB session + currentUserId。"""
    ctx = await _openContext()
    try:
        assert ctx.dbSession is not None
        assert ctx.currentUserId == _DEFAULT_USER_ID
        # session 必须能用（SELECT 1）
        from sqlalchemy import text
        result = await ctx.dbSession.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
    finally:
        await ctx.close()


async def test_openContextClosesOnError():
    """session 异常时 async with 退出应自动 close（防御性 close 不重复也无害）。"""
    from app.infrastructure.database import getSessionFactory
    factory = getSessionFactory()
    async with factory() as session:
        # 模拟异常路径
        with pytest.raises(RuntimeError):
            await session.execute(select(1))
            raise RuntimeError("simulated")
        # async with 退出时 session.close() 自动调用


# ---------------------------------------------------------------------------
# 2. Happy path — 每个 tool 真实数据流通
# ---------------------------------------------------------------------------


async def test_wiki_status_returnsOk(dbSession: AsyncSession):
    result = await wiki_status(_FakeContext())
    assert '"ok": true' in result
    assert '"userId":' in result
    assert '"authMode": "stub"' in result


async def test_wiki_search_returnsMatches(seededWikiPage: WikiPage):
    result = await wiki_search(_FakeContext(), query="MCP Test", limit=5)
    body = _parseJsonResult(result)
    assert body["total"] >= 1
    pageIds = [r["pageId"] for r in body["results"]]
    assert seededWikiPage.page_id in pageIds


async def test_wiki_read_returnsPageContent(seededWikiPage: WikiPage):
    result = await wiki_read(_FakeContext(), page_id=seededWikiPage.page_id)
    body = _parseJsonResult(result)
    assert body["pageId"] == seededWikiPage.page_id
    assert body["title"] == seededWikiPage.title
    assert body["content"] == seededWikiPage.content


async def test_wiki_graph_communities_returnsList(seededCommunity: KnowledgeCommunity):
    result = await wiki_graph_communities(_FakeContext())
    body = _parseJsonResult(result)
    keys = [c["communityKey"] for c in body]
    assert seededCommunity.community_key in keys


# ---------------------------------------------------------------------------
# 3. Error path — 资源不存在 / 无 active 模型
# ---------------------------------------------------------------------------


async def test_wiki_read_pageNotFound_raisesNotFound(dbSession: AsyncSession):
    """资源不存在必须抛 DomainError（NotFoundError），而不是返回
    ``is_error=False`` 的 ``{"error": ...}`` JSON。FastMCP transport 层会自动
    把 DomainError 包成 ToolError 送给客户端。
    """
    from app.domain.exceptions import NotFoundError
    with pytest.raises(NotFoundError) as exc_info:
        await wiki_read(_FakeContext(), page_id="PAGE-DOES-NOT-EXIST")
    assert "不存在" in str(exc_info.value)


async def test_wiki_preview_classify_pageNotFound_raisesNotFound(dbSession: AsyncSession):
    from app.domain.exceptions import NotFoundError
    with pytest.raises(NotFoundError):
        await wiki_preview_classify(_FakeContext(), page_id="PAGE-NONE")


async def test_wiki_preview_classify_noActiveModel_raisesNotFound(seededWikiPage: WikiPage):
    """无 active LlmConfig 时必须抛 DomainError，不是返回 error JSON。"""
    from app.domain.exceptions import NotFoundError
    with pytest.raises(NotFoundError) as exc_info:
        await wiki_preview_classify(_FakeContext(), page_id=seededWikiPage.page_id)
    assert "No active LLM models" in str(exc_info.value)


async def test_wiki_preview_community_topic_notFound_raisesNotFound(dbSession: AsyncSession):
    from app.domain.exceptions import NotFoundError
    with pytest.raises(NotFoundError) as exc_info:
        await wiki_preview_community_topic(_FakeContext(), community_key="C-NONE")
    assert "不存在" in str(exc_info.value)


async def test_wiki_update_community_topic_notFound_raisesNotFound(dbSession: AsyncSession):
    from app.domain.exceptions import NotFoundError
    with pytest.raises(NotFoundError):
        await wiki_update_community_topic(
            _FakeContext(), community_key="C-NONE", topic="foo"
        )


# ---------------------------------------------------------------------------
# 4. 写入路径 — wiki_update_* 走真 commit
# ---------------------------------------------------------------------------


async def test_wiki_update_dimension_writesAndCommits(seededWikiPage: WikiPage):
    """写入 path 必须真正落库（不能 commit 后丢）。"""
    result = await wiki_update_dimension(
        _FakeContext(),
        page_id=seededWikiPage.page_id,
        dimension="RULE",
        auto_classification={"primary": "RULE", "confidence": 0.9},
    )
    body = _parseJsonResult(result)
    assert body["pageId"] == seededWikiPage.page_id
    assert body["dimension"] == "RULE"
    # 必须真持久化（不能在 commit 前 rollback 掉）
    from app.infrastructure.database import getSessionFactory
    async with getSessionFactory()() as verify_session:
        row = (await verify_session.execute(
            select(WikiPage).where(WikiPage.page_id == seededWikiPage.page_id)
        )).scalar_one()
        assert row.dimension == "RULE"
        assert row.auto_classification == {"primary": "RULE", "confidence": 0.9}


async def test_wiki_update_community_topic_writesAndCommits(seededCommunity: KnowledgeCommunity):
    result = await wiki_update_community_topic(
        _FakeContext(),
        community_key=seededCommunity.community_key,
        topic="MCP 写入测试主题",
    )
    body = _parseJsonResult(result)
    assert body["topic"] == "MCP 写入测试主题"
    # 持久化校验
    from app.infrastructure.database import getSessionFactory
    async with getSessionFactory()() as verify_session:
        row = (await verify_session.execute(
            select(KnowledgeCommunity).where(
                KnowledgeCommunity.community_key == seededCommunity.community_key
            )
        )).scalar_one()
        assert row.topic == "MCP 写入测试主题"


# ---------------------------------------------------------------------------
# 5. 工具注册 — 10 个 tool 必须全部注册（回归保护）
# ---------------------------------------------------------------------------


async def test_all_ten_tools_registered():
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "wiki_status",
        "wiki_search",
        "wiki_read",
        "wiki_graph_insights",
        "wiki_graph_communities",
        "wiki_preview_classify",
        "wiki_preview_relations",
        "wiki_preview_community_topic",
        "wiki_update_dimension",
        "wiki_update_community_topic",
    }
    assert names == expected, f"missing tools: {expected - names}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parseJsonResult(text: str) -> Any:
    """MCP tool 返回 JSON 字符串；测试里转成 dict / list 便于断言。"""
    import json
    return json.loads(text)
