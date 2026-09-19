"""apply_dim_imaterial_join_edges.py 集成测试（真实 PG）。

背景（2026-09-19 需求）：DIM_IMATERIAL 是由 ODS_ITMMASTER 加工的物料维度
（ITMREF_0 物料编码 + ITMDES1_0~3_0 物料描述），需要与采购/到货/收货明细
及物料-厂地枢纽关联，让 NL2SQL 可通过物料编码取物料描述等属性。

连接键探针（2026-09-19，THBI）：DIM_IMATERIAL.ITMREF_0(350922) ↔ 各
MATERIAL_CODE 值域重叠全部 100%。

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
from scripts.apply_dim_imaterial_join_edges import (
    DIM_CLASS_NAME,
    DIM_CODE_COLUMN,
    EDGES,
    MATERIAL_COLUMN,
    apply,
)

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
    now = datetime.now(timezone.utc)
    names = {n for e in EDGES for n in (e["sourceClass"], e["targetClass"])} | {DIM_CLASS_NAME}
    if omit:
        names.discard(omit)
    # 按 EDGES 找每个源类该用什么列名（_DTL 用 ITEM_CODE，其他用 MATERIAL_CODE）
    colsPerSrc: dict[str, str] = {
        e["sourceClass"]: e["sourceColumn"] for e in EDGES
    }
    ids: dict[str, int] = {}
    for name in sorted(names):
        cls = OntologyClass(
            class_name=name, source_table=name,
            created_time=now, updated_time=now,
        )
        session.add(cls)
        await session.flush()
        if name == DIM_CLASS_NAME:
            cols = [DIM_CODE_COLUMN]
        else:
            cols = [colsPerSrc[name]] if name in colsPerSrc else [MATERIAL_COLUMN]
        for col in cols:
            session.add(
                OntologyProperty(
                    class_id=cls.id, property_name=col, source_column=col,
                    data_type="varchar", created_time=now, updated_time=now,
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


class TestApplyDimImaterialJoinEdges:
    async def test_all_edges_created(self, _fakeNeo4j, dbSession) -> None:
        ids = await _seedClasses(dbSession)

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        assert len(summary["created"]) == len(EDGES)
        assert summary["skipped"] == []
        assert summary["missing"] == []
        keys = await _joinKeys(dbSession)
        for e in EDGES:
            assert (
                ids[e["sourceClass"]],
                (MATERIAL_COLUMN,),
                ids[e["targetClass"]],
                (DIM_CODE_COLUMN,),
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

    async def test_missing_line_class_reported_not_fatal(self, _fakeNeo4j, dbSession) -> None:
        await _seedClasses(dbSession, omit="DWD_ARRIVAL_NOTICE_LINE")

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        assert len(summary["created"]) == len(EDGES) - 1
        assert summary["missing"] == ["DWD_ARRIVAL_NOTICE_LINE:类不存在"]
        rows = await dbSession.execute(OntologyJoin.__table__.select())
        assert rows.rowcount == len(EDGES) - 1
