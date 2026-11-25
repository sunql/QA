"""到货跟踪结果装配（2026-11-25，P5 Task 4）。

纯函数：adapter 行 → spec 5.3 的三段结构（camelCase）+ 三态自洽校验。
单独成篇的原因：`delivery_tracking_query`（窗口 + SQL 模板 + handler）已接近
800 行上限；本模块无 IO，可独立单测。

⚠️ 三态必须**互斥且穷尽**（三态之和 == 总订单数）：订购量 ≤ 0 且未收货的
订单会同时命中「全到货」与「未到货」⇒ 到货率 > 100%（本项目已知风险点）；
SQL 侧已用 `OPEN_LINES > 0` 消除重叠，本模块 `_assertStateTotal` 再兜底。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.domain.exceptions import DomainError
from app.services.delivery_tracking_sql import _OPEN_QTY_SANITY_MAX


class DeliveryTrackingInvariantError(DomainError):
    """三段结果自身不自洽（三态重复/漏计数）—— fail-closed，宁可不返回错数。"""


def _col(row: Any, name: str, default: Any = None) -> Any:
    """大小写不敏感取列值。

    adapter 把 Oracle 的 `cursor.description` 大写列名统一小写化
    （`business_db_pool`）⇒ 只按大写 `row.get` 会**永远拿到 None**。
    """
    if not isinstance(row, dict):
        return default
    if name in row:
        return row[name]
    lowered = name.lower()
    for key, value in row.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return default


def _rate(numerator: float, denominator: float) -> float:
    """率值（百分比，2 位小数）；分母为 0 ⇒ 0（不抛）。"""
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100, 2)


def _isoDate(value: Any) -> str | None:
    """date/datetime/字符串 → `YYYY-MM-DD`；None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _assertStateTotal(
    *,
    label: str,
    totalOrders: int,
    allArrived: int,
    partial: int,
    notArrived: int,
) -> None:
    """三态互斥且穷尽校验：和 ≠ 总订单数 ⇒ 重复/漏计数（已知风险点）。

    触发场景：订购量 ≤ 0 且未收货的订单被同时算进「全到货」与「未到货」，
    会让到货率 > 100%。SQL 已用 `OPEN_LINES > 0` 消除该重叠；此处兜底，
    保证坏口径不会以"看起来正常"的数字流到前端。
    """
    counted = allArrived + partial + notArrived
    if counted != totalOrders:
        raise DeliveryTrackingInvariantError(
            f"三态计数不自洽（{label}）：ALL_ARRIVED({allArrived}) + PARTIAL({partial})"
            f" + NOT_ARRIVED({notArrived}) = {counted} != TOTAL_ORDERS({totalOrders})"
        )


def _assembleSummary(row: dict[str, Any]) -> dict[str, Any]:
    """汇总行 → summary 结构（两个率值 + 三态计数 + 剔除行数）。"""
    totalOrders = int(_col(row, "TOTAL_ORDERS") or 0)
    allArrived = int(_col(row, "ALL_ARRIVED") or 0)
    partial = int(_col(row, "PARTIAL") or 0)
    notArrived = int(_col(row, "NOT_ARRIVED") or 0)
    orderedQty = float(_col(row, "ORDERED_QTY") or 0.0)
    receivedQty = float(_col(row, "RECEIVED_QTY") or 0.0)
    _assertStateTotal(
        label="summary",
        totalOrders=totalOrders,
        allArrived=allArrived,
        partial=partial,
        notArrived=notArrived,
    )
    return {
        "totalLines": int(_col(row, "TOTAL_LINES") or 0),
        "totalOrders": totalOrders,
        "orderedQty": orderedQty,
        "receivedQty": receivedQty,
        "orderArrivalRate": _rate(allArrived, totalOrders),
        "qtyArrivalRate": _rate(receivedQty, orderedQty),
        "allArrivedOrders": allArrived,
        "partialArrivedOrders": partial,
        "notArrivedOrders": notArrived,
        "excludedRows": int(_col(row, "EXCLUDED_ROWS") or 0),
    }


def _orderStatus(lines: list[dict[str, Any]]) -> str:
    """行级 → 订单三态：all / partial / notArrived。

    **按行谓词计数**（不按数量求和），与 SQL `_statesSelect` 逐字同口径：
    - 无欠交行（`openQty > 0` 计数为 0）⇒ 全到（含订购量 ≤ 0 且未收货）；
    - 有欠交且收过货（`receivedQty > 0` 行数 > 0）⇒ 部分；
    - 有欠交且一行未收 ⇒ 未到货。
    三支互斥（`_assertStateTotal` 的前提）。

    ⚠️ 不能改成 `sum(openQty) <= 0` 判全到：一正一负的两行（超收 + 欠交）
    会相互抵消 ⇒ 汇总判「部分」、明细判「全到」（口径分叉）。
    """
    openLines = sum(1 for line in lines if float(line.get("openQty") or 0.0) > 0)
    if openLines == 0:
        return "all"
    receivedLines = sum(1 for line in lines if float(line.get("receivedQty") or 0.0) > 0)
    return "partial" if receivedLines > 0 else "notArrived"


