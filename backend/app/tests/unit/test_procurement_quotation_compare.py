"""`quotation_compare` 确定性比价的纯函数与 handler 守卫。

为什么必须确定性：价格结论**不能由 LLM 算**。这里钉住四件事——
归一化（非法价格不猜成 0）、分组口径（同物料同年度）、偏差标记（相对中位价）、
以及「0 豁免 / 排序稳定」（否则同一份数据两次跑出不同结论）。
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.procurement_quotation_compare import (
    FLAG_ABOVE,
    FLAG_BELOW,
    FLAG_NORMAL,
    QuotationQuote,
    buildComparison,
    flagFor,
    quotesFromRows,
    toDecimal,
)


def _q(supplier: str, price: str, *, item: str = "ITM-1", year: int = 2026) -> QuotationQuote:
    return QuotationQuote(
        supplierCode=supplier,
        itemCode=item,
        segmentYear=year,
        unitPriceExTax=Decimal(price),
    )


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_toDecimal_rejects_invalid_without_guessing() -> None:
    assert toDecimal(None) is None
    assert toDecimal("") is None
    assert toDecimal("N/A") is None
    assert toDecimal("12.50") == Decimal("12.50")
    assert toDecimal(Decimal("3.1")) == Decimal("3.1")


def test_quotesFromRows_drops_rows_without_price_or_code() -> None:
    rows = [
        {"SUPPLIER_CODE": "S1", "ITEM_CODE": "I1", "SEG_YEAR": 2026, "UNIT_PRICE_EX_TAX": "10"},
        {"SUPPLIER_CODE": "S2", "ITEM_CODE": "I1", "SEG_YEAR": 2026, "UNIT_PRICE_EX_TAX": None},
        {"SUPPLIER_CODE": "", "ITEM_CODE": "I1", "SEG_YEAR": 2026, "UNIT_PRICE_EX_TAX": "9"},
        {"SUPPLIER_CODE": "S3", "ITEM_CODE": "I1", "SEG_YEAR": "2026", "UNIT_PRICE_EX_TAX": "bad"},
    ]

    quotes = quotesFromRows(rows)

    assert [q.supplierCode for q in quotes] == ["S1"]


# ---------------------------------------------------------------------------
# 偏差标记
# ---------------------------------------------------------------------------


def test_flag_thresholds_are_relative_to_median() -> None:
    low, high = Decimal("0.20"), Decimal("0.30")
    assert flagFor(Decimal("-20.00"), lowThreshold=low, highThreshold=high) == FLAG_BELOW
    assert flagFor(Decimal("-19.99"), lowThreshold=low, highThreshold=high) == FLAG_NORMAL
    assert flagFor(Decimal("30.00"), lowThreshold=low, highThreshold=high) == FLAG_ABOVE
    assert flagFor(Decimal("29.99"), lowThreshold=low, highThreshold=high) == FLAG_NORMAL


# ---------------------------------------------------------------------------
# 比价矩阵
# ---------------------------------------------------------------------------


def test_buildComparison_marks_low_and_high_against_median() -> None:
    groups = buildComparison(_q(s, p) for s, p in [("S1", "70"), ("S2", "100"), ("S3", "140")])

    assert len(groups) == 1
    group = groups[0]
    assert group.currency == "CNY" and group.priceBasis == "EX_TAX"
    assert group.lowestPrice == Decimal("70")
    assert group.medianPrice == Decimal("100")
    assert group.bestSupplierCode == "S1"
    flags = {row.supplierCode: row.flag for row in group.rows}
    assert flags["S1"] == FLAG_BELOW  # -30% vs median
    assert flags["S2"] == FLAG_NORMAL
    assert flags["S3"] == FLAG_ABOVE  # +40% vs median


def test_buildComparison_groups_by_item_and_year_with_stable_order() -> None:
    groups = buildComparison(
        [
            _q("S2", "10", item="ITM-B", year=2026),
            _q("S1", "10", item="ITM-A", year=2026),
            _q("S1", "12", item="ITM-A", year=2025),
        ]
    )

    assert [(g.itemCode, g.segmentYear) for g in groups] == [
        ("ITM-A", 2025),
        ("ITM-A", 2026),
        ("ITM-B", 2026),
    ]
    # 同价并列 ⇒ 取 supplierCode 最小者（确定性，不是「谁先来」）
    tieGroup = groups[1]
    assert tieGroup.bestSupplierCode == "S1"
    assert tieGroup.spreadPct == Decimal("0.00")


def test_buildComparison_single_quote_has_no_spread() -> None:
    group = buildComparison([_q("S1", "88")])[0]

    assert group.spreadPct == Decimal("0.00")
    assert group.rows[0].deltaVsMinPct == Decimal("0.00")
    assert group.rows[0].flag == FLAG_NORMAL


def test_buildComparison_empty_input() -> None:
    assert buildComparison([]) == []


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


def test_buildQuotationCompareSql_dedupes_and_caps_items() -> None:
    from app.services.agent_tools_procurement import buildQuotationCompareSql

    items = ["A", "A", "B", "C", "D", "E"] + [f"X{i}" for i in range(30)]
    sql = buildQuotationCompareSql(items, 2026)

    assert "SEG_YEAR = 2026" in sql
    assert "'A'" in sql and sql.count("'A'") == 1
    # 上限 20 个
    assert sql.count("'") == 20 * 2


@pytest.mark.asyncio
async def test_quotation_compare_handler_returns_groups(monkeypatch) -> None:
    from app.services import agent_tools_procurement as mod

    class _Adapter:
        async def execute_read_only(self, sql: str):
            return [
                {"SUPPLIER_CODE": "S1", "ITEM_CODE": "ITM-1", "SEG_YEAR": 2026, "UNIT_PRICE_EX_TAX": "80"},
                {"SUPPLIER_CODE": "S2", "ITEM_CODE": "ITM-1", "SEG_YEAR": 2026, "UNIT_PRICE_EX_TAX": "120"},
            ]

    async def _fakeResolve(session, ctx):
        return SimpleNamespace(id=2, is_active=True)

    monkeypatch.setattr(mod, "_resolveBusinessDatasource", _fakeResolve)
    monkeypatch.setattr(mod, "_ensureActive", lambda ds: None)
    monkeypatch.setattr(mod, "_adapterFor", lambda dsId, ds: _Adapter())

    result = await mod.quotationCompareHandler(
        None, {"itemKeys": ["ITM-1"], "segmentYear": 2026}, SimpleNamespace()
    )

    assert result.data["currency"] == "CNY"
    group = result.data["groups"][0]
    assert group["bestSupplierCode"] == "S1"
    assert [row["supplierCode"] for row in group["rows"]] == ["S1", "S2"]
    # 价格以字符串出（Decimal 不进 JSON）
    assert group["lowestPrice"] == "80"


@pytest.mark.asyncio
async def test_quotation_compare_handler_requires_inputs() -> None:
    from app.services import agent_tools_procurement as mod

    noItems = await mod.quotationCompareHandler(None, {}, SimpleNamespace())
    assert noItems.data["groups"] == [] and "itemKeys" in noItems.answer

    noYear = await mod.quotationCompareHandler(
        None, {"itemKeys": ["ITM-1"]}, SimpleNamespace()
    )
    assert noYear.data["groups"] == [] and "segmentYear" in noYear.answer
