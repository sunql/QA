"""POST /api/v1/lineage/edges/extract 集成测试（真实 PG + 完整 API 链路）。

覆盖：
- 三类血缘源（JOIN / Metric.formula / schema introspection CDC）经 HTTP 触发抽取并落库
- 幂等：同数据第二次 extract → created=0（extractEdges 内置与现有行去重）
- 空 ontology → created=0，不报错

数据准备用 dbSession（真实 PG 造数），Act 走 client（HTTP 入口），Assert 走
GET /api/v1/lineage/edges + DB 查询。与 test_lineage_auto_extract_integration.py
共用同一套 seed helper 形态（每测试 TRUNCATE 清库，无需手动 cleanup）。
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy import select as sql_select

from app.domain.enums import DataSourceType
from app.domain.models import (
    DataLineage,
    DataSource,
    OntologyClass,
    OntologyJoin,
    OntologyMetric,
    SchemaCache,
)

_EXTRACT_URL = "/api/v1/lineage/edges/extract"
_EDGES_URL = "/api/v1/lineage/edges"


async def _seedClass(dbSession, *, name: str, table: str | None = None) -> OntologyClass:
    """在真实 PG 中创建 ontology class（手动 flush 以拿到 id）。"""
    cls = OntologyClass(
        class_name=name,
        source_table=table or name,
        version=1,
    )
    dbSession.add(cls)
    await dbSession.flush()
    return cls


async def _seedJoin(
    dbSession,
    *,
    src: OntologyClass,
    tgt: OntologyClass,
    src_cols: list[str],
    tgt_cols: list[str],
) -> OntologyJoin:
    join = OntologyJoin(
        source_class_id=src.id,
        target_class_id=tgt.id,
        source_columns=src_cols,
        target_columns=tgt_cols,
        join_type="INNER",
        relation_type="business",
        join_key="|".join(f"{s}={t}" for s, t in zip(src_cols, tgt_cols, strict=False)),
    )
    dbSession.add(join)
    await dbSession.flush()
    return join


class TestLineageExtractApi:
    async def test_extract_persists_join_edges_via_api(self, dbSession, client) -> None:
        # Arrange：一个多列 JOIN（无前缀表名 → SOURCE_SYSTEM 层字段级边）
        cls_a = await _seedClass(dbSession, name="TEST_EXTRACT_A", table="TA")
        cls_b = await _seedClass(dbSession, name="TEST_EXTRACT_B", table="TB")
        await _seedJoin(
            dbSession, src=cls_a, tgt=cls_b, src_cols=["K1", "K2"], tgt_cols=["K1", "K2"]
        )
        await dbSession.commit()

        # Act：HTTP 触发自动抽取
        resp = await client.post(_EXTRACT_URL)

        # Assert：created=2 + 已落库可通过 API 读到
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"created": 2}

        listing = (await client.get(_EDGES_URL)).json()
        matching = [e for e in listing if e["sourceObject"] == "TA"]
        assert len(matching) == 2
        assert sorted(e["sourceField"] for e in matching) == ["K1", "K2"]
        for edge in matching:
            assert edge["sourceLayer"] == "SOURCE_SYSTEM"
            assert edge["targetObject"] == "TB"
            assert edge["isActive"] is True

    async def test_extract_is_idempotent(self, dbSession, client) -> None:
        # Arrange
        cls_a = await _seedClass(dbSession, name="TEST_IDEM_A", table="IA")
        cls_b = await _seedClass(dbSession, name="TEST_IDEM_B", table="IB")
        await _seedJoin(dbSession, src=cls_a, tgt=cls_b, src_cols=["K"], tgt_cols=["K"])
        await dbSession.commit()

        # Act 1
        first = await client.post(_EXTRACT_URL)
        assert first.status_code == 200
        assert first.json()["created"] == 1

        # Act 2：同一数据再跑 → 0 新增（幂等）
        second = await client.post(_EXTRACT_URL)
        assert second.status_code == 200
        assert second.json() == {"created": 0}

        # Assert：行数不增
        rows = list(
            (
                await dbSession.execute(
                    sql_select(DataLineage).where(DataLineage.source_object == "IA")
                )
            ).scalars().all()
        )
        assert len(rows) == 1

    async def test_extract_empty_ontology_returns_zero(self, client) -> None:
        resp = await client.post(_EXTRACT_URL)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"created": 0}

    async def test_extract_skips_self_join(self, dbSession, client) -> None:
        """自连接 join（同一 class + 同名列）不产自环退化边 → created=0。"""
        cls_a = await _seedClass(dbSession, name="TEST_SELF_JOIN", table="SA")
        dbSession.add(
            OntologyJoin(
                source_class_id=cls_a.id,
                target_class_id=cls_a.id,  # 自连接
                source_columns=["K"],
                target_columns=["K"],
                join_type="INNER",
                relation_type="business",
                join_key="K=K",
            )
        )
        await dbSession.commit()

        resp = await client.post(_EXTRACT_URL)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"created": 0}

    async def test_extract_metric_formula_to_kpi(self, dbSession, client) -> None:
        # Arrange：一条 metric（formula 引用 ORDER_QTY → KPI 层边）
        cls_porder = await _seedClass(dbSession, name="TEST_METRIC", table="PORDER")
        metric = OntologyMetric(
            metric_name="TOTAL_QTY",
            formula="SUM(t.ORDER_QTY)",
            agg_function="SUM",
            target_class_id=cls_porder.id,
        )
        dbSession.add(metric)
        await dbSession.commit()

        # Act
        resp = await client.post(_EXTRACT_URL)

        # Assert：恰好 1 条 KPI 边，目标 KPI_TOTAL_QTY
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"created": 1}
        listing = (await client.get(_EDGES_URL)).json()
        kpi = [e for e in listing if e["targetLayer"] == "KPI"]
        assert len(kpi) == 1
        assert kpi[0]["sourceObject"] == "PORDER"
        assert kpi[0]["sourceField"] == "ORDER_QTY"
        assert kpi[0]["targetObject"] == "KPI_TOTAL_QTY"
        assert kpi[0]["transformationRule"] == "SUM(t.ORDER_QTY)"

    async def test_extract_schema_ods_cdc_edge(self, dbSession, client) -> None:
        # Arrange：schema introspection 缓存含 ODS_ 物理表 → SYSTEM→ODS 表级 CDC 边
        ds = DataSource(
            name="TEST_CDC_SOURCE",
            type=DataSourceType.ORACLE,
            host="localhost",
            port=1521,
            database_name="svc",
            username="TEST_USER",
            password_encrypted="enc",
        )
        dbSession.add(ds)
        await dbSession.flush()
        cache = SchemaCache(
            datasource_id=ds.id,
            schema_data=[
                {"table_name": "PORDER"},
                {"table_name": "ODS_PORDER"},
                {"table_name": "BPSUPPLIER"},
            ],
            schema_version="v1",
        )
        dbSession.add(cache)
        await dbSession.commit()

        # Act
        resp = await client.post(_EXTRACT_URL)

        # Assert：恰好 1 条 ODS 表级边
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"created": 1}
        listing = (await client.get(_EDGES_URL)).json()
        ods = [e for e in listing if e["targetLayer"] == "ODS"]
        assert len(ods) == 1
        assert ods[0]["sourceLayer"] == "SOURCE_SYSTEM"
        assert ods[0]["sourceObject"] == "PORDER"
        assert ods[0]["sourceField"] is None
        assert ods[0]["targetObject"] == "ODS_PORDER"
        assert ods[0]["transformationRule"] == "CDC 原样接入"

        # Cleanup（per-test TRUNCATE 兜底，双保险与 sibling 测试一致）
        await dbSession.execute(
            delete(DataLineage).where(DataLineage.target_object == "ODS_PORDER")
        )
        await dbSession.commit()
