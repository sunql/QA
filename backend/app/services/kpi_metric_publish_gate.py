"""KPI / 本体指标发布闸门（P1，spec §7.5）。

两个方向：

- **KPI**（``kpi_catalog.status`` DRAFT → PUBLISHED）：5 项硬条件
- **本体指标**（``ontology_metric.status`` DRAFT → PUBLISHED）：结构化齐全 +
  技术形态未降级 + 关联 KPI 无 mismatch

**开关** ``KPI_METRIC_GATE_ENABLED``（默认 false）—— 数据没补齐前只观察不阻断：
关闭时记录 warning 并放行；开启时不通过抛 ``ConflictError``（⇒ HTTP 409）。

**打破循环依赖**：本体指标方向**不要求**治理侧对账 pass。治理侧字段在 KPI 上，
若指标发布也要求治理 pass，则「KPI 要指标已发布 / 指标要 KPI 已发布」互锁。
指标方向只校验技术侧自洽（层 1 / 层 3 不 mismatch）+ 关联 KPI **不得 mismatch**
（unknown 放行）。

**不可变约定**：本模块只构造新对象（frozen dataclass / tuple），不修改入参
（KPI / 指标实体、治理快照、查询结果）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MetricExecutionSource, MetricType
from app.domain.exceptions import ConflictError
from app.domain.models import KpiCatalog, OntologyMetric
from app.services.kpi_metric_reconcile import (
    _RATIO_AGGREGATION_ORDERS,
    _TYPES_REQUIRING_DENOMINATOR,
    VERDICT_MISMATCH,
    VERDICT_PASS,
    GovernanceSpec,
    MeasureSpec,
    MetricTechnique,
    reconcileExecutableVsStructured,
    reconcileGovernanceVsTechnique,
    reconcileTechniqueInternal,
)
from app.services.kpi_metric_switches import SWITCH_GATE, readKpiMetricSwitch
from app.services.messages_zh import (
    MSG_KPI_GATE_BLOCKED,
    MSG_KPI_GATE_METRIC_DEGRADED,
    MSG_KPI_GATE_METRIC_LINKED_MISMATCH,
    MSG_KPI_GATE_METRIC_NO_AGGREGATION_ORDER,
    MSG_KPI_GATE_METRIC_NO_DENOMINATOR,
    MSG_KPI_GATE_METRIC_NO_GRAIN,
    MSG_KPI_GATE_METRIC_NO_NUMERATOR,
    MSG_KPI_GATE_METRIC_NO_TYPE,
    MSG_KPI_GATE_METRIC_NOT_FOUND,
    MSG_KPI_GATE_METRIC_NOT_PUBLISHED,
    MSG_KPI_GATE_MISSING_METRIC,
    MSG_KPI_GATE_NO_DATASOURCE,
    MSG_KPI_GATE_NO_EXECUTION_SOURCE,
    MSG_KPI_GATE_RECONCILE_MISMATCH,
    MSG_KPI_GATE_RECONCILE_UNKNOWN,
)

logger = logging.getLogger(__name__)

ENTITY_TYPE_KPI = "kpi_catalog"
ENTITY_TYPE_METRIC = "ontology_metric"

_PUBLISHED = "PUBLISHED"


class _Unset:
    """哨兵：区分「未覆盖（沿用实体现值）」与「显式 None（清空挂靠）」。

    KPI 的 ``metric_id`` / ``datasource_id`` 都可被显式置空（解绑）。
    若把「None」一律当成「未覆盖」，则「解绑 + 发布」同一次请求会被判成
    「仍挂着指标」⇒ 闸门漏放行。故覆盖参数用哨兵表达「未覆盖」。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<UNSET>"


UNSET = _Unset()


@dataclass(frozen=True)
class GateItem:
    """一条闸门检查项。"""

    code: str
    message: str
    ok: bool


@dataclass(frozen=True)
class GateResult:
    """闸门结果（不可变）。

    ``entityId`` 在 create 路径上可能是 None（闸门在 flush 之前触发，
    主键尚未分配）。
    """

    entityType: str
    entityId: int | None
    items: tuple[GateItem, ...]

    @property
    def passed(self) -> bool:
        return all(item.ok for item in self.items)

    @property
    def failedItems(self) -> tuple[GateItem, ...]:
        return tuple(item for item in self.items if not item.ok)

    def message(self) -> str:
        return MSG_KPI_GATE_BLOCKED.format(
            items="；".join(item.message for item in self.failedItems)
        )


