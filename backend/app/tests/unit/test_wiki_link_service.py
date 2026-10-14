"""WikiLinkService 单测（wiki-ontology-link Task 2，real PG per Harness 测试规范）。

覆盖：CRUD 5 个 + recall 4 个 + listLinkableTargets 3 个。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, ValidationError
from app.domain.models import OntologyMetric, WikiOntologyLink
from app.services.wiki_link_service import (
    LinkNotFoundError, WikiLinkService,
)

_svc = WikiLinkService()


async def _seed_page(dbSession: AsyncSession, page_id: str = "p001") -> None:
    """插入 wiki_page 行（FK 目标）。"""
    from app.domain.models import WikiPage
    dbSession.add(WikiPage(
        page_id=page_id, title="测试页", dimension="test",
        content="content", status="PUBLISHED",
        created_time=datetime.now(timezone.utc), updated_time=datetime.now(timezone.utc),
    ))
    await dbSession.flush()


async def _actor(userId: int = 42, dbUserId: int | None = 42):
    return type("Actor", (), {"userId": userId, "dbUserId": dbUserId})()


async def test_create_link_persists_row(dbSession):
    await _seed_page(dbSession)
    row = await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    assert row.page_id == "p001"
    assert row.ontology_type == "class"
    assert row.ontology_id == 12
    assert row.weight == Decimal("1.00")


async def test_create_link_conflict_returns_409(dbSession):
    await _seed_page(dbSession)
    await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    with pytest.raises(ConflictError):
        await _svc.createLink(
            dbSession,
            page_id="p001", chunk_id=None, ontology_type="class",
            ontology_id=12, weight=Decimal("1.00"), note=None,
            actor=await _actor(42),
        )


async def test_revoke_link_sets_revoked_time(dbSession):
    await _seed_page(dbSession)
    row = await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    revoked = await _svc.revokeLink(dbSession, row.id, actor=await _actor(42))
    assert revoked.revoked_time is not None


async def test_revoke_link_not_found(dbSession):
    with pytest.raises(LinkNotFoundError):
        await _svc.revokeLink(dbSession, 99999, actor=await _actor(42))


async def test_update_link_changes_weight_and_note(dbSession):
    await _seed_page(dbSession)
    row = await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    updated = await _svc.updateLink(
        dbSession, row.id, weight=Decimal("0.5"), note="updated",
        actor=await _actor(42),
    )
    assert updated.weight == Decimal("0.5")
    assert updated.note == "updated"


async def test_get_links_by_ontology_filters_unrevoked_pairs(dbSession):
    await _seed_page(dbSession)
    await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    pairs = [("class", 12), ("property", 99)]
    rows = await _svc.getLinksByOntology(dbSession, pairs)
    assert len(rows) == 1
    assert rows[0].ontology_id == 12


async def test_get_links_by_ontology_skips_revoked(dbSession):
    await _seed_page(dbSession)
    row = await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    await _svc.revokeLink(dbSession, row.id, actor=await _actor(42))
    rows = await _svc.getLinksByOntology(dbSession, [("class", 12)])
    assert rows == []


async def test_get_links_by_page_returns_all(dbSession):
    await _seed_page(dbSession, page_id="p001")
    await _seed_page(dbSession, page_id="p002")
    await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    await _svc.createLink(
        dbSession,
        page_id="p002", chunk_id="c001", ontology_type="property",
        ontology_id=99, weight=Decimal("0.5"), note=None,
        actor=await _actor(42),
    )
    rows = await _svc.getLinksByPage(dbSession, "p001")
    assert len(rows) == 1
    assert rows[0].page_id == "p001"


async def test_get_links_by_ontology_empty_pairs_returns_empty(dbSession):
    rows = await _svc.getLinksByOntology(dbSession, [])
    assert rows == []


async def test_list_linkable_targets_filters_by_type_class(dbSession):
    targets = await _svc.listLinkableTargets(dbSession, "class", query=None, limit=10)
    assert all(t.type == "class" for t in targets)


async def test_list_linkable_targets_query_filters_by_name(dbSession):
    # Seed a known class so we can verify query filtering
    from app.domain.models import OntologyClass
    dbSession.add(OntologyClass(class_name="DIM_SUPPLIER", class_alias="供应商", description="供应商维度"))
    await dbSession.flush()
    targets = await _svc.listLinkableTargets(dbSession, "class", query="SUPPLIER", limit=10)
    assert len(targets) >= 1
    assert any("SUPPLIER" in t.name.upper() for t in targets)


async def test_list_linkable_targets_invalid_type_returns_empty(dbSession):
    targets = await _svc.listLinkableTargets(dbSession, "unknown", query=None, limit=10)
    assert targets == []


async def test_create_link_invalid_type_returns_422(dbSession):
    await _seed_page(dbSession)
    with pytest.raises(ValidationError):
        await _svc.createLink(
            dbSession,
            page_id="p001", chunk_id=None, ontology_type="unknown",
            ontology_id=12, weight=Decimal("1.00"), note=None,
            actor=await _actor(42),
        )


async def test_list_configured_ontology_types_excludes_revoked(dbSession):
    """只统计**未撤销**的链接类型。

    构造要点（这一条写错整个测试就是假的）：被撤销的那条必须用一个
    **只出现在被撤销行里**的类型，否则删掉实现里的 ``revoked_time IS NULL``
    过滤时结果集不变、测试照样绿 —— 那就成了对核心契约零保护的假测试。
    """
    await _seed_page(dbSession)
    actor = await _actor(42)
    await _svc.createLink(
        dbSession, page_id="p001", chunk_id=None, ontology_type="property",
        ontology_id=13, weight=Decimal("1.00"), note=None, actor=actor,
    )
    revoked = await _svc.createLink(
        dbSession, page_id="p001", chunk_id=None, ontology_type="class",
        ontology_id=12, weight=Decimal("1.00"), note=None, actor=actor,
    )
    await _svc.revokeLink(dbSession, revoked.id, actor=actor)

    # class 只以「已撤销」的身份出现 ⇒ 漏掉 revoked 过滤会得到 {"class","property"}
    assert await _svc.listConfiguredOntologyTypes(dbSession) == {"property"}


async def test_list_configured_ontology_types_empty_when_no_links(dbSession):
    """空表返回空集 —— 这是「没配就零成本」的前提。"""
    assert await _svc.listConfiguredOntologyTypes(dbSession) == set()


async def test_list_linkable_targets_metric_returns_metrics(dbSession):
    """metric 现在必须返回指标，而不是空列表。"""
    dbSession.add(OntologyMetric(
        id=801, metric_name="KPI_SUPPLIER_OTD", metric_alias="供应商准时交付率",
        formula="SUM(a)/SUM(b)", agg_function="SUM",
    ))
    await dbSession.flush()

    targets = await _svc.listLinkableTargets(dbSession, "metric", query=None, limit=10)
    assert [(t.id, t.type, t.name, t.alias) for t in targets] == [
        (801, "metric", "KPI_SUPPLIER_OTD", "供应商准时交付率"),
    ]


async def test_create_metric_link_persists(dbSession):
    """metric 链接必须能落库（service 层成功路径）。

    与 integration 的 test_create_metric_link_accepted 互补：那条走 HTTP 全链路，
    这条只压 service 层（createLink 的类型校验 + 落库两件事）。

    注意：本用例**不能**用来钉 ORM 的 chk_link_type —— unit/ 目录由
    app/tests/unit/conftest.py 覆盖 dbSession 为真实 PG（表结构由 alembic 产生），
    ORM 元数据不参与建表。app/domain/models.py 那处 CheckConstraint 与迁移 0106
    对齐是为了 ORM↔迁移一致；当前**没有任何测试路径会强制执行它**
    （根 conftest 的 create_all 路径已无消费者）。
    """
    await _seed_page(dbSession)
    row = await _svc.createLink(
        dbSession,
        page_id="p001", chunk_id=None, ontology_type="metric",
        ontology_id=801, weight=Decimal("1.00"), note=None,
        actor=await _actor(42),
    )
    assert row.ontology_type == "metric"
    assert row.ontology_id == 801
