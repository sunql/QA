"""KPI 指标技术形态三层对账（纯函数，零 IO）。

| 层 | 对账对 | 基准 | mismatch 后果 |
|---|---|---|---|
| 1 technique_internal | 结构化 measure ⇄ formula 列引用 | 结构化 | degraded，Tier A 不可用 |
| 2 governance | 治理列/粒度 ⇄ 技术 measure | 治理 | 阻断注入 |
| 3 executable | execution_source 指向的 SQL ⇄ 结构化 | 结构化 | degraded，降 Tier B |

层内判定为 mismatch > unknown > pass；跨层只有层 2 决定注入门禁。
层 1/3 在 formula、结构化或标识符缺失时不判；层 3 在 COMPILED/NONE 时不判。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import (
    MetricAggregationOrder,
    MetricExecutionSource,
    MetricType,
    TimeGrain,
)
from app.services.kpi_alignment_resolver import ResolutionOutcome
from app.services.nl2sql_semantic_guard import (
    extractSqlIdentifiers,
    measureColumnIdentifiers,
)

LAYER_TECHNIQUE = "technique_internal"
LAYER_GOVERNANCE = "governance"
LAYER_EXECUTABLE = "executable"

VERDICT_PASS = "pass"
VERDICT_MISMATCH = "mismatch"
VERDICT_UNKNOWN = "unknown"

REASON_FORMULA_COLUMN_MISSING = "structured_column_missing_in_formula"
REASON_GOVERNANCE_COLUMNS_ABSENT = "governance_columns_absent"
REASON_GOVERNANCE_MEASURE_ABSENT = "governance_measure_absent"
REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL = "governance_column_not_in_technical"
REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE = "technical_column_not_in_governance"
REASON_TECHNICAL_MEASURE_UNJUDGEABLE = "technical_measure_unjudgeable"
REASON_GOVERNANCE_GRAIN_ABSENT = "governance_grain_absent"
REASON_TECHNICAL_GRAIN_ABSENT = "technical_grain_absent"
REASON_GRAIN_MISMATCH = "grain_mismatch"
REASON_RATIO_AGGREGATION_ORDER_MISSING = "ratio_aggregation_order_missing"
REASON_RATIO_AGGREGATION_ORDER_INVALID = "ratio_aggregation_order_invalid"
REASON_EXECUTABLE_COLUMN_MISSING = "executable_column_missing_in_measure"
# P3 D3：治理 token 经别名解析层后的结果（resolution 非 None 时）。
REASON_GOVERNANCE_TOKEN_UNRESOLVABLE = "governance_token_unresolvable"
REASON_GOVERNANCE_TOKEN_AMBIGUOUS = "governance_token_ambiguous"
REASON_TIME_GRAIN_ABSENT_GOVERNANCE = "time_grain_absent_governance"
REASON_TIME_GRAIN_ABSENT_TECHNICAL = "time_grain_absent_technical"
REASON_TIME_GRAIN_MISMATCH = "time_grain_mismatch"
REASON_TIME_COLUMN_NOT_IN_SOURCE = "time_column_not_in_source"

_TYPES_REQUIRING_DENOMINATOR = frozenset(
    {MetricType.RATIO.value, MetricType.DIFFERENCE.value}
)
_RATIO_AGGREGATION_ORDERS = frozenset({"WEIGHTED_RATIO", "AVG_OF_RATIOS"})


def _optStr(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optInt(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _tokens(values: tuple[str, ...] | list[str] | None) -> set[str]:
    """把一列字符串归一化成小写集合（去空白、去空项）。"""
    if not values:
        return set()
    return {v.strip().lower() for v in values if isinstance(v, str) and v.strip()}


@dataclass(frozen=True)
class MeasureSpec:
    """结构化度量快照（来自 ontology_metric 的 measure JSON 列）。"""

    agg: str
    expr: str | None = None
    filter: str | None = None
    class_id: int | None = None
    class_alias: str | None = None

    @staticmethod
    def fromRaw(raw: object) -> MeasureSpec | None:
        """非 dict / 缺 agg 返回 None，由上层按不可判处理。"""
        if not isinstance(raw, dict):
            return None
        agg = raw.get("agg")
        if not isinstance(agg, str) or not agg.strip():
            return None
        return MeasureSpec(
            agg=agg.strip().upper(),
            expr=_optStr(raw.get("expr")),
            filter=_optStr(raw.get("filter")),
            class_id=_optInt(raw.get("class_id")),
            class_alias=_optStr(raw.get("class_alias")),
        )

    def columns(self) -> set[str]:
        """只取 expr；filter 可能引用其他源类的列。"""
        return measureColumnIdentifiers(self.expr)


@dataclass(frozen=True)
class MetricTechnique:
    """技术形态快照（ontology_metric 一侧）。"""

    metric_type: str = MetricType.MEASURE.value
    numerator: MeasureSpec | None = None
    denominator: MeasureSpec | None = None
    aggregation_order: str | None = None
    grain_keys: tuple[str, ...] = ()
    filter_expression: str | None = None
    formula: str | None = None
    execution_source: str | None = None
    # P3 D3：技术侧时间粒度与源类日期列（0143 新列）。
    time_grain: str | None = None
    time_column: str | None = None

    def structuredColumns(self) -> set[str]:
        """结构化 measure 声明的全部列（分子 ∪ 分母）。"""
        cols: set[str] = set()
        if self.numerator is not None:
            cols |= self.numerator.columns()
        if self.denominator is not None:
            cols |= self.denominator.columns()
        return cols


@dataclass(frozen=True)
class GovernanceSpec:
    """治理快照（kpi_catalog 一侧）。"""

    numerator_columns: tuple[str, ...] = ()
    denominator_columns: tuple[str, ...] = ()
    grain_keys: tuple[str, ...] = ()
    # P3 D3：治理侧时间粒度（由 kpi.grain 自由文本推导，R6）。
    time_grain: str | None = None


def governanceTokensOf(governance: GovernanceSpec) -> tuple[str, ...]:
    """治理侧全部结构化 token（分子 ∪ 分母 ∪ 粒度键），供 resolver 一次性解析。"""
    return (
        tuple(governance.numerator_columns)
        + tuple(governance.denominator_columns)
        + tuple(governance.grain_keys)
    )


@dataclass(frozen=True)
class ReconcileFinding:
    layer: str
    reason: str
    detail: str
    verdict: str


@dataclass(frozen=True)
class ReconcileResult:
    """verdict 只来自层 2；degraded 表示层 1/3 存在 mismatch。"""

    verdict: str
    findings: tuple[ReconcileFinding, ...] = ()
    degraded: bool = False

    @property
    def isPass(self) -> bool:
        return self.verdict == VERDICT_PASS

    @property
    def isTierAEligible(self) -> bool:
        return self.isPass and not self.degraded

    def reasons(self) -> tuple[str, ...]:
        return tuple(f.reason for f in self.findings)


def _finding(layer: str, reason: str, detail: str, verdict: str) -> ReconcileFinding:
    return ReconcileFinding(layer=layer, reason=reason, detail=detail, verdict=verdict)


def _result(findings: tuple[ReconcileFinding, ...]) -> ReconcileResult:
    if any(f.verdict == VERDICT_MISMATCH for f in findings):
        verdict = VERDICT_MISMATCH
    elif findings:
        verdict = VERDICT_UNKNOWN
    else:
        verdict = VERDICT_PASS
    return ReconcileResult(verdict=verdict, findings=findings)


def reconcileTechniqueInternal(technique: MetricTechnique) -> ReconcileResult:
    """层 1：结构化 measure ⇄ formula；无法明确判断时不报 mismatch。"""
    if not (technique.formula or "").strip():
        return _result(())
    structured = technique.structuredColumns()
    if not structured:
        return _result(())
    formulaCols = extractSqlIdentifiers(technique.formula)
    if not formulaCols:
        return _result(())
    missing = sorted(structured - formulaCols)
    if not missing:
        return _result(())
    return _result((
        _finding(
            LAYER_TECHNIQUE,
            REASON_FORMULA_COLUMN_MISSING,
            f"结构化列未出现在 formula 中：{', '.join(missing)}",
            VERDICT_MISMATCH,
        ),
    ))


def _checkRole(
    declared: tuple[str, ...], measure: MeasureSpec | None, label: str
) -> tuple[ReconcileFinding, ...]:
    """单个角色（分子 / 分母）的治理⇄技术比对。"""
    declaredSet = _tokens(declared)
    if not declaredSet:
        return (_finding(
            LAYER_GOVERNANCE,
            REASON_GOVERNANCE_COLUMNS_ABSENT,
            f"治理侧未声明{label}列",
            VERDICT_UNKNOWN,
        ),)
    if measure is None:
        return (_finding(
            LAYER_GOVERNANCE,
            REASON_GOVERNANCE_MEASURE_ABSENT,
            f"治理侧声明了{label}列，但技术侧缺{label}度量",
            VERDICT_MISMATCH,
        ),)
    technical = measure.columns()
    if not technical:
        return (_finding(
            LAYER_GOVERNANCE,
            REASON_TECHNICAL_MEASURE_UNJUDGEABLE,
            f"技术侧{label}度量未填表达式，无法比对",
            VERDICT_UNKNOWN,
        ),)
    missingInTechnical = sorted(declaredSet - technical)
    extraInTechnical = sorted(technical - declaredSet)
    findings: list[ReconcileFinding] = []
    if missingInTechnical:
        findings.append(_finding(
            LAYER_GOVERNANCE,
            REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL,
            f"治理声明的{label}列未出现在技术度量中：{', '.join(missingInTechnical)}",
            VERDICT_MISMATCH,
        ))
    if extraInTechnical:
        findings.append(_finding(
            LAYER_GOVERNANCE,
            REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE,
            f"技术{label}度量含治理未声明的列（技术跑偏）：{', '.join(extraInTechnical)}",
            VERDICT_MISMATCH,
        ))
    return tuple(findings)


def _checkGrain(
    declared: tuple[str, ...], technical: tuple[str, ...]
) -> tuple[ReconcileFinding, ...]:
    """粒度键按集合语义比对，顺序无关。"""
    declaredSet = _tokens(declared)
    technicalSet = _tokens(technical)
    if not declaredSet:
        return (_finding(
            LAYER_GOVERNANCE,
            REASON_GOVERNANCE_GRAIN_ABSENT,
            "治理侧未声明结构化粒度键",
            VERDICT_UNKNOWN,
        ),)
    if not technicalSet:
        return (_finding(
            LAYER_GOVERNANCE,
            REASON_TECHNICAL_GRAIN_ABSENT,
            "治理侧声明了粒度键，但技术侧为空",
            VERDICT_MISMATCH,
        ),)
    if declaredSet != technicalSet:
        return (_finding(
            LAYER_GOVERNANCE,
            REASON_GRAIN_MISMATCH,
            f"治理与技术粒度键不一致：治理={sorted(declaredSet)} 技术={sorted(technicalSet)}",
            VERDICT_MISMATCH,
        ),)
    return ()


def _parseTimeGrain(value: str | None) -> TimeGrain | None:
    """``time_grain`` 文本 → 枚举（大小写/空白不敏感）；非法值按缺失处理。"""
    text = (value or "").strip().lower()
    if not text:
        return None
    try:
        return TimeGrain(text)
    except ValueError:
        return None


def _resolvedColumnsFor(
    tokens: tuple[str, ...], resolution: ResolutionOutcome
) -> tuple[str, ...]:
    """从扁平解析结果里取出属于本角色 token 的物理列（集合语义，排序）。"""
    declared = _tokens(tokens)
    columns = {
        column.strip().lower()
        for token, column in resolution.resolved
        if token.strip().lower() in declared
    }
    return tuple(sorted(columns))


def _checkTimeGrain(
    governance: GovernanceSpec, technique: MetricTechnique
) -> tuple[ReconcileFinding, ...]:
    """时间粒度枚举比较；任一侧缺失只判 unknown（不 mismatch，设计 §7.3）。"""
    governanceGrain = _parseTimeGrain(governance.time_grain)
    technicalGrain = _parseTimeGrain(technique.time_grain)
    findings: list[ReconcileFinding] = []
    if governanceGrain is None:
        findings.append(_finding(
            LAYER_GOVERNANCE,
            REASON_TIME_GRAIN_ABSENT_GOVERNANCE,
            "治理侧未声明时间粒度",
            VERDICT_UNKNOWN,
        ))
    if technicalGrain is None:
        findings.append(_finding(
            LAYER_GOVERNANCE,
            REASON_TIME_GRAIN_ABSENT_TECHNICAL,
            "技术侧未声明时间粒度",
            VERDICT_UNKNOWN,
        ))
    if (
        governanceGrain is not None
        and technicalGrain is not None
        and governanceGrain != technicalGrain
    ):
        findings.append(_finding(
            LAYER_GOVERNANCE,
            REASON_TIME_GRAIN_MISMATCH,
            f"时间粒度不一致：治理={governanceGrain.value} 技术={technicalGrain.value}",
            VERDICT_MISMATCH,
        ))
    return tuple(findings)


def _checkGrainWithResolution(
    governance: GovernanceSpec,
    technique: MetricTechnique,
    resolution: ResolutionOutcome,
) -> tuple[ReconcileFinding, ...]:
    """粒度比较 = time_grain 枚举相等 + 维度列集合（经 resolver）相等。"""
    findings = list(_checkTimeGrain(governance, technique))
    declaredRaw = _tokens(governance.grain_keys)
    if not declaredRaw:
        findings.append(_finding(
            LAYER_GOVERNANCE,
            REASON_GOVERNANCE_GRAIN_ABSENT,
            "治理侧未声明结构化粒度键",
            VERDICT_UNKNOWN,
        ))
        return tuple(findings)
    resolvedDimensions = set(
        _resolvedColumnsFor(governance.grain_keys, resolution)
    )
    technicalDimensions = _tokens(technique.grain_keys)
    if not technicalDimensions:
        findings.append(_finding(
            LAYER_GOVERNANCE,
            REASON_TECHNICAL_GRAIN_ABSENT,
            "治理侧声明了粒度键，但技术侧为空",
            VERDICT_MISMATCH,
        ))
        return tuple(findings)
    if resolvedDimensions != technicalDimensions:
        findings.append(_finding(
            LAYER_GOVERNANCE,
            REASON_GRAIN_MISMATCH,
            "治理与技术粒度键不一致："
            f"治理={sorted(resolvedDimensions)} 技术={sorted(technicalDimensions)}",
            VERDICT_MISMATCH,
        ))
    return tuple(findings)


def _checkTimeColumn(
    technique: MetricTechnique, resolution: ResolutionOutcome
) -> tuple[ReconcileFinding, ...]:
    """技术声明的 ``time_column`` 必须落在源类日期候选内（否则 unknown 降级）。"""
    column = (technique.time_column or "").strip()
    if not column:
        return ()
    candidates = {candidate.strip().lower() for candidate in resolution.timeCandidates}
    if column.lower() in candidates:
        return ()
    return (_finding(
        LAYER_GOVERNANCE,
        REASON_TIME_COLUMN_NOT_IN_SOURCE,
        f"技术时间列 {column} 不在源类日期候选：{sorted(candidates)}",
        VERDICT_UNKNOWN,
    ),)


def _checkRatio(technique: MetricTechnique) -> tuple[ReconcileFinding, ...]:
    """RATIO 型必须配合法聚合阶，防止两种比率语义被混用。"""
    if technique.metric_type != MetricType.RATIO.value:
        return ()
    order = (technique.aggregation_order or "").strip().upper()
    if not order or order == MetricAggregationOrder.NONE.value:
        return (_finding(
            LAYER_GOVERNANCE,
            REASON_RATIO_AGGREGATION_ORDER_MISSING,
            "比率型指标未配置聚合阶（aggregation_order）",
            VERDICT_MISMATCH,
        ),)
    if order not in _RATIO_AGGREGATION_ORDERS:
        return (_finding(
            LAYER_GOVERNANCE,
            REASON_RATIO_AGGREGATION_ORDER_INVALID,
            f"比率型指标聚合阶非法：{order}",
            VERDICT_MISMATCH,
        ),)
    return ()


def _needsDenominator(
    governance: GovernanceSpec, technique: MetricTechnique
) -> bool:
    return (
        technique.metric_type in _TYPES_REQUIRING_DENOMINATOR
        or bool(_tokens(governance.denominator_columns))
    )


def _reconcileGovernanceIdentity(
    governance: GovernanceSpec, technique: MetricTechnique
) -> ReconcileResult:
    """``resolution=None`` 的恒等回退：与 P2 行为逐字相同（硬门槛 DoD#5）。"""
    findings: list[ReconcileFinding] = []
    findings.extend(_checkRole(
        governance.numerator_columns, technique.numerator, "分子"
    ))
    if _needsDenominator(governance, technique):
        findings.extend(_checkRole(
            governance.denominator_columns, technique.denominator, "分母"
        ))
    findings.extend(_checkGrain(governance.grain_keys, technique.grain_keys))
    findings.extend(_checkRatio(technique))
    return _result(tuple(findings))


