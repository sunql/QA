"""渲染器：ChartSpec + 结果数据 → ECharts option（或表格/KPI 负载）。

**为什么由代码渲染，而不是让 LLM 写 option**：LLM 写 option 是「图能不能出来」的
唯一失败面 —— 写错模板变量会显示字面量 `{d}`（用户报过）、引用不存在的字段会渲染空白、
写出非法 JSON 会直接 503。这些工作全部是确定性的映射，交给 LLM 只会拿到不确定性。

**服务端只发结构，不发颜色**（决策 6）：本模块产出的 option 里没有任何 `#hex` /
`rgb()` / 调色板数组，颜色由前端主题层注入。唯一例外是瀑布图的透明占位系列 ——
`"transparent"` 是让堆叠基线不可见的结构手段，不是主题色。

**本模块是全函数**：`renderChartOption` 绝不抛错。任何异常都降级为表格负载 ——
前端渲染门是 `chartType && chartOption`，返回 `None` 或抛错等于让用户什么都看不到，
而表格至少把数据交出去了。

**`{d}` 只允许出现在饼图/环形图**：它是 ECharts 的百分比模板变量，其他图形里
ECharts 找不到替换目标，会原样输出字面量。出口的 `_normalizeFormatters` 是护栏。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from app.domain.chart_spec import ChartSpec
from app.domain.enums import ChartType

logger = logging.getLogger(__name__)

# 值标签的显示上限。超过这个点数逐点标数字会糊成一片，故即使 spec 要求也关掉。
# 结构可读性上界，不是可调策略（不治理）。
_LABEL_MAX_POINTS = 12

# 饼图/环形图之外的 kind 不允许出现 `{d}` 模板变量。
_PIE_LIKE = frozenset({ChartType.PIE, ChartType.DONUT})


# ---------------------------------------------------------------------------
# 取值助手
# ---------------------------------------------------------------------------


def toNumber(value: Any) -> float | int | None:
    """把单元格值转成 JSON 数值；非数值返回 None。

    None 必须留成 None（而不是 0）：缺失值在折线上是断点，在柱状上是空柱，
    当成 0 会凭空造出一个「本期为零」的假事实。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _asLabel(value: Any) -> str:
    """把维度值转成类目名（None → 空串，不产生字面量 "None"）。"""
    return "" if value is None else str(value)


def _columnValues(data: list[dict], field: str) -> list[Any]:
    return [row.get(field) for row in data]


def _categories(data: list[dict], field: str) -> list[str]:
    seen: list[str] = []
    for row in data:
        name = _asLabel(row.get(field))
        if name not in seen:
            seen.append(name)
    return seen


def _measureOf(spec: ChartSpec, index: int) -> str | None:
    """第 index 个指标的字段名；不存在返 None（调用方须容忍）。

    优先取 ``spec.series[index].measure`` —— 系列的字段由 series 声明（builder 写、
    ``validateSpec`` 校），渲染器不再自己从 measures 拼第二份真相。
    """
    if index < len(spec.series) and spec.series[index].measure:
        return spec.series[index].measure
    if index < len(spec.measures):
        return spec.measures[index].field
    return None


def _seriesNameOf(spec: ChartSpec, index: int) -> str:
    """系列名：series 声明优先，退回指标标签。"""
    if index < len(spec.series) and spec.series[index].name:
        return spec.series[index].name
    return _labelOf(spec, index)


def _seriesAxisIndex(spec: ChartSpec, index: int) -> int:
    """组合图的轴归属：series 声明的 ``axis`` 是唯一真相；series 缺失时按位置兜底。"""
    if index < len(spec.series):
        return 1 if spec.series[index].axis == "right" else 0
    return 0 if index == 0 else 1


def _labelOf(spec: ChartSpec, index: int) -> str:
    if index < len(spec.measures):
        return spec.measures[index].label or spec.measures[index].field
    return ""


def _dimensionOf(spec: ChartSpec, index: int) -> str | None:
    if index < len(spec.dimensions):
        return spec.dimensions[index].field
    return None


def _valueLabels(spec: ChartSpec, rowCount: int, position: str) -> dict[str, Any]:
    show = spec.showValueLabels and rowCount <= _LABEL_MAX_POINTS
    return {"show": show, "position": position, "formatter": "{c}"}


def _title(spec: ChartSpec) -> dict[str, Any]:
    return {"title": {"text": spec.title}} if spec.title else {}


def _legend(spec: ChartSpec, seriesCount: int) -> dict[str, Any]:
    if not spec.showLegend or seriesCount <= 1:
        return {}
    return {"legend": {"show": True}}


