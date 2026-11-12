"""存量 12 条 KPI 层 2 verdict 回归（P3 D3，真实 PG）。

基线来自 ``.superpowers/sdd/p3-recon.md`` §5（2026-11-11 实测）：**8 pass / 4 unknown**，
4 条 unknown 全部是 ``execution_source=NONE`` 的 KPI，reasons 为
``governance_columns_absent ×2 + governance_grain_absent``。

本测试不读 dev 库，而是用**公开 seed 式夹具**复现 12 行：治理结构化列
（``numerator_columns`` / ``denominator_columns`` / ``grain_keys``）、
``ontology_metric`` 的 measure JSON / formula、以及 ``ontology_metric_source``
源类 + ``ontology_property`` 行（保证 recon 实测的 31/31 治理 token 可解析）。
技术侧 ``time_grain`` 按回填后的等价状态种入（与 ``kpi.grain`` 推导值一致），
因此走 **resolution 接线后** 的真实路径仍应逐条保持基线 verdict。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyMetricSource,
    OntologyProperty,
)
from app.services.kpi_metric_reconcile import (
    REASON_GOVERNANCE_COLUMNS_ABSENT,
    REASON_GOVERNANCE_GRAIN_ABSENT,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    reconcileMetric,
)
from app.services.kpi_metric_spec import (
    governanceSpecOf,
    resolutionForMetric,
    techniqueOf,
)

pytestmark = pytest.mark.asyncio

_EXECUTION_SOURCE_NONE = "NONE"

# recon §5 的 12 行：治理 token、measure expr、类型/来源、聚合阶、grain 文本。
# ``techGrain`` = 回填后 ontology_metric.time_grain 的等价状态（与 grain 文本推导一致）。
_ROWS: tuple[dict[str, object], ...] = (
    {
        "code": "KPI_COST_SAVING",
        "metric": "stock-metric-8-cost-saving",
        "source": "ESCAPE_HATCH",
        "type": "MEASURE",
        "numerator": ("ORDER_QTY", "UNIT_PRICE"),
        "denominator": (),
        "grainKeys": ("SUPPLIER_CODE", "MATERIAL_CODE"),
        "numeratorExpr": "(P.UNIT_PRICE - O.UNIT_PRICE) * O.ORDER_QTY",
        "denominatorExpr": None,
        "aggregationOrder": None,
        "grainText": "供应商+物料+月",
        "techGrain": "MONTH",
        "expected": VERDICT_PASS,
    },
    {
        "code": "KPI_INVOICE_MATCH_RATE",
        "metric": "stock-metric-5-invoice-match-rate",
        "source": "ESCAPE_HATCH",
        "type": "RATIO",
        "numerator": ("LINE_AMOUNT_EXCL_TAX",),
        "denominator": ("AMOUNT_EXCL_TAX",),
        "grainKeys": ("SUPPLIER_CODE", "INVOICE_DATE"),
        "numeratorExpr": "LINE_AMOUNT_EXCL_TAX",
        "denominatorExpr": "AMOUNT_EXCL_TAX",
        "aggregationOrder": "WEIGHTED_RATIO",
        "grainText": "供应商+发票日期+月",
        "techGrain": "MONTH",
        "expected": VERDICT_PASS,
    },
    {
        "code": "KPI_PAYMENT_ON_TIME",
        "metric": "stock-metric-4-payment-on-time",
        "source": "ESCAPE_HATCH",
        "type": "RATIO",
        "numerator": ("VALUE_DATE",),
        "denominator": ("DUE_DATE",),
        "grainKeys": ("SUPPLIER_CODE",),
        "numeratorExpr": "VALUE_DATE",
        "denominatorExpr": "DUE_DATE",
        "aggregationOrder": "WEIGHTED_RATIO",
        "grainText": "供应商+月",
        "techGrain": "MONTH",
        "expected": VERDICT_PASS,
    },
    {
        "code": "KPI_PURCHASE_CYCLE_TIME",
        "metric": "stock-metric-6-purchase-cycle-time",
        "source": "ESCAPE_HATCH",
        "type": "MEASURE",
        "numerator": ("CREATION_DATE", "RECEIPT_DATE"),
        "denominator": (),
        "grainKeys": ("SUPPLIER_CODE", "FACILITY_CODE"),
        "numeratorExpr": "TRUNC(R.RECEIPT_DATE) - TRUNC(L.CREATION_DATE)",
        "denominatorExpr": None,
        "aggregationOrder": None,
        "grainText": "供应商+工厂+月",
        "techGrain": "MONTH",
        "expected": VERDICT_PASS,
    },
    {
        "code": "KPI_PURCHASE_PRICE_VARIANCE",
        "metric": "stock-metric-9-purchase-price-variance",
        "source": "ESCAPE_HATCH",
        "type": "RATIO",
        "numerator": ("UNIT_PRICE",),
        "denominator": ("UNIT_PRICE",),
        "grainKeys": ("MATERIAL_CODE", "FACILITY_CODE"),
        "numeratorExpr": "(O.UNIT_PRICE - P.UNIT_PRICE)",
        "denominatorExpr": "NULLIF(P.UNIT_PRICE, 0)",
        "aggregationOrder": "AVG_OF_RATIOS",
        "grainText": "供应商+物料+月",
        "techGrain": "MONTH",
        "expected": VERDICT_PASS,
    },
    {
        "code": "KPI_SUPPLIER_DEFECT_RATE",
        "metric": "stock-metric-11-supplier-defect-rate",
        "source": _EXECUTION_SOURCE_NONE,
        "type": "RATIO",
        "numerator": (),
        "denominator": (),
        "grainKeys": (),
        "numeratorExpr": None,
        "denominatorExpr": None,
        "aggregationOrder": "WEIGHTED_RATIO",
        "grainText": "供应商+月",
        "techGrain": "MONTH",
        "expected": VERDICT_UNKNOWN,
    },
    {
        "code": "KPI_SUPPLIER_DELAY_DAYS",
        "metric": "stock-metric-7-supplier-delay-days",
        "source": "ESCAPE_HATCH",
        "type": "MEASURE",
        "numerator": ("PROMISED_RECEIPT_DATE", "RECEIPT_DATE"),
        "denominator": (),
        "grainKeys": ("SUPPLIER_CODE", "FACILITY_CODE"),
        "numeratorExpr": "TRUNC(R.RECEIPT_DATE) - TRUNC(O.PROMISED_RECEIPT_DATE)",
        "denominatorExpr": None,
        "aggregationOrder": None,
        "grainText": "供应商+工厂+月",
        "techGrain": "MONTH",
        "expected": VERDICT_PASS,
    },
    {
        "code": "KPI_SUPPLIER_FPY",
        "metric": "stock-metric-12-supplier-fpy",
        "source": _EXECUTION_SOURCE_NONE,
        "type": "RATIO",
        "numerator": (),
        "denominator": (),
        "grainKeys": (),
        "numeratorExpr": None,
        "denominatorExpr": None,
        "aggregationOrder": "WEIGHTED_RATIO",
        "grainText": "供应商+月",
        "techGrain": "MONTH",
        "expected": VERDICT_UNKNOWN,
    },
    {
        "code": "KPI_SUPPLIER_MAINTENANCE_RATIO",
        "metric": "stock-metric-10-supplier-maintenance-ratio",
        "source": _EXECUTION_SOURCE_NONE,
        "type": "RATIO",
        "numerator": (),
        "denominator": (),
        "grainKeys": (),
        "numeratorExpr": None,
        "denominatorExpr": None,
        "aggregationOrder": "WEIGHTED_RATIO",
        "grainText": "供应商（快照）",
        "techGrain": None,
        "expected": VERDICT_UNKNOWN,
    },
    {
        "code": "KPI_SUPPLIER_NCR_RATE",
        "metric": "stock-metric-13-supplier-ncr-rate",
        "source": _EXECUTION_SOURCE_NONE,
        "type": "RATIO",
        "numerator": (),
        "denominator": (),
        "grainKeys": (),
        "numeratorExpr": None,
        "denominatorExpr": None,
        "aggregationOrder": "WEIGHTED_RATIO",
        "grainText": "供应商+月",
        "techGrain": "MONTH",
        "expected": VERDICT_UNKNOWN,
    },
    {
        "code": "KPI_SUPPLIER_OTD",
        "metric": "stock-metric-2-supplier-otd",
        "source": "ESCAPE_HATCH",
        "type": "RATIO",
        "numerator": ("RECEIPT_DATE",),
        "denominator": ("PROMISED_RECEIPT_DATE",),
        "grainKeys": ("SUPPLIER_CODE", "FACILITY_CODE"),
        "numeratorExpr": "RECEIPT_DATE",
        "denominatorExpr": "PROMISED_RECEIPT_DATE",
        "aggregationOrder": "WEIGHTED_RATIO",
        "grainText": "供应商+工厂+月",
        "techGrain": "MONTH",
        "expected": VERDICT_PASS,
    },
    {
        "code": "KPI_SUPPLIER_OVERDUE_RATIO",
        "metric": "stock-metric-3-supplier-overdue-ratio",
        "source": "ESCAPE_HATCH",
        "type": "RATIO",
        "numerator": ("RECEIPT_DATE",),
        "denominator": ("PROMISED_RECEIPT_DATE",),
        "grainKeys": ("SUPPLIER_CODE", "FACILITY_CODE"),
        "numeratorExpr": "RECEIPT_DATE",
        "denominatorExpr": "PROMISED_RECEIPT_DATE",
        "aggregationOrder": "WEIGHTED_RATIO",
        "grainText": "供应商+工厂+月",
        "techGrain": "MONTH",
        "expected": VERDICT_PASS,
    },
)


def _formulaOf(row: dict[str, object]) -> str:
    exprs = [
        expr for expr in (row["numeratorExpr"], row["denominatorExpr"]) if isinstance(expr, str) and expr
    ]
    return "SELECT " + ", ".join(exprs) + " FROM SRC_TABLE" if exprs else "SELECT 1 FROM SRC_TABLE"


def _measureOf(expr: object) -> dict[str, object] | None:
    if not isinstance(expr, str) or not expr:
        return None
    return {"agg": "SUM", "expr": expr}


def _dataTypeOf(token: str) -> str:
    if token.endswith("_DATE") or token.endswith("_TS"):
        return "DATE"
    return "VARCHAR"


async def _seedRow(session: AsyncSession, row: dict[str, object]) -> None:
    metric = OntologyMetric(
        metric_name=str(row["metric"]),
        formula=_formulaOf(row),
        metric_type=str(row["type"]),
        numerator_measure=_measureOf(row["numeratorExpr"]),
        denominator_measure=_measureOf(row["denominatorExpr"]),
        aggregation_order=row["aggregationOrder"],  # type: ignore[arg-type]
        grain_keys=list(row["grainKeys"]),  # type: ignore[arg-type]
        execution_source=str(row["source"]),
        time_grain=row["techGrain"],  # type: ignore[arg-type]
    )
    session.add(metric)
    await session.flush()

    kpi = KpiCatalog(
        kpi_code=str(row["code"]),
        kpi_name=str(row["code"]),
        status="PUBLISHED",
        metric_id=metric.id,
        numerator_columns=list(row["numerator"]),  # type: ignore[arg-type]
        denominator_columns=list(row["denominator"]),  # type: ignore[arg-type]
        grain_keys=list(row["grainKeys"]),  # type: ignore[arg-type]
        grain=str(row["grainText"]),
        created_by="stock-verdict-test",
    )
    session.add(kpi)

    if str(row["source"]) == _EXECUTION_SOURCE_NONE:
        return

    groups: list[tuple[str, set[str]]] = []
    numeratorTokens = set(row["numerator"]) | set(row["grainKeys"])  # type: ignore[arg-type]
    if numeratorTokens:
        groups.append(("NUMERATOR", numeratorTokens))
    denominatorTokens = set(row["denominator"])  # type: ignore[arg-type]
    if denominatorTokens:
        groups.append(("DENOMINATOR", denominatorTokens))

    for role, tokens in groups:
        ontologyClass = OntologyClass(
            class_name=f"{row['metric']}-{role}",
            version=1,
            created_by="stock-verdict-test",
        )
        session.add(ontologyClass)
        await session.flush()
        for token in sorted(tokens):
            session.add(
                OntologyProperty(
                    class_id=ontologyClass.id,
                    property_name=token,
                    source_column=token,
                    data_type=_dataTypeOf(token),
                )
            )
        session.add(
            OntologyMetricSource(
                metric_id=metric.id,
                class_id=ontologyClass.id,
                role=role,
            )
        )


async def test_stock_12_kpi_layer2_verdict_matches_baseline(
    dbSession: AsyncSession,
) -> None:
    """resolution 接线后，12 条 KPI 的层 2 verdict 逐条保持 recon §5 基线。"""
    for row in _ROWS:
        await _seedRow(dbSession, row)
    await dbSession.commit()

    failures: list[str] = []
    for row in _ROWS:
        kpi = await _loadKpi(dbSession, str(row["code"]))
        metric = await _loadMetric(dbSession, kpi.metric_id)
        governance = governanceSpecOf(kpi)
        resolution = await resolutionForMetric(dbSession, metric.id, governance)
        result = reconcileMetric(governance, techniqueOf(metric), resolution=resolution)

        expected = row["expected"]
        if result.verdict != expected:
            failures.append(
                f"{row['code']}: expected {expected} got {result.verdict} reasons={result.reasons()}"
            )
            continue
        if expected == VERDICT_PASS:
            if result.reasons():
                failures.append(f"{row['code']}: pass 但仍有 findings={result.reasons()}")
            if result.degraded:
                failures.append(f"{row['code']}: pass 但被标记 degraded")
        else:
            reasons = result.reasons()
            if reasons.count(REASON_GOVERNANCE_COLUMNS_ABSENT) != 2:
                failures.append(f"{row['code']}: 期望 governance_columns_absent×2，实际 {reasons}")
            if REASON_GOVERNANCE_GRAIN_ABSENT not in reasons:
                failures.append(f"{row['code']}: 缺少 governance_grain_absent，实际 {reasons}")
            if not result.degraded:
                failures.append(f"{row['code']}: unknown 应标注 degraded")

    assert not failures, "\n".join(failures)


async def _loadKpi(session: AsyncSession, kpiCode: str) -> KpiCatalog:
    from sqlalchemy import select

    row = await session.execute(select(KpiCatalog).where(KpiCatalog.kpi_code == kpiCode))
    return row.scalar_one()


async def _loadMetric(session: AsyncSession, metricId: int | None) -> OntologyMetric:
    assert metricId is not None
    metric = await session.get(OntologyMetric, metricId)
    assert metric is not None
    return metric
