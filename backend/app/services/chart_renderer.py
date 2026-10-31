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

# grid 底部留白：无图例 32；有图例 56 —— 图例常驻底部单行（见 _scrollLegend），
# 多留一截给刻度标签，避免图例与 x 轴刻度贴在一起。
_GRID_BOTTOM_DEFAULT = 32
_GRID_BOTTOM_WITH_LEGEND = 56

# 2026-10-31：类目轴可读性（结构边界，不做数据治理）。
# 标签 > 12 个时强制逐条显示：横条图截断超长名称，纵向柱状图旋转 45°。
_CATEGORY_LABEL_DENSE_THRESHOLD = 12
_AXIS_LABEL_MAX_WIDTH = 160
# 类别 > 40 时绘图区已塞不下，打开缩放滑块（inside 滚轮 + slider 拖拽）。
# 折线是唯一例外：标签稀疏就能读，不应被缩放框改变视野；组合图与柱状图同为
# 笛卡尔装配，同样收不下密集类目，故一并启用。
_DATAZOOM_THRESHOLD = 40
_DATAZOOM_SLIDER_HEIGHT = 20
_DATAZOOM_BAND_HEIGHT = 34
_DATAZOOM_LEGEND_BOTTOM = 24
_DATAZOOM_KINDS = frozenset({ChartType.BAR, ChartType.HBAR, ChartType.COMBO})

# 饼图/环形图之外的 kind 不允许出现 `{d}` 模板变量。
_PIE_LIKE = frozenset({ChartType.PIE, ChartType.DONUT})

# 2026-10-31：排名类图的默认视图只留前 20 类，尾部合成「其他（N 项）」。
# 只作用于排名语义成立的图形；折线看趋势、散点/热力/瀑布看分布，截尾会篡改
# 图形语义，一律不截断。
_TOP_N_LIMIT = 20
_TOP_N_KINDS = frozenset({ChartType.BAR, ChartType.HBAR, ChartType.PIE, ChartType.DONUT})


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


def _axisNameOf(spec: ChartSpec, field: str | None) -> str | None:
    """类目轴的轴名 = 该维度的**显示名**；无注入或**恒等映射**时返 None（不挂轴名）。

    恒等映射必须屏蔽：上游 alias 原样返列名时，原始列名会经轴名漏进 option。
    """
    if not field:
        return None
    label = spec.axisLabels.get(field)
    return label if label and label != field else None


def _namedAxis(axis: dict[str, Any], name: str | None) -> dict[str, Any]:
    """给轴挂显示名；无名字时**原样返回**（chat 形态的负载因此逐字不变）。"""
    if not name:
        return axis
    return {**axis, "name": name}


def _valueLabels(spec: ChartSpec, rowCount: int, position: str) -> dict[str, Any]:
    show = spec.showValueLabels and rowCount <= _LABEL_MAX_POINTS
    return {"show": show, "position": position, "formatter": "{c}"}


def _title(spec: ChartSpec) -> dict[str, Any]:
    return {"title": {"text": spec.title}} if spec.title else {}


def _scrollLegend(show: bool) -> dict[str, Any]:
    """图例常驻**底部单行滚动**：默认顶部放置时，多系列会折成多行直接盖住标题
    （线上实测：31 条系列把标题糊成一片）。`type: "scroll"` 让图例分页而不是折行，
    永远只占一行。图例位置是结构（不是颜色），所以归服务端。"""
    return {"show": show, "type": "scroll", "bottom": 0}


def _legend(spec: ChartSpec, seriesCount: int) -> dict[str, Any]:
    if not spec.showLegend or seriesCount <= 1:
        return {}
    return {"legend": _scrollLegend(True)}


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


def _denseLabel(kind: ChartType, horizontal: bool) -> dict[str, Any] | None:
    """类别多到必须逐条显示时，标签怎么摆；line/combo 保持 ECharts 自动稀疏。

    斜着的日期标签比稀疏标签更难读，所以只有纵向柱状图旋转。
    """
    if horizontal:
        return {"interval": 0, "overflow": "truncate", "width": _AXIS_LABEL_MAX_WIDTH}
    if kind is ChartType.BAR:
        return {"rotate": 45, "interval": 0, "hideOverlap": True}
    return None


def _buildCategoryAxis(
    spec: ChartSpec, categories: list[str], catField: str | None, kind: ChartType
) -> dict[str, Any]:
    """类目轴：Top N 翻转 + 标签密度 + （可选的）轴名。两种朝向共用这一份。"""
    horizontal = kind is ChartType.HBAR
    axis: dict[str, Any] = {"type": "category", "data": categories}
    if horizontal:
        # Top N 的语义是「最大的在最上面」：类目轴默认自下而上，故翻转。
        axis["inverse"] = True
    if len(categories) > _CATEGORY_LABEL_DENSE_THRESHOLD:
        label = _denseLabel(kind, horizontal)
        if label is not None:
            axis["axisLabel"] = label
    # 轴名只此一处。无注入 ⇒ 原对象原样返回。
    return _namedAxis(axis, _axisNameOf(spec, catField))