# ---------------------------------------------------------------------------
# 各 kind 的 builder
# ---------------------------------------------------------------------------


def _buildTable(spec: ChartSpec, data: list[dict]) -> dict[str, Any]:
    """沿用既有 ``{"columns", "rows"}`` 负载 —— TABLE 契约不变，前端表格分支零改动。"""
    return {"columns": list(spec.columns), "rows": list(data)}


def _buildKpi(spec: ChartSpec, data: list[dict]) -> dict[str, Any]:
    """KPI 不是 ECharts：负载沿用 TABLE 的先例（chartOption = 该类型的渲染负载）。"""
    kpi = spec.kpi
    if kpi is None:
        return {"kpi": None}
    return {
        "kpi": {
            "label": kpi.label,
            "value": toNumber(kpi.value),
            "unit": kpi.unit,
            "delta": toNumber(kpi.delta),
        }
    }


def _buildAxisOption(spec: ChartSpec, data: list[dict], kind: ChartType) -> dict[str, Any]:
    """折线/柱状/横柱/组合共用一套笛卡尔装配。

    - 第一个维度作类目轴；若还有第二个维度，则摊成多系列（分组柱状）。
    - 多个指标 = 多条系列。
    """
    catField = _dimensionOf(spec, 0)
    groupField = _dimensionOf(spec, 1)
    categories = _categories(data, catField) if catField else []
    series = _cartesianSeries(
        spec, data, categories, catField, groupField, withAxisIndex=kind is ChartType.COMBO
    )

    horizontal = kind is ChartType.HBAR
    categoryAxis: dict[str, Any] = {"type": "category", "data": categories}
    if horizontal:
        # Top N 的语义是「最大的在最上面」：类目轴默认自下而上，故翻转。
        categoryAxis["inverse"] = True
    valueAxis: dict[str, Any] = {"type": "value"}

    option: dict[str, Any] = {
        **_title(spec),
        "tooltip": {"trigger": "axis"},
        **_legend(spec, len(series)),
        "grid": {"left": 48, "right": 48, "bottom": 32, "top": 40, "containLabel": True},
        "series": series,
    }
    if horizontal:
        option["yAxis"] = categoryAxis
        option["xAxis"] = valueAxis
        return option
    option["xAxis"] = categoryAxis
    option["yAxis"] = _valueAxes(spec, series) if kind is ChartType.COMBO else valueAxis
    return option


def _valueAxes(spec: ChartSpec, series: list[dict[str, Any]]) -> Any:
    """组合图的双轴：第二个指标挂右轴（量级差很大的两个指标不能共用一根轴）。"""
    usesRight = any(entry.get("yAxisIndex") == 1 for entry in series)
    if not usesRight:
        return {"type": "value"}
    return [
        {"type": "value", "name": _seriesNameOf(spec, 0)},
        {"type": "value", "name": _seriesNameOf(spec, 1)},
    ]


def _cartesianSeries(
    spec: ChartSpec,
    data: list[dict],
    categories: list[str],
    catField: str | None,
    groupField: str | None,
    *,
    withAxisIndex: bool,
) -> list[dict[str, Any]]:
    """装配笛卡尔系列。有第二维度时按该维度摊成多系列（分组柱状）。"""
    series: list[dict[str, Any]] = []
    for index, measure in enumerate(spec.measures):
        # 系列的字段以 spec.series 为准（见 _measureOf），measures 只用来定位第几条
        field = _measureOf(spec, index) or measure.field
        if groupField and catField:
            series.extend(_groupedSeries(data, categories, catField, groupField, field))
            continue
        kind = _seriesKindFor(spec, index, "bar")
        entry: dict[str, Any] = {
            "name": _seriesNameOf(spec, index),
            "type": kind,
            "data": [toNumber(v) for v in _columnValues(data, field)],
            "label": _valueLabels(spec, len(data), "top"),
        }
        if kind == "line":
            entry["connectNulls"] = True
        if withAxisIndex:
            entry["yAxisIndex"] = _seriesAxisIndex(spec, index)
        series.append(entry)
    return series


def _seriesKindFor(spec: ChartSpec, index: int, fallback: str) -> str:
    """系列类型：优先用 spec.series 里声明的（组合图的关键），否则按 spec.kind 推。"""
    if index < len(spec.series) and spec.series[index].kind in {"bar", "line", "scatter"}:
        return spec.series[index].kind
    return "line" if spec.kind is ChartType.LINE else fallback


