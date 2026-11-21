"""I3 `quotation_compare`：年度报价的**确定性**归一化比价（设计稿 §5.2 / §8.6）。

设计稿把「币种/税率/单位归一 + 偏差解释」列为确定性评分服务——**不让 LLM
算价格**。本模块就是那个服务，纯函数、可复算、零 LLM。

归一化口径（2026-11-21 实测源表 `DWD_PURCHASE_QUOTATION_YEARLY_DTL` 后确认）：

================  ====================================================
维度              口径
================  ====================================================
币种              全部 CNY（源表无币种列；多币种源出现前不引入汇率）
税                统一用**未税**价 `UNIT_PRICE_EX_TAX`（列名即口径）
单位              同 `ITEM_CODE` 视为同计量单位（源表无 UOM 列）
比较窗口          同 `ITEM_CODE` × 同 `SEG_YEAR` 内横向比价
================  ====================================================

偏差解释（阈值是**参数**，不是写死的业务结论）：
- 比该组中位价低 `lowThreshold`（默认 20%）及以上 ⇒ ``BELOW_MARKET``
  （「异常低价」提示，可能是清库/口径错误，要人工看）
- 比中位价高 `highThreshold`（默认 30%）及以上 ⇒ ``ABOVE_MARKET``
- 其余 ``NORMAL``

`Decimal` 全程使用：价格是钱，不经过 float。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: 归一化口径常量（可随业务变更，但每次变更都要更新本模块文档与用例）。
CURRENCY = "CNY"
PRICE_BASIS = "EX_TAX"

#: 偏差阈值（相对中位价的**比例**）。
DEFAULT_LOW_THRESHOLD = Decimal("0.20")
DEFAULT_HIGH_THRESHOLD = Decimal("0.30")

#: 结果精度（百分比保留 2 位）。
_PCT_QUANT = Decimal("0.01")

FLAG_BELOW = "BELOW_MARKET"
FLAG_ABOVE = "ABOVE_MARKET"
FLAG_NORMAL = "NORMAL"


@dataclass(frozen=True)
class QuotationQuote:
    """一条报价（归一化后的最小事实集）。"""

    supplierCode: str
    itemCode: str
    segmentYear: int
    unitPriceExTax: Decimal


@dataclass(frozen=True)
class CompareRow:
    """比价矩阵中的一行（相对本组中位价 / 最低价）。"""

    itemCode: str
    segmentYear: int
    supplierCode: str
    unitPriceExTax: Decimal
    deltaVsMinPct: Decimal
    deltaVsMedianPct: Decimal
    flag: str


@dataclass(frozen=True)
class CompareGroup:
    """同一个 (物料, 年度) 的比价组。"""

    itemCode: str
    segmentYear: int
    currency: str
    priceBasis: str
    lowestPrice: Decimal
    medianPrice: Decimal
    spreadPct: Decimal
    bestSupplierCode: str
    rows: tuple[CompareRow, ...]


def toDecimal(value: object) -> Decimal | None:
    """价格归一：None / 非法 ⇒ None（**不猜**成 0——0 会变成「最便宜」）。"""
    if value is None:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    """百分比（%），分母为 0 ⇒ 0（同价组不该除零）。"""
    if denominator == 0:
        return Decimal("0")
    return ((numerator / denominator) * 100).quantize(_PCT_QUANT, rounding=ROUND_HALF_UP)


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


def flagFor(deltaVsMedianPct: Decimal, *, lowThreshold: Decimal, highThreshold: Decimal) -> str:
    """按**相对中位价**的偏差给结论标记（阈值是比例，入参用百分比口径换算）。"""
    low = -(lowThreshold * 100)
    high = highThreshold * 100
    if deltaVsMedianPct <= low:
        return FLAG_BELOW
    if deltaVsMedianPct >= high:
        return FLAG_ABOVE
    return FLAG_NORMAL


def buildComparison(
    quotes: Iterable[QuotationQuote],
    *,
    lowThreshold: Decimal = DEFAULT_LOW_THRESHOLD,
    highThreshold: Decimal = DEFAULT_HIGH_THRESHOLD,
) -> list[CompareGroup]:
    """报价集合 → 按 (物料, 年度) 分组的比价矩阵（确定性排序）。"""
    grouped: dict[tuple[str, int], list[QuotationQuote]] = {}
    for quote in quotes:
        grouped.setdefault((quote.itemCode, quote.segmentYear), []).append(quote)

    groups: list[CompareGroup] = []
    for (itemCode, year) in sorted(grouped):
        rowsQuotes = sorted(grouped[(itemCode, year)], key=lambda q: q.supplierCode)
        prices = [q.unitPriceExTax for q in rowsQuotes]
        lowest = min(prices)
        median = _median(prices)
        best = next(q for q in rowsQuotes if q.unitPriceExTax == lowest)
        rows = tuple(
            CompareRow(
                itemCode=itemCode,
                segmentYear=year,
                supplierCode=q.supplierCode,
                unitPriceExTax=q.unitPriceExTax,
                deltaVsMinPct=_pct(q.unitPriceExTax - lowest, lowest),
                deltaVsMedianPct=_pct(q.unitPriceExTax - median, median),
                flag=flagFor(
                    _pct(q.unitPriceExTax - median, median),
                    lowThreshold=lowThreshold,
                    highThreshold=highThreshold,
                ),
            )
            for q in rowsQuotes
        )
        groups.append(
            CompareGroup(
                itemCode=itemCode,
                segmentYear=year,
                currency=CURRENCY,
                priceBasis=PRICE_BASIS,
                lowestPrice=lowest,
                medianPrice=median,
                spreadPct=_pct(max(prices) - lowest, lowest),
                bestSupplierCode=best.supplierCode,
                rows=rows,
            )
        )
    return groups


def quotesFromRows(rows: Iterable[dict]) -> list[QuotationQuote]:
    """业务库行（UPPER_SNAKE 列名）→ 归一化报价；价格非法的行丢弃（不猜）。"""
    quotes: list[QuotationQuote] = []
    for row in rows:
        price = toDecimal(row.get("UNIT_PRICE_EX_TAX"))
        supplier = str(row.get("SUPPLIER_CODE") or "").strip()
        item = str(row.get("ITEM_CODE") or "").strip()
        year = row.get("SEG_YEAR")
        if price is None or not supplier or not item or year is None:
            continue
        try:
            segmentYear = int(year)
        except (TypeError, ValueError):
            continue
        quotes.append(
            QuotationQuote(
                supplierCode=supplier,
                itemCode=item,
                segmentYear=segmentYear,
                unitPriceExTax=price,
            )
        )
    return quotes
