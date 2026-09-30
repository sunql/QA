"""spec 构建器：决策 + 信号 + 计划 → 标准化 ChartSpec（确定性，不调 LLM）。

**标签从哪来**（约束决定的，不是偏好）：本体实测 `property_alias` 填了 0/3642 行、
`business_aliases` 0 行、`description` 只有 10 行，所以**拿不到中文属性名**。
代码可得的人类可读串只有三处：

1. `plan.aggregations[].alias` —— LLM 在上游已写好的中文别名（如「占比」「总供货量」）
2. `plan.interpretation` —— LLM 已写好的中文概述，直接作标题（免费，不必再问一次）
3. 列名本身（`SUPPLIER_NAME`）—— 兜底，下划线换空格便于阅读

标题兜底复用既有 i18n（`MSG_CHART_TITLE_RESULT` / `MSG_CHART_TITLE_PIE`），
而不是拼「RCV QTY PUU 按 SUPPLIER NAME」这种中英混排 —— 那种标题比通用标题更难看。

**产出必须恒过 `validateSpec`**：构建器的第一职责是产出合法 spec。不合法的后果是
渲染前被降级成表格，等于这次决策白做。所以这里只取 `signals` 里已经过形状校验的
字段，不做任何猜测性引用。
"""

from __future__ import annotations

from app.domain.chart_spec import (
    ChartSpec,
    SpecDimension,
    SpecKpi,
    SpecMeasure,
    SpecSeries,
    SpecSort,
)
from app.domain.enums import ChartType
from app.domain.query_plan import QueryPlan
from app.services.chart_decision import ChartDecision, ChartSignals
from app.services.messages_zh import MSG_CHART_TITLE_PIE, MSG_CHART_TITLE_RESULT

# 标题长度上限：图上的标题超过一行就挤掉绘图区了。
_TITLE_MAX_CHARS = 80

# spec 的维度/指标上限（与 chart_spec.MAX_* 一致；超出的字段会被 validateSpec 拒掉）。
_MAX_DIMENSIONS = 2
_MAX_MEASURES = 2

# 每种图型下 series 的 ECharts series 类型。渲染器按 kind 装配，这里声明语义。
_SERIES_KIND: dict[ChartType, str] = {
    ChartType.LINE: "line",
    ChartType.BAR: "bar",
    ChartType.HBAR: "bar",
    ChartType.PIE: "pie",
    ChartType.DONUT: "pie",
    ChartType.SCATTER: "scatter",
    ChartType.HEATMAP: "heatmap",
    ChartType.WATERFALL: "bar",
    ChartType.COMBO: "bar",
}

_PIE_LIKE = frozenset({ChartType.PIE, ChartType.DONUT})


def _prettify(field: str) -> str:
    """列名 → 可读标签。只把下划线换成空格，不动大小写（保留 RCV 这类缩写）。"""
    return field.replace("_", " ").strip()


def _resolveColumn(name: str | None, columns: tuple[str, ...]) -> str | None:
    """把 plan 的标识符解析成**结果集里的真实列名**（大小写不敏感）。

    plan 用的是本体属性名（`SUPPLIER_CODE`），而结果集列名由适配器统一转小写
    （`business_db_pool.py:639` 的 `c.lower()`）—— 两者指的是同一列，但字符串不等。
    任何**从 plan 抄进 spec 的引用**都必须先过这里：`validateSpec` 按字面比对列名，
    对不上就把整张图降级成表格（2026-09-30 线上：强制饼图变成表格，
    日志 `rule=R_FORCED_CLIENT decision=pie：引用了结果集中不存在的列 'SUPPLIER_CODE'`）。

    解析不出来返回 None：宁可放弃这条引用，也不要留一个指向空列的引用 ——
    后者会让 `validateSpec` 把整张图判死。
    """
    if not name:
        return None
    target = name.strip().lower()
    for column in columns:
        if column.strip().lower() == target:
            return column
    return None


def _aliasByField(plan: QueryPlan | None, columns: tuple[str, ...]) -> dict[str, str]:
    """「结果集列名 → 中文别名」映射（别名优先，缺了才退回列名）。

    键必须是**结果集列名**（`signals.measures` 就是它），故先用 `_resolveColumn`
    把 plan 的属性名折过去 —— 否则 `aliases.get("rcv_qty_puu")` 永远落空，
    「占比」这类中文别名一个都显示不出来。
    """
    if plan is None:
        return {}
    aliases: dict[str, str] = {}
    for aggregation in plan.aggregations:
        if not aggregation.alias:
            continue
        column = _resolveColumn(aggregation.property, columns)
        if column is not None:
            aliases[column] = aggregation.alias
    return aliases


def _measureFields(signals: ChartSignals, kind: ChartType) -> tuple[str, ...]:
    if kind in _PIE_LIKE or kind is ChartType.HEATMAP or kind is ChartType.WATERFALL:
        return signals.measures[:1]
    return signals.measures[:_MAX_MEASURES]


