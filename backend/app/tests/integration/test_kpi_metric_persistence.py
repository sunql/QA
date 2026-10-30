"""新列落库（真实 PG + service 直调）。

**为什么必须有这组测试**：DTO 扩了字段不等于数据写进去了。
`createMetric` / `createKpi` 是**显式列清单**，漏写一个字段 DB 里就是 NULL，
而对账 / 契约注入 / 发布闸门读到的全是空值 —— 表现为「功能不报错但永远 unknown」。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import (
    MetricAggregationOrder,
    MetricExecutionSource,
    MetricStatus,
    MetricType,
)
from app.domain.models import DataSource, KpiCatalog, OntologyMetric
from app.domain.schemas import (
    KpiCatalogCreate,
    KpiCatalogUpdate,
    OntologyMetricCreate,
    OntologyMetricUpdate,
)
from app.services.kpi_catalog_service import KpiCatalogService
from app.services.ontology_service import OntologyService

ADMIN = CurrentUser(userId="u1", roles=("admin",), departments=("d1",))


async def _addSource(dbSession: AsyncSession, name: str) -> DataSource:
    ds = DataSource(
        name=name,
        type="mysql",
        host="x",
        port=3306,
        database_name="x",
        username="x",
        password_encrypted="x",
        is_active=True,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def test_create_metric_persists_all_new_columns(
    dbSession: AsyncSession,
) -> None:
    dto = OntologyMetricCreate(
        metric_name="落库测试指标",
        formula="SELECT SUM(QTY) FROM T",
        agg_function="SUM",
        metric_type=MetricType.RATIO,
        numerator_measure={
            "agg": "SUM",
            "expr": "QTY",
            "class_id": 12,
            "class_alias": "DWD_INCOMING_INSPECTION",
        },
        denominator_measure={"agg": "SUM", "expr": "AMOUNT"},
        aggregation_order=MetricAggregationOrder.WEIGHTED_RATIO,
        grain_keys=["plant_code", "supplier_code"],
        filter_expression="STATUS = 'OK'",
        status=MetricStatus.DRAFT,
        owner="tech-owner",
        execution_source=MetricExecutionSource.COMPILED,
    )
    metric = await OntologyService().createMetric(dbSession, dto, actor="u1")

    row = await dbSession.get(OntologyMetric, metric.id)
    await dbSession.refresh(row)
    assert row.metric_type == "RATIO"
    assert row.aggregation_order == "WEIGHTED_RATIO"
    assert row.status == "DRAFT"
    assert row.execution_source == "COMPILED"
    assert row.owner == "tech-owner"
    assert row.filter_expression == "STATUS = 'OK'"
    assert row.grain_keys == ["plant_code", "supplier_code"]
    # JSONB 里必须是 snake_case（MeasureSpec.fromRaw 按 snake_case 读）
    assert row.numerator_measure is not None
    assert row.numerator_measure["class_id"] == 12
    assert row.numerator_measure["class_alias"] == "DWD_INCOMING_INSPECTION"
    assert row.denominator_measure is not None
    assert row.denominator_measure["expr"] == "AMOUNT"


async def test_update_metric_normalizes_enums_and_persists(
    dbSession: AsyncSession,
) -> None:
    dto = OntologyMetricCreate(
        metric_name="归一测试指标",
        formula="SELECT SUM(QTY) FROM T",
        agg_function="SUM",
    )
    metric = await OntologyService().createMetric(dbSession, dto, actor="u1")

    update = OntologyMetricUpdate(
        metric_type=MetricType.RATIO,
        aggregation_order=MetricAggregationOrder.AVG_OF_RATIOS,
        status=MetricStatus.PUBLISHED,
        execution_source=MetricExecutionSource.ESCAPE_HATCH,
        numerator_measure={"agg": "SUM", "expr": "QTY"},
        denominator_measure={"agg": "SUM", "expr": "AMOUNT"},
        grain_keys=["plant_code"],
    )
    await OntologyService().updateMetric(dbSession, metric.id, update, actor="u1")

    row = await dbSession.get(OntologyMetric, metric.id)
    await dbSession.refresh(row)
    # 枚举必须落成**字符串**（不是枚举成员）
    assert row.metric_type == "RATIO"
    assert row.aggregation_order == "AVG_OF_RATIOS"
    assert row.status == "PUBLISHED"
    assert row.execution_source == "ESCAPE_HATCH"
    assert row.grain_keys == ["plant_code"]
    assert row.numerator_measure is not None
    assert row.numerator_measure["expr"] == "QTY"


async def test_create_kpi_persists_structured_columns(
    dbSession: AsyncSession,
) -> None:
    ds = await _addSource(dbSession, "PERSIST_DS")

    dto = KpiCatalogCreate(
        kpi_code="PERSIST_KPI",
        kpi_name="落库测试",
        datasource_id=ds.id,
        grain_keys=["plant_code"],
        numerator_columns=["QTY"],
        denominator_columns=["AMOUNT"],
        semantic_keywords=["准时", "交付"],
        match_threshold=0.8,
    )
    kpi = await KpiCatalogService().createKpi(dbSession, dto, ADMIN)

    row = await dbSession.get(KpiCatalog, kpi.id)
    await dbSession.refresh(row)
    assert row.grain_keys == ["plant_code"]
    assert row.numerator_columns == ["QTY"]
    assert row.denominator_columns == ["AMOUNT"]
    assert row.semantic_keywords == ["准时", "交付"]
    assert row.match_threshold == Decimal("0.8")


async def test_update_kpi_persists_structured_columns(
    dbSession: AsyncSession,
) -> None:
    ds = await _addSource(dbSession, "PERSIST_DS2")

    dto = KpiCatalogCreate(
        kpi_code="PERSIST_KPI2",
        kpi_name="落库测试2",
        datasource_id=ds.id,
    )
    kpi = await KpiCatalogService().createKpi(dbSession, dto, ADMIN)

    update = KpiCatalogUpdate(
        semantic_keywords=["比率"],
        match_threshold=0.6,
        grain_keys=["supplier_code"],
        numerator_columns=["QTY2"],
        denominator_columns=["AMOUNT2"],
    )
    await KpiCatalogService().updateKpi(dbSession, kpi.id, update, ADMIN)

    row = await dbSession.get(KpiCatalog, kpi.id)
    await dbSession.refresh(row)
    assert row.semantic_keywords == ["比率"]
    assert row.match_threshold == Decimal("0.6")
    assert row.grain_keys == ["supplier_code"]
    assert row.numerator_columns == ["QTY2"]
    assert row.denominator_columns == ["AMOUNT2"]
