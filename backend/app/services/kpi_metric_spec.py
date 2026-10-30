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

from app.domain.enums import MetricType
from app.domain.models import KpiCatalog, OntologyMetric
from app.services.kpi_metric_reconcile import (
    GovernanceSpec,
    MeasureSpec,
    MetricTechnique,
)


def governanceSpecOf(kpi: KpiCatalog) -> GovernanceSpec:
    """KPI 治理侧快照：结构化列 + 粒度键（列名按小写比较，集合语义）。"""
    return GovernanceSpec(
        numerator_columns=tuple(kpi.numerator_columns or ()),
        denominator_columns=tuple(kpi.denominator_columns or ()),
        grain_keys=tuple(kpi.grain_keys or ()),
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
    )


__all__ = ["governanceSpecOf", "techniqueOf"]