def _axisFields(signals: ChartSignals) -> tuple[str, ...]:
    """类目轴的候选列。

    ⚠️ 时间列**不在** `signals.dimensions` 里（决策引擎把时间维单独拆出来了），
    所以这里按**信号**兜底：一个普通维度都没有时，时间列就是这份数据唯一的轴。

    **为什么兜底不能按 kind 硬编码**：消歧会把 kind 在候选集里换掉
    （问「按月趋势」但分类器答 COMPARE → LINE 变 BAR），换完之后仍是同一份数据、
    同一个轴。若只在 `kind is LINE` 时才去取时间列，换过的 BAR 会带着 0 个维度
    去形状校验，合法图被白降级成表格。
    """
    if signals.dimensions:
        return signals.dimensions
    return signals.timeColumns[:1]


def _dimensionFields(signals: ChartSignals, kind: ChartType) -> tuple[str, ...]:
    """spec 的 dimensions 字段（按 kind 决定要几根轴）。"""
    if kind is ChartType.HEATMAP:
        return signals.dimensions[:_MAX_DIMENSIONS]
    axes = _axisFields(signals)
    if kind in _PIE_LIKE or kind is ChartType.WATERFALL:
        return axes[:1]
    if kind in {ChartType.LINE, ChartType.COMBO} and signals.timeColumns:
        return signals.timeColumns[:1]
    # 笛卡尔系：第一个维度作类目轴；第二个维度由渲染器摊成分组系列。
    return axes[:_MAX_DIMENSIONS]


def _buildDimensions(fields: tuple[str, ...]) -> tuple[SpecDimension, ...]:
    return tuple(SpecDimension(field=f, label=_prettify(f)) for f in fields)


def _buildMeasures(fields: tuple[str, ...], aliases: dict[str, str]) -> tuple[SpecMeasure, ...]:
    return tuple(
        SpecMeasure(field=f, label=aliases.get(f) or _prettify(f)) for f in fields
    )


def _buildSeries(
    kind: ChartType,
    dimensions: tuple[SpecDimension, ...],
    measures: tuple[SpecMeasure, ...],
) -> tuple[SpecSeries, ...]:
    """每条指标一条系列；组合图的第一条柱、第二条线并挂右轴。"""
    if kind in {ChartType.TABLE, ChartType.KPI} or not measures:
        return ()
    # 散点没有「类目轴」的概念：x 是第一个指标、y 是第二个，维度仅作点位标签。
    dimField = dimensions[0].field if dimensions else ""
    if kind is ChartType.SCATTER:
        return (
            SpecSeries(
                name=f"{measures[0].label} × {measures[-1].label}",
                kind="scatter",
                dimension=dimField,
                measure=measures[0].field,
            ),
        )

    defaultKind = _SERIES_KIND.get(kind, "bar")
    series: list[SpecSeries] = []
    for index, measure in enumerate(measures):
        # 组合图：后一条指标必须落在右轴（量级差很大的两个指标共用一根轴读不出来）。
        if kind is ChartType.COMBO:
            seriesKind, axis = ("bar", "left") if index == 0 else ("line", "right")
        else:
            seriesKind, axis = defaultKind, "left"
        series.append(
            SpecSeries(
                name=measure.label,
                kind=seriesKind,
                dimension=dimField,
                measure=measure.field,
                axis=axis,
            )
        )
    return tuple(series)


def _buildKpi(
    measures: tuple[SpecMeasure, ...], data: list[dict]
) -> SpecKpi | None:
    if not measures:
        return None
    field = measures[0].field
    value = data[0].get(field) if data else None
    return SpecKpi(label=measures[0].label, value=value, unit=None, delta=None)


def _buildSort(plan: QueryPlan | None, columns: tuple[str, ...]) -> SpecSort | None:
    """把 plan 的排序规则落到 spec；引用解析不出来就**不带排序**返回 None。

    `by` 用解析后的真实列名（见 `_resolveColumn`）：plan 的 `SUPPLIER_CODE` 与结果集的
    `supplier_code` 是同一列，抄原样会被 `validateSpec` 判成「引用了不存在的列」。
    """
    if plan is None or not plan.sortBy:
        return None
    first = plan.sortBy[0]
    by = _resolveColumn(first.property, columns)
    if by is None:
        return None
    return SpecSort(by=by, order=first.direction.lower() or "desc")


def _buildTitle(kind: ChartType, plan: QueryPlan | None) -> str:
    """标题优先用 plan 已写好的中文概述（上游 LLM 产出，不再多调一次）。"""
    interpretation = (plan.interpretation or "").strip() if plan is not None else ""
    if interpretation:
        return interpretation[:_TITLE_MAX_CHARS]
    return MSG_CHART_TITLE_PIE if kind in _PIE_LIKE else MSG_CHART_TITLE_RESULT


def buildSpec(
    decision: ChartDecision,
    signals: ChartSignals,
    data: list[dict],
    plan: QueryPlan | None,
) -> ChartSpec:
    """把决策落到一份完整 spec。纯函数：不改 plan、不改 data、不改 signals。"""
    kind = decision.kind
    aliases = _aliasByField(plan, signals.columns)
    dimensions = _buildDimensions(_dimensionFields(signals, kind))
    measures = _buildMeasures(_measureFields(signals, kind), aliases)

    return ChartSpec(
        kind=kind,
        title=_buildTitle(kind, plan),
        columns=signals.columns,
        dimensions=dimensions,
        measures=measures,
        series=_buildSeries(kind, dimensions, measures),
        orientation="horizontal" if kind is ChartType.HBAR else "vertical",
        sort=_buildSort(plan, signals.columns),
        kpi=_buildKpi(measures, data) if kind is ChartType.KPI else None,
    )
