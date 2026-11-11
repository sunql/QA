"""从 ORM 行构造**三层对账的输入**（治理侧 / 技术侧）。

⚠️ 为什么不放在 ``kpi_metric_reconcile.py``：后者是**纯函数模块**
（不 import app.models、不做 IO），而本模块要读 ORM 行。

本模块是「治理口径 ⇄ 技术形态」在代码里的**唯一切换点**：
契约注入（Tier B）、发布闸门、真机验证脚本都走这里，
保证三处看到的是同一份构造结果（避免三份同义实现各自漂移）。

**空值契约**：``techniqueOf(None)`` 返回**空技术形态**（``MetricTechnique()``），
调用方一律用它 —— 不要再在外面写 ``techniqueOf(metric) if metric else …``，
否则会重新长出「两套空值语义」的错觉。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import MetricType
from app.domain.models import KpiCatalog, OntologyMetric
from app.services.kpi_alignment_resolver import (
    ResolutionOutcome,
    deriveTimeGrain,
    resolveGovernanceTokens,
    sourcePropertiesForMetric,
)
from app.services.kpi_metric_reconcile import (
    GovernanceSpec,
    MeasureSpec,
    MetricTechnique,
    governanceTokensOf,
)

logger = logging.getLogger(__name__)


def governanceSpecOf(kpi: KpiCatalog) -> GovernanceSpec:
    """KPI 治理侧快照：结构化列 + 粒度键 + 时间粒度（R6 由 grain 文本推导）。"""
    timeGrain = deriveTimeGrain(kpi.grain)
    return GovernanceSpec(
        numerator_columns=tuple(kpi.numerator_columns or ()),
        denominator_columns=tuple(kpi.denominator_columns or ()),
        grain_keys=tuple(kpi.grain_keys or ()),
        time_grain=timeGrain.value if timeGrain is not None else None,
    )


def techniqueOf(metric: OntologyMetric | None) -> MetricTechnique:
    """本体指标技术形态快照；``metric`` 为空 ⇒ 空技术形态（对账判 unknown）。"""
    if metric is None:
        return MetricTechnique()
    return MetricTechnique(
        metric_type=metric.metric_type or MetricType.MEASURE.value,
        numerator=MeasureSpec.fromRaw(metric.numerator_measure),
        denominator=MeasureSpec.fromRaw(metric.denominator_measure),
        aggregation_order=metric.aggregation_order,
        grain_keys=tuple(metric.grain_keys or ()),
        filter_expression=metric.filter_expression,
        formula=metric.formula,
        execution_source=metric.execution_source,
        time_grain=metric.time_grain,
        time_column=metric.time_column,
    )


async def resolutionForMetric(
    session: AsyncSession,
    metricId: int | None,
    governance: GovernanceSpec,
) -> ResolutionOutcome | None:
    """治理 token 别名解析（薄 loader + 纯函数）；失败 ⇒ ``None`` 恒等回退。

    三处接线点（契约构建 / chat Tier 判定 / 诊断）共用本函数。**绝不让解析
    失败变成请求失败**：DB/读异常只记 warning，返回 None 走 ``resolution=None``
    的既有行为（设计 §7.1 的恒等回退，DoD#5）。
    """
    if metricId is None:
        return None
    try:
        properties = await sourcePropertiesForMetric(session, metricId)
        return resolveGovernanceTokens(
            governanceTokensOf(governance), properties
        )
    except Exception:
        logger.warning(
            "KPI 治理 token 别名解析失败，回退恒等对账 metric_id=%s",
            metricId,
            exc_info=True,
        )
        return None


__all__ = ["governanceSpecOf", "resolutionForMetric", "techniqueOf"]
