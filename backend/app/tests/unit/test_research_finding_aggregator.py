"""research_finding_aggregator：纯函数聚合（R2 Task 1）。

6 个单测覆盖空行 / 维度 topN / MoM / z-score 异常 / 全空 / dimensionHints，
AAA 结构，无 DB 无 LLM 无 IO。
"""

from __future__ import annotations

import pytest

from app.services.research_finding_aggregator import (
    FindingStats,
    aggregateRows,
    buildEnhancedClaim,
)

_EMPTY = FindingStats((), {}, (), (), (), (), 0)


def test_empty_rows_returns_empty_stats_without_raising() -> None:
    stats = aggregateRows([])
    assert isinstance(stats, FindingStats)
    assert stats.rowCount == 0
    assert stats.topContributors == ()
    assert stats.distribution == {}
    assert stats.mom == ()
    assert stats.anomalies == ()
    assert stats.numericColumns == ()
    assert stats.dimensionColumns == ()


def test_dimension_top_n_and_numeric_distribution_share_pct_rounded_1_decimal() -> None:
    rows = [
        {"base": "J1", "amount": 100},
        {"base": "J1", "amount": 50},
        {"base": "T2", "amount": 25},
    ]
    stats = aggregateRows(rows)
    assert stats.rowCount == 3
    assert stats.dimensionColumns == ("base",)
    assert stats.numericColumns == ("amount",)
    top = stats.topContributors[0]
    assert top == {"dim": "base", "value": "J1", "count": 2, "sharePct": 66.7}
    assert stats.distribution["amount"]["min"] == 25.0
    assert stats.distribution["amount"]["max"] == 100.0
    assert stats.distribution["amount"]["mean"] == 58.33
    assert stats.distribution["amount"]["std"] == pytest.approx(38.19, abs=0.01)


def test_mom_delta_calculated_for_monthly_rows() -> None:
    rows = [
        {"month": "2026-01", "cnt": 100},
        {"month": "2026-02", "cnt": 120},
        {"month": "2026-03", "cnt": 90},
        {"month": "2026-04", "cnt": 180},
        {"month": "2026-05", "cnt": 150},
    ]
    stats = aggregateRows(rows)
    assert len(stats.mom) == 4
    # 各 entry 的 deltaPct 正确：100→120 (+20%)，120→90 (-25%)，90→180 (+100%)，180→150 (-16.7%)
    assert stats.mom[0]["period"] == "2026-02" and stats.mom[0]["deltaPct"] == pytest.approx(20.0, abs=0.01)
    assert stats.mom[1]["period"] == "2026-03" and stats.mom[1]["deltaPct"] == pytest.approx(-25.0, abs=0.01)
    assert stats.mom[2]["period"] == "2026-04" and stats.mom[2]["deltaPct"] == pytest.approx(100.0, abs=0.01)
    assert stats.mom[3]["period"] == "2026-05" and stats.mom[3]["deltaPct"] == pytest.approx(-16.7, abs=0.01)


def test_anomaly_detects_z_score_above_2_outlier_row() -> None:
    # 6 行：brief 示例 N=5 在数学上无法触发 z > 2（N=5+1 极端离群上限是
    # 1.79 [sample std] / 2.0 [pop std]），需 N≥6 才稳定触发。
    rows = [
        {"base": "A", "amount": 10},
        {"base": "B", "amount": 11},
        {"base": "C", "amount": 12},
        {"base": "D", "amount": 11},
        {"base": "E", "amount": 13},
        {"base": "F", "amount": 1000},
    ]
    stats = aggregateRows(rows)
    assert len(stats.anomalies) >= 1
    out = next((a for a in stats.anomalies if a["value"] == "F"), None)
    assert out is not None
    assert out["zScore"] > 2


def test_all_none_or_empty_rows_returns_empty_stats_without_raising() -> None:
    rows = [{"a": None, "b": None}, {}, {"a": "", "b": None}]
    stats = aggregateRows(rows)
    assert stats.rowCount == 3
    assert stats.topContributors == ()
    assert stats.distribution == {}
    assert stats.mom == ()
    assert stats.anomalies == ()
    assert stats.numericColumns == ()
    assert stats.dimensionColumns == ()


def test_dimension_hints_overrides_heuristic_treating_numeric_as_dimension() -> None:
    rows = [{"x": i, "y": i * 10} for i in range(5)]
    stats = aggregateRows(rows, dimensionHints=["x"])
    assert stats.dimensionColumns == ("x",)
    assert stats.numericColumns == ("y",)
    assert len(stats.topContributors) == 5
    assert {tc["value"] for tc in stats.topContributors} == {"0", "1", "2", "3", "4"}


def test_build_enhanced_claim_falls_back_to_original_on_empty_stats() -> None:
    assert buildEnhancedClaim(_EMPTY, originalClaim="原 hypothesis") == "原 hypothesis"


def test_build_enhanced_claim_includes_top_share_pct_and_dim() -> None:
    stats = aggregateRows([
        {"base": "J1", "amount": 100},
        {"base": "J1", "amount": 50},
        {"base": "T2", "amount": 25},
    ])
    text = buildEnhancedClaim(stats, originalClaim="原 hypothesis")
    assert "J1" in text and "66.7" in text and "base" in text


def test_asdict_round_trip_preserves_structured_fields() -> None:
    stats = aggregateRows([{"base": "J1", "amount": 100}, {"base": "T2", "amount": 50}])
    out = stats.asdict()
    assert out["rowCount"] == 2
    assert out["topContributors"][0]["value"] == "J1"
    assert "amount" in out["distribution"]