def _reconcileGovernanceWithResolution(
    governance: GovernanceSpec,
    technique: MetricTechnique,
    resolution: ResolutionOutcome,
) -> ReconcileResult:
    """解析结果可用：治理列集合 = resolver 结果；粒度按枚举 + 维度列比较。"""
    findings: list[ReconcileFinding] = []
    if resolution.missingTokens:
        findings.append(_finding(
            LAYER_GOVERNANCE,
            REASON_GOVERNANCE_TOKEN_UNRESOLVABLE,
            f"治理 token 在源类无对应列：{', '.join(resolution.missingTokens)}",
            VERDICT_MISMATCH,
        ))
    if resolution.unknownTokens:
        findings.append(_finding(
            LAYER_GOVERNANCE,
            REASON_GOVERNANCE_TOKEN_AMBIGUOUS,
            f"治理 token 命中多个候选，无法判定：{', '.join(resolution.unknownTokens)}",
            VERDICT_UNKNOWN,
        ))
    findings.extend(_checkRole(
        _resolvedColumnsFor(governance.numerator_columns, resolution),
        technique.numerator,
        "分子",
    ))
    if _needsDenominator(governance, technique):
        findings.extend(_checkRole(
            _resolvedColumnsFor(governance.denominator_columns, resolution),
            technique.denominator,
            "分母",
        ))
    findings.extend(_checkGrainWithResolution(governance, technique, resolution))
    findings.extend(_checkRatio(technique))
    findings.extend(_checkTimeColumn(technique, resolution))
    return _result(tuple(findings))