def buildKpiGateItems(
    *,
    metricId: int | None,
    metricStatus: str | None,
    executionSource: str | None,
    datasourceId: int | None,
    reconcileVerdict: str,
    reconcileDetail: str,
) -> tuple[GateItem, ...]:
    """KPI 发布闸门 5 项（spec §7.5）。纯函数，无 IO。"""
    hasExecution = bool(executionSource) and (
        executionSource != MetricExecutionSource.NONE.value
    )
    if reconcileVerdict == VERDICT_MISMATCH:
        reconcileMessage = MSG_KPI_GATE_RECONCILE_MISMATCH.format(
            detail=reconcileDetail or "口径不一致"
        )
    else:
        reconcileMessage = MSG_KPI_GATE_RECONCILE_UNKNOWN
    return (
        GateItem("metric_linked", MSG_KPI_GATE_MISSING_METRIC, metricId is not None),
        GateItem(
            "metric_published",
            MSG_KPI_GATE_METRIC_NOT_FOUND
            if metricStatus is None
            else MSG_KPI_GATE_METRIC_NOT_PUBLISHED,
            metricStatus == _PUBLISHED,
        ),
        GateItem("execution_source", MSG_KPI_GATE_NO_EXECUTION_SOURCE, hasExecution),
        GateItem(
            "reconcile",
            reconcileMessage,
            reconcileVerdict == VERDICT_PASS,
        ),
        GateItem("datasource", MSG_KPI_GATE_NO_DATASOURCE, datasourceId is not None),
    )


def buildMetricGateItems(
    *,
    metricType: str | None,
    numeratorMeasure: object | None,
    denominatorMeasure: object | None,
    aggregationOrder: str | None,
    grainKeys: tuple[str, ...],
    executionSource: str | None,
    degraded: bool,
    linkedMismatch: tuple[str, ...],
) -> tuple[GateItem, ...]:
    """本体指标发布闸门。纯函数，无 IO。"""
    metricTypeValue = (metricType or "").strip().upper()
    needsDenominator = metricTypeValue in _TYPES_REQUIRING_DENOMINATOR
    order = (aggregationOrder or "").strip().upper()
    hasExecution = bool(executionSource) and (
        executionSource != MetricExecutionSource.NONE.value
    )
    items: tuple[GateItem, ...] = (
        GateItem("metric_type", MSG_KPI_GATE_METRIC_NO_TYPE, bool(metricTypeValue)),
        GateItem(
            "numerator",
            MSG_KPI_GATE_METRIC_NO_NUMERATOR,
            numeratorMeasure is not None,
        ),
        GateItem(
            "execution_source",
            MSG_KPI_GATE_NO_EXECUTION_SOURCE,
            hasExecution,
        ),
        GateItem("grain", MSG_KPI_GATE_METRIC_NO_GRAIN, bool(grainKeys)),
    )
    if needsDenominator:
        items = items + (
            GateItem(
                "denominator",
                MSG_KPI_GATE_METRIC_NO_DENOMINATOR,
                denominatorMeasure is not None,
            ),
            GateItem(
                "aggregation_order",
                MSG_KPI_GATE_METRIC_NO_AGGREGATION_ORDER,
                order in _RATIO_AGGREGATION_ORDERS,
            ),
        )
    return items + (
        GateItem("degraded", MSG_KPI_GATE_METRIC_DEGRADED, not degraded),
        GateItem(
            "linked_mismatch",
            MSG_KPI_GATE_METRIC_LINKED_MISMATCH.format(
                items="；".join(linkedMismatch)
            ),
            not linkedMismatch,
        ),
    )


def _techniqueOf(metric: OntologyMetric) -> MetricTechnique:
    """指标实体 → 技术形态快照（只读，不改实体）。"""
    return MetricTechnique(
        metric_type=metric.metric_type or MetricType.MEASURE.value,
        numerator=MeasureSpec.fromRaw(metric.numerator_measure),
        denominator=MeasureSpec.fromRaw(metric.denominator_measure),
        aggregation_order=metric.aggregation_order,
        grain_keys=tuple(metric.grain_keys or ()),
        filter_expression=metric.filter_expression,
        formula=metric.formula,
        execution_source=metric.execution_source,
    )


def _governanceOf(kpi: KpiCatalog) -> GovernanceSpec:
    """KPI 实体 → 治理侧快照（只读，不改实体）。"""
    return GovernanceSpec(
        numerator_columns=tuple(kpi.numerator_columns or ()),
        denominator_columns=tuple(kpi.denominator_columns or ()),
        grain_keys=tuple(kpi.grain_keys or ()),
    )


async def _loadLinkedKpis(
    session: AsyncSession, metricId: int | None
) -> tuple[KpiCatalog, ...]:
    """查挂到该指标上的 KPI。metricId 为 None（未 flush）⇒ 无从查起。"""
    if metricId is None:
        return ()
    result = await session.execute(
        select(KpiCatalog).where(KpiCatalog.metric_id == metricId)
    )
    return tuple(result.scalars().all())


