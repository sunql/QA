"""apply_supplier_join_edges.py 数据修复脚本集成测试（真实 PG）。

背景（2026-09-18 诊断）：问「三家供应商 3 月供货量 top3 物料占比」报
「以下表无法通过关联路径连通：DIM_SUPPLIER」。根因：DIM_SUPPLIER (id 99)
在本体关联图中是零边孤岛——10 张带 SUPPLIER_CODE 的事实表没有一条连到它。
前置修复（apply_dim_supplier_metadata.py）把描述收敛成「供应商主数据优先用
DIM_SUPPLIER」后，LLM 服从了引导，反而踩中孤岛。

连接键已用 THBI 真实数据验证（2026-09-18 探针）：
DIM_SUPPLIER.BPSNUM_0 ↔ 事实表.SUPPLIER_CODE 重叠率 99%~100%。

覆盖：
- 经 OntologyService.createJoin 落 PG（含 audit + Neo4j fake 入图）——
  直写 PG 不会进 Neo4j，必须走 API 路径
- 幂等：重复执行全部 skipped
- 类缺失 / 列缺失只记 missing，不中断其余边
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import app.services.ontology_service as ontology_service_module
from app.dependencies import CurrentUser
from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty
from app.services.acl_service import ADMIN_ROLE
from app.services.ontology_service import OntologyService, makeJoinKey
from scripts.apply_supplier_join_edges import (
    DIM_CLASS_NAME,
    DIM_CODE_COLUMN,
    FACT_CLASSES,
    SUPPLIER_CODE_COLUMN,
    apply,
)

_ADMIN = CurrentUser(userId="t-admin", roles=(ADMIN_ROLE,), departments=())


class _FakeNeo4j:
    def __init__(self) -> None:
        self.joined: list[tuple[int, int]] = []

    def linkClassJoin(self, sourceId, targetId):  # noqa: N802
        self.joined.append((sourceId, targetId))

    def upsertClassNode(self, **kw):  # noqa: N802
        pass

    def upsertPropertyNode(self, **kw):  # noqa: N802
        pass

    def linkClassHasProperty(self, classId, propId):  # noqa: N802
        pass

    def reconcileClassSubclassOf(self, classId, parentId):  # noqa: N803
        pass


async def _noopSync(self, entity) -> None:
    return None


@pytest.fixture()
def _fakeNeo4j(monkeypatch):
    fake = _FakeNeo4j()
    monkeypatch.setattr(ontology_service_module, "neo4j", fake)
    # createJoin / createClass 均有 best-effort 入图调用；embedding 同步走后台任务
    monkeypatch.setattr(
        ontology_service_module.OntologyService,
        "_syncClassEmbeddingBestEffort",
        _noopSync,
    )
    return fake


async def _seedClasses(session, factNames: list[str] | None = None) -> dict[str, int]:
    """落 DIM_SUPPLIER（含 BPSNUM_0）+ 全部/部分事实表类（含 SUPPLIER_CODE）。"""
    now = datetime.now(timezone.utc)
    names = factNames if factNames is not None else list(FACT_CLASSES)
    ids: dict[str, int] = {}
    dim = OntologyClass(
        class_name=DIM_CLASS_NAME,
        source_table=DIM_CLASS_NAME,
        description="供应商主数据",
        created_time=now,
        updated_time=now,
    )
    session.add(dim)
    await session.flush()
    session.add(
        OntologyProperty(
            class_id=dim.id,
            property_name=DIM_CODE_COLUMN,
            source_column=DIM_CODE_COLUMN,
            data_type="varchar",
            created_time=now,
            updated_time=now,
        )
    )
    ids[DIM_CLASS_NAME] = dim.id
    for name in names:
        cls = OntologyClass(
            class_name=name,
            source_table=name,
            description=name,
            created_time=now,
            updated_time=now,
        )
        session.add(cls)
        await session.flush()
        session.add(
            OntologyProperty(
                class_id=cls.id,
                property_name=SUPPLIER_CODE_COLUMN,
                source_column=SUPPLIER_CODE_COLUMN,
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


class TestApplySupplierJoinEdges:
    async def test_all_edges_created_via_service(self, _fakeNeo4j, dbSession) -> None:
        ids = await _seedClasses(dbSession)

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        dimId = ids[DIM_CLASS_NAME]
        # 全部事实表建边成功，无缺失
        assert summary["created"] == [c for c in FACT_CLASSES]
        assert summary["skipped"] == []
        assert summary["missing"] == []

        keys = await _joinKeys(dbSession)
        for name in FACT_CLASSES:
            assert (
                ids[name],
                (SUPPLIER_CODE_COLUMN,),
                dimId,
                (DIM_CODE_COLUMN,),
            ) in keys

        # 走的是 createJoin → Neo4j fake 收到全部入图调用
        assert len(_fakeNeo4j.joined) == len(FACT_CLASSES)

    async def test_idempotent_rerun_all_skipped(self, _fakeNeo4j, dbSession) -> None:
        ids = await _seedClasses(dbSession)
        await apply(dbSession, OntologyService(), actor=_ADMIN)

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        assert summary["created"] == []
        assert sorted(summary["skipped"]) == sorted(FACT_CLASSES)
        # 不会重复入图
        assert len(_fakeNeo4j.joined) == len(FACT_CLASSES)
        rows = await dbSession.execute(OntologyJoin.__table__.select())
        assert rows.rowcount == len(FACT_CLASSES)
        assert ids[DIM_CLASS_NAME] > 0

    async def test_missing_fact_class_reported_not_fatal(self, _fakeNeo4j, dbSession) -> None:
        """只 seed 一张事实表：其余记 missing，已有的照常建边。"""
        only = "DWD_GOODS_RECEIPT"
        ids = await _seedClasses(dbSession, factNames=[only])

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        assert summary["created"] == [only]
        # missing 条目格式为「类名:原因」，按类名前缀比较
        assert {m.split(":")[0] for m in summary["missing"]} == set(FACT_CLASSES) - {only}

        keys = await _joinKeys(dbSession)
        assert (
            ids[only],
            (SUPPLIER_CODE_COLUMN,),
            ids[DIM_CLASS_NAME],
            (DIM_CODE_COLUMN,),
        ) in keys

    async def test_missing_fact_column_reported_not_fatal(self, _fakeNeo4j, dbSession) -> None:
        """事实表缺 SUPPLIER_CODE 属性：记 missing，不建死边。"""
        ids = await _seedClasses(dbSession)
        # 删掉一张事实表的 SUPPLIER_CODE 属性
        target = ids["DWD_PURCHASE_ORDER"]
        from sqlalchemy import delete

        await dbSession.execute(
            delete(OntologyProperty).where(
                OntologyProperty.class_id == target,
                OntologyProperty.property_name == SUPPLIER_CODE_COLUMN,
            )
        )
        await dbSession.commit()

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        assert "DWD_PURCHASE_ORDER:缺 SUPPLIER_CODE 属性" in summary["missing"]
        assert summary["created"] == [c for c in FACT_CLASSES if c != "DWD_PURCHASE_ORDER"]
        keys = await _joinKeys(dbSession)
        assert all(k[2] != target for k in keys)

    async def test_dim_code_column_missing_reported(self, _fakeNeo4j, dbSession) -> None:
        """DIM 侧连 BPSNUM_0 都没有：全部记 missing，零建边。"""
        now = datetime.now(timezone.utc)
        dim = OntologyClass(
            class_name=DIM_CLASS_NAME,
            source_table=DIM_CLASS_NAME,
            created_time=now,
            updated_time=now,
        )
        session = dbSession
        session.add(dim)
        await session.flush()
        for name in FACT_CLASSES:
            cls = OntologyClass(
                class_name=name,
                source_table=name,
                created_time=now,
                updated_time=now,
            )
            session.add(cls)
            await session.flush()
            session.add(
                OntologyProperty(
                    class_id=cls.id,
                    property_name=SUPPLIER_CODE_COLUMN,
                    source_column=SUPPLIER_CODE_COLUMN,
                    data_type="varchar",
                    created_time=now,
                    updated_time=now,
                )
            )
        await session.commit()

        summary = await apply(session, OntologyService(), actor=_ADMIN)

        # DIM 侧根因缺失：只报 DIM 根因，事实表不再逐个列举
        assert summary["missing"] == [f"{DIM_CLASS_NAME}:缺 BPSNUM_0 属性"]
        assert summary["created"] == []
        keys = await _joinKeys(session)
        assert keys == set()

    async def test_join_key_matches_service_convention(self, _fakeNeo4j, dbSession) -> None:
        """脚本预判幂等用的 join_key 必须与 service 落库值一致（错位会导致重复建边）。"""
        ids = await _seedClasses(dbSession)
        await apply(dbSession, OntologyService(), actor=_ADMIN)

        rows = await dbSession.execute(OntologyJoin.__table__.select())
        for r in rows:
            expected = makeJoinKey(
                r.source_class_id,
                [SUPPLIER_CODE_COLUMN],
                r.target_class_id,
                [DIM_CODE_COLUMN],
            )
            assert r.join_key == expected
