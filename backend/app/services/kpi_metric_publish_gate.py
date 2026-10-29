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
    MetricTechnique,
    ReconcileFinding,
    reconcileExecutableVsStructured,
    reconcileGovernanceVsTechnique,
    reconcileTechniqueInternal,
)
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf
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
    MSG_KPI_GATE_RECONCILE_PASS,
    MSG_KPI_GATE_RECONCILE_UNKNOWN,
    MSG_KPI_RECONCILE_FINDING,
    MSG_KPI_RECONCILE_FINDING_NO_DETAIL,
    MSG_KPI_RECONCILE_LAYER_NAMES,
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

    ``reasonCodes`` 是对账 finding 的英文机器码（``grain_mismatch`` 等）：
    **不进用户正文**，只供 ``enforce*`` 的 warn 日志 grep。
    """

    entityType: str
    entityId: int | None
    items: tuple[GateItem, ...]
    reasonCodes: tuple[str, ...] = ()

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


def renderReconcileFindings(findings: tuple[ReconcileFinding, ...]) -> str:
    """对账 findings → 用户可见中文正文（层名映射 + finding 的中文 detail）。

    英文 reason 码（``grain_mismatch`` 等）不进正文，只由 ``enforce*`` 写 warn 日志。
    未知层值原样展示，不静默丢层次信息。
    """
    if not findings:
        return MSG_KPI_RECONCILE_FINDING_NO_DETAIL
    return "；".join(
        MSG_KPI_RECONCILE_FINDING.format(
            layer=MSG_KPI_RECONCILE_LAYER_NAMES.get(finding.layer, finding.layer),
            detail=finding.detail or MSG_KPI_RECONCILE_FINDING_NO_DETAIL,
        )
        for finding in findings
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
    detail = reconcileDetail or MSG_KPI_RECONCILE_FINDING_NO_DETAIL
    if reconcileVerdict == VERDICT_PASS:
        # Q7：pass 时不得复用「未完成（unknown）」文案——闸门通过项写着
        # 「口径对账未完成」会让运维误判为仍有缺口。
        reconcileMessage = MSG_KPI_GATE_RECONCILE_PASS
    elif reconcileVerdict == VERDICT_MISMATCH:
        reconcileMessage = MSG_KPI_GATE_RECONCILE_MISMATCH.format(detail=detail)
    else:
        reconcileMessage = MSG_KPI_GATE_RECONCILE_UNKNOWN.format(detail=detail)
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


def _baseMetricItems(
    *,
    metricType: str,
    numeratorMeasure: object | None,
    executionSource: str | None,
    grainKeys: tuple[str, ...],
) -> tuple[GateItem, ...]:
    """本体指标自身的基础 4 项。"""
    hasExecution = bool(executionSource) and (
        executionSource != MetricExecutionSource.NONE.value
    )
    return (
        GateItem("metric_type", MSG_KPI_GATE_METRIC_NO_TYPE, bool(metricType)),
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


def _denominatorItems(
    *,
    metricType: str,
    denominatorMeasure: object | None,
    aggregationOrder: str | None,
) -> tuple[GateItem, ...]:
    """分母 + 比率聚合阶。

    RATIO 与 DIFFERENCE 都需要分母（DIFFERENCE 的分母语义是减数）；
    **只有 RATIO** 需要合法的比率聚合阶（差值型没有辛普森悖论，
    ``MetricAggregationOrder.NONE`` 即为非比率型指标的合法值）。
    """
    if metricType not in _TYPES_REQUIRING_DENOMINATOR:
        return ()
    items = (
        GateItem(
            "denominator",
            MSG_KPI_GATE_METRIC_NO_DENOMINATOR,
            denominatorMeasure is not None,
        ),
    )
    if metricType != MetricType.RATIO.value:
        return items
    order = (aggregationOrder or "").strip().upper()
    return items + (
        GateItem(
            "aggregation_order",
            MSG_KPI_GATE_METRIC_NO_AGGREGATION_ORDER,
            order in _RATIO_AGGREGATION_ORDERS,
        ),
    )


def _qualityItems(
    *, degraded: bool, linkedMismatch: tuple[str, ...]
) -> tuple[GateItem, ...]:
    """技术形态降级 + 关联 KPI 对账 2 项。"""
    return (
        GateItem("degraded", MSG_KPI_GATE_METRIC_DEGRADED, not degraded),
        GateItem(
            "linked_mismatch",
            MSG_KPI_GATE_METRIC_LINKED_MISMATCH.format(
                items="；".join(linkedMismatch)
            ),
            not linkedMismatch,
        ),
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
    return (
        _baseMetricItems(
            metricType=metricTypeValue,
            numeratorMeasure=numeratorMeasure,
            executionSource=executionSource,
            grainKeys=grainKeys,
        )
        + _denominatorItems(
            metricType=metricTypeValue,
            denominatorMeasure=denominatorMeasure,
            aggregationOrder=aggregationOrder,
        )
        + _qualityItems(degraded=degraded, linkedMismatch=linkedMismatch)
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


@dataclass(frozen=True)
class _LinkedMismatch:
    """一个关联 KPI 的层 2 mismatch：中文正文 + 英文 reason 码分离。"""

    message: str
    reasonCodes: tuple[str, ...]


def _linkedMismatchOf(
    linked: tuple[KpiCatalog, ...], technique: MetricTechnique
) -> tuple[_LinkedMismatch, ...]:
    """关联 KPI 的层 2 对账：**只拦 mismatch**（unknown 放行，打破互锁）。"""
    mismatches: list[_LinkedMismatch] = []
    for kpi in linked:
        layer2 = reconcileGovernanceVsTechnique(governanceSpecOf(kpi), technique)
        # 只有 mismatch 计入（unknown 放行；反向实验把它改成 != VERDICT_PASS 时
        # ⇒ 用例 test_metric_publish_allowed_when_linked_kpi_governance_unknown 必须变红）
        if layer2.verdict == VERDICT_MISMATCH:
            mismatches.append(
                _LinkedMismatch(
                    message=(
                        f"{kpi.kpi_code}: "
                        f"{renderReconcileFindings(layer2.findings)}"
                    ),
                    reasonCodes=layer2.reasons(),
                )
            )
    return tuple(mismatches)


async def checkMetricPublishGate(
    session: AsyncSession,
    metric: OntologyMetric,
    *,
    technique: MetricTechnique | None = None,
) -> GateResult:
    """本体指标发布闸门（只读检查）。"""
    # 统一用 techniqueOf() 的空值语义（None ⇒ 空技术形态），不再有两套判断。
    tech = technique if technique is not None else techniqueOf(metric)
    layer1 = reconcileTechniqueInternal(tech)
    layer3 = reconcileExecutableVsStructured(tech)
    degraded = (
        layer1.verdict == VERDICT_MISMATCH or layer3.verdict == VERDICT_MISMATCH
    )
    linkedFindings = _linkedMismatchOf(await _loadLinkedKpis(session, metric.id), tech)
    items = buildMetricGateItems(
        metricType=tech.metric_type,
        numeratorMeasure=tech.numerator,
        denominatorMeasure=tech.denominator,
        aggregationOrder=tech.aggregation_order,
        grainKeys=tech.grain_keys,
        executionSource=tech.execution_source,
        degraded=degraded,
        linkedMismatch=tuple(f.message for f in linkedFindings),
    )
    reasonCodes = (
        layer1.reasons()
        + layer3.reasons()
        + tuple(code for f in linkedFindings for code in f.reasonCodes)
    )
    return GateResult(
        entityType=ENTITY_TYPE_METRIC,
        entityId=metric.id,
        items=items,
        reasonCodes=reasonCodes,
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
    technique = techniqueOf(metric)
    effectiveGovernance = (
        governance if governance is not None else governanceSpecOf(kpi)
    )
    layer2 = reconcileGovernanceVsTechnique(effectiveGovernance, technique)
    items = buildKpiGateItems(
        metricId=effectiveMetricId,
        metricStatus=metric.status if metric is not None else None,
        executionSource=metric.execution_source if metric is not None else None,
        datasourceId=effectiveDatasourceId,
        reconcileVerdict=layer2.verdict,
        reconcileDetail=renderReconcileFindings(layer2.findings),
    )
    return GateResult(
        entityType=ENTITY_TYPE_KPI,
        entityId=kpi.id,
        items=items,
        reasonCodes=layer2.reasons(),
    )


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
            "KPI 发布闸门未通过（开关关闭，仅观察）id=%s code=%s items=%s reasons=%s",
            kpi.id,
            kpi.kpi_code,
            [item.code for item in result.failedItems],
            list(result.reasonCodes),
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
            "本体指标发布闸门未通过（开关关闭，仅观察）id=%s items=%s reasons=%s",
            metric.id,
            [item.code for item in result.failedItems],
            list(result.reasonCodes),
        )
        return result
    raise ConflictError(result.message())
