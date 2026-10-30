"""KPI 指标技术形态三层对账（纯函数单测，不碰库）。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services.kpi_metric_reconcile import (
    LAYER_EXECUTABLE,
    LAYER_GOVERNANCE,
    LAYER_TECHNIQUE,
    REASON_EXECUTABLE_COLUMN_MISSING,
    REASON_FORMULA_COLUMN_MISSING,
    REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL,
    REASON_GOVERNANCE_COLUMNS_ABSENT,
    REASON_GOVERNANCE_GRAIN_ABSENT,
    REASON_GOVERNANCE_MEASURE_ABSENT,
    REASON_GRAIN_MISMATCH,
    REASON_RATIO_AGGREGATION_ORDER_INVALID,
    REASON_RATIO_AGGREGATION_ORDER_MISSING,
    REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE,
    REASON_TECHNICAL_GRAIN_ABSENT,
    REASON_TECHNICAL_MEASURE_UNJUDGEABLE,
    VERDICT_MISMATCH,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    GovernanceSpec,
    MeasureSpec,
    MetricTechnique,
    reconcileExecutableVsStructured,
    reconcileGovernanceVsTechnique,
    reconcileMetric,
    reconcileTechniqueInternal,
)


def _measure(expr: str | None, agg: str = "SUM") -> MeasureSpec:
    return MeasureSpec(agg=agg, expr=expr)


def _fullGovernance() -> GovernanceSpec:
    return GovernanceSpec(
        numerator_columns=("QTY",),
        denominator_columns=("AMOUNT",),
        grain_keys=("plant_code",),
    )


def _fullTechnique() -> MetricTechnique:
    return MetricTechnique(
        metric_type="RATIO",
        numerator=_measure("QTY"),
        denominator=_measure("AMOUNT"),
        aggregation_order="WEIGHTED_RATIO",
        grain_keys=("plant_code",),
        formula="SELECT SUM(QTY), SUM(AMOUNT) FROM T",
        execution_source="ESCAPE_HATCH",
    )


# ── MeasureSpec ────────────────────────────────────────────────────────


def test_measure_from_raw_accepts_dict() -> None:
    m = MeasureSpec.fromRaw(
        {"agg": "sum", "expr": "QTY", "filter": "X = 1", "class_id": 12}
    )
    assert m is not None
    assert m.agg == "SUM"
    assert m.expr == "QTY"
    assert m.class_id == 12


def test_measure_from_raw_rejects_non_dict() -> None:
    assert MeasureSpec.fromRaw("QTY") is None
    assert MeasureSpec.fromRaw(None) is None
    assert MeasureSpec.fromRaw([1, 2]) is None


def test_measure_from_raw_rejects_missing_agg() -> None:
    assert MeasureSpec.fromRaw({"expr": "QTY"}) is None
    assert MeasureSpec.fromRaw({"agg": "  "}) is None


def test_measure_columns_only_from_expr() -> None:
    m = MeasureSpec(agg="SUM", expr="QTY", filter="OTHER_COL = 'X'")
    assert m.columns() == {"qty"}


# ── 层 1：技术内部（结构化 ⇄ formula），基准 = 结构化 ───────────────────


def test_layer1_no_formula_is_not_a_finding() -> None:
    t = MetricTechnique(numerator=_measure("QTY"), formula=None)
    r = reconcileTechniqueInternal(t)
    assert r.verdict == VERDICT_PASS
    assert r.findings == ()


def test_layer1_no_structured_is_not_a_finding() -> None:
    t = MetricTechnique(numerator=None, formula="SELECT SUM(QTY) FROM T")
    r = reconcileTechniqueInternal(t)
    assert r.verdict == VERDICT_PASS
    assert r.findings == ()


def test_layer1_formula_without_identifiers_is_not_a_finding() -> None:
    t = MetricTechnique(numerator=_measure("QTY"), formula="按期交付量")
    r = reconcileTechniqueInternal(t)
    assert r.verdict == VERDICT_PASS


def test_layer1_missing_structured_column_in_formula_is_mismatch() -> None:
    t = MetricTechnique(
        numerator=_measure("QTY"), denominator=_measure("AMOUNT"),
        formula="SELECT SUM(QTY) FROM T",
    )
    r = reconcileTechniqueInternal(t)
    assert r.verdict == VERDICT_MISMATCH
    assert r.reasons() == (REASON_FORMULA_COLUMN_MISSING,)
    assert r.findings[0].layer == LAYER_TECHNIQUE


def test_layer1_extra_formula_identifier_is_not_judged() -> None:
    """formula 里多出表名/别名 ⇒ 不判（宁漏不误报）。"""
    t = MetricTechnique(
        numerator=_measure("QTY"),
        formula="SELECT SUM(QTY) FROM DWD_SUPPLIER_PRICE_LIST_HEADER",
    )
    r = reconcileTechniqueInternal(t)
    assert r.verdict == VERDICT_PASS


# ── 层 2：治理 ⇄ 技术，基准 = 治理 ───────────────────────────────────


def test_layer2_governance_columns_absent_is_unknown() -> None:
    r = reconcileGovernanceVsTechnique(GovernanceSpec(), MetricTechnique())
    assert r.verdict == VERDICT_UNKNOWN
    assert REASON_GOVERNANCE_COLUMNS_ABSENT in r.reasons()


def test_layer2_governance_declares_but_technique_missing_is_mismatch() -> None:
    g = GovernanceSpec(numerator_columns=("QTY",))
    t = MetricTechnique(numerator=None)
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_MISMATCH
    assert REASON_GOVERNANCE_MEASURE_ABSENT in r.reasons()


def test_layer2_governance_column_not_in_technique_is_mismatch() -> None:
    g = GovernanceSpec(numerator_columns=("QTY", "EXTRA"))
    t = MetricTechnique(numerator=_measure("QTY"))
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_MISMATCH
    assert REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL in r.reasons()


def test_layer2_technical_column_not_in_governance_is_mismatch() -> None:
    """技术跑偏：技术侧多出治理未声明的列。"""
    g = GovernanceSpec(numerator_columns=("QTY",))
    t = MetricTechnique(numerator=_measure("QTY - DISCOUNT"))
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_MISMATCH
    assert REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE in r.reasons()


def test_layer2_technical_measure_without_expr_is_unknown() -> None:
    g = GovernanceSpec(numerator_columns=("QTY",))
    t = MetricTechnique(numerator=_measure(None))
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_UNKNOWN
    assert REASON_TECHNICAL_MEASURE_UNJUDGEABLE in r.reasons()


def test_layer2_grain_absent_on_governance_is_unknown() -> None:
    g = GovernanceSpec(numerator_columns=("QTY",), grain_keys=())
    t = MetricTechnique(numerator=_measure("QTY"))
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_UNKNOWN
    assert REASON_GOVERNANCE_GRAIN_ABSENT in r.reasons()


def test_layer2_technical_grain_absent_is_mismatch() -> None:
    g = GovernanceSpec(numerator_columns=("QTY",), grain_keys=("plant_code",))
    t = MetricTechnique(numerator=_measure("QTY"), grain_keys=())
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_MISMATCH
    assert REASON_TECHNICAL_GRAIN_ABSENT in r.reasons()


def test_layer2_grain_order_insensitive() -> None:
    """粒度键按集合语义比对（顺序无关）。"""
    g = GovernanceSpec(numerator_columns=("QTY",), grain_keys=("a", "b"))
    t = MetricTechnique(numerator=_measure("QTY"), grain_keys=("b", "a"))
    r = reconcileGovernanceVsTechnique(g, t)
    assert REASON_GRAIN_MISMATCH not in r.reasons()


def test_layer2_grain_mismatch_is_mismatch() -> None:
    g = GovernanceSpec(numerator_columns=("QTY",), grain_keys=("plant_code",))
    t = MetricTechnique(numerator=_measure("QTY"), grain_keys=("supplier_code",))
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_MISMATCH
    assert REASON_GRAIN_MISMATCH in r.reasons()


def test_layer2_grain_aligned_columns_differ_is_mismatch_without_grain_code() -> None:
    """FIX-1 反向用例：粒度已对齐、列不一致 ⇒ 仍 mismatch，且**不含** grain code。

    真实数据里 12/12 的 findings 都带 ``grain_mismatch``（治理粒度是中文业务词、
    技术粒度是列名）⇒ 它会**掩盖**列比对的失效。本用例把 grain 对齐，单独钉住
    ``_checkRole`` 的列比对：把该比对删掉时，本用例必须变红。
    """
    g = GovernanceSpec(
        numerator_columns=("RECEIPT_DATE",),
        grain_keys=("supplier_code", "facility_code"),
    )
    t = MetricTechnique(
        numerator=_measure("PROMISED_RECEIPT_DATE"),
        grain_keys=("SUPPLIER_CODE", "FACILITY_CODE"),
    )
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_MISMATCH
    assert REASON_GRAIN_MISMATCH not in r.reasons()
    assert REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL in r.reasons()
    assert REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE in r.reasons()


def test_layer2_ratio_without_aggregation_order_is_mismatch() -> None:
    g = GovernanceSpec(numerator_columns=("QTY",), denominator_columns=("AMOUNT",))
    t = MetricTechnique(
        metric_type="RATIO",
        numerator=_measure("QTY"),
        denominator=_measure("AMOUNT"),
        aggregation_order=None,
    )
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_MISMATCH
    assert REASON_RATIO_AGGREGATION_ORDER_MISSING in r.reasons()


def test_layer2_ratio_with_none_aggregation_order_is_mismatch() -> None:
    g = GovernanceSpec(numerator_columns=("QTY",), denominator_columns=("AMOUNT",))
    t = MetricTechnique(
        metric_type="RATIO",
        numerator=_measure("QTY"),
        denominator=_measure("AMOUNT"),
        aggregation_order="NONE",
    )
    r = reconcileGovernanceVsTechnique(g, t)
    assert REASON_RATIO_AGGREGATION_ORDER_MISSING in r.reasons()


def test_layer2_ratio_with_invalid_aggregation_order_is_mismatch() -> None:
    g = GovernanceSpec(numerator_columns=("QTY",), denominator_columns=("AMOUNT",))
    t = MetricTechnique(
        metric_type="RATIO",
        numerator=_measure("QTY"),
        denominator=_measure("AMOUNT"),
        aggregation_order="WHATEVER",
    )
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_MISMATCH
    assert REASON_RATIO_AGGREGATION_ORDER_INVALID in r.reasons()


def test_layer2_measure_type_skips_denominator_check() -> None:
    """MEASURE 型指标不查分母（治理侧分母列为空 ⇒ 不出 unknown）。"""
    g = GovernanceSpec(numerator_columns=("QTY",), grain_keys=("plant_code",))
    t = MetricTechnique(
        metric_type="MEASURE",
        numerator=_measure("QTY"),
        grain_keys=("plant_code",),
    )
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_PASS
    assert r.findings == ()


def test_layer2_ratio_type_requires_denominator_check() -> None:
    """RATIO 型指标即使治理侧分母列为空，也要查分母（⇒ unknown）。"""
    g = GovernanceSpec(numerator_columns=("QTY",), grain_keys=("plant_code",))
    t = MetricTechnique(
        metric_type="RATIO",
        numerator=_measure("QTY"),
        aggregation_order="WEIGHTED_RATIO",
        grain_keys=("plant_code",),
    )
    r = reconcileGovernanceVsTechnique(g, t)
    assert r.verdict == VERDICT_UNKNOWN


def test_layer2_full_match_is_pass() -> None:
    r = reconcileGovernanceVsTechnique(_fullGovernance(), _fullTechnique())
    assert r.verdict == VERDICT_PASS
    assert r.findings == ()


# ── 层 3：可执行 ⇄ 结构化，基准 = 结构化 ───────────────────────────


def test_layer3_execution_source_absent_is_not_a_finding() -> None:
    t = MetricTechnique(numerator=_measure("QTY"), execution_source=None)
    assert reconcileExecutableVsStructured(t).verdict == VERDICT_PASS


def test_layer3_execution_source_none_is_not_a_finding() -> None:
    t = MetricTechnique(numerator=_measure("QTY"), execution_source="NONE")
    assert reconcileExecutableVsStructured(t).verdict == VERDICT_PASS


def test_layer3_execution_source_compiled_is_not_a_finding() -> None:
    """P1 无编译器 ⇒ 判不了就不判。"""
    t = MetricTechnique(numerator=_measure("QTY"), execution_source="COMPILED")
    assert reconcileExecutableVsStructured(t).verdict == VERDICT_PASS


def test_layer3_escape_hatch_missing_column_is_mismatch() -> None:
    t = MetricTechnique(
        numerator=_measure("QTY"),
        formula="SELECT SUM(OTHER) FROM T",
        execution_source="ESCAPE_HATCH",
    )
    r = reconcileExecutableVsStructured(t)
    assert r.verdict == VERDICT_MISMATCH
    assert r.reasons() == (REASON_EXECUTABLE_COLUMN_MISSING,)
    assert r.findings[0].layer == LAYER_EXECUTABLE


def test_layer3_escape_hatch_match_is_pass() -> None:
    t = MetricTechnique(
        numerator=_measure("QTY"),
        formula="SELECT SUM(QTY) FROM T",
        execution_source="ESCAPE_HATCH",
    )
    assert reconcileExecutableVsStructured(t).verdict == VERDICT_PASS


# ── 合并 ─────────────────────────────────────────────────────────────


def test_merge_mismatch_beats_unknown() -> None:
    g = GovernanceSpec(numerator_columns=("QTY", "EXTRA"))
    t = MetricTechnique(numerator=_measure("QTY"), grain_keys=())
    r = reconcileMetric(g, t)
    assert r.verdict == VERDICT_MISMATCH


def test_merge_unknown_when_only_governance_empty() -> None:
    r = reconcileMetric(GovernanceSpec(), MetricTechnique())
    assert r.verdict == VERDICT_UNKNOWN


def test_merge_pass_when_everything_aligned() -> None:
    r = reconcileMetric(_fullGovernance(), _fullTechnique())
    assert r.verdict == VERDICT_PASS
    assert r.findings == ()
    assert r.isPass is True


def test_merge_collects_all_layer_findings() -> None:
    g = GovernanceSpec(numerator_columns=("QTY", "EXTRA"))
    t = MetricTechnique(
        numerator=_measure("QTY"),
        formula="SELECT SUM(OTHER) FROM T",
        execution_source="ESCAPE_HATCH",
    )
    r = reconcileMetric(g, t)
    layers = {f.layer for f in r.findings}
    assert LAYER_TECHNIQUE in layers
    assert LAYER_GOVERNANCE in layers
    assert LAYER_EXECUTABLE in layers


def test_merge_layer1_mismatch_does_not_block_injection() -> None:
    """层 1（技术内部）mismatch **不阻断注入**，只标 degraded（spec §6.1）。"""
    g = GovernanceSpec(numerator_columns=("QTY",), grain_keys=("plant_code",))
    t = MetricTechnique(
        metric_type="MEASURE",
        numerator=_measure("QTY"),
        grain_keys=("plant_code",),
        formula="SELECT SUM(OTHER) FROM T",
        execution_source="NONE",
    )
    r = reconcileMetric(g, t)
    assert r.verdict == VERDICT_PASS
    assert r.degraded is True
    assert LAYER_TECHNIQUE in {f.layer for f in r.findings}
    assert r.isTierAEligible is False


def test_merge_layer3_mismatch_degrades_without_blocking() -> None:
    """层 3（可执行⇄结构化）mismatch 同样只降级、不阻断（spec §6.1）。"""
    g = GovernanceSpec(numerator_columns=("QTY",), grain_keys=("plant_code",))
    t = MetricTechnique(
        metric_type="MEASURE",
        numerator=_measure("QTY"),
        grain_keys=("plant_code",),
        formula="SELECT SUM(OTHER) FROM T",
        execution_source="ESCAPE_HATCH",
    )
    r = reconcileMetric(g, t)
    assert r.verdict == VERDICT_PASS
    assert r.degraded is True
    assert LAYER_EXECUTABLE in {f.layer for f in r.findings}


def test_merge_clean_is_not_degraded_and_tier_a_eligible() -> None:
    r = reconcileMetric(_fullGovernance(), _fullTechnique())
    assert r.verdict == VERDICT_PASS
    assert r.degraded is False
    assert r.isTierAEligible is True


def test_result_is_immutable() -> None:
    r = reconcileMetric(GovernanceSpec(), MetricTechnique())
    with pytest.raises(FrozenInstanceError):
        r.verdict = "pass"  # type: ignore[misc]
