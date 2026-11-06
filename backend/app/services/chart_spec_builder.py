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

from collections.abc import Mapping

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


def aliasByField(
    plan: QueryPlan | None,
    columns: tuple[str, ...],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """「结果集列名 → 中文别名」映射（overrides 最优先，其次 plan 聚合别名）.

    `overrides` 供**无 QueryPlan** 的路径（研究报告）注入标签 —— 否则
    `aliases.get("site_code")` 永远落空，图表轴/图例只能是英文列名。

    plan 映射**保持原语义：后写覆盖**（同一结果列上多条聚合时，最后一条的别名胜出 ——
    chat 侧行为逐字不变）；overrides 只是叠在它上面的一层，不改 plan 侧的赋值语义。
    """
    aliases: dict[str, str] = {}
    if plan is not None:
        for aggregation in plan.aggregations:
            if not aggregation.alias:
                continue
            column = _resolveColumn(aggregation.property, columns)
            if column is not None:
                aliases[column] = aggregation.alias
    return {**aliases, **(overrides or {})}


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


def dimensionLikeFields(signals: ChartSignals) -> frozenset[str]:
    """非指标列（维度/时间/ETL）：只吃注入的 overrides，绝不消费 plan 聚合别名。

    用「全部非指标列」而不是 `_dimensionFields()` 的切片：后者按图型决定画几根轴，
    TABLE 只取前 2 个分组列，第 3 个及以后会漏进 `aliases` 分支被聚合别名改名。
    也不用 `signals.dimensions`：双射折叠会把维度从该元组里删掉，但列仍在结果集里。
    """
    return frozenset(signals.columns) - frozenset(signals.measures)


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


def _buildDimensions(
    fields: tuple[str, ...], aliases: dict[str, str]
) -> tuple[SpecDimension, ...]:
    """维度标签：显示名优先，缺了才退回「下划线换空格」的列名。

    维度标签此前恒为 `_prettify(f)` —— 报告路径（plan=None）注入的显示名若只在
    指标上生效，「图表轴」就仍是英文列名，与注入的意图相反。
    """
    return tuple(
        SpecDimension(field=f, label=aliases.get(f) or _prettify(f)) for f in fields
    )


def _withUnit(label: str, unit: str | None) -> str:
    """拼「标签（单位）」；标签已带同一单位时**不重复拼接**。"""
    if not unit or label.endswith(f"（{unit}）"):
        return label
    return f"{label}（{unit}）"


def _buildMeasures(
    fields: tuple[str, ...],
    aliases: dict[str, str],
    units: Mapping[str, str],
) -> tuple[SpecMeasure, ...]:
    return tuple(
        SpecMeasure(field=f, label=_withUnit(aliases.get(f) or _prettify(f), units.get(f)))
        for f in fields
    )


def _buildColumnLabels(
    columns: tuple[str, ...],
    dimensionLike: frozenset[str],
    aliases: dict[str, str],
    overrides: Mapping[str, str] | None,
    units: Mapping[str, str],
) -> dict[str, str]:
    """只收录「有中文名或有单位」的列；都没有的列缺键 ⇒ 前端回落原始列名。

    维度列只吃注入的 overrides（策略与理由见 `_buildLabels`）。单位拼出的标签
    （`qty（件）`）不是中文名，所以缺名时回落**原始列名**而不是 `_prettify` 后的形态。
    """
    overrideMap = dict(overrides or {})
    labels: dict[str, str] = {}
    for column in columns:
        unit = units.get(column)
        alias = (overrideMap if column in dimensionLike else aliases).get(column)
        if not alias and not unit:
            continue
        labels[column] = _withUnit(alias or column, unit)
    return labels


def _buildLabels(
    signals: ChartSignals,
    kind: ChartType,
    aliases: dict[str, str],
    aliasOverrides: Mapping[str, str] | None,
    units: Mapping[str, str],
) -> tuple[tuple[SpecDimension, ...], dict[str, str]]:
    """维度 + 列显示名（表头/图例）。二者共用一条策略：维度只吃**注入的 overrides**。

    不吃 plan 聚合别名的理由：聚合别名属于**指标**，而它的键是「聚合 property 解析到的
    结果集列」——`COUNT(<维度列>)`（validatePlan 不要求 property 是数值列）会把别名挂到
    维度列上。若维度标签/表头也吃合并映射，chat 图表的轴会被静默改名，文本表还会出现
    两列同名，并误导 `missingFieldHints` 的「该列没有中文名」判断。
    """
    overrideMap = dict(aliasOverrides or {})
    dimensions = _buildDimensions(_dimensionFields(signals, kind), overrideMap)
    labels = _buildColumnLabels(
        signals.columns, dimensionLikeFields(signals), aliases, overrideMap, units
    )
    return dimensions, labels


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
    plan: QueryPlan | None = None,
    *,
    aliasOverrides: Mapping[str, str] | None = None,
    axisLabels: Mapping[str, str] | None = None,
    units: Mapping[str, str] | None = None,
) -> ChartSpec:
    """把决策落到一份完整 spec。纯函数：不改 plan、不改 data、不改 signals。

    `aliasOverrides`（关键字专用，默认 None）是「结果集列名 → 中文名」，由 chat / 研究
    两条链的字段解析层注入；按本特性的契约，它只携带 ontology / business_aliases /
    代码字典来源，**绝不携带 plan 聚合别名**（聚合别名只在内部 `aliasByField` 里参与
    指标标签）。**维度**标签只由它驱动（见 `_buildLabels`）：这是**构造保证**而非巧合 ——
    指标标签沿用既有「plan 聚合别名优先、overrides 覆盖」的合并映射，行为未变。

    `axisLabels`（关键字专用，默认 None）是「维度字段 → 显示名」，只给**渲染器**当轴名
    用（`ChartSpec.axisLabels`）。它不改 `dimensions[].label` —— 后者的来源是
    `aliasOverrides`（解析层中文名）或 `_prettify(field)` 兜底。chat 调用形态不传
    ⇒ 空 map ⇒ 渲染器不额外挂轴名（轴名仍取 `dimensions[].label`）。

    `units`（关键字专用，默认 None）是「结果集列名 → 单位」：指标标签变
    `中文名（单位）`（系列名随标签走），并汇总进 `ChartSpec.columnLabels` 供表头/图例；
    维度列只吃 overrides（同轴标签）。不传 ⇒ 空 map ⇒ 指标标签与既有行为逐字一致。
    """
    kind = decision.kind
    unitsMap = dict(units or {})
    aliases = aliasByField(plan, signals.columns, aliasOverrides)
    dimensions, columnLabels = _buildLabels(signals, kind, aliases, aliasOverrides, unitsMap)
    measures = _buildMeasures(_measureFields(signals, kind), aliases, unitsMap)

    return ChartSpec(
        kind=kind,
        title=_buildTitle(kind, plan),
        columns=signals.columns,
        dimensions=dimensions,
        measures=measures,
        columnLabels=columnLabels,
        series=_buildSeries(kind, dimensions, measures),
        orientation="horizontal" if kind is ChartType.HBAR else "vertical",
        sort=_buildSort(plan, signals.columns),
        kpi=_buildKpi(measures, data) if kind is ChartType.KPI else None,
        axisLabels=dict(axisLabels or {}),
    )
