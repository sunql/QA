"""lineage_extractor 集成测试（真实 PG + 完整链路，Phase 2.2）。

覆盖：
- 在真实 PG 上建 ontology 数据（class + join + metric）
- 调用 extractEdges + 写入 data_lineage
- 再次调用 → 0 新边（幂等）
- 边界：class 缺失 → 跳过；formula 异常 → 跳过
"""

from __future__ import annotations

from sqlalchemy import delete, select as sql_select

from app.domain.enums import DataSourceType, LineageLayer
from app.domain.models import (
    DataLineage,
    DataSource,
    OntologyClass,
    OntologyJoin,
    OntologyMetric,
    SchemaCache,
)
from app.services.lineage_extractor import extractEdges


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
    relation_type: str = "business",
    description: str | None = None,
) -> OntologyJoin:
    """在真实 PG 中创建 ontology join。"""
    join = OntologyJoin(
        source_class_id=src.id,
        target_class_id=tgt.id,
        source_columns=src_cols,
        target_columns=tgt_cols,
        join_type="INNER",
        relation_type=relation_type,
        description=description,
        join_key="|".join(f"{s}={t}" for s, t in zip(src_cols, tgt_cols, strict=False)),
    )
    dbSession.add(join)
    await dbSession.flush()
    return join


async def _seedMetric(
    dbSession,
    *,
    name: str,
    formula: str,
    target_class: OntologyClass,
    agg_function: str = "SUM",
) -> OntologyMetric:
    """在真实 PG 中创建 ontology metric。"""
    metric = OntologyMetric(
        metric_name=name,
        formula=formula,
        agg_function=agg_function,
        target_class_id=target_class.id,
    )
    dbSession.add(metric)
    await dbSession.flush()
    return metric


