"""Wiki 知识条目 REST 端点集成测试（feat-wiki-knowledge M1）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。
覆盖：创建/查询/更新/删除、page_id 生成与冲突、维度白名单、
分页、级联删除（claim/relation 随 Page 一起清）。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_models import KnowledgeClaim, KnowledgeRelation, WikiPage

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/wiki/pages"


async def _createPage(
    client: AsyncClient, *, title: str = "供应商准入规则", body: str = "注册资本 >= 1000 万"
) -> dict:
    resp = await client.post(_BASE, json={"title": title, "content": body})
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# 创建 / 读取
# ---------------------------------------------------------------------------


async def test_create_generates_page_id_and_defaults(client: AsyncClient) -> None:
    """未提供 pageId 时自动生成；默认 DRAFT + MARKDOWN 阶段。"""
    # Arrange / Act
    body = await _createPage(client)

    # Assert
    assert body["pageId"].startswith("PAGE-")
    assert body["title"] == "供应商准入规则"
    assert body["status"] == "DRAFT"
    assert body["structureStage"] == "MARKDOWN"
    assert body["dimension"] is None
    assert body["version"] == "v1.0"


async def test_create_with_explicit_page_id_and_dimension(client: AsyncClient) -> None:
    """业务专家可显式指定 pageId 与维度。"""
    resp = await client.post(
        _BASE,
        json={
            "pageId": "RULE-SUPPLIER-QUALIFY-V3",
            "title": "供应商准入规则 V3.0",
            "content": "注册资本 >= 1000 万，且成立 >= 3 年",
            "dimension": "RULE",
            "authorityLevel": "L5",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["pageId"] == "RULE-SUPPLIER-QUALIFY-V3"
    assert body["dimension"] == "RULE"
    assert body["authorityLevel"] == "L5"


async def test_duplicate_page_id_returns_409(client: AsyncClient) -> None:
    """同一 pageId 重复创建 → 409。"""
    payload = {"pageId": "DUP-001", "title": "重复", "content": "x"}
    assert (await client.post(_BASE, json=payload)).status_code == 201
    assert (await client.post(_BASE, json=payload)).status_code == 409


async def test_create_invalid_dimension_returns_422(client: AsyncClient) -> None:
    """维度不在白名单 → 422（挡住脏数据污染覆盖度聚合轴）。"""
    resp = await client.post(
        _BASE,
        json={"title": "非法维度", "content": "x", "dimension": "NOT_A_DIMENSION"},
    )
    assert resp.status_code == 422


async def test_get_not_found_returns_404(client: AsyncClient) -> None:
    resp = await client.get(f"{_BASE}/NO-SUCH-PAGE")
    assert resp.status_code == 404


async def test_get_roundtrip(client: AsyncClient) -> None:
    """创建后可按 pageId 读回。"""
    created = await _createPage(client)
    resp = await client.get(f"{_BASE}/{created['pageId']}")
    assert resp.status_code == 200
    assert resp.json()["pageId"] == created["pageId"]


# ---------------------------------------------------------------------------
# 更新（PATCH 语义）
# ---------------------------------------------------------------------------


async def test_patch_updates_only_provided_fields(client: AsyncClient) -> None:
    """PATCH 只改传入字段，未传字段保持原值。"""
    # Arrange
    created = await _createPage(client)

    # Act
    resp = await client.patch(
        f"{_BASE}/{created['pageId']}",
        json={"dimension": "RULE", "title": "供应商准入规则（修订）"},
    )

    # Assert
    assert resp.status_code == 200
    body = resp.json()
    assert body["dimension"] == "RULE"
    assert body["title"] == "供应商准入规则（修订）"
    assert body["content"] == created["content"]  # 未传 → 保持
    assert body["status"] == "DRAFT"  # 未传 → 保持


async def test_patch_invalid_dimension_returns_422(client: AsyncClient) -> None:
    created = await _createPage(client)
    resp = await client.patch(
        f"{_BASE}/{created['pageId']}", json={"dimension": "BOGUS"}
    )
    assert resp.status_code == 422


async def test_patch_invalid_structure_stage_returns_422(client: AsyncClient) -> None:
    created = await _createPage(client)
    resp = await client.patch(
        f"{_BASE}/{created['pageId']}", json={"structureStage": "HALF_BAKED"}
    )
    assert resp.status_code == 422


async def test_patch_structure_stage_accepted(client: AsyncClient) -> None:
    """合法的结构阶段升级（机制 5 的落点）。"""
    created = await _createPage(client)
    resp = await client.patch(
        f"{_BASE}/{created['pageId']}", json={"structureStage": "FULLY_STRUCTURED"}
    )
    assert resp.status_code == 200
    assert resp.json()["structureStage"] == "FULLY_STRUCTURED"


async def test_patch_explicit_null_clears_nullable_field(client: AsyncClient) -> None:
    """显式传 null = 清除（区别于「未提供」= 保持）。

    这条守的是 PATCH 语义的边界：``updatePage`` 用 model_fields_set +
    isinstance 判断，必须让 None 通过、只跳过 UNSET 哨兵。
    """
    # Arrange：先写入一个可空字段
    created = await client.post(
        _BASE,
        json={"title": "带维度的条目", "content": "x", "dimension": "RULE",
              "authorityLevel": "L5"},
    )
    assert created.status_code == 201
    pageId = created.json()["pageId"]

    # Act：显式置空 dimension，不动其它
    resp = await client.patch(f"{_BASE}/{pageId}", json={"dimension": None})

    # Assert
    assert resp.status_code == 200
    body = resp.json()
    assert body["dimension"] is None
    assert body["authorityLevel"] == "L5"  # 未传 → 保持


async def test_patch_invalid_status_returns_422(client: AsyncClient) -> None:
    """状态不在白名单 → 422（挡住脏值污染状态轴统计）。"""
    created = await _createPage(client)
    resp = await client.patch(f"{_BASE}/{created['pageId']}", json={"status": "GARBAGE"})
    assert resp.status_code == 422


async def test_patch_valid_status_accepted(client: AsyncClient) -> None:
    created = await _createPage(client)
    resp = await client.patch(f"{_BASE}/{created['pageId']}", json={"status": "APPROVED"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "APPROVED"


async def test_patch_overlong_title_returns_422_not_500(client: AsyncClient) -> None:
    """超长 title 必须在边界被挡（422），不能走到 INSERT 变成 500。"""
    created = await _createPage(client)
    resp = await client.patch(f"{_BASE}/{created['pageId']}", json={"title": "x" * 250})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 列表 / 分页 / 过滤
# ---------------------------------------------------------------------------


async def test_list_empty(client: AsyncClient) -> None:
    resp = await client.get(_BASE)
    assert resp.status_code == 200
    assert resp.json() == {"rows": [], "total": 0}


async def test_list_pagination_and_filter(client: AsyncClient) -> None:
    """分页 total 独立于 limit；按维度过滤生效。"""
    # Arrange：3 条 RULE + 1 条 CONCEPT
    for i in range(3):
        await client.post(
            _BASE,
            json={"title": f"规则{i}", "content": "x", "dimension": "RULE"},
        )
    await client.post(
        _BASE, json={"title": "术语", "content": "x", "dimension": "CONCEPT"}
    )

    # Act
    resp = await client.get(_BASE, params={"limit": 2})

    # Assert
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 2
    assert body["total"] == 4

    filtered = await client.get(_BASE, params={"dimension": "RULE"})
    assert filtered.json()["total"] == 3


# ---------------------------------------------------------------------------
# 删除 / 级联
# ---------------------------------------------------------------------------


async def test_delete_removes_page(client: AsyncClient) -> None:
    created = await _createPage(client)
    assert (await client.delete(f"{_BASE}/{created['pageId']}")).status_code == 204
    assert (await client.get(f"{_BASE}/{created['pageId']}")).status_code == 404


async def test_delete_cascades_claims_and_relations(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """删除 Page 连带清理 claim / relation（DB 级联，service 不手工级联）。"""
    # Arrange
    created = await _createPage(client)
    pageId = created["pageId"]
    dbSession.add(
        KnowledgeClaim(page_id=pageId, claim_text="注册资本 >= 1000 万", claim_type="RULE")
    )
    dbSession.add(
        KnowledgeRelation(
            upstream_page_id=pageId,
            downstream_type="ONTOLOGY_CLASS",
            downstream_id="SUPPLIER",
            relation_type="DESCRIBES",
        )
    )
    await dbSession.commit()

    # Act
    assert (await client.delete(f"{_BASE}/{pageId}")).status_code == 204

    # Assert
    dbSession.expire_all()
    claims = (
        await dbSession.execute(
            select(KnowledgeClaim).where(KnowledgeClaim.page_id == pageId)
        )
    ).scalars().all()
    relations = (
        await dbSession.execute(
            select(KnowledgeRelation).where(
                KnowledgeRelation.upstream_page_id == pageId
            )
        )
    ).scalars().all()
    assert list(claims) == []
    assert list(relations) == []


async def test_delete_not_found_returns_404(client: AsyncClient) -> None:
    assert (await client.delete(f"{_BASE}/NO-SUCH-PAGE")).status_code == 404


# ---------------------------------------------------------------------------
# claims / relations 读取
# ---------------------------------------------------------------------------


async def test_list_claims_with_evidence(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """claims 读取返回事实原子（含证据子列表）。"""
    # Arrange
    created = await _createPage(client)
    pageId = created["pageId"]
    claim = KnowledgeClaim(
        page_id=pageId, claim_text="成立 >= 3 年", claim_type="RULE"
    )
    dbSession.add(claim)
    await dbSession.commit()

    # Act
    resp = await client.get(f"{_BASE}/{pageId}/claims")

    # Assert
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["claimText"] == "成立 >= 3 年"
    assert rows[0]["evidences"] == []


async def test_list_relations_confirmed_filter(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """confirmedOnly=true 只返回已审核生效的关系。"""
    # Arrange
    created = await _createPage(client)
    pageId = created["pageId"]
    dbSession.add_all(
        [
            KnowledgeRelation(
                upstream_page_id=pageId,
                downstream_type="ONTOLOGY_CLASS",
                downstream_id="SUPPLIER",
                relation_type="DESCRIBES",
                confirmed=True,
            ),
            KnowledgeRelation(
                upstream_page_id=pageId,
                downstream_type="ONTOLOGY_CLASS",
                downstream_id="MATERIAL",
                relation_type="DESCRIBES",
                auto_detected=True,
                confirmed=False,
            ),
        ]
    )
    await dbSession.commit()

    # Act
    allResp = await client.get(f"{_BASE}/{pageId}/relations")
    confirmedResp = await client.get(
        f"{_BASE}/{pageId}/relations", params={"confirmedOnly": "true"}
    )

    # Assert
    assert allResp.status_code == 200
    assert len(allResp.json()) == 2
    assert len(confirmedResp.json()) == 1
    assert confirmedResp.json()[0]["downstreamId"] == "SUPPLIER"


async def test_list_claims_page_not_found_returns_404(client: AsyncClient) -> None:
    resp = await client.get(f"{_BASE}/NO-SUCH-PAGE/claims")
    assert resp.status_code == 404
