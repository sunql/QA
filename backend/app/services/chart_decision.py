"""图表决策引擎：从「数据形状 + 查询计划 + 问句」选出该画什么图。

**为什么要有它**（取代旧的 `ChartService.recommendChartType`）：旧实现只看列类型与
行数，出口只有 4 个（table/pie/bar/line），永远不返回散点；「供应商 + 数量」这种形状
既可能是占比也可能是分类比较，纯看形状无法区分。这里改成**规则优先**：有序规则表按
数据语义选型，只有规则之间真的歧义时才请 LLM 给一个**语义标签**。

**三条硬约束**（探索已证实，改动时不要违反）：

1. **LLM 绝不定图型**。它只输出 ``TREND|SHARE|RANK|COMPARE|RELATION|DETAIL|KPI``
   之一（见 `_LABEL_TO_KIND`），且只能从该规则**已算出的候选集**里挑；挑不出来
   （标签非法 / 目标 kind 不满足形状守卫）就退回规则原判。所以最坏情况是「选得不合
   心意」，不会是「渲染不出来」。
2. **ETL 列必须拉黑**。同一张表既有业务日期（`RECEIPT_DATE`）也有 ETL 加载时间
   （`ETL_LOAD_TS`/`UPDATE_DATE`/`CREATE_TIME`）。不拉黑就会出现「按 ETL 加载时间画
   折线」这种看着对、实际无意义的图。黑名单在 `_isTimeColumn` 里**先于**任何正向判定。
3. **降级终点是表格**。无信息、明细、规则全不命中 → TABLE，绝不返回空图。

**不可变性**：全部 frozen dataclass；`resolveByLabel` 返回新对象（`dataclasses.replace`）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace

from app.domain.enums import ChartType
from app.domain.query_plan import QueryPlan
from app.services.chart_thresholds import ChartThresholds
from app.utils.column_types import (
    COLUMN_TYPE_NUMBER,
    COLUMN_TYPE_TIME,
    infer_column_types,
)
from app.utils.column_types import is_number as _isNumber

# ---------------------------------------------------------------------------
# 不治理的常量（`Harness/rules/魔数治理.md` 反例档）
#
# 这些是**解析语法与契约**，不是可调策略：正则写错等于时间维识别全废、关键词表
# admin 改了没有任何运营意义。改它们必须改代码+过测试，这正是想要的摩擦。
# ---------------------------------------------------------------------------

# ETL/审计列：这些列名永远不能成为趋势轴（约束 2）。
_ETL_COLUMN_DENYLIST: frozenset[str] = frozenset(
    {
        "ETL_LOAD_TS", "ETL_LOAD_TIME", "ETL_UPDATE_TS", "ETL_BATCH_ID", "ETL_SOURCE_ID",
        "UPDATE_DATE", "UPDATE_TIME", "UPDATE_TS", "LAST_UPDATE", "LAST_UPDATED",
        "LAST_MODIFIED", "CREATE_TIME", "CREATE_DATE", "CREATION_DATE", "CREATED_AT",
        "CREATED_TIME", "INSERT_TIME", "MODIFY_TIME", "MODIFIED_TIME", "DWH_LOAD_TS",
        "LOAD_DATE", "LOAD_TIME",
    }
)
_ETL_SUFFIX_RE = re.compile(r"(_LOAD_TS|_LOAD_TIME|_ETL_TS|_UPDATE_TS|_CREATED_AT)$")

# 时间列名线索：命中且值不是数值时视为时间（业务日期列的命名习惯）。
_TIME_NAME_RE = re.compile(
    r"(^|_)(DATE|DT|DAY|MONTH|YM|YEAR|PERIOD|WEEK|QUARTER|FISCAL|POSTING|RECEIPT"
    r"|ARRIVAL|PLANNED|VALID|SHIP|DELIVERY)(_|$)"
)
# 期间值：202605 / 2026-05 / 2026/5 / 2026Q1 / 5月 / 2026。
# 存在的理由：`infer_column_type` 的 TIME 只认 `^\d{4}-\d{2}-\d{2}`，所以
# 「按月」的分组列会被判成 STRING —— 不补这条，「按月趋势」永远出不了折线。
_PERIOD_VALUE_RE = re.compile(
    r"^\d{4}(0[1-9]|1[0-2])$|^\d{4}[-/]\d{1,2}$|^\d{4}Q[1-4]$|^\d{1,2}月$|^\d{4}$"
)
# 增减量列名：瀑布图必须有可拆解的增减，见规则 R05 的理由。
_DELTA_NAME_RE = re.compile(
    r"(DELTA|DIFF|CHANGE|GAP|VARIANCE|VAR)(_|$)|增减|变化量|差异|涨跌", re.I
)

# 问句意图关键词（同上，不治理）。
_RANKING_Q_RE = re.compile(r"最多|最少|最高|最低|最大|最小|前\s*\d+|排名|top\s*\d+|前几")
_SHARE_Q_RE = re.compile(r"占比|份额|构成比|百分比|比例|比率")
_TREND_Q_RE = re.compile(r"趋势|走势|逐月|按天|按月|按周|按年|变化情况")
_YOY_Q_RE = re.compile(r"同比|环比|对比")
_WATERFALL_Q_RE = re.compile(r"瀑布|构成|拆解|归因|贡献")

# 瀑布图最多拆几步。超过就不是「构成拆解」而是长尾罗列 —— 结构上界，非可调策略。
_WATERFALL_MAX_STEPS = 20

# 需要「维度 + 指标」才能画的规则里，哪些算歧义（形状相同、只能靠语义分）。
# 只有这两条：占比 vs 分类比较（1 维 1 指标），趋势 vs 分类比较（时间维 1 指标）。
# 是否真调分类器还要过 `_isAmbiguous` 的问句线索闸 —— 用户自己说出口的意图不必再猜。
_AMBIGUOUS_RULES: frozenset[str] = frozenset({"R07_TREND_LINE", "R12_CATEGORY_BAR"})

# LLM 白名单：标签 → 它想表达的图型。标签非法 = 解析失败，不是「换一个」。
_LABEL_TO_KIND: dict[str, ChartType] = {
    "TREND": ChartType.LINE,
    "SHARE": ChartType.DONUT,
    "RANK": ChartType.HBAR,
    "COMPARE": ChartType.BAR,
    "RELATION": ChartType.SCATTER,
    "DETAIL": ChartType.TABLE,
    "KPI": ChartType.KPI,
}
SEMANTIC_LABELS: frozenset[str] = frozenset(_LABEL_TO_KIND)


@dataclass(frozen=True)
class ChartSignals:
    """决策所需的全部信号（从 plan + columns + data + question 一次性派生）。

    决策引擎与标签消歧共用同一个实例 —— 后者要复算形状守卫，若各自派生就会漂移。
    """

    rowCount: int
    columns: tuple[str, ...]
    dimensions: tuple[str, ...]
    measures: tuple[str, ...]
    timeColumns: tuple[str, ...]
    etlColumns: tuple[str, ...]
    heatmapCoverage: float
    hasShareFormula: bool
    isRawDetail: bool
    isTopN: bool
    hasRankingIntent: bool
    wantCombo: bool
    wantWaterfall: bool
    hasSignedMeasure: bool
    hasDeltaMeasure: bool
    questionShare: bool
    questionTrend: bool

    @property
    def chartable(self) -> bool:
        """有数据可画的最低条件（供上层判断「要不要走图这条路」）。"""
        return self.rowCount > 0 and bool(self.columns)

    @property
    def hasDecomposableDelta(self) -> bool:
        """是否真有可拆解的增减量（瀑布图的前提，见 `_structuralKind` 的 R05）。"""
        return self.hasSignedMeasure or self.hasDeltaMeasure


@dataclass(frozen=True)
class ChartDecision:
    """决策结果：确定的 kind + 依据的规则 + 候选集 + 是否歧义 + 标签命中。"""

    kind: ChartType
    ruleId: str
    candidates: tuple[ChartType, ...]
    ambiguous: bool
    labelHint: str | None = None


def _isEtlColumn(name: str) -> bool:
    upper = name.upper()
    return upper in _ETL_COLUMN_DENYLIST or bool(_ETL_SUFFIX_RE.search(upper))


def _looksLikePeriod(values: list[object]) -> bool:
    """列值是否整体呈期间形态（202605 / 2026-05 / 5月 …）。"""
    sample = [v for v in values if v is not None]
    if not sample:
        return False
    return all(
        isinstance(v, str) and _PERIOD_VALUE_RE.match(v.strip()) for v in sample
    )


def _isTimeColumn(
    name: str,
    values: list[object],
    columnType: str,
    planGrouped: frozenset[str],
    etlColumns: frozenset[str],
) -> bool:
    """判定某列是否为可作趋势轴的时间维。

    顺序即优先级：ETL 拉黑 **先于** 一切正向判定（约束 2）—— 否则 `ETL_LOAD_TS`
    的值是合法日期，会被后两条规则捞回来。
    """
    if name in etlColumns:
        return False
    if columnType == COLUMN_TYPE_NUMBER:
        return False
    if columnType == COLUMN_TYPE_TIME:
        return True
    if _TIME_NAME_RE.search(name.upper()):
        return True
    # 期间值兜底：只认被 plan 当维度分组过的列，避免把 4 位字符串编码猜成时间。
    return name in planGrouped and _looksLikePeriod(values)


def _heatmapCoverage(
    dimensions: tuple[str, ...], data: list[dict]
) -> float:
    """交叉矩阵完备度 = 行数 / (维度1基数 × 维度2基数)；维度不足 2 个返 0。

    稀疏交叉表画成热力图是大片空白，所以这个数字是 R09 与 R10 的分界。
    """
    if len(dimensions) < 2 or not data:
        return 0.0
    first, second = dimensions[0], dimensions[1]
    cardFirst = len({row.get(first) for row in data})
    cardSecond = len({row.get(second) for row in data})
    if cardFirst <= 0 or cardSecond <= 0:
        return 0.0
    return len(data) / (cardFirst * cardSecond)


def buildChartSignals(
    plan: QueryPlan | None,
    columns: list[str],
    data: list[dict],
    question: str,
) -> ChartSignals:
    """一次性派生决策信号。plan 允许为 None（多步的某些分支没有 plan）。"""
    columnTypes = infer_column_types(list(columns), data)
    etlColumns = tuple(c for c in columns if _isEtlColumn(c))
    etlSet = frozenset(etlColumns)

    planGrouped = frozenset(
        list(plan.groupBy) + [s.property for s in plan.sortBy] if plan is not None else []
    )

    timeColumns = tuple(
        c
        for c in columns
        if _isTimeColumn(
            c,
            [row.get(c) for row in data],
            columnTypes.get(c, ""),
            planGrouped,
            etlSet,
        )
    )
    measures = tuple(c for c in columns if columnTypes.get(c) == COLUMN_TYPE_NUMBER)
    measureSet = frozenset(measures)
    timeSet = frozenset(timeColumns)
    # 维度 = 既不是指标、也不是时间轴、也不是 ETL 列的其余列。
    dimensions = tuple(
        c for c in columns if c not in measureSet and c not in timeSet and c not in etlSet
    )

    aggregations = plan.aggregations if plan is not None else ()
    signedMeasure = any(
        _isNumber(row.get(m)) and row.get(m) < 0 for row in data for m in measures
    )
    return ChartSignals(
        rowCount=len(data),
        columns=tuple(columns),
        dimensions=dimensions,
        measures=measures,
        timeColumns=timeColumns,
        etlColumns=etlColumns,
        heatmapCoverage=_heatmapCoverage(dimensions, data),
        hasShareFormula=any(a.formula for a in aggregations),
        isRawDetail=not aggregations and not (plan.groupBy if plan is not None else ()),
        isTopN=bool(
            plan is not None
            and (
                (plan.rowLimit is not None and _isDescending(plan))
                or plan.perGroupLimit is not None
            )
        ),
        hasRankingIntent=bool(_RANKING_Q_RE.search(question)),
        wantCombo=bool(_YOY_Q_RE.search(question)) or any(a.formula for a in aggregations),
        wantWaterfall=bool(_WATERFALL_Q_RE.search(question)),
        hasSignedMeasure=signedMeasure,
        hasDeltaMeasure=any(_DELTA_NAME_RE.search(m) for m in measures),
        questionShare=bool(_SHARE_Q_RE.search(question)),
        questionTrend=bool(_TREND_Q_RE.search(question)),
    )


def _isDescending(plan: QueryPlan) -> bool:
    return any(s.direction.lower() == "desc" for s in plan.sortBy)


def _candidatesFor(ruleId: str, signals: ChartSignals, thresholds: ChartThresholds) -> tuple[ChartType, ...]:
    """该规则下「数据形状支持」的 kind 集合（LLM 只能从这里挑）。

    TABLE 恒在集合内：它是通用降级终点，任何形状都能渲染，且 DETAIL 是合法意图。
    """
    if ruleId == "R07_TREND_LINE":
        return (ChartType.LINE, ChartType.BAR, ChartType.TABLE)
    if ruleId == "R12_CATEGORY_BAR":
        candidates = [ChartType.BAR]
        if len(signals.measures) == 1 and signals.rowCount <= thresholds.pieMaxRows:
            candidates.append(ChartType.DONUT)
        candidates.extend([ChartType.HBAR, ChartType.TABLE])
        return tuple(candidates)
    return ()


def _structuralKind(signals: ChartSignals, thresholds: ChartThresholds) -> tuple[ChartType, str]:
    """有序规则表：首个命中者胜。返回 (kind, ruleId)。"""
    if not signals.columns or signals.rowCount == 0:
        return ChartType.TABLE, "R00_EMPTY_TABLE"

    # R01 单行单指标 → 指标卡。`measures == 1` 是「单值」的本义：一行里并排两个
    # 数值是「两个指标并列」，该画图不是卡片。
    if signals.rowCount == 1 and len(signals.measures) == 1 and len(signals.dimensions) <= 1:
        return ChartType.KPI, "R01_SINGLE_VALUE_KPI"

    # R01S 单行的多指标 / 多维 → 表格。R01 已经认掉「单行单指标」，剩下的单行形状
    # 会一路掉到 R08 散点、R09 热力图：一个点的散布图、一个格子的热力图都不是图。
    # 这类结果的真实产物是 `SELECT SUM(amount), SUM(qty)`（不带 GROUP BY）——把两个
    # 数并排显示成表格才是它该有的样子。
    #
    # 只拦「有指标」的形状：无聚合的多列明细本来就走 R13 明细表，不该被这条改判。
    if signals.rowCount == 1 and signals.measures and (
        len(signals.measures) >= 2 or len(signals.dimensions) >= 2
    ):
        return ChartType.TABLE, "R01S_SINGLE_ROW_TABLE"

    # R02/R03 占比：`formula` 是 validatePlan **已强制**的信号（别名含占比/比率/
    # 比例/百分比/ratio/percent/share/pct 时必填），比任何启发式都可靠。
    if signals.hasShareFormula and len(signals.measures) == 1 and len(signals.dimensions) == 1:
        if signals.rowCount <= thresholds.pieMaxRows:
            return ChartType.DONUT, "R02_SHARE_DONUT"
        return ChartType.HBAR, "R03_SHARE_OVERFLOW_HBAR"

    oneDimOneMeasure = len(signals.dimensions) == 1 and len(signals.measures) >= 1
    # R04 排名：分类维上的 TOP N，类目名长所以横过来。
    # 维度放宽到「≥1」而不是「==1」：`perGroupLimit`（「每个供应商各自的前三物料」）
    # 的结果集天然是 2 维（分区维 + 组内维），它同样是排名，不该掉进二维热力图。
    if (
        (signals.isTopN or signals.hasRankingIntent)
        and not signals.timeColumns
        and len(signals.dimensions) >= 1
        and len(signals.measures) >= 1
    ):
        return ChartType.HBAR, "R04_TOPN_HBAR"
    # R04T 排名压过时间维：「供货量最多的 3 个月」是排名，不是趋势。
    if signals.hasRankingIntent and signals.timeColumns and len(signals.measures) >= 1:
        return ChartType.HBAR, "R04_TOPN_HBAR"

    # R05 瀑布：问句要构成拆解，**且真有可拆解的增减量**。
    # 缺了后半条时画瀑布图是编故事：把累计值当增减量摊平会得出一个看起来很权威、
    # 实际错误的图。宁可降级成柱状（落到 R11/R12）。
    if (
        signals.wantWaterfall
        and signals.hasDecomposableDelta
        and len(signals.dimensions) == 1
        and len(signals.measures) == 1
        and 2 <= signals.rowCount <= _WATERFALL_MAX_STEPS
    ):
        return ChartType.WATERFALL, "R05_WATERFALL"

    # R06 柱线组合：时间维上两个指标（同比环比 / 量+率）。
    if (
        signals.timeColumns
        and len(signals.measures) >= 2
        and (signals.wantCombo or signals.hasShareFormula)
    ):
        return ChartType.COMBO, "R06_COMBO"

    # R07 趋势：时间维（ETL 已被排除）+ 指标。歧义规则之一。
    if signals.timeColumns and len(signals.measures) >= 1:
        return ChartType.LINE, "R07_TREND_LINE"

    # R08 关系：两个指标一个维度 = 两个指标的关系（散点）。
    if len(signals.measures) == 2 and len(signals.dimensions) <= 1:
        return ChartType.SCATTER, "R08_RELATION_SCATTER"

    # R13 明细：无聚合无分组的多列原文 → 表格。
    #
    # **求值位置刻意早于 ID 顺序**：明细优先于 R09/R10/R11/R12 —— 一个没有聚合、
    # 没有分组的行清单（PO_NO + 供应商 + 金额）画成热力图或柱状是把明细当统计量，
    # 读出来的「数」没有意义。放在 R04 之后是为了保住「ORDER BY x DESC LIMIT 10
    # 但没 GROUP BY」仍按 TOP N 处理（那是排名，不是明细）。
    if signals.isRawDetail and len(signals.dimensions) >= 2:
        return ChartType.TABLE, "R13_RAW_DETAIL_TABLE"

    # R09/R10 多维：两维一指标。矩阵够稠才画热力图，稀了退普通柱状。
    if len(signals.dimensions) == 2 and len(signals.measures) == 1:
        if signals.heatmapCoverage >= thresholds.heatmapMinCoverage:
            return ChartType.HEATMAP, "R09_MULTIDIM_HEATMAP"
        return ChartType.BAR, "R10_MULTIDIM_BAR"

    # R11 类目多到竖着挤不下 → 横过来。边界是「严格大于」。
    if oneDimOneMeasure and signals.rowCount > thresholds.hbarMinRows:
        return ChartType.HBAR, "R11_HBAR_MANY_ROWS"

    # R12S 问句明说「占比」但计划没算出 formula。这不是理论情况：`validatePlan`
    # 只在**聚合别名**含占比时才强制 formula，用户问了「占多少」而别名被写成
    # 「入库量」的情形真实存在。用户的原话是确定性线索 —— 分类器能给的最优答案
    # 就是 SHARE→DONUT，这里直接给，省掉一次会失败、会判错的往返。
    if oneDimOneMeasure and signals.questionShare:
        if signals.rowCount <= thresholds.pieMaxRows:
            return ChartType.DONUT, "R12S_QUESTION_SHARE_DONUT"
        return ChartType.HBAR, "R12S_QUESTION_SHARE_HBAR"

    # R12 分类比较。歧义规则之二（形状与占比完全相同）。
    if oneDimOneMeasure:
        return ChartType.BAR, "R12_CATEGORY_BAR"

    return ChartType.TABLE, "R14_DEFAULT_TABLE"


def _isAmbiguous(
    ruleId: str,
    signals: ChartSignals,
    candidates: tuple[ChartType, ...],
) -> bool:
    """歧义 = 候选集里真有不同的 kind，**且问句自己没给出确定性的线索**。

    问句说「趋势」时仍要问分类器是说不通的：那是用户的原话，比模型猜得准，
    且分类器的失败面（超时、乱答、白名单外）在趋势这种最常见问法上被反复踩。
    线索指向的 kind 必须真在候选集里才算数 —— 形状不支持时线索不成立，照旧歧义。
    """
    if ruleId not in _AMBIGUOUS_RULES or len(set(candidates)) <= 1:
        return False
    cue = _QUESTION_CUES.get(ruleId)
    if cue is None:
        return True
    hasCue, kind = cue(signals)
    return not (hasCue and kind in candidates)


# 歧义规则 ← 问句里的确定性线索。线索命中且目标 kind 在候选集内 → 不再歧义。
#
# 表里只有 R07：R12 也曾挂过「占比」线索，但那是**死线索** —— 占比问句在 R12S 就
# 被接住了，走到 R12 时 `questionShare` 必为 False，那个 lambda 永远返回 False。
# 挂在那里读起来像「占比省掉一次分类往返」，实际一次都不会触发。
_QUESTION_CUES: dict[str, Callable[[ChartSignals], tuple[bool, ChartType]]] = {
    "R07_TREND_LINE": lambda s: (s.questionTrend, ChartType.LINE),
}


def decideChartKind(
    signals: ChartSignals,
    thresholds: ChartThresholds,
    labelHint: str | None = None,
) -> ChartDecision:
    """跑规则表得到决策；候选集蕴含多个 kind 时标记为歧义（上层才去问 LLM）。"""
    kind, ruleId = _structuralKind(signals, thresholds)
    candidates = _candidatesFor(ruleId, signals, thresholds)
    ambiguous = _isAmbiguous(ruleId, signals, candidates)
    decision = ChartDecision(
        kind=kind, ruleId=ruleId, candidates=candidates, ambiguous=ambiguous
    )
    if labelHint is None:
        return decision
    return resolveByLabel(decision, signals, labelHint, thresholds)


def resolveByLabel(
    decision: ChartDecision,
    signals: ChartSignals,
    label: str | None,
    thresholds: ChartThresholds,
) -> ChartDecision:
    """用 LLM 给的语义标签在候选集里选一个 kind；选不出来就退回规则原判。

    绝不抛错、绝不换到候选集之外的 kind —— 标签的作用域被限制在「规则已经算出的
    可能性」里，所以 LLM 挂掉/乱答的最坏后果只是「按规则选」，不影响出图。
    """
    if label is None or label not in _LABEL_TO_KIND:
        return decision
    if not decision.ambiguous:
        return decision
    target = _LABEL_TO_KIND[label]
    if target not in decision.candidates:
        return decision
    return replace(decision, kind=target, labelHint=label)