def _groupedSeries(
    data: list[dict],
    categories: list[str],
    catField: str,
    groupField: str,
    measureField: str,
) -> list[dict[str, Any]]:
    """把「两维一指标」摊成「第二个维度的每个取值一条系列」，用于分组柱状。

    缺失的交叉格留 None（空柱），不补 0 —— 补 0 会凭空造出「这一格是零」的假事实。
    """
    groups: list[str] = []
    lookup: dict[tuple[str, str], Any] = {}
    for row in data:
        cat = _asLabel(row.get(catField))
        group = _asLabel(row.get(groupField))
        if group not in groups:
            groups.append(group)
        lookup[(cat, group)] = toNumber(row.get(measureField))
    return [
        {
            "name": group,
            "type": "bar",
            "data": [lookup.get((cat, group)) for cat in categories],
        }
        for group in groups
    ]


def _buildPieLike(spec: ChartSpec, data: list[dict], kind: ChartType) -> dict[str, Any]:
    catField = _dimensionOf(spec, 0)
    measureField = _measureOf(spec, 0)
    points = [
        {"name": _asLabel(row.get(catField)) if catField else "", "value": toNumber(row.get(measureField)) if measureField else None}
        for row in data
    ]
    inner = kind is ChartType.DONUT
    return {
        **_title(spec),
        "tooltip": {"trigger": "item"},
        "legend": {"show": spec.showLegend},
        "series": [
            {
                "type": "pie",
                "name": _labelOf(spec, 0),
                "radius": ["40%", "70%"] if inner else "60%",
                "avoidLabelOverlap": True,
                "label": {
                    "show": spec.showValueLabels,
                    # 饼图/环形图的标签是「类目 + 占比」：占比本就是饼图的读法，
                    # 也是 `{d}` 唯一有效的场合。
                    "formatter": "{b}: {d}%",
                },
                "data": points,
            }
        ],
    }


def _buildScatter(spec: ChartSpec, data: list[dict]) -> dict[str, Any]:
    xField = _measureOf(spec, 0)
    yField = _measureOf(spec, 1)
    points = [
        [
            toNumber(row.get(xField)) if xField else None,
            toNumber(row.get(yField)) if yField else None,
        ]
        for row in data
    ]
    return {
        **_title(spec),
        "tooltip": {"trigger": "item"},
        "grid": {"left": 56, "right": 32, "bottom": 40, "top": 40, "containLabel": True},
        "xAxis": {"type": "value", "name": _labelOf(spec, 0)},
        "yAxis": {"type": "value", "name": _labelOf(spec, 1)},
        "series": [
            {
                "type": "scatter",
                "name": f"{_labelOf(spec, 0)} × {_labelOf(spec, 1)}",
                "symbolSize": 8,
                "data": points,
            }
        ],
    }


def _buildHeatmap(spec: ChartSpec, data: list[dict]) -> dict[str, Any]:
    """交叉矩阵：x = dimensions[1]（列），y = dimensions[0]（行），值 = 唯一指标。"""
    rowField = _dimensionOf(spec, 0)
    colField = _dimensionOf(spec, 1)
    measureField = _measureOf(spec, 0)
    rows = _categories(data, rowField) if rowField else []
    cols = _categories(data, colField) if colField else []
    rowIndex = {name: i for i, name in enumerate(rows)}
    colIndex = {name: i for i, name in enumerate(cols)}
    points = [
        [
            colIndex.get(_asLabel(row.get(colField))),
            rowIndex.get(_asLabel(row.get(rowField))),
            toNumber(row.get(measureField)) if measureField else None,
        ]
        for row in data
    ]
    numbers = [p[2] for p in points if isinstance(p[2], (int, float))]
    return {
        **_title(spec),
        "tooltip": {"trigger": "item"},
        "grid": {"left": 64, "right": 32, "bottom": 56, "top": 40, "containLabel": True},
        "xAxis": {"type": "category", "data": cols, "splitArea": {"show": True}},
        "yAxis": {"type": "category", "data": rows, "splitArea": {"show": True}},
        # visualMap 必须有 min/max 才渲染；色带交给 ECharts 默认（服务端不选色）。
        "visualMap": {
            "min": min(numbers) if numbers else 0,
            "max": max(numbers) if numbers else 1,
            "calculable": True,
            "orient": "horizontal",
            "left": "center",
            "bottom": 0,
        },
        "series": [
            {
                "type": "heatmap",
                "name": _labelOf(spec, 0),
                "data": points,
                "label": {"show": spec.showValueLabels and len(points) <= _LABEL_MAX_POINTS},
            }
        ],
    }