def _assembleSupplier(row: dict[str, Any]) -> dict[str, Any]:
    """供应商分组行 → bySupplier 结构（名称/企业键由 handler 从 MDM 注入行内）。"""
    totalOrders = int(_col(row, "TOTAL_ORDERS") or 0)
    allArrived = int(_col(row, "ALL_ARRIVED") or 0)
    partial = int(_col(row, "PARTIAL") or 0)
    notArrived = int(_col(row, "NOT_ARRIVED") or 0)
    orderedQty = float(_col(row, "ORDERED_QTY") or 0.0)
    receivedQty = float(_col(row, "RECEIVED_QTY") or 0.0)
    code = str(_col(row, "SUPPLIER_CODE") or "")
    _assertStateTotal(
        label=f"supplier {code}",
        totalOrders=totalOrders,
        allArrived=allArrived,
        partial=partial,
        notArrived=notArrived,
    )
    return {
        "supplierCode": code,
        "supplierName": _col(row, "SUPPLIER_NAME"),
        "enterpriseKey": _col(row, "ENTERPRISE_KEY"),
        "totalOrders": totalOrders,
        "allArrivedOrders": allArrived,
        "partialArrivedOrders": partial,
        "notArrivedOrders": notArrived,
        "orderArrivalRate": _rate(allArrived, totalOrders),
        "orderedQty": orderedQty,
        "receivedQty": receivedQty,
        "qtyArrivalRate": _rate(receivedQty, orderedQty),
        "excludedRows": int(_col(row, "EXCLUDED_ROWS") or 0),
    }


def _newOrderEntry(poNo: str, row: dict[str, Any]) -> dict[str, Any]:
    """订单级聚合容器（首行的供应商与下单日期；后续行只补空日期）。"""
    return {
        "poNo": poNo,
        "supplierCode": str(_col(row, "SUPPLIER_CODE") or ""),
        "supplierName": _col(row, "SUPPLIER_NAME"),
        "enterpriseKey": _col(row, "ENTERPRISE_KEY"),
        "orderDate": _isoDate(_col(row, "ORDER_DATE")),
        "status": "notArrived",
        "hasDirtyLines": False,
        "totalLines": 0,
        "arrivedLines": 0,
        "notArrivedLines": 0,
        "orderedQty": 0.0,
        "receivedQty": 0.0,
        "lines": [],
    }


def _assembleLine(row: dict[str, Any]) -> dict[str, Any]:
    """单行明细（物料 / 订购 / 已收 / 欠交 / 预期与最后收货日 / 行状态）。

    `isDirty` = 欠交量 ≥ 1e6（与 SQL `_dirtyCondition` 同一阈值）：
    明细**保留**脏行供人核查，但它不进订单级派生值。
    """
    orderedQty = float(_col(row, "ORDER_QTY") or 0.0)
    receivedQty = float(_col(row, "RECEIVED_QTY") or 0.0)
    openQty = float(_col(row, "OPEN_QTY") or 0.0)
    return {
        "poLine": int(_col(row, "PO_LINE_NO") or 0),
        "materialCode": _col(row, "MATERIAL_CODE"),
        "materialDesc": _col(row, "MATERIAL_DESC"),
        "orderedQty": orderedQty,
        "receivedQty": receivedQty,
        "openQty": openQty,
        "expectedDate": _isoDate(_col(row, "EXPECTED_DATE")),
        "lastReceiptDate": _isoDate(_col(row, "RECEIPT_DATE")),
        "status": _orderStatus([{"openQty": openQty, "receivedQty": receivedQty}]),
        "isDirty": openQty >= _OPEN_QTY_SANITY_MAX,
    }


def _aggregateOrderLines(entry: dict[str, Any]) -> None:
    """订单级派生值 —— **只统计非脏行**（与 summary / bySupplier 同口径）。

    脏行留在 `lines[]` 供核查，但不参与 `status` / 行数 / 订购量 / 已收量：
    否则「一条正常行已全收 + 一条脏行未收」的订单会被明细判成未到货，
    而 summary 判成全到货（同一订单两个结论）。
    """
    validLines = [line for line in entry["lines"] if not line["isDirty"]]
    if validLines:
        entry["status"] = _orderStatus(validLines)
    entry["totalLines"] = len(validLines)
    entry["arrivedLines"] = sum(1 for line in validLines if line["receivedQty"] > 0)
    entry["notArrivedLines"] = sum(1 for line in validLines if line["receivedQty"] <= 0)
    entry["orderedQty"] = sum(line["orderedQty"] for line in validLines)
    entry["receivedQty"] = sum(line["receivedQty"] for line in validLines)
    entry["hasDirtyLines"] = any(line["isDirty"] for line in entry["lines"])


def _assembleOrders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """明细行 → 订单级结构（按 PO_NO 聚合 + 行明细；脏行保留供核查）。"""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        poNo = str(_col(row, "PO_NO") or "")
        entry = grouped.setdefault(poNo, _newOrderEntry(poNo, row))
        if entry["orderDate"] is None:
            entry["orderDate"] = _isoDate(_col(row, "ORDER_DATE"))
        entry["lines"].append(_assembleLine(row))
    for entry in grouped.values():
        _aggregateOrderLines(entry)
    return list(grouped.values())


def _supplierCodesOf(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    """结果行里出现的供应商编码（去重保序）—— MDM 名称只需查这些码。"""
    codes: list[str] = []
    for row in rows:
        code = str(_col(row, "SUPPLIER_CODE") or "")
        if code and code not in codes:
            codes.append(code)
    return tuple(codes)


def _withSupplierDirectory(
    rows: list[dict[str, Any]], directory: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """把 MDM 名称/企业键注入结果行（返回新行，不改 adapter 返回对象）。

    spec 5.7：展示以 MDM `name` 为准，**MDM 无行回退 ERP 名**
    （SQL 侧可选属性 `SUPPLIER_NAME` → 行内 `ERP_SUPPLIER_NAME`）；都没有 ⇒ None。
    """
    enriched: list[dict[str, Any]] = []
    for row in rows:
        info = directory.get(str(_col(row, "SUPPLIER_CODE") or ""), {})
        enriched.append(
            {
                **row,
                "SUPPLIER_NAME": info.get("name") or _col(row, "ERP_SUPPLIER_NAME"),
                "ENTERPRISE_KEY": info.get("enterpriseKey"),
            }
        )
    return enriched
