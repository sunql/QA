"""机制 6 覆盖度自感知的集成测试（feat-wiki-knowledge M7）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）。

覆盖：
- 网格形状（类 × 维度 × 域）、UNASSIGNED 哨兵、刷新幂等
- **判据只认已确认的关系**（候选/打回不算 —— 看板的价值就是数字可信）
- ``valid_to IS NULL`` 软删过滤 + 孤儿格自愈（域摘掉 / 类软删 / 维度改掉）
- 四态推导 MISSING → PARTIAL → OUTDATED → COMPLETE 及其优先级
- 缺口清单（默认排除未标域、MISSING 优先、limit 边界）
- 域标注归一化 / 幂等 / 404 / 422
- 孤儿条目统计、未认证 403
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig, OntologyClass
from app.domain.wiki_coverage_models import (
    DOMAIN_UNASSIGNED,
    ClassDomainMapping,
    CoverageCell,
)
from app.domain.wiki_models import KNOWLEDGE_DIMENSIONS, KnowledgeRelation
from app.infrastructure.llm.base_client import LlmResponse

pytestmark = pytest.mark.asyncio

_PAGES = "/api/v1/wiki/pages"
_RELATIONS = "/api/v1/wiki/relations"
_COVERAGE = "/api/v1/wiki/coverage"

# patch 目标：invoker 模块级导入的 createClient
_INVOKER_CLIENT = "app.services.learning.llm_invoker.createClient"

_DIMENSIONS = len(KNOWLEDGE_DIMENSIONS)
_ENTITIES_JSON = json.dumps({"entities": ["供应商"]})


class _FakeLlmClient:
    """假 LLM 客户端（与 test_wiki_relation_api 同形）。"""

    def __init__(self, content: str = _ENTITIES_JSON) -> None:
        self._content = content
        self.calls = 0

    async def complete(self, messages, **kwargs) -> LlmResponse:
        self.calls += 1
        return LlmResponse(
            content=self._content,
            modelName="fake-model",
            promptTokens=100,
            completionTokens=50,
            totalTokens=150,
        )


# ---------------------------------------------------------------------------
# Arrange 助手
# ---------------------------------------------------------------------------


async def _seedModel(dbSession: AsyncSession) -> int:
    config = LlmConfig(
        model_name="wiki-coverage-model",
        provider="openai_compatible_proxy",
        is_active=True,
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config.id


async def _seedClass(
    dbSession: AsyncSession,
    *,
    className: str,
    version: int = 1,
    deleted: bool = False,
) -> int:
    """建一个本体类，返回数值主键（覆盖度矩阵的键是 id 不是 name）。

    ``version`` 可调：``ontology_class`` 上有 UNIQUE(class_name, version)，
    同名多版本行必须靠 version 区分。
    """
    entity = OntologyClass(
        class_name=className,
        version=version,
        valid_to=datetime.now(UTC) if deleted else None,
    )
    dbSession.add(entity)
    await dbSession.commit()
    await dbSession.refresh(entity)
    return entity.id


async def _createPage(
    client: AsyncClient,
    *,
    title: str,
    dimension: str | None = None,
    status: str | None = None,
) -> str:
    resp = await client.post(_PAGES, json={"title": title, "content": "正文"})
    assert resp.status_code == 201, resp.text
    pageId = resp.json()["pageId"]
    patchBody = {
        key: value
        for key, value in (("dimension", dimension), ("status", status))
        if value is not None
    }
    if patchBody:
        resp = await client.patch(f"{_PAGES}/{pageId}", json=patchBody)
        assert resp.status_code == 200, resp.text
    return pageId


async def _link(
    dbSession: AsyncSession,
    pageId: str,
    className: str,
    *,
    confirmed: bool = True,
    rejected: bool = False,
    relationType: str = "DESCRIBES",
) -> int:
    """直接落一条「条目 → 本体类」关系行（Arrange 造数，不经过 LLM）。"""
    entity = KnowledgeRelation(
        upstream_page_id=pageId,
        downstream_type="ONTOLOGY_CLASS",
        downstream_id=className,
        relation_type=relationType,
        auto_detected=True,
        confirmed=confirmed,
        rejected_at=datetime.now(UTC) if rejected else None,
    )
    dbSession.add(entity)
    await dbSession.commit()
    await dbSession.refresh(entity)
    return entity.id


async def _refresh(client: AsyncClient) -> dict:
    resp = await client.post(f"{_COVERAGE}/refresh")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _cells(client: AsyncClient, **params) -> list[dict]:
    resp = await client.get(_COVERAGE, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _assign(
    client: AsyncClient, ontologyClassId: int, domain: str
) -> tuple[int, dict]:
    resp = await client.post(
        f"{_COVERAGE}/mappings",
        json={"ontologyClassId": ontologyClassId, "domain": domain},
    )
    return resp.status_code, (resp.json() if resp.content else {})


async def _findCell(
    client: AsyncClient, *, className: str, dimension: str, domain: str | None = None
) -> dict:
    """取指定类 + 维度的那一格（域默认 UNASSIGNED）。"""
    target = DOMAIN_UNASSIGNED if domain is None else domain
    matching = [
        cell
        for cell in await _cells(client)
        if cell["className"] == className
        and cell["dimension"] == dimension
        and cell["domain"] == target
    ]
    assert len(matching) == 1, f"期望恰好一格，实际 {len(matching)}：{matching}"
    return matching[0]


async def _mappings(dbSession: AsyncSession) -> list[ClassDomainMapping]:
    result = await dbSession.execute(
        select(ClassDomainMapping).order_by(ClassDomainMapping.id)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# 网格形状与刷新
# ---------------------------------------------------------------------------


async def test_refresh_builds_full_grid(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """一个已标域的类 × 全部维度 → 一个完整网格，且初始全红。"""
    # Arrange
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")

    # Act
    result = await _refresh(client)

    # Assert
    assert result["cellCount"] == _DIMENSIONS
    assert result["classCount"] == 1
    assert result["unmappedClassCount"] == 0
    cells = await _cells(client)
    assert len(cells) == _DIMENSIONS
    # 一条知识都没有 → 每一格都是 MISSING（宁可先全红，也不要假绿）
    assert {cell["coverageStatus"] for cell in cells} == {"MISSING"}
    assert {cell["domain"] for cell in cells} == {"PROCUREMENT"}
    assert {cell["className"] for cell in cells} == {"供应商"}
    assert {cell["pageCount"] for cell in cells} == {0}


async def test_refresh_uses_unassigned_sentinel(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """没标业务域的类落在 UNASSIGNED 桶里，并被单独计数。"""
    await _seedClass(dbSession, className="供应商")

    result = await _refresh(client)

    assert result["unmappedClassCount"] == 1
    assert {cell["domain"] for cell in await _cells(client)} == {DOMAIN_UNASSIGNED}


async def test_refresh_is_idempotent(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """重复刷新不重复建格（唯一键 + UPSERT），也不虚增计数。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")

    first = await _refresh(client)
    second = await _refresh(client)
    third = await _refresh(client)

    assert first["cellCount"] == second["cellCount"] == third["cellCount"]
    assert second["removedCount"] == 0
    assert len(await _cells(client)) == _DIMENSIONS
    rows = (await dbSession.execute(select(CoverageCell))).scalars().all()
    assert len(rows) == _DIMENSIONS


