"""KPI 契约块渲染（比率型文案正反断言是核心 —— 防辛普森悖论）。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.services import kpi_contract_builder as builderModule
from app.services.kpi_contract_builder import (
    KPI_CONTRACT_MAX_FIELD_CHARS,
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


# ── F11：字段 / 整块长度上限（绝不静默截断） ─────────────────────────


def test_render_entry_truncates_long_field_with_explicit_marker() -> None:
    """超长治理文本必须**原位**截断并写明截掉多少字（静默截断会让模型误读口径）。"""
    longText = "甲" * (KPI_CONTRACT_MAX_FIELD_CHARS + 10)
    entry = ContractEntry(
        kpi_code="KPI_LONG",
        kpi_name="长字段指标",
        verdict="pass",
        business_definition=longText,
    )

    text = renderContractEntry(entry)

    assert longText not in text
    assert "…[已截断 10 字]" in text
    assert "甲" * KPI_CONTRACT_MAX_FIELD_CHARS in text


def test_render_entry_short_values_are_unchanged() -> None:
    """正常长度**逐字相等**（截断逻辑不得改写正常文本）。"""
    entry = ContractEntry(
        kpi_code="KPI_SHORT",
        kpi_name="短名",
        verdict="pass",
        business_definition="定义",
    )

    assert renderContractEntry(entry) == (
        "- KPI_SHORT（短名）\n  - 业务定义：定义\n  - 对账状态：pass"
    )


def test_render_block_drops_entries_over_limit_and_notes_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整块超限：丢**整条**后续 entry（不从 markup 中间切），并加尾注写明条数。"""
    entries = tuple(
        ContractEntry(
            kpi_code=f"KPI_{index}",
            kpi_name="长条目",
            verdict="pass",
            business_definition="乙" * 200,
        )
        for index in range(3)
    )
    monkeypatch.setattr(builderModule, "KPI_CONTRACT_MAX_BLOCK_CHARS", 500)

    block = renderContractBlock(entries)

    assert block.count("- KPI_") == 1
    assert "KPI_0" in block
    assert "KPI_1" not in block
    assert "KPI_2" not in block
    assert "[…更多 KPI 契约因长度上限未注入，共 2 条]" in block
