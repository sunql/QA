"""I3 `quotation_compare`：年度报价的**确定性**归一化比价（设计稿 §5.2 / §8.6）。

设计稿把「币种/税率/单位归一 + 偏差解释」列为确定性评分服务——**不让 LLM
算价格**。本模块就是那个服务，纯函数、可复算、零 LLM。

归一化口径（**2026-11-22 连 THBI Oracle 实测 2026 年度 50,599 行后确认**）：

================  ====================================================
维度              口径
================  ====================================================
币种              `currency_code` 列（实测全库仅 CNY）；多币种出现前不引入汇率
税                **必须按 `price_list_code` 过滤**——源表有 4 个报价单，
                  同一列 `UNIT_PRICE_EX_TAX` 在不同报价单下含义不同：
                  T21/T11 = 「不含税价」、T20/T10 = 「含税价」
                  ⇒ 混着比会拿**含税价当未税价**比
单位              `uom` 列（实测 20 种：KG/UN/SET/TAI…）；实测**同物料多 UOM = 0**
                  ⇒ 同一物料单位一致，但**跨物料不可比**
去重              实测 6,270 组 (物料,供应商,报价单,价) 完全重复 ⇒ 先去重
                  （否则重复行会扭曲中位价与供应商家数）
比较窗口          同 `ITEM_CODE` × 同 `SEG_YEAR` × 同 `price_list_code` 横向比价
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
from typing import Any
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: 归一化口径常量（可随业务变更，但每次变更都要更新本模块文档与用例）。
CURRENCY = "CNY"
PRICE_BASIS = "EX_TAX"

#: 「不含税价」报价单（实测 2026 年度：T21=不含税价(地点)、T11=不含税价）。
#: ⚠️ **不能只看列名** —— 同一列 `UNIT_PRICE_EX_TAX` 在 T20/T10（含税价）
#: 下装的是**含税价**；不按报价单过滤就会拿含税价当未税价比。
EX_TAX_PRICE_LIST_CODES = frozenset({"T21", "T11"})

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
    #: 报价单号（实测 T21=不含税价 / T20=含税价 ⇒ 比价必须按它分组）
    priceListCode: str = ""
    #: 计量单位（实测 20 种）
    uom: str = ""
    #: 币种（实测全库 CNY）
    currency: str = CURRENCY


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
    """同一个 (物料, 年度, 报价单, 单位) 的比价组。

    分组键带 ``priceListCode`` 与 ``uom``：**含税价与不含税价不可混比**，
    **不同单位不可比**。
    """

    itemCode: str
    segmentYear: int
    currency: str
    priceBasis: str
    lowestPrice: Decimal
    medianPrice: Decimal
    spreadPct: Decimal
    bestSupplierCode: str
    rows: tuple[CompareRow, ...]
    priceListCode: str = ""
    uom: str = ""


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
    priceListCodes: frozenset[str] | None = EX_TAX_PRICE_LIST_CODES,
) -> list[CompareGroup]:
    """报价集合 → 比价矩阵（确定性排序）。

    三步（都是 2026-11-22 真数据逼出来的）：

    1. **按报价单过滤**（默认只要「不含税价」T21/T11）——同一列在不同报价单
       下装的是含税价/不含税价，混着比会得出错误结论；
    2. **去重**（实测 6,270 组完全重复行）——重复行会扭曲中位价与供应商家数；
    3. **按 (物料, 年度, 报价单, 单位) 分组**——含税/不含税不可混比，
       不同单位不可比。

    ``priceListCodes=None`` 表示不过滤（调用方明确知道自己在比什么时用）。
    """
    if priceListCodes is not None:
        allowed = frozenset(priceListCodes)
        quotes = [q for q in quotes if not q.priceListCode or q.priceListCode in allowed]
    # 去重：同一 (物料, 年度, 报价单, 单位, 供应商, 价格) 只算一条
    unique: dict[tuple, QuotationQuote] = {}
    for quote in quotes:
        key = (
            quote.itemCode,
            quote.segmentYear,
            quote.priceListCode,
            quote.uom,
            quote.supplierCode,
            quote.unitPriceExTax,
        )
        unique.setdefault(key, quote)
    deduped = list(unique.values())

    grouped: dict[tuple[str, int, str, str], list[QuotationQuote]] = {}
    for quote in deduped:
        key = (quote.itemCode, quote.segmentYear, quote.priceListCode, quote.uom)
        grouped.setdefault(key, []).append(quote)

    groups: list[CompareGroup] = []
    for (itemCode, year, priceListCode, uom) in sorted(grouped):
        rowsQuotes = sorted(grouped[(itemCode, year, priceListCode, uom)], key=lambda q: q.supplierCode)
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
                currency=rowsQuotes[0].currency or CURRENCY,
                priceBasis=PRICE_BASIS,
                lowestPrice=lowest,
                medianPrice=median,
                spreadPct=_pct(max(prices) - lowest, lowest),
                bestSupplierCode=best.supplierCode,
                rows=rows,
                priceListCode=priceListCode,
                uom=uom,
            )
        )
    return groups


def _col(row: dict, name: str) -> Any:
    """**大小写不敏感**取列。

    ⚠️ Oracle 的 ``cursor.description`` 返回大写标识符，而
    ``business_db_pool`` 统一**小写化**（:698）⇒ 大写 key 取值永远是 None
    （2026-11-22 实测：既有 ``quotation_query`` / ``purchase_history`` 两个
    工具因此在生产上从没返回过真实数据）。
    """
    if name in row:
        return row[name]
    lowered = name.lower()
    for key, value in row.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


def quotesFromRows(rows: Iterable[dict]) -> list[QuotationQuote]:
    """业务库行 → 归一化报价；价格非法的行丢弃（不猜）。"""
    quotes: list[QuotationQuote] = []
    for row in rows:
        price = toDecimal(_col(row, "UNIT_PRICE_EX_TAX"))
        supplier = str(_col(row, "SUPPLIER_CODE") or "").strip()
        item = str(_col(row, "ITEM_CODE") or "").strip()
        year = _col(row, "SEG_YEAR")
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
                priceListCode=str(_col(row, "PRICE_LIST_CODE") or "").strip(),
                uom=str(_col(row, "UOM") or "").strip(),
                currency=str(_col(row, "CURRENCY_CODE") or CURRENCY).strip(),
            )
        )
    return quotes