async def test_refresh_with_no_classes_yields_empty_matrix(
    client: AsyncClient,
) -> None:
    """一个本体类都没有时刷新不炸，返回空网格。"""
    result = await _refresh(client)

    assert result["cellCount"] == 0
    assert result["classCount"] == 0
    assert result["unmappedClassCount"] == 0
    assert result["removedCount"] == 0
    assert await _cells(client) == []


# ---------------------------------------------------------------------------
# 判据：只认已确认的关系
# ---------------------------------------------------------------------------


async def test_unconfirmed_relation_does_not_color_cell(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """未确认的候选关系**不算覆盖** —— 确认后同一格才变绿。

    这是整个机制的承重墙：拿机器猜测去刷绿，看板就失去意义了。
    """
    # Arrange：条目有维度、有关系行，但关系是**候选**（confirmed=False）
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    pageId = await _createPage(client, title="供应商准入", dimension="OBJECT")
    relationId = await _link(dbSession, pageId, "供应商", confirmed=False)

    # Act：先刷一次（候选状态）
    await _refresh(client)

    # Assert：仍然是 MISSING，条目数被算作 0
    cell = await _findCell(
        client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
    )
    assert cell["coverageStatus"] == "MISSING"
    assert cell["pageCount"] == 0

    # Act：确认关系后重刷
    resp = await client.post(f"{_RELATIONS}/{relationId}/confirm")
    assert resp.status_code == 200, resp.text
    await _refresh(client)

    # Assert：这一格认下了这条知识
    cell = await _findCell(
        client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
    )
    assert cell["pageCount"] == 1
    assert cell["coverageStatus"] == "PARTIAL"


async def test_rejected_relation_does_not_count(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """已打回的候选即使 confirmed 被置真也不算（rejected_at 是回收站标记）。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    pageId = await _createPage(client, title="供应商准入", dimension="OBJECT")
    await _link(dbSession, pageId, "供应商", confirmed=True, rejected=True)

    await _refresh(client)

    cell = await _findCell(
        client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
    )
    assert cell["pageCount"] == 0
    assert cell["coverageStatus"] == "MISSING"


async def test_page_without_dimension_is_not_counted(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """没维度的条目落不进任何一格（维度是矩阵的一根轴）。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    pageId = await _createPage(client, title="供应商准入")  # 不设 dimension
    await _link(dbSession, pageId, "供应商")

    await _refresh(client)

    cell = await _findCell(
        client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
    )
    assert cell["pageCount"] == 0


async def test_duplicate_relations_count_page_once(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同一条目对同一个类有多条关系（不同 relation_type）只算一条知识。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    pageId = await _createPage(client, title="供应商准入", dimension="OBJECT")
    await _link(dbSession, pageId, "供应商", relationType="DESCRIBES")
    await _link(dbSession, pageId, "供应商", relationType="APPLIES_TO")

    await _refresh(client)

    cell = await _findCell(
        client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
    )
    assert cell["pageCount"] == 1


async def test_duplicate_class_names_resolve_to_single_cell(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同名类多版本行只出一格（取最早一行），与关系发现的类目录口径一致。"""
    firstId = await _seedClass(dbSession, className="供应商")
    await _seedClass(dbSession, className="供应商", version=2)
    await _assign(client, firstId, "PROCUREMENT")

    result = await _refresh(client)

    assert result["classCount"] == 1
    cells = await _cells(client)
    assert len(cells) == _DIMENSIONS
    assert {cell["ontologyClassId"] for cell in cells} == {firstId}


# ---------------------------------------------------------------------------
# 四态推导
# ---------------------------------------------------------------------------


async def test_status_lifecycle_walks_all_four_states(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同一条知识走完 缺失 → 写了没审 → 失效 → 已生效 四态。

    四态各对应不同的修复动作，合并任何一个看板都说不出话。
    """
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    pageId = await _createPage(client, title="供应商准入", dimension="OBJECT")

    # 1) 一条都没有 → MISSING
    await _refresh(client)
    assert (
        await _findCell(
            client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
        )
    )["coverageStatus"] == "MISSING"

    # 2) 有知识但还在待审 → PARTIAL（该去审核）
    await _link(dbSession, pageId, "供应商")
    resp = await client.patch(f"{_PAGES}/{pageId}", json={"status": "REVIEW"})
    assert resp.status_code == 200, resp.text
    await _refresh(client)
    cell = await _findCell(
        client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
    )
    assert (cell["pageCount"], cell["approvedCount"], cell["coverageStatus"]) == (
        1,
        0,
        "PARTIAL",
    )

    # 3) 知识失效（没生效过）→ OUTDATED（该去更新那几条）
    resp = await client.patch(f"{_PAGES}/{pageId}", json={"status": "EXPIRED"})
    assert resp.status_code == 200, resp.text
    await _refresh(client)
    cell = await _findCell(
        client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
    )
    assert (cell["pageCount"], cell["approvedCount"], cell["coverageStatus"]) == (
        1,
        0,
        "OUTDATED",
    )

    # 4) 审到生效 → COMPLETE（无需动作）
    resp = await client.patch(f"{_PAGES}/{pageId}", json={"status": "APPROVED"})
    assert resp.status_code == 200, resp.text
    await _refresh(client)
    cell = await _findCell(
        client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
    )
    assert (cell["pageCount"], cell["approvedCount"], cell["coverageStatus"]) == (
        1,
        1,
        "COMPLETE",
    )


async def test_partial_when_only_some_pages_approved(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """两条知识只生效一条 → PARTIAL，两个计数都要如实给出。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    approvedId = await _createPage(
        client, title="供应商准入", dimension="OBJECT", status="EFFECTIVE"
    )
    draftId = await _createPage(client, title="供应商退出", dimension="OBJECT")
    await _link(dbSession, approvedId, "供应商")
    await _link(dbSession, draftId, "供应商")

    await _refresh(client)

    cell = await _findCell(
        client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
    )
    assert (cell["pageCount"], cell["approvedCount"], cell["coverageStatus"]) == (
        2,
        1,
        "PARTIAL",
    )


async def test_dimension_change_moves_page_between_cells(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """维度改了 → 知识从旧格移到新格，旧格自愈回 MISSING。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    pageId = await _createPage(
        client, title="供应商准入", dimension="OBJECT", status="APPROVED"
    )
    await _link(dbSession, pageId, "供应商")
    await _refresh(client)
    assert (
        await _findCell(
            client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
        )
    )["coverageStatus"] == "COMPLETE"

    # Act：专家把它改判为 RULE
    resp = await client.patch(f"{_PAGES}/{pageId}", json={"dimension": "RULE"})
    assert resp.status_code == 200, resp.text
    await _refresh(client)

    # Assert：新格绿，旧格红 —— 旧格是同一行被 UPSERT 改写，不是新插入
    assert (
        await _findCell(
            client, className="供应商", dimension="RULE", domain="PROCUREMENT"
        )
    )["coverageStatus"] == "COMPLETE"
    assert (
        await _findCell(
            client, className="供应商", dimension="OBJECT", domain="PROCUREMENT"
        )
    )["coverageStatus"] == "MISSING"
    assert len(await _cells(client)) == _DIMENSIONS


# ---------------------------------------------------------------------------
# 孤儿格自愈
# ---------------------------------------------------------------------------


async def test_soft_deleted_class_cells_are_removed(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """类下线（valid_to 落章）后重刷，它的格全部消失。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    await _refresh(client)
    assert len(await _cells(client)) == _DIMENSIONS

    # Act：软删这个类
    entity = (
        await dbSession.execute(select(OntologyClass).where(OntologyClass.id == classId))
    ).scalar_one()
    entity.valid_to = datetime.now(UTC)
    await dbSession.commit()

    result = await _refresh(client)

    # Assert：网格空了，且报告清理了几行（自愈，不是静默留脏）
    assert result["cellCount"] == 0
    assert result["removedCount"] == _DIMENSIONS
    assert await _cells(client) == []


async def test_removing_domain_mapping_returns_cells_to_unassigned(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """摘掉域标注 → 下一轮格回到 UNASSIGNED，旧域格消失。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    await _refresh(client)
    assert {cell["domain"] for cell in await _cells(client)} == {"PROCUREMENT"}

    # Act
    resp = await client.delete(
        f"{_COVERAGE}/mappings",
        params={"ontologyClassId": classId, "domain": "PROCUREMENT"},
    )
    assert resp.status_code == 204, resp.text
    result = await _refresh(client)

    # Assert：域格被清理，哨兵格顶上
    assert result["removedCount"] == _DIMENSIONS
    assert {cell["domain"] for cell in await _cells(client)} == {DOMAIN_UNASSIGNED}
    assert len(await _cells(client)) == _DIMENSIONS


async def test_class_with_two_domains_gets_two_cells(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """一个类可挂在多个域上（「供应商」既是采购也是质量），各出一格。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    await _assign(client, classId, "QUALITY")

    result = await _refresh(client)

    assert result["cellCount"] == _DIMENSIONS * 2
    assert {cell["domain"] for cell in await _cells(client)} == {
        "PROCUREMENT",
        "QUALITY",
    }


# ---------------------------------------------------------------------------
# 读取过滤
# ---------------------------------------------------------------------------


async def test_list_cells_filters(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """按域/维度/状态过滤矩阵。"""
    procurementId = await _seedClass(dbSession, className="供应商")
    qualityId = await _seedClass(dbSession, className="检验批")
    await _assign(client, procurementId, "PROCUREMENT")
    await _assign(client, qualityId, "QUALITY")
    pageId = await _createPage(
        client, title="供应商准入", dimension="OBJECT", status="APPROVED"
    )
    await _link(dbSession, pageId, "供应商")
    await _refresh(client)

    byDomain = await _cells(client, domain="procurement")  # 归一化后再匹配
    assert {cell["domain"] for cell in byDomain} == {"PROCUREMENT"}
    assert len(byDomain) == _DIMENSIONS

    byDimension = await _cells(client, dimension="OBJECT")
    assert {cell["dimension"] for cell in byDimension} == {"OBJECT"}
    assert len(byDimension) == 2

    byStatus = await _cells(client, coverageStatus="COMPLETE")
    assert [
        (cell["className"], cell["dimension"]) for cell in byStatus
    ] == [("供应商", "OBJECT")]


# ---------------------------------------------------------------------------
# 缺口清单与汇总
# ---------------------------------------------------------------------------


async def test_overview_reports_summary_gaps_and_unlinked(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """看板一次取齐三块：汇总 / 缺口 / 孤儿条目。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    pageId = await _createPage(
        client, title="供应商准入", dimension="OBJECT", status="APPROVED"
    )
    await _link(dbSession, pageId, "供应商")
    await _refresh(client)

    resp = await client.get(f"{_COVERAGE}/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 汇总：一格绿，其余全红；已标域故未分配格数为 0
    assert body["summary"]["totalCells"] == _DIMENSIONS
    assert body["summary"]["byStatus"]["COMPLETE"] == 1
    assert body["summary"]["byStatus"]["MISSING"] == _DIMENSIONS - 1
    assert body["summary"]["unassignedCells"] == 0

    # 缺口：只有红的那些格，且不含已绿的那一格
    assert len(body["gaps"]) == _DIMENSIONS - 1
    assert all(gap["status"] != "COMPLETE" for gap in body["gaps"])
    assert all(gap["domain"] == "PROCUREMENT" for gap in body["gaps"])
    assert all(gap["className"] == "供应商" for gap in body["gaps"])

    # 孤儿条目：这条已挂到类上，故为 0
    assert body["unlinked"]["gapType"] == "UNLINKED"
    assert body["unlinked"]["pageCount"] == 0


async def test_gaps_exclude_unassigned_by_default(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """未标域的类默认**不进**缺口清单（它是一条元待办，不是 8 条）。

    几十个未标域的类各自产出「维度数」条缺口，能把待办列表整个淹掉；
    它们由 ``unmappedCells`` 单独汇报，界面上再给一个开关放进来。
    """
    await _seedClass(dbSession, className="供应商")
    await _refresh(client)

    defaultBody = (await client.get(f"{_COVERAGE}/overview")).json()
    assert defaultBody["gaps"] == []
    assert defaultBody["summary"]["unassignedCells"] == _DIMENSIONS

    included = (
        await client.get(f"{_COVERAGE}/overview", params={"includeUnassigned": "true"})
    ).json()
    assert len(included["gaps"]) == _DIMENSIONS
    assert {gap["domain"] for gap in included["gaps"]} == {DOMAIN_UNASSIGNED}


async def test_gaps_put_missing_before_partial(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """完全没写过（MISSING）比「写了没审」（PARTIAL）更急，排前面。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    pageId = await _createPage(client, title="供应商准入", dimension="FAQ")
    await _link(dbSession, pageId, "供应商")
    await _refresh(client)

    gaps = (await client.get(f"{_COVERAGE}/overview")).json()["gaps"]

    assert [gap["status"] for gap in gaps] == ["MISSING"] * (
        _DIMENSIONS - 1
    ) + ["PARTIAL"]
    assert gaps[-1]["dimension"] == "FAQ"
    assert gaps[-1]["pageCount"] == 1


async def test_gap_limit_bounds(client: AsyncClient, dbSession: AsyncSession) -> None:
    """gapLimit 生效，且越界值被 FastAPI 挡在 422（不是静默截断）。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    await _refresh(client)

    limited = (
        await client.get(f"{_COVERAGE}/overview", params={"gapLimit": 3})
    ).json()
    assert len(limited["gaps"]) == 3
    # limit 只截清单，不动汇总
    assert limited["summary"]["totalCells"] == _DIMENSIONS

    tooBig = await client.get(f"{_COVERAGE}/overview", params={"gapLimit": 5000})
    assert tooBig.status_code == 422


async def test_unlinked_pages_gap(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """矩阵看不见的那部分缺口：条目有维度却没挂到任何已确认的类上。"""
    # Arrange：一条挂上了，一条没挂，一条只有候选关系
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    linkedId = await _createPage(client, title="供应商准入", dimension="OBJECT")
    await _createPage(client, title="验收标准", dimension="RULE")
    candidateId = await _createPage(client, title="退货流程", dimension="PROCESS")
    await _link(dbSession, linkedId, "供应商")
    await _link(dbSession, candidateId, "供应商", confirmed=False)

    body = (await client.get(f"{_COVERAGE}/overview")).json()

    assert body["unlinked"]["pageCount"] == 2
    assert body["unlinked"]["dimensions"] == {"RULE": 1, "PROCESS": 1}


# ---------------------------------------------------------------------------
# 域标注
# ---------------------------------------------------------------------------


async def test_assign_domain_normalizes_and_is_idempotent(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """域做 strip+upper 归一化；重复标注返回既有行而不是第二行。"""
    classId = await _seedClass(dbSession, className="供应商")

    status, first = await _assign(client, classId, "  procurement ")
    assert status == 201
    assert first["domain"] == "PROCUREMENT"
    assert first["className"] == "供应商"

    status, second = await _assign(client, classId, "PROCUREMENT")
    assert status == 201
    assert second["domain"] == "PROCUREMENT"
    assert second["createdTime"] == first["createdTime"]  # 同一行，不是新建一行

    mappings = await _mappings(dbSession)
    assert len(mappings) == 1


async def test_domain_has_no_whitelist(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """域词表不设白名单：知识积累到新领域时就该冒出新域（不锁业务域）。"""
    classId = await _seedClass(dbSession, className="供应商")

    status, body = await _assign(client, classId, "after_sales")
    assert status == 201
    assert body["domain"] == "AFTER_SALES"

    resp = await client.get(f"{_COVERAGE}/domains")
    assert resp.status_code == 200, resp.text
    assert resp.json()["domains"] == ["AFTER_SALES"]


async def test_assign_domain_rejects_blank_and_overlong(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """空串 / 纯空白 / 超长都要在边界上被挡住（422），不落到 DB 才炸。"""
    classId = await _seedClass(dbSession, className="供应商")

    status, _ = await _assign(client, classId, "")
    assert status == 422

    status, _ = await _assign(client, classId, "   ")
    assert status == 422

    status, _ = await _assign(client, classId, "X" * 101)
    assert status == 422

    assert await _mappings(dbSession) == []


async def test_assign_domain_unknown_class_404(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """给不存在的类标域返回 404，而不是留下一条指向空气的标注。"""
    status, _ = await _assign(client, 999999, "PROCUREMENT")

    assert status == 404


async def test_assign_domain_soft_deleted_class_404(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """已下线的类不能新标域（墓碑不该再长出标注）。"""
    classId = await _seedClass(dbSession, className="供应商", deleted=True)

    status, _ = await _assign(client, classId, "PROCUREMENT")

    assert status == 404


async def test_remove_missing_mapping_404(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """删不存在的标注报 404，不静默成功（静默成功会让前端以为删掉了）。"""
    classId = await _seedClass(dbSession, className="供应商")

    resp = await client.delete(
        f"{_COVERAGE}/mappings",
        params={"ontologyClassId": classId, "domain": "PROCUREMENT"},
    )

    assert resp.status_code == 404, resp.text


async def test_list_mappings_returns_class_name(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """标注列表带类名（只有 id 的界面没法对账）。"""
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")

    resp = await client.get(f"{_COVERAGE}/mappings")

    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["className"] == "供应商"
    assert rows[0]["ontologyClassId"] == classId
    assert rows[0]["domain"] == "PROCUREMENT"


# ---------------------------------------------------------------------------
# 端到端：发现 → 审核 → 刷新
# ---------------------------------------------------------------------------


async def test_discover_confirm_then_refresh_colors_cell(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """走完整链路：LLM 抽实体建候选 → 人工确认 → 刷新后该格认下这条知识。

    前面的判据测试是直接造关系行（聚焦矩阵逻辑），这一条覆盖「真实写入的
    关系行能否被矩阵接住」—— 两处口径（关系发现的类目录 vs 覆盖度的类目录）
    一旦漂移，就只有这条测得出。
    """
    # Arrange
    modelId = await _seedModel(dbSession)
    classId = await _seedClass(dbSession, className="供应商")
    await _assign(client, classId, "PROCUREMENT")
    pageId = await _createPage(
        client, title="采购管理办法", dimension="CONCEPT", status="APPROVED"
    )
    await _refresh(client)
    assert (
        await _findCell(
            client, className="供应商", dimension="CONCEPT", domain="PROCUREMENT"
        )
    )["coverageStatus"] == "MISSING"

    # Act：发现候选（假 LLM，但保留真实调用链）
    with patch(_INVOKER_CLIENT, return_value=_FakeLlmClient()):
        resp = await client.post(
            f"{_PAGES}/{pageId}/relations/discover", json={"modelId": modelId}
        )
    assert resp.status_code == 200, resp.text
    candidates = resp.json()["candidates"]
    assert [c["downstreamId"] for c in candidates] == ["供应商"]
    assert candidates[0]["confirmed"] is False

    resp = await client.post(f"{_RELATIONS}/{candidates[0]['id']}/confirm")
    assert resp.status_code == 200, resp.text
    await _refresh(client)

    # Assert
    cell = await _findCell(
        client, className="供应商", dimension="CONCEPT", domain="PROCUREMENT"
    )
    assert cell["pageCount"] == 1
    assert cell["approvedCount"] == 1
    assert cell["coverageStatus"] == "COMPLETE"


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------


async def test_coverage_requires_auth(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """认证关闭时整组覆盖度接口 403（router 级依赖，同 wiki 其他路由）。"""
    monkeypatch.setenv("AUTH_STUB_ENABLED", "0")

    assert (await client.get(_COVERAGE)).status_code == 403
    assert (await client.get(f"{_COVERAGE}/overview")).status_code == 403
    assert (await client.get(f"{_COVERAGE}/domains")).status_code == 403
    assert (await client.get(f"{_COVERAGE}/mappings")).status_code == 403
    assert (await client.post(f"{_COVERAGE}/refresh")).status_code == 403
    assert (
        await client.post(
            f"{_COVERAGE}/mappings",
            json={"ontologyClassId": 1, "domain": "PROCUREMENT"},
        )
    ).status_code == 403
    assert (
        await client.delete(
            f"{_COVERAGE}/mappings",
            params={"ontologyClassId": 1, "domain": "PROCUREMENT"},
        )
    ).status_code == 403