def _dataZoomOf(horizontal: bool) -> list[dict[str, Any]]:
    """缩放滑块：inside 负责滚轮/拖拽，slider 负责可见的把手。

    slider 必须显式 `orient: "horizontal"`：绑到 yAxis 时 ECharts 默认把它转成
    **竖向**，`height` 于是变成滑块长度，渲染成右下角一个小方块 —— 而 grid.bottom
    与 legend.bottom 都是按整条底部横条预留的。两种朝向都留底部横条，故恒定横向。
    """
    axisIndex = "yAxisIndex" if horizontal else "xAxisIndex"
    return [
        {"type": "inside", axisIndex: 0},
        {
            "type": "slider",
            axisIndex: 0,
            "height": _DATAZOOM_SLIDER_HEIGHT,
            "bottom": 0,
            "orient": "horizontal",
        },
    ]


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
    categoryAxis = _buildCategoryAxis(spec, categories, catField, kind)
    valueAxis: dict[str, Any] = {"type": "value"}
    legend = _legend(spec, len(series))

    zoomed = kind in _DATAZOOM_KINDS and len(categories) > _DATAZOOM_THRESHOLD
    gridBottom = (_GRID_BOTTOM_WITH_LEGEND if legend else _GRID_BOTTOM_DEFAULT) + (
        _DATAZOOM_BAND_HEIGHT if zoomed else 0
    )
    if zoomed and legend:
        # 图例让位给滑块：滑块贴底 0，图例抬到它上面 4px。
        legend = {"legend": {**legend["legend"], "bottom": _DATAZOOM_LEGEND_BOTTOM}}
    option: dict[str, Any] = {
        **_title(spec),
        "tooltip": {"trigger": "axis"},
        **legend,
        # 有图例时底部多留一截给滚动条 + 刻度标签（32/56 常数见模块头）。
        "grid": {
            "left": 48,
            "right": 48,
            "bottom": gridBottom,
            "top": 40,
            "containLabel": True,
        },
        "series": series,
    }
    if zoomed:
        option["dataZoom"] = _dataZoomOf(horizontal)
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
        "legend": _scrollLegend(spec.showLegend),
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
        "xAxis": _namedAxis(
            {"type": "category", "data": cols, "splitArea": {"show": True}},
            _axisNameOf(spec, colField),
        ),
        "yAxis": _namedAxis(
            {"type": "category", "data": rows, "splitArea": {"show": True}},
            _axisNameOf(spec, rowField),
        ),
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

    bases: list[float | None] = []
    heights: list[float | None] = []
    cumulative = 0.0
    for value in values:
        if value is None:
            # 缺失/非数值差值 = 断点：不伪造 0（0 是「变化量为零」的假事实），
            # 该位不画，累计量不推进 —— 后续项从既有累计量继续。
            bases.append(None)
            heights.append(None)
            continue
        delta = float(value)
        base = cumulative if delta >= 0 else cumulative + delta
        bases.append(base)
        heights.append(abs(delta))
        cumulative += delta

    return {
        **_title(spec),
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 48, "right": 32, "bottom": 32, "top": 40, "containLabel": True},
        "xAxis": _namedAxis(
            {"type": "category", "data": _categories(data, catField) if catField else []},
            _axisNameOf(spec, catField),
        ),
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


def renderChartOption(
    spec: ChartSpec, data: list[dict], *, topN: bool = False
) -> dict[str, Any]:
    """把 spec + 数据渲染成图表负载。**全函数，绝不抛错**（异常降级为表格）。

    ``topN=True`` 时，若图形是排名语义且类目超过 ``_TOP_N_LIMIT``，额外挂一份
    「前 20 + 其他」的视图：``topNOption``（完整 option）+ ``topNMeta``
    （``limit/hidden/total``）。全量 option 逐字不变 —— 截断是**附加**字段，
    前端切换、PDF 截图、研究线报告都从同一份 JSONB 里读，不需要各自造规则。
    """
    rows = list(data or [])
    builder = _BUILDERS.get(spec.kind)
    if builder is None:
        logger.warning("渲染器没有 %s 的 builder，降级为表格", spec.kind)
        return _buildTable(spec, rows)
    try:
        option = _normalizeFormatters(builder(spec, rows), spec.kind)
    except Exception:
        logger.warning(
            "渲染 %s 失败，降级为表格负载（数据不丢）", spec.kind, exc_info=True
        )
        return _buildTable(spec, rows)
    if not topN:
        return option
    try:
        return _attachTopN(spec, rows, builder, option)
    except Exception:
        # 截断视图是增强项：构造失败只发全量图，绝不让整张图降级。
        logger.warning("构造 Top-N 视图失败，退化为全量图", exc_info=True)
        return option