def reconcileGovernanceVsTechnique(
    governance: GovernanceSpec,
    technique: MetricTechnique,
    resolution: ResolutionOutcome | None = None,
) -> ReconcileResult:
    """层 2：治理 ⇄ 技术；治理是签字基准。

    ``resolution`` 为 None ⇒ 恒等回退（存量行为不变）；非 None ⇒ 治理 token
    先用别名解析层换成物理列，再与技术 measure 比较（P3 D3）。
    """
    if resolution is None:
        return _reconcileGovernanceIdentity(governance, technique)
    return _reconcileGovernanceWithResolution(governance, technique, resolution)


def reconcileExecutableVsStructured(technique: MetricTechnique) -> ReconcileResult:
    """层 3：仅 ESCAPE_HATCH 在 P1 可比对；其余来源不判。"""
    source = (technique.execution_source or "").strip().upper()
    if source != MetricExecutionSource.ESCAPE_HATCH.value:
        return _result(())
    if not (technique.formula or "").strip():
        return _result(())
    structured = technique.structuredColumns()
    if not structured:
        return _result(())
    formulaCols = extractSqlIdentifiers(technique.formula)
    if not formulaCols:
        return _result(())
    missing = sorted(structured - formulaCols)
    if not missing:
        return _result(())
    return _result((
        _finding(
            LAYER_EXECUTABLE,
            REASON_EXECUTABLE_COLUMN_MISSING,
            f"可执行 SQL 未覆盖结构化列：{', '.join(missing)}",
            VERDICT_MISMATCH,
        ),
    ))


def reconcileMetric(
    governance: GovernanceSpec,
    technique: MetricTechnique,
    resolution: ResolutionOutcome | None = None,
) -> ReconcileResult:
    """合并三层 findings；注入门禁只采用层 2，层 1/3/层 2 unknown 仅降级。"""
    layer1 = reconcileTechniqueInternal(technique)
    layer2 = reconcileGovernanceVsTechnique(governance, technique, resolution)
    layer3 = reconcileExecutableVsStructured(technique)
    findings = tuple(
        f for layer in (layer1, layer2, layer3) for f in layer.findings
    )
    # R4：unknown 放行注入，但必须标注 degraded（设计 §7.3）。
    degraded = (
        layer1.verdict == VERDICT_MISMATCH
        or layer3.verdict == VERDICT_MISMATCH
        or layer2.verdict == VERDICT_UNKNOWN
    )
    return ReconcileResult(
        verdict=layer2.verdict, findings=findings, degraded=degraded
    )
