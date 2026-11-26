"""待到货结果行的整形（2026-11-25，文件规模治理）。

只放**纯函数**：给 SQL 行补 MDM 名称、转 camelCase。无 IO、不依赖模块状态，
从 `pending_delivery_query` 拆出后单文件仍可独立单测。
"""

from __future__ import annotations

from typing import Any

from app.services.procurement_query_utils import colValue


def withNames(
    row: dict[str, Any],
    *,
    supplierNames: dict[str, str],
    materialNames: dict[str, str],
) -> dict[str, Any]:
    """给 SQL 行补 MDM 名称（返回新 dict，不原地改）。"""
    enriched = dict(row)
    supplierCode = colValue(row, "SUPPLIER_CODE")
    if supplierCode is not None:
        enriched["SUPPLIER_NAME"] = supplierNames.get(str(supplierCode))
    materialCode = colValue(row, "ITEM_CODE")
    if materialCode is not None:
        enriched["MATERIAL_DESC"] = materialNames.get(str(materialCode))
    return enriched


def camelPendingRow(row: dict[str, Any]) -> dict[str, Any]:
    """欠交明细行 → camelCase（工具 / 明细页共用同一形状）。"""
    expected = colValue(row, "EXPECTED_DATE")
    return {
        "poNo": colValue(row, "PO_NO"),
        "poLine": colValue(row, "PO_LINE_NO"),
        "materialCode": colValue(row, "ITEM_CODE"),
        "materialDesc": colValue(row, "MATERIAL_DESC"),
        "supplierCode": colValue(row, "SUPPLIER_CODE"),
        "supplierName": colValue(row, "SUPPLIER_NAME"),
        "orderedQty": colValue(row, "ORDERED_QTY"),
        "receivedQty": colValue(row, "RECEIVED_QTY"),
        "openQty": colValue(row, "OPEN_QTY"),
        "expectedDate": str(expected) if expected else None,
        "overdueDays": colValue(row, "OVERDUE_DAYS"),
    }