class TestLineageExtractorIntegration:
    async def test_join_extracts_field_level_edges(self, dbSession, client) -> None:
        # Arrange
        cls_porder = await _seedClass(dbSession, name="TEST_PURCHASE_ORDER", table="PORDER")
        cls_supplier = await _seedClass(dbSession, name="TEST_SUPPLIER", table="BPSUPPLIER")
        await _seedJoin(
            dbSession,
            src=cls_porder,
            tgt=cls_supplier,
            src_cols=["BPSNUM"],
            tgt_cols=["BPSNUM"],
        )
        await dbSession.commit()

        # Act
        edges = await extractEdges(dbSession)

        # Assert
        matching = [
            e
            for e in edges
            if e.source_object == "PORDER"
            and e.target_object == "BPSUPPLIER"
            and e.source_field == "BPSNUM"
        ]
        assert len(matching) >= 1
        edge = matching[0]
        assert edge.source_layer == LineageLayer.SOURCE_SYSTEM
        assert edge.target_layer == LineageLayer.SOURCE_SYSTEM
        assert edge.source_field == "BPSNUM"
        assert edge.target_field == "BPSNUM"

    async def test_metric_extracts_columns_to_kpi_layer(self, dbSession, client) -> None:
        # Arrange
        cls_porder = await _seedClass(dbSession, name="TEST_PORDER_METRIC", table="PORDER")
        await _seedMetric(
            dbSession,
            name="TOTAL_QTY",
            formula="SUM(t.ORDER_QTY)",
            target_class=cls_porder,
        )
        await dbSession.commit()

        # Act
        edges = await extractEdges(dbSession)

        # Assert：至少 1 条 KPI 边
        kpi_edges = [e for e in edges if e.target_layer == LineageLayer.KPI]
        assert len(kpi_edges) >= 1
        edge = kpi_edges[0]
        assert edge.source_object == "PORDER"
        assert edge.source_field == "ORDER_QTY"
        assert edge.target_object == "KPI_TOTAL_QTY"
        assert edge.transformation_rule == "SUM(t.ORDER_QTY)"

    async def test_idempotent_on_second_run(self, dbSession, client) -> None:
        # Arrange
        cls_a = await _seedClass(dbSession, name="TEST_IDEMPOTENT_A", table="TA")
        cls_b = await _seedClass(dbSession, name="TEST_IDEMPOTENT_B", table="TB")
        await _seedJoin(
            dbSession, src=cls_a, tgt=cls_b,
            src_cols=["K"], tgt_cols=["K"],
        )
        await dbSession.commit()

        # Act 1：抽取 + 持久化
        edges1 = await extractEdges(dbSession)
        assert len(edges1) >= 1
        for edge in edges1:
            dbSession.add(
                DataLineage(
                    source_layer=edge.source_layer,
                    source_system=edge.source_system,
                    source_object=edge.source_object,
                    source_field=edge.source_field,
                    target_layer=edge.target_layer,
                    target_system=edge.target_system,
                    target_object=edge.target_object,
                    target_field=edge.target_field,
                    transformation_rule=edge.transformation_rule,
                    refresh_frequency=edge.refresh_frequency,
                    is_active=True,
                )
            )
        await dbSession.commit()

        # Act 2：再次抽取 → 0 条（已有跳过）
        edges2 = await extractEdges(dbSession)
        assert edges2 == []

        # Cleanup
        await dbSession.execute(
            delete(DataLineage).where(DataLineage.source_object == "TA")
        )
        await dbSession.commit()

    async def test_skips_join_with_class_missing_source_table(self, dbSession, client) -> None:
        """class.source_table 为 NULL 的 join 跳过（避免不完整血缘污染）。

        注：FK 约束不允许 join 引用不存在的 class；此处通过让 class.source_table
        为 NULL 来触发 extractor 的兜底跳过逻辑。
        """
        # Arrange：建两个 class，但 target_class.source_table 留空
        cls_a = OntologyClass(class_name="TEST_SKIP_A", source_table="TA", version=1)
        cls_b = OntologyClass(class_name="TEST_SKIP_B", source_table=None, version=1)
        dbSession.add_all([cls_a, cls_b])
        await dbSession.flush()

        join = OntologyJoin(
            source_class_id=cls_a.id,
            target_class_id=cls_b.id,
            source_columns=["K"],
            target_columns=["K"],
            join_type="INNER",
            relation_type="business",
            join_key="K=K",
        )
        dbSession.add(join)
        await dbSession.commit()

        # Act
        edges = await extractEdges(dbSession)

        # Assert：跳过（target.source_table 缺失） → 无对应 TA → TB 边
        matching = [
            e
            for e in edges
            if e.source_object == "TA" and e.target_object is None
        ]
        assert matching == []

    async def test_skips_metric_with_missing_target_class(self, dbSession, client) -> None:
        """Metric 引用了不存在的 target_class：extractor 静默跳过（不抛错）。

        通过创建 metric 时不指定 target_class_id 来模拟（避免 FK 约束）。
        """
        # Arrange
        cls_a = await _seedClass(dbSession, name="TEST_NO_TARGET", table="TA")
        metric = OntologyMetric(
            metric_name="ORPHAN_METRIC",
            formula="SUM(t.QTY)",
            agg_function="SUM",
            target_class_id=None,  # 无 target_class → extractor 跳过
        )
        dbSession.add(metric)
        await dbSession.commit()

        # Act
        edges = await extractEdges(dbSession)

        # Assert：无 KPI 边（target_class_id 为 NULL）
        kpi_edges = [e for e in edges if e.target_layer == LineageLayer.KPI]
        # 可能有其他测试遗留 KPI 边；但 ORPHAN_METRIC 对应的 KPI_TOTAL_QTY 边不存在
        for edge in kpi_edges:
            assert "ORPHAN_METRIC" not in edge.target_object

    async def test_full_pipeline_persist_and_verify(self, dbSession, client) -> None:
        """完整链路：抽取 → 写入 data_lineage → DB 查询验证。"""
        # Arrange
        cls_a = await _seedClass(dbSession, name="TEST_PIPE_A", table="TA")
        cls_b = await _seedClass(dbSession, name="TEST_PIPE_B", table="TB")
        await _seedJoin(
            dbSession, src=cls_a, tgt=cls_b,
            src_cols=["K1", "K2"], tgt_cols=["K1", "K2"],
        )
        await dbSession.commit()

        # Act 1: 抽取
        edges = await extractEdges(dbSession)
        assert len(edges) == 2

        # Act 2: 持久化（模拟脚本行为）
        for edge in edges:
            dbSession.add(
                DataLineage(
                    source_layer=edge.source_layer,
                    source_system=edge.source_system,
                    source_object=edge.source_object,
                    source_field=edge.source_field,
                    target_layer=edge.target_layer,
                    target_system=edge.target_system,
                    target_object=edge.target_object,
                    target_field=edge.target_field,
                    transformation_rule=edge.transformation_rule,
                    refresh_frequency=edge.refresh_frequency,
                    is_active=True,
                )
            )
        await dbSession.commit()

        # Assert: 实际行数 = 2
        stmt = sql_select(DataLineage).where(DataLineage.source_object == "TA")
        rows = list((await dbSession.execute(stmt)).scalars().all())
        assert len(rows) == 2
        fields = sorted([r.source_field for r in rows])
        assert fields == ["K1", "K2"]

        # Cleanup
        await dbSession.execute(delete(DataLineage).where(DataLineage.source_object == "TA"))
        await dbSession.commit()

    async def test_schema_introspection_adds_system_to_ods_edges(
        self, dbSession, client
    ) -> None:
        """计划 Change 2.2 阶段 3：schema cache 有 ODS 物理表 → SYSTEM→ODS 层边（真实 PG）。

        ODS 表是物理 schema 中真实存在的表（如 Sage X3 的 ODS_PORDER），
        extractor 从 schema introspection 缓存识别并补 CDC 层边，不依赖 ontology。
        """
        # Arrange：种子 DataSource（schema_cache 的 FK 依赖）+ schema cache 含 ODS 表
        ds = DataSource(
            name="TEST_SOURCE",
            type=DataSourceType.ORACLE,
            host="localhost",
            port=1521,
            database_name="svc",
            username="TEST_USER",
            password_encrypted="enc",  # 非本测试关注点，任意串即可通过 FK
        )
        dbSession.add(ds)
        await dbSession.flush()

        cache = SchemaCache(
            datasource_id=ds.id,
            schema_data=[
                {"table_name": "PORDER"},
                {"table_name": "ODS_PORDER"},
                {"table_name": "BPSUPPLIER"},  # 非 ODS 表不产边
            ],
            schema_version="v1",
        )
        dbSession.add(cache)
        await dbSession.commit()

        # Act：不种任何 ontology class/join，纯靠 schema introspection
        edges = await extractEdges(dbSession)

        # Assert：恰好 1 条 SYSTEM→ODS 表级边
        ods_edges = [e for e in edges if e.target_layer == LineageLayer.ODS]
        assert len(ods_edges) == 1
        edge = ods_edges[0]
        assert edge.source_layer == LineageLayer.SOURCE_SYSTEM
        assert edge.source_object == "PORDER"
        assert edge.source_field is None  # 表级
        assert edge.target_object == "ODS_PORDER"
        assert edge.target_field is None  # 表级
        assert edge.transformation_rule == "CDC 原样接入"

        # Cleanup
        await dbSession.execute(delete(SchemaCache).where(SchemaCache.datasource_id == ds.id))
        await dbSession.execute(delete(DataSource).where(DataSource.id == ds.id))
        await dbSession.commit()