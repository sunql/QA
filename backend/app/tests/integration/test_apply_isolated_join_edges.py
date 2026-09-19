"""apply_isolated_join_edges.py 数据修复脚本集成测试（真实 PG）。

背景（2026-09-18 孤岛巡检）：DIM_SUPPLIER 补边后巡检发现还有一批零边类。
本脚本按「探针先行」原则补 6 条经 THBI 真实数据验证（100% 重叠）的边：

- DWD_SUPPLIER_PAYMENT_LINE.PAYMENT_NO ↔ DWD_SUPPLIER_PAYMENT.PAYMENT_NO
- DWD_SUPPLIER_PRICE_LIST_HEADER.PRICE_LIST_CODE ↔ DWD_SUPPLIER_PRICE_LIST.PRICE_LIST_CODE
- DWD_SUPPLIER_PRICE_LIST_CONFIG.PRICE_LIST_CODE ↔ DWD_SUPPLIER_PRICE_LIST.PRICE_LIST_CODE
- DWD_BUSINESS_PARTNER.PARTNER_CODE ↔ DIM_SUPPLIER.BPSNUM_0
- DWD_CUSTOMER.CUSTOMER_CODE ↔ DWD_BUSINESS_PARTNER.PARTNER_CODE
- DIM_FACILITY.FCY_0 ↔ DWD_ITEM_FACILITY.FACILITY_CODE

LINKED_INVOICE_NO ↔ INVOICE_NO 探针 0% 重叠——刻意不建（死边比缺边更糟）。

覆盖：走 createJoin API（PG+audit+Neo4j fake）、幂等、缺类不中断。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import app.services.ontology_service as ontology_service_module
from app.dependencies import CurrentUser
from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.services.acl_service import ADMIN_ROLE
from app.services.ontology_service import OntologyService
from scripts.apply_isolated_join_edges import EDGES, apply

_ADMIN = CurrentUser(userId="t-admin", roles=(ADMIN_ROLE,), departments=())


class _FakeNeo4j:
    def __init__(self) -> None:
        self.joined: list[tuple[int, int]] = []

    def linkClassJoin(self, sourceId, targetId):  # noqa: N802
        self.joined.append((sourceId, targetId))


async def _noopSync(self, entity) -> None:
    return None


@pytest.fixture()
def _fakeNeo4j(monkeypatch):
    fake = _FakeNeo4j()
    monkeypatch.setattr(ontology_service_module, "neo4j", fake)
    monkeypatch.setattr(
        ontology_service_module.OntologyService,
        "_syncClassEmbeddingBestEffort",
        _noopSync,
    )
    return fake


async def _seedClasses(session, *, omit: str | None = None) -> dict[str, int]:
    """落 EDGES 涉及的全部类与列（可省略一个类验证 missing 路径）。"""
    now = datetime.now(timezone.utc)
    names = {name for e in EDGES for name in (e["sourceClass"], e["targetClass"])}
    if omit:
        names.discard(omit)
    ids: dict[str, int] = {}
    for name in sorted(names):
        cls = OntologyClass(
            class_name=name,
            source_table=name,
            created_time=now,
            updated_time=now,
        )
        session.add(cls)
        await session.flush()
        cols = {c for e in EDGES for c, clsName in (
            (e["sourceColumn"], e["sourceClass"]),
            (e["targetColumn"], e["targetClass"]),
        ) if clsName == name}
        for col in sorted(cols):
            session.add(
                OntologyProperty(
                    class_id=cls.id,
                    property_name=col,
                    source_column=col,
                    data_type="varchar",
                    created_time=now,
                    updated_time=now,
                )
            )
        ids[name] = cls.id
    await session.commit()
    return ids


async def _joinKeys(session) -> set[tuple]:
    rows = await session.execute(OntologyJoin.__table__.select())
    return {
        (r.source_class_id, tuple(r.source_columns), r.target_class_id, tuple(r.target_columns))
        for r in rows
    }


class TestApplyIsolatedJoinEdges:
    async def test_all_edges_created(self, _fakeNeo4j, dbSession) -> None:
        ids = await _seedClasses(dbSession)

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        assert summary["created"] == [f"{e['sourceClass']}→{e['targetClass']}" for e in EDGES]
        assert summary["skipped"] == []
        assert summary["missing"] == []
        keys = await _joinKeys(dbSession)
        for e in EDGES:
            assert (
                ids[e["sourceClass"]],
                (e["sourceColumn"],),
                ids[e["targetClass"]],
                (e["targetColumn"],),
            ) in keys
        assert len(_fakeNeo4j.joined) == len(EDGES)

    async def test_idempotent_rerun_all_skipped(self, _fakeNeo4j, dbSession) -> None:
        await _seedClasses(dbSession)
        await apply(dbSession, OntologyService(), actor=_ADMIN)

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        assert summary["created"] == []
        assert len(summary["skipped"]) == len(EDGES)
        rows = await dbSession.execute(OntologyJoin.__table__.select())
        assert rows.rowcount == len(EDGES)

    async def test_missing_class_reported_not_fatal(self, _fakeNeo4j, dbSession) -> None:
        await _seedClasses(dbSession, omit="DWD_SUPPLIER_PAYMENT")

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        assert len(summary["created"]) == len(EDGES) - 1
        assert summary["missing"] == ["DWD_SUPPLIER_PAYMENT:类不存在"]
        rows = await dbSession.execute(OntologyJoin.__table__.select())
        assert rows.rowcount == len(EDGES) - 1
