"""S3 判定 oracle 的单测（真库实跑只覆盖「有偏差」一条路径）。"""

from types import SimpleNamespace

from app.services.kpi_metric_reconcile import (
    REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL,
    REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE,
    MeasureSpec,
)
from scripts.kpi_metric_realdata_s3 import (
    ReconcileOutcome,
    actualColumnCodes,
    anyColumnDiffers,
    expectedColumnCodes,
    formulaCoverage,
    needsDenominatorCheck,
    normTokens,
    roleColumnDiffers,
)


def _measure(expr: str) -> MeasureSpec:
    measure = MeasureSpec.fromRaw({"agg": "SUM", "expr": expr})
    assert measure is not None
    return measure


def _governance(
    numerator: object = ("RECEIPT_DATE",),
    denominator: object = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        numerator_columns=numerator, denominator_columns=denominator
    )


def _technique(
    numerator: object = None,
    denominator: object = None,
    metricType: str = "MEASURE",
) -> SimpleNamespace:
    return SimpleNamespace(
        metric_type=metricType, numerator=numerator, denominator=denominator
    )


def test_norm_tokens_lowercases_and_drops_blanks() -> None:
    assert normTokens([" A ", "b", "", None, "  "]) == {"a", "b"}


def test_norm_tokens_non_sequence_returns_empty_set() -> None:
    for value in (None, "ABC", 42, {"a": 1}):
        assert normTokens(value) == set()


def test_role_column_differs_equal_non_empty_is_false() -> None:
    assert roleColumnDiffers(["QTY"], _measure("qty")) is False


def test_role_column_differs_different_non_empty_is_true() -> None:
    assert roleColumnDiffers(["QTY"], _measure("AMOUNT")) is True


def test_role_column_differs_measure_none_is_false_without_raising() -> None:
    assert roleColumnDiffers(["QTY"], None) is False


def test_role_column_differs_empty_declared_is_false() -> None:
    assert roleColumnDiffers([], _measure("QTY")) is False
    assert roleColumnDiffers(None, _measure("QTY")) is False


def test_needs_denominator_check_ratio_and_difference() -> None:
    ratio = _technique(metricType="RATIO")
    difference = _technique(metricType="DIFFERENCE")
    measure = _technique(metricType="MEASURE")
    assert needsDenominatorCheck(_governance(), ratio) is True
    assert needsDenominatorCheck(_governance(), difference) is True
    assert needsDenominatorCheck(_governance(), measure) is False


def test_needs_denominator_check_honors_declared_denominator() -> None:
    governance = _governance(denominator=("PROMISED_DATE",))
    assert needsDenominatorCheck(governance, _technique()) is True


def test_any_column_differs_numerator_only() -> None:
    governance = _governance(numerator=("RECEIPT_DATE",))
    technique = _technique(numerator=_measure("PROMISED_RECEIPT_DATE"))
    assert anyColumnDiffers(governance, technique) is True


def test_any_column_differs_denominator_only_when_needed() -> None:
    governance = _governance(
        numerator=("RECEIPT_DATE",), denominator=("PROMISED_DATE",)
    )
    ratio = _technique(
        numerator=_measure("RECEIPT_DATE"),
        denominator=_measure("OTHER_DATE"),
        metricType="RATIO",
    )
    assert anyColumnDiffers(governance, ratio) is True
    # MEASURE 型且治理侧未声明分母 ⇒ 分母不参与判定
    noDenominator = _governance(numerator=("RECEIPT_DATE",))
    measure = _technique(
        numerator=_measure("RECEIPT_DATE"), denominator=_measure("OTHER_DATE")
    )
    assert anyColumnDiffers(noDenominator, measure) is False


def test_any_column_differs_aligned_is_false() -> None:
    governance = _governance(numerator=("RECEIPT_DATE",))
    technique = _technique(numerator=_measure("receipt_date"))
    assert anyColumnDiffers(governance, technique) is False


def test_column_code_sets_ignore_non_column_reasons() -> None:
    outcomes = (
        ReconcileOutcome(
            code="KPI_A",
            verdict="mismatch",
            reasons=(
                REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL,
                "grain_mismatch",
            ),
            columnDiffers=True,
        ),
        ReconcileOutcome(
            code="KPI_B",
            verdict="mismatch",
            reasons=(REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE,),
            columnDiffers=True,
        ),
        ReconcileOutcome(
            code="KPI_C",
            verdict="mismatch",
            reasons=("grain_mismatch",),
            columnDiffers=False,
        ),
    )
    assert expectedColumnCodes(outcomes) == {"KPI_A", "KPI_B"}
    assert actualColumnCodes(outcomes) == {"KPI_A", "KPI_B"}


def test_actual_column_codes_detects_missing_column_finding() -> None:
    outcomes = (
        ReconcileOutcome(
            code="KPI_A", verdict="mismatch", reasons=("grain_mismatch",), columnDiffers=True
        ),
    )
    assert expectedColumnCodes(outcomes) == {"KPI_A"}
    assert actualColumnCodes(outcomes) == set()


def test_formula_coverage_reports_missing_columns() -> None:
    # 引擎的标识符抽取与 measureColumnIdentifiers 都归一为小写
    ok, missing = formulaCoverage({"qty", "amount"}, "SUM(QTY)")
    assert ok is False
    assert missing == ["amount"]


def test_formula_coverage_without_structured_measure_is_not_ok() -> None:
    ok, missing = formulaCoverage(set(), "SUM(QTY)")
    assert ok is False
    assert missing