def _buildWaterfall(spec: ChartSpec, data: list[dict]) -> dict[str, Any]:
    """瀑布图 = 透明基线堆叠 + 高度堆叠（ECharts 没有原生 waterfall）。

    基线取「累计量的下沿」：递增项从当前累计量起画，递减项从「累计量 + 变化量」
    起画，这样柱子正好覆盖它贡献的那一段。
    """
    catField = _dimensionOf(spec, 0)
    measureField = _measureOf(spec, 0)
    values = [toNumber(row.get(measureField)) if measureField else None for row in data]

    bases: list[float] = []
    heights: list[float] = []
    cumulative = 0.0
    for value in values:
        delta = float(value) if isinstance(value, (int, float)) else 0.0
        base = cumulative if delta >= 0 else cumulative + delta
        bases.append(base)
        heights.append(abs(delta))
        cumulative += delta

    return {
        **_title(spec),
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 48, "right": 32, "bottom": 32, "top": 40, "containLabel": True},
        "xAxis": {
            "type": "category",
            "data": _categories(data, catField) if catField else [],
        },
        "yAxis": {"type": "value"},
        "series": [
            {
                "name": "__baseline__",
                "type": "bar",
                "stack": "waterfall",
                # 结构手段而非主题色：透明才不会盖住真实柱体。
                "itemStyle": {"color": "transparent"},
                "silent": True,
                "label": {"show": False},
                "data": bases,
            },
            {
                "name": _labelOf(spec, 0),
                "type": "bar",
                "stack": "waterfall",
                "label": _valueLabels(spec, len(data), "top"),
                "data": heights,
            },
        ],
    }


_BUILDERS: dict[ChartType, Callable[[ChartSpec, list[dict]], dict[str, Any]]] = {
    ChartType.TABLE: _buildTable,
    ChartType.KPI: _buildKpi,
    ChartType.PIE: lambda spec, data: _buildPieLike(spec, data, ChartType.PIE),
    ChartType.DONUT: lambda spec, data: _buildPieLike(spec, data, ChartType.DONUT),
    ChartType.SCATTER: _buildScatter,
    ChartType.HEATMAP: _buildHeatmap,
    ChartType.WATERFALL: _buildWaterfall,
    ChartType.LINE: lambda spec, data: _buildAxisOption(spec, data, ChartType.LINE),
    ChartType.BAR: lambda spec, data: _buildAxisOption(spec, data, ChartType.BAR),
    ChartType.HBAR: lambda spec, data: _buildAxisOption(spec, data, ChartType.HBAR),
    ChartType.COMBO: lambda spec, data: _buildAxisOption(spec, data, ChartType.COMBO),
}


# ---------------------------------------------------------------------------
# formatter 归一化（出口护栏）
# ---------------------------------------------------------------------------


def _normalizeFormatters(option: dict[str, Any], kind: ChartType) -> dict[str, Any]:
    """非饼图/环形图里把 `{d}` 换成 `{c}`。

    `{d}` 是 ECharts 的**百分比**模板变量，只在饼图上有效；其他图形里 ECharts
    找不到替换目标会原样输出字面量（用户报过的老 bug）。渲染器本身不会写错，
    这一层是出口护栏：将来有人手改 builder 写进 `{d}`，这里会兜住。
    """
    if kind in _PIE_LIKE:
        return option
    return _walkAndNormalizeFormatters(option)


def _walkAndNormalizeFormatters(node: Any) -> Any:
    """递归走 option 树，把所有字符串 formatter 里的 `{d}` → `{c}`（不可变）。"""
    if isinstance(node, dict):
        newDict: dict = {}
        for key, value in node.items():
            if key == "formatter" and isinstance(value, str):
                newDict[key] = value.replace("{d}", "{c}")
            else:
                newDict[key] = _walkAndNormalizeFormatters(value)
        return newDict
    if isinstance(node, list):
        return [_walkAndNormalizeFormatters(item) for item in node]
    return node


def renderChartOption(spec: ChartSpec, data: list[dict]) -> dict[str, Any]:
    """把 spec + 数据渲染成图表负载。**全函数，绝不抛错**（异常降级为表格）。"""
    rows = list(data or [])
    try:
        builder = _BUILDERS.get(spec.kind)
        if builder is None:
            logger.warning("渲染器没有 %s 的 builder，降级为表格", spec.kind)
            return _buildTable(spec, rows)
        return _normalizeFormatters(builder(spec, rows), spec.kind)
    except Exception:
        logger.warning(
            "渲染 %s 失败，降级为表格负载（数据不丢）", spec.kind, exc_info=True
        )
        return _buildTable(spec, rows)
