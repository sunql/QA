"""标准化图表规格（ChartSpec）—— 图表决策引擎的输出契约。

**定位**：这是**内部领域对象**，不是线上契约。线上仍发 `chartType` + `chartOption`
（ECharts option），由 `chart_renderer` 从本 spec 渲染出来。故用 frozen dataclass
（与 `QueryPlan` 同范式），而非 Pydantic 线上 DTO。

**为什么不让 LLM 产出这份 spec**：spec 的每个字段（维度、指标、标题、排序、朝向）
都能从 `QueryPlan` 与列名确定性派生。让 LLM 写只会引入「引用了不存在的列」这类
失败面，却换不来额外信息 —— 而本体 `description` 实测只填了 10/3642 行，单位之类
的信息代码同样拿不到，所以 LLM 也没有信息优势。LLM 的唯一职责是规则歧义时的
**语义标签**分类（见 `chart_decision`）。

**不可变性**：全部 frozen dataclass；`coerceSpec` 返回新对象，绝不原地改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.enums import ChartType

# spec 版本。渲染器据它决定如何解读字段；未来加字段时升版本。
SPEC_VERSION = 1

# spec 允许的最大维度/指标数（超过就不是「一张图」能表达的了）。
MAX_DIMENSIONS = 2
MAX_MEASURES = 2

# 需要「维度 + 指标 + series」三件套的图形类 kind（TABLE/KPI 不走这条）。
_CHART_KINDS: frozenset[ChartType] = frozenset(
    {
        ChartType.LINE,
        ChartType.BAR,
        ChartType.HBAR,
        ChartType.PIE,
        ChartType.DONUT,
        ChartType.SCATTER,
        ChartType.HEATMAP,
        ChartType.COMBO,
        ChartType.WATERFALL,
    }
)

# 每种 kind 对维度个数的硬要求（缺省 = 至少 1 个）。
_REQUIRED_DIMENSION_COUNT: dict[ChartType, int] = {ChartType.HEATMAP: 2}
# 每种 kind 对指标个数的硬要求。
_REQUIRED_MEASURE_COUNT: dict[ChartType, int] = {ChartType.SCATTER: 2}


@dataclass(frozen=True)
class SpecDimension:
    """一个维度引用：结果集里的列 + 人类可读标签。"""

    field: str
    label: str


@dataclass(frozen=True)
class SpecMeasure:
    """一个指标引用：列 + 标签 + 可选单位/聚合函数。"""

    field: str
    label: str
    unit: str | None = None
    agg: str | None = None


@dataclass(frozen=True)
class SpecSeries:
    """一条系列：画什么、用哪个维度和指标、挂哪根轴。

    kind 是 ECharts 的 series 类型（"bar"/"line"），与 spec 的 kind（图型）不同层：
    柱线组合（COMBO）就是同一张图里混 bar 与 line 两条 series。
    """

    name: str
    kind: str
    dimension: str
    measure: str
    axis: str = "left"


@dataclass(frozen=True)
class SpecSort:
    """排序意图。applied 表示排序是否真的作用到了数据上（渲染器据此决定要不要重排）。"""

    by: str
    order: str = "desc"
    applied: bool = True


@dataclass(frozen=True)
class SpecKpi:
    """指标卡载荷（仅 kind=KPI）。delta 预留给同环比差值，一期恒 None。"""

    label: str
    value: Any
    unit: str | None = None
    delta: Any | None = None


@dataclass(frozen=True)
class ChartSpec:
    """一张图/表的完整描述（不可变）。"""

    kind: ChartType
    title: str = ""
    # 结果集列名。TABLE 与校验都用它；图表类 kind 也带上，便于渲染器取原始数据。
    columns: tuple[str, ...] = ()
    dimensions: tuple[SpecDimension, ...] = ()
    measures: tuple[SpecMeasure, ...] = ()
    series: tuple[SpecSeries, ...] = ()
    orientation: str = "vertical"  # vertical | horizontal
    sort: SpecSort | None = None
    showLegend: bool = True
    showValueLabels: bool = True
    stacked: bool = False
    kpi: SpecKpi | None = None
    specVersion: int = field(default=SPEC_VERSION)


def tableSpec(columns: list[str] | tuple[str, ...]) -> ChartSpec:
    """构造 TABLE spec —— 也是所有降级路径的终点。"""
    return ChartSpec(kind=ChartType.TABLE, columns=tuple(columns))


def _allReferencedFields(spec: ChartSpec) -> list[str]:
    """收集 spec 引用到的全部列名（按出现顺序，便于报错时指出第一个坏引用）。

    **空串引用被跳过**：散点图可以没有维度（x/y 都是指标），此时 `series.dimension`
    是空串 —— 那是「这条系列不引用维度」，不是「引用了一个叫空串的列」。
    """
    refs: list[str] = [d.field for d in spec.dimensions]
    refs.extend(m.field for m in spec.measures)
    for s in spec.series:
        refs.append(s.dimension)
        refs.append(s.measure)
    if spec.sort is not None:
        refs.append(spec.sort.by)
    return [r for r in refs if r]


def validateSpec(spec: ChartSpec, columns: list[str] | tuple[str, ...]) -> str | None:
    """校验 spec 能否渲染。合法返回 None，否则返回第一个违规原因（可直接进日志）。

    只做两件事：**引用存在性** + **形状与 kind 相容**。不校验数值语义（那是决策
    引擎的职责），也不校验配色（颜色属于前端主题层）。
    """
    known = set(columns)
    for ref in _allReferencedFields(spec):
        if ref not in known:
            return f"引用了结果集中不存在的列 {ref!r}（列：{sorted(known)}）"

    if len(spec.dimensions) > MAX_DIMENSIONS:
        return f"维度数 {len(spec.dimensions)} 超过上限 {MAX_DIMENSIONS}"
    if len(spec.measures) > MAX_MEASURES:
        return f"指标数 {len(spec.measures)} 超过上限 {MAX_MEASURES}"

    if spec.kind is ChartType.TABLE:
        return None
    if spec.kind is ChartType.KPI:
        return None if spec.kpi is not None else "KPI spec 缺少 kpi 载荷"
    if spec.kind not in _CHART_KINDS:
        return f"未知图表类型 {spec.kind!r}"

    # 散点是唯一的例外：它的两根轴都是指标，维度只是点位标签（可以没有）。
    if spec.kind is not ChartType.SCATTER and not spec.dimensions:
        return f"{spec.kind.value} 需要至少一个维度"
    if not spec.measures:
        return f"{spec.kind.value} 需要至少一个指标"
    if not spec.series:
        return f"{spec.kind.value} 需要至少一条 series"

    requiredDims = _REQUIRED_DIMENSION_COUNT.get(spec.kind)
    if requiredDims is not None and len(spec.dimensions) != requiredDims:
        return f"{spec.kind.value} 需要恰好 {requiredDims} 个维度，实际 {len(spec.dimensions)}"
    requiredMeasures = _REQUIRED_MEASURE_COUNT.get(spec.kind)
    if requiredMeasures is not None and len(spec.measures) != requiredMeasures:
        return f"{spec.kind.value} 需要恰好 {requiredMeasures} 个指标，实际 {len(spec.measures)}"
    return None


def coerceSpec(
    spec: ChartSpec, columns: list[str] | tuple[str, ...]
) -> tuple[ChartSpec, str | None]:
    """校验 spec；不过则**降级为 TABLE** 并返回原因。

    降级而不是报错的理由：前端的渲染门是 `chartType && chartOption`
    （`MessageItem.tsx:133`），返回一个渲染不出来的 kind 等于让用户什么都看不到。
    表格至少把数据交出去了 —— 出图失败不该连带丢数据。
    """
    reason = validateSpec(spec, columns)
    if reason is None:
        return spec, None
    return tableSpec(columns), reason
