"""KPI 契约块渲染（比率型文案正反断言是核心 —— 防辛普森悖论）。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services.kpi_contract_builder import (
    ContractEntry,
    SourceClassRef,
    ratioSqlGuidance,
    renderContractBlock,
    renderContractEntry,
)


def _ratioEntry(order: str) -> ContractEntry:
    allowed, forbidden = ratioSqlGuidance(order)  # type: ignore[misc]
    return ContractEntry(
        kpi_code="KPI_SUPPLIER_OTD",
        kpi_name="供应商准时交付率",
        verdict="pass",
        numerator="按期到货订单数",
        denominator="应到货订单数",
        metric_name="准时交付率",
        metric_type="RATIO",
        aggregation_order=order,
        allowed_sql_shape=allowed,
        forbidden_sql_shape=forbidden,
    )


def test_weighted_ratio_allows_sum_over_sum() -> None:
    text = renderContractEntry(_ratioEntry("WEIGHTED_RATIO"))
    assert "SUM(分子)/SUM(分母)" in text
    assert "禁止：AVG(分子/分母)" in text


def test_weighted_ratio_forbids_avg_of_ratios() -> None:
    """防假绿：必须断言**禁止**形态也出现在文案里（否则模型仍可能写错）。"""
    text = renderContractEntry(_ratioEntry("WEIGHTED_RATIO"))
    assert "允许且仅允许的 SQL 形态：SUM(分子)/SUM(分母)" in text


def test_avg_of_ratios_allows_avg_form() -> None:
    text = renderContractEntry(_ratioEntry("AVG_OF_RATIOS"))
    assert "允许且仅允许的 SQL 形态：AVG(分子/分母)" in text
    assert "禁止：SUM(分子)/SUM(分母)" in text


def test_two_ratio_orders_render_differently() -> None:
    """两种聚合阶的文案不得相同（写反了就是辛普森悖论）。"""
    weighted = renderContractEntry(_ratioEntry("WEIGHTED_RATIO"))
    avg = renderContractEntry(_ratioEntry("AVG_OF_RATIOS"))
    assert weighted != avg
    assert "SUM(分子)/SUM(分母)" not in avg.split("禁止")[0]


def test_ratio_guidance_unknown_order_returns_none() -> None:
    assert ratioSqlGuidance("NONE") is None
    assert ratioSqlGuidance("") is None
    assert ratioSqlGuidance(None) is None


def test_ratio_guidance_only_for_ratio_metric_type() -> None:
    """MEASURE 型指标即使残留 aggregation_order 也不发比率指引。"""
    assert ratioSqlGuidance("WEIGHTED_RATIO", "RATIO") is not None
    assert ratioSqlGuidance("WEIGHTED_RATIO", "MEASURE") is None


def test_render_entry_includes_governance_five_tuple() -> None:
    entry = ContractEntry(
        kpi_code="KPI_A",
        kpi_name="指标A",
        verdict="unknown",
        business_definition="按期到货订单数 / 应到货订单数",
        numerator="按期到货订单数",
        denominator="应到货订单数",
        grain="供应商 × 工厂 × 月",
        unit="%",
        owner="张三",
    )
    text = renderContractEntry(entry)
    for expected in ("按期到货订单数", "应到货订单数", "供应商 × 工厂 × 月", "%", "张三"):
        assert expected in text


def test_render_entry_marks_unknown_verdict() -> None:
    entry = ContractEntry(
        kpi_code="KPI_A", kpi_name="指标A", verdict="unknown"
    )
    assert "unknown" in renderContractEntry(entry)


def test_render_entry_lists_source_classes_with_roles() -> None:
    entry = ContractEntry(
        kpi_code="KPI_A",
        kpi_name="指标A",
        verdict="pass",
        source_classes=(
            SourceClassRef("DWD_INCOMING_INSPECTION", "DWD_INCOMING_INSPECTION", "NUMERATOR"),
            SourceClassRef("DWD_SUPPLIER", "DWD_SUPPLIER", "DENOMINATOR"),
        ),
    )
    text = renderContractEntry(entry)
    assert "DWD_INCOMING_INSPECTION（NUMERATOR）" in text
    assert "DWD_SUPPLIER（DENOMINATOR）" in text


def test_render_entry_shows_source_table_when_differs() -> None:
    """源表名与别名不同时要出现（表名是 NL2SQL 落地依据）。"""
    entry = ContractEntry(
        kpi_code="KPI_A",
        kpi_name="指标A",
        verdict="pass",
        source_classes=(
            SourceClassRef("DWD_INSPECTION", "dwd_inspection_t", "NUMERATOR"),
        ),
    )
    assert "DWD_INSPECTION（NUMERATOR）→dwd_inspection_t" in renderContractEntry(entry)


def test_render_block_is_empty_for_no_entries() -> None:
    assert renderContractBlock(()) == ""


def test_render_block_contains_all_entries() -> None:
    block = renderContractBlock(
        (
            ContractEntry(kpi_code="KPI_A", kpi_name="指标A", verdict="pass"),
            ContractEntry(kpi_code="KPI_B", kpi_name="指标B", verdict="pass"),
        )
    )
    assert "KPI_A" in block
    assert "KPI_B" in block
    assert "【KPI 口径契约】" in block


def test_contract_entry_is_immutable() -> None:
    entry = ContractEntry(kpi_code="KPI_A", kpi_name="指标A", verdict="pass")
    with pytest.raises(FrozenInstanceError):
        entry.verdict = "mismatch"  # type: ignore[misc]