def _attachTopN(
    spec: ChartSpec,
    rows: list[dict],
    builder: Callable[[ChartSpec, list[dict]], dict[str, Any]],
    option: dict[str, Any],
) -> dict[str, Any]:
    """在全量 option 上附加 ``topNOption`` / ``topNMeta``；条件不满足时原样返回。"""
    catField = _dimensionOf(spec, 0)
    if spec.kind not in _TOP_N_KINDS or not catField:
        return option
    categories = _categories(rows, catField)
    hidden = len(categories) - _TOP_N_LIMIT
    if hidden <= 0:
        return option
    measureFields = _measureFields(spec)
    if not measureFields or not _isOrderedByMeasure(rows, categories, catField, measureFields[0]):
        # 行序不可信（SQL 没 ORDER BY）：任意 20 个类目冒充「前 20」比不截断更糟。
        logger.info(
            "图表行序与度量 %s 不单调，跳过 Top-N 截断（SQL 可能缺 ORDER BY）",
            measureFields[0],
        )
        return option
    headSet = set(categories[:_TOP_N_LIMIT])
    kept = [row for row in rows if _asLabel(row.get(catField)) in headSet]
    tail = [row for row in rows if _asLabel(row.get(catField)) not in headSet]
    synthetic = _otherRows(spec, tail, catField, f"其他（{hidden} 项）")
    topOption = _normalizeFormatters(builder(spec, kept + synthetic), spec.kind)
    return {
        **option,
        "topNOption": topOption,
        "topNMeta": {
            "limit": _TOP_N_LIMIT,
            "hidden": hidden,
            "total": len(categories),
        },
    }


def _isOrderedByMeasure(
    rows: list[dict], categories: list[str], catField: str, measureField: str
) -> bool:
    """类目按该度量的**合计**单调（升或降，允许并列）⇒ 行序可当「前 N」的依据。

    Top-N 的正确性依赖「行序 == 度量序」：SQL 有 ORDER BY 时成立（降序对应
    「最多的 N 个」，升序对应「最低的 N 个」）；planner 只出 groupBy 没出排序时
    DB 返回任意顺序，此时按行序切出的「前 20」是随机样本。宁可不截断，只发全量图。

    同一类目可能有多行（多维组合），所以按类目合计判断，而不是逐行取值 ——
    否则 `ORDER BY` 按类目汇总排序的分组数据会被误判成无序。

    ``None``（该类目无可用数值）不携带排序信息，跳过不参与比较；全都为 None 时
    无从证伪，按「有序」处理（保持既有截断行为，「其他」项如实留空而不是补 0）。
    """
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(_asLabel(row.get(catField)), []).append(row)
    totals = [
        total
        for total in (_sumOf(buckets.get(name, []), measureField) for name in categories)
        if total is not None
    ]
    if len(totals) < 2:
        return True
    return all(a >= b for a, b in zip(totals, totals[1:])) or all(
        a <= b for a, b in zip(totals, totals[1:])
    )


def _otherRows(
    spec: ChartSpec, tail: list[dict], catField: str, otherLabel: str
) -> list[dict[str, Any]]:
    """把尾部类目合成「其他」行。

    分组柱状图按「类目 × 其余维度组合」各生成一行 —— 只生成一行会让「其他」
    整根堆在第一个分组上，另外几个分组凭空缺一格。饼图的其余维度本就不参与
    渲染（``_buildPieLike`` 只读第一个维度），故恒定合成单行。
    """
    if not tail:
        return []
    groupFields = _otherGroupFields(spec)
    measureFields = _measureFields(spec)
    buckets: dict[tuple[str, ...], list[dict]] = {}
    for row in tail:
        key = tuple(_asLabel(row.get(field)) for field in groupFields)
        buckets.setdefault(key, []).append(row)
    synthetic: list[dict[str, Any]] = []
    for key, bucket in buckets.items():
        other: dict[str, Any] = {field: None for field in spec.columns}
        other[catField] = otherLabel
        other.update(zip(groupFields, key))
        for field in measureFields:
            other[field] = _sumOf(bucket, field)
        synthetic.append(other)
    return synthetic


def _otherGroupFields(spec: ChartSpec) -> tuple[str, ...]:
    """合成行的分组维度 = 第一个维度之外的所有维度（饼图除外，见 ``_otherRows``）。"""
    if spec.kind in _PIE_LIKE:
        return ()
    return tuple(
        field
        for field in (_dimensionOf(spec, index) for index in range(1, len(spec.dimensions)))
        if field
    )


def _measureFields(spec: ChartSpec) -> tuple[str, ...]:
    """需要求和的字段：series 声明优先，再补 measures（去重保序）。"""
    fields: list[str] = []
    for candidate in [entry.measure for entry in spec.series] + [
        entry.field for entry in spec.measures
    ]:
        if candidate and candidate not in fields:
            fields.append(candidate)
    return tuple(fields)


def _sumOf(rows: list[dict], field: str) -> float | int | None:
    """尾部合计；全空返回 None（不能补 0 —— 那会造出「其他项为零」的假事实）。"""
    numbers = [
        value
        for value in (toNumber(row.get(field)) for row in rows)
        if value is not None
    ]
    return sum(numbers) if numbers else None