def _linkedMismatchOf(
    linked: tuple[KpiCatalog, ...], technique: MetricTechnique
) -> tuple[str, ...]:
    """关联 KPI 的层 2 对账：**只拦 mismatch**（unknown 放行，打破互锁）。"""
    mismatches: list[str] = []
    for kpi in linked:
        layer2 = reconcileGovernanceVsTechnique(_governanceOf(kpi), technique)
        if layer2.verdict == VERDICT_MISMATCH:
            mismatches.append(f"{kpi.kpi_code}: {'；'.join(layer2.reasons())}")
    return tuple(mismatches)


async def checkMetricPublishGate(
    session: AsyncSession,
    metric: OntologyMetric,
    *,
    technique: MetricTechnique | None = None,
) -> GateResult:
    """本体指标发布闸门（只读检查）。"""
    tech = technique if technique is not None else _techniqueOf(metric)
    layer1 = reconcileTechniqueInternal(tech)
    layer3 = reconcileExecutableVsStructured(tech)
    degraded = (
        layer1.verdict == VERDICT_MISMATCH or layer3.verdict == VERDICT_MISMATCH
    )
    linked = await _loadLinkedKpis(session, metric.id)
    items = buildMetricGateItems(
        metricType=tech.metric_type,
        numeratorMeasure=tech.numerator,
        denominatorMeasure=tech.denominator,
        aggregationOrder=tech.aggregation_order,
        grainKeys=tech.grain_keys,
        executionSource=tech.execution_source,
        degraded=degraded,
        linkedMismatch=_linkedMismatchOf(linked, tech),
    )
    return GateResult(
        entityType=ENTITY_TYPE_METRIC, entityId=metric.id, items=items
    )


async def checkKpiPublishGate(
    session: AsyncSession,
    kpi: KpiCatalog,
    *,
    metricId: int | None | _Unset = UNSET,
    datasourceId: int | None | _Unset = UNSET,
    governance: GovernanceSpec | None = None,
) -> GateResult:
    """KPI 发布闸门（只读检查）。

    metricId / datasourceId / governance 允许覆盖（同一请求里既改挂靠又发布时，
    要按**新值**判定，而不是旧值）。未覆盖（默认 ``UNSET``）时沿用实体现值；
    显式传 ``None`` 表示「本次要把该挂靠清空」⇒ 按「无挂靠」判定。
    """
    effectiveMetricId = (
        kpi.metric_id if isinstance(metricId, _Unset) else metricId
    )
    effectiveDatasourceId = (
        kpi.datasource_id if isinstance(datasourceId, _Unset) else datasourceId
    )
    metric: OntologyMetric | None = None
    if effectiveMetricId is not None:
        metric = await session.get(OntologyMetric, effectiveMetricId)
    technique = _techniqueOf(metric) if metric is not None else MetricTechnique()
    effectiveGovernance = (
        governance if governance is not None else _governanceOf(kpi)
    )
    layer2 = reconcileGovernanceVsTechnique(effectiveGovernance, technique)
    detail = "；".join(layer2.reasons())
    items = buildKpiGateItems(
        metricId=effectiveMetricId,
        metricStatus=metric.status if metric is not None else None,
        executionSource=metric.execution_source if metric is not None else None,
        datasourceId=effectiveDatasourceId,
        reconcileVerdict=layer2.verdict,
        reconcileDetail=detail,
    )
    return GateResult(entityType=ENTITY_TYPE_KPI, entityId=kpi.id, items=items)


async def _gateEnabled(session: AsyncSession) -> bool:
    return await readKpiMetricSwitch(session, SWITCH_GATE, False)


async def enforceKpiPublishGate(
    session: AsyncSession,
    kpi: KpiCatalog,
    *,
    metricId: int | None | _Unset = UNSET,
    datasourceId: int | None | _Unset = UNSET,
    governance: GovernanceSpec | None = None,
) -> GateResult:
    """KPI 发布闸门强制执行。开关关闭 ⇒ 只 warning 放行；开启且不通过 ⇒ 409。"""
    result = await checkKpiPublishGate(
        session,
        kpi,
        metricId=metricId,
        datasourceId=datasourceId,
        governance=governance,
    )
    if result.passed:
        return result
    if not await _gateEnabled(session):
        logger.warning(
            "KPI 发布闸门未通过（开关关闭，仅观察）id=%s code=%s items=%s",
            kpi.id,
            kpi.kpi_code,
            [item.code for item in result.failedItems],
        )
        return result
    raise ConflictError(result.message())


async def enforceMetricPublishGate(
    session: AsyncSession,
    metric: OntologyMetric,
    *,
    technique: MetricTechnique | None = None,
) -> GateResult:
    """本体指标发布闸门强制执行（同上：开关关闭只观察）。"""
    result = await checkMetricPublishGate(session, metric, technique=technique)
    if result.passed:
        return result
    if not await _gateEnabled(session):
        logger.warning(
            "本体指标发布闸门未通过（开关关闭，仅观察）id=%s items=%s",
            metric.id,
            [item.code for item in result.failedItems],
        )
        return result
    raise ConflictError(result.message())
