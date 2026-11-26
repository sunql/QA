"""到货跟踪 · 观测体积预算（2026-11-26，P5 Task 12）。

缺陷：I1 修复（订单级分页）后，工具返回 50 单 / 566 行 ⇒ `data` 序列化
**167,038 字符**；编排观测上限是 50,000（`OBSERVATION_CHARS_MAX`，硬上限不可
调高）⇒ 观测被**截断**成不完整 JSON ⇒ 前端 `JSON.parse` 抛错 ⇒ 结果卡
完全不渲染（用户只看到空白）。

修法：**按实际字符预算裁剪**（不猜行数上限 ⇒ 任何数据规模都不会超限）：
- `summary` 恒完整保留；
- `bySupplier` 按未到货订单数降序取前 `MAX_SUPPLIERS` 家；
- `byOrder` 逐单装入（欠交量降序），单内行按剩余预算裁剪；
- 被裁掉的订单/行**显式回报标记**（不静默丢）。
"""

from __future__ import annotations

import json
from typing import Any

from app.services import delivery_tracking_query as mod
from app.services.delivery_tracking_assembly import (
    MATERIAL_DESC_MAX_CHARS,
    MAX_SUPPLIERS,
    OBSERVATION_BUDGET_CHARS,
    fitToObservationBudget,
)


def _chars(value: Any) -> int:
    """JSON 序列化字符数（默认分隔符 ⇒ 比观测用的紧凑格式更保守）。"""
    return len(json.dumps(value))


def _supplier(index: int, *, notArrived: int = 1) -> dict[str, Any]:
    return {
        "supplierCode": f"S{index:03d}",
        "supplierName": f"供应商{index}",
        "enterpriseKey": index,
        "totalOrders": 10,
        "allArrivedOrders": 1,
        "partialArrivedOrders": 2,
        "notArrivedOrders": notArrived,
        "orderArrivalRate": 10.0,
        "orderedQty": 100.0,
        "receivedQty": 10.0,
        "qtyArrivalRate": 10.0,
        "excludedRows": 0,
    }


def _line(index: int, *, descChars: int = 200) -> dict[str, Any]:
    return {
        "poLine": index,
        "materialCode": f"M{index:06d}",
        "materialDesc": "物" * descChars,
        "orderedQty": 1000.0,
        "receivedQty": 100.0,
        "openQty": 900.0,
        "expectedDate": "2026-11-07",
        "lastReceiptDate": "2026-11-24",
        "status": "partial",
        "isDirty": False,
    }


def _order(index: int, *, lineCount: int = 10, descChars: int = 200) -> dict[str, Any]:
    return {
        "poNo": f"PO{index:04d}",
        "supplierCode": f"S{index % 7:03d}",
        "supplierName": f"供应商{index % 7}",
        "enterpriseKey": index % 7,
        "orderDate": "2026-10-28",
        "status": "partial",
        "hasDirtyLines": False,
        "totalLines": lineCount,
        "arrivedLines": 1,
        "notArrivedLines": lineCount - 1,
        "orderedQty": 1000.0 * lineCount,
        "receivedQty": 100.0 * lineCount,
        "lines": [_line(i, descChars=descChars) for i in range(lineCount)],
    }


def _shell() -> dict[str, Any]:
    """不含 bySupplier / byOrder 的固定外壳。"""
    return {
        "via": "business_db",
        "datasourceId": 1,
        "dataLayer": "thbi_dwd",
        "window": {"dateFrom": "2026-10-24", "dateTo": "2026-11-24", "mode": "anchorDay"},
        "summary": {
            "totalLines": 19908,
            "totalOrders": 4844,
            "orderedQty": 98495265.0,
            "receivedQty": 39904579.0,
            "orderArrivalRate": 82.29,
            "qtyArrivalRate": 40.51,
            "allArrivedOrders": 3986,
            "partialArrivedOrders": 448,
            "notArrivedOrders": 410,
            "excludedRows": 0,
        },
    }


def _data(
    *,
    supplierCount: int = 50,
    orderCount: int = 50,
    lineCount: int = 10,
    descChars: int = 200,
) -> dict[str, Any]:
    return {
        **_shell(),
        "bySupplier": [
            _supplier(i, notArrived=supplierCount - i) for i in range(supplierCount)
        ],
        "byOrder": [
            _order(i, lineCount=lineCount, descChars=descChars) for i in range(orderCount)
        ],
    }


# ---------------------------------------------------------------- 预算常量


def test_budget_is_below_observation_hard_limit() -> None:
    """预算必须小于编排观测硬上限（50,000）⇒ 观测永不被截断。"""
    assert OBSERVATION_BUDGET_CHARS == 40_000
    assert OBSERVATION_BUDGET_CHARS < 50_000


# ---------------------------------------------------------------- 超预算数据集


def test_oversized_dataset_is_trimmed_within_budget() -> None:
    """超预算数据集 ⇒ 裁剪后体积 ≤ 预算（任何数据规模都不会超限）。"""
    result = fitToObservationBudget(_data(), totalOrders=4844)

    assert _chars(result) <= OBSERVATION_BUDGET_CHARS


def test_oversized_dataset_reports_truncation_flags() -> None:
    """被裁掉的供应商/订单/行都显式回报标记（不静默丢）。"""
    result = fitToObservationBudget(_data(), totalOrders=4844)

    assert result["suppliersTruncated"] is True
    assert result["ordersTruncated"] is True
    assert len(result["bySupplier"]) == MAX_SUPPLIERS
    assert any(order.get("linesTruncated") is True for order in result["byOrder"])


def test_oversized_dataset_keeps_summary_intact() -> None:
    """summary 是主指标 ⇒ 恒完整保留。"""
    source = _data()
    result = fitToObservationBudget(source, totalOrders=4844)

    assert result["summary"] == source["summary"]


def test_oversized_dataset_keeps_most_outstanding_orders_first() -> None:
    """订单按欠交量降序装入 ⇒ 砍掉的是尾部（欠交量小的），不是随手丢。"""
    source = _data()
    result = fitToObservationBudget(source, totalOrders=4844)

    kept = [order["poNo"] for order in result["byOrder"]]
    assert kept == [order["poNo"] for order in source["byOrder"][: len(kept)]]


def test_oversized_dataset_keeps_suppliers_with_most_not_arrived() -> None:
    """供应商按未到货订单数降序取前 N 家。"""
    source = _data()
    result = fitToObservationBudget(source, totalOrders=4844)

    notArrived = [supplier["notArrivedOrders"] for supplier in result["bySupplier"]]
    assert notArrived == sorted(notArrived, reverse=True)
    assert notArrived[0] == max(
        supplier["notArrivedOrders"] for supplier in source["bySupplier"]
    )


def test_trimming_does_not_mutate_input() -> None:
    """全局不可变约束：裁剪不得原地改入参。"""
    source = _data()
    snapshot = json.dumps(source, ensure_ascii=False, sort_keys=True)

    fitToObservationBudget(source, totalOrders=4844)

    assert json.dumps(source, ensure_ascii=False, sort_keys=True) == snapshot


# ---------------------------------------------------------------- 小数据集


def test_small_dataset_is_not_trimmed() -> None:
    """未超预算 ⇒ 不误裁：数据完整、标记全 false。"""
    source = _data(supplierCount=3, orderCount=2, lineCount=2, descChars=5)

    result = fitToObservationBudget(source, totalOrders=2)

    assert result["bySupplier"] == source["bySupplier"]
    assert result["byOrder"] == source["byOrder"]
    assert result["suppliersTruncated"] is False
    assert result["ordersTruncated"] is False
    assert all("linesTruncated" not in order for order in result["byOrder"])


def test_small_dataset_stays_within_budget() -> None:
    source = _data(supplierCount=3, orderCount=2, lineCount=2, descChars=5)

    result = fitToObservationBudget(source, totalOrders=2)

    assert _chars(result) <= OBSERVATION_BUDGET_CHARS


# ---------------------------------------------------------------- 语义


def test_orders_truncated_reflects_total_orders_not_sample_size() -> None:
    """`ordersTruncated` = 明细不是全量（总数 > 实际展示单数）。"""
    result = fitToObservationBudget(_data(supplierCount=1, orderCount=1, lineCount=1), totalOrders=99)

    assert result["ordersTruncated"] is True


def test_orders_not_truncated_when_all_orders_shown() -> None:
    """展示了全部订单 ⇒ 未截断。"""
    result = fitToObservationBudget(_data(supplierCount=1, orderCount=1, lineCount=1), totalOrders=1)

    assert result["ordersTruncated"] is False


def test_supplier_cap_is_twenty() -> None:
    """供应商分组保留上限 20 家（未到货订单数最多的那批）。"""
    assert MAX_SUPPLIERS == 20


def test_single_giant_order_is_reported_as_line_truncated() -> None:
    """单行超长 ⇒ 裁行并标记（而不是整单丢 silently）。"""
    source = _data(supplierCount=1, orderCount=1, lineCount=200, descChars=4000)

    result = fitToObservationBudget(source, totalOrders=1)

    assert len(result["byOrder"]) == 1
    assert result["byOrder"][0]["linesTruncated"] is True
    assert len(result["byOrder"][0]["lines"]) < 200
    assert _chars(result) <= OBSERVATION_BUDGET_CHARS


def test_line_trimming_keeps_order_level_caliber() -> None:
    """裁行不改订单级口径（三态/行数/数量仍是整单真值，与 summary 一致）。"""
    source = _data(supplierCount=1, orderCount=1, lineCount=200, descChars=4000)

    result = fitToObservationBudget(source, totalOrders=1)

    trimmed = result["byOrder"][0]
    assert trimmed["status"] == source["byOrder"][0]["status"]
    assert trimmed["totalLines"] == source["byOrder"][0]["totalLines"]
    assert trimmed["orderedQty"] == source["byOrder"][0]["orderedQty"]
    # 保留的行仍带全部识别性字段（PO 号 / 物料码 / 数量）
    for line in trimmed["lines"]:
        assert line["poLine"] is not None
        assert line["materialCode"]
        assert line["orderedQty"] is not None


def test_order_not_truncated_when_only_lines_were_trimmed() -> None:
    """只裁了行、没丢单 ⇒ `ordersTruncated` 仍按「订单是否全量」判定。"""
    source = _data(supplierCount=1, orderCount=1, lineCount=200, descChars=4000)

    result = fitToObservationBudget(source, totalOrders=1)

    assert result["ordersTruncated"] is False
    assert result["byOrder"][0]["linesTruncated"] is True


def test_long_material_desc_is_shortened_before_dropping_rows() -> None:
    """预算不足时**先缩描述**（保行数与识别性字段），丢行是最后手段。"""
    source = _data(supplierCount=1, orderCount=1, lineCount=8, descChars=4000)

    result = fitToObservationBudget(source, totalOrders=1)

    kept = result["byOrder"][0]["lines"]
    assert len(kept) == 8  # 缩描述后 8 行都保住了
    for line in kept:
        assert len(line["materialDesc"]) <= MATERIAL_DESC_MAX_CHARS + 1  # 含省略号
        assert line["materialCode"]  # 识别性字段保留
        assert line["orderedQty"] is not None


# ---------------------------------------------------------------- handler 端到端


async def test_handler_payload_stays_within_budget(monkeypatch) -> None:
    """真实 handler（假 adapter 返回大结果）⇒ `data` 体积 ≤ 预算。"""

    async def _fakeResolve(session, *, className, requiredProperties, datasourceId=1):
        from app.services.ontology_binding_resolver import ResolvedBinding

        columns = (
            {
                "PO_NO": "PO_NO",
                "PO_LINE_NO": "PO_LINE_NO",
                "PO_QTY": "PO_QTY",
                "ORDER_DATE": "ORDER_DATE",
                "SUPPLIER_CODE": "SUPPLIER_CODE",
                "ITEM_CODE": "ITEM_CODE",
                "EXP_RCV_DATE": "EXP_RCV_DATE",
            }
            if className == mod.ORDER_CLASS_NAME
            else {
                "PO_NO": "PO_NO",
                "PO_LINE_NO": "PO_LINE_NO",
                "RCV_QTY_STU": "RCV_QTY_STU",
                "ITEM_DESC": "ITEM_DESC",
                "RCV_DATE": "RCV_DATE",
            }
        )
        return ResolvedBinding(
            className=className,
            datasourceId=1,
            schemaName="THBI",
            sourceTable=className,
            columns=columns,
        )

    class _Adapter:
        async def execute_read_only(self, sql: str):
            if "TOTAL_LINES" in sql:
                return [
                    {
                        "TOTAL_LINES": 19908,
                        "TOTAL_ORDERS": 4844,
                        "ORDERED_QTY": 1000.0,
                        "RECEIVED_QTY": 100.0,
                        "ALL_ARRIVED": 3986,
                        "PARTIAL": 448,
                        "NOT_ARRIVED": 410,
                        "EXCLUDED_ROWS": 0,
                    }
                ]
            if "GROUP BY S.SUPPLIER_CODE" in sql:
                return [
                    {
                        "SUPPLIER_CODE": f"S{i:03d}",
                        "TOTAL_ORDERS": 10,
                        "ALL_ARRIVED": 1,
                        "PARTIAL": 2,
                        "NOT_ARRIVED": 7,
                    }
                    for i in range(50)
                ]
            if "AS TOTAL_ORDERS" in sql:
                return [{"TOTAL_ORDERS": 4844}]
            return [
                {
                    "PO_NO": f"PO{po:04d}",
                    "PO_LINE_NO": line,
                    "SUPPLIER_CODE": f"S{po % 7:03d}",
                    "ORDER_DATE": "2026-10-28",
                    "MATERIAL_CODE": f"M{line:06d}",
                    "MATERIAL_DESC": "物" * 200,
                    "ORDER_QTY": 1000.0,
                    "RECEIVED_QTY": 100.0,
                    "OPEN_QTY": 900.0,
                    "EXPECTED_DATE": "2026-11-07",
                    "RECEIPT_DATE": "2026-11-24",
                }
                for po in range(50)
                for line in range(10)
            ]

    async def _fakeDirectory(session, *, entityType, codes):
        return {}

    async def _fakeLoadDataSource(session, datasourceId):
        return {"id": datasourceId}

    monkeypatch.setattr(mod, "resolveBinding", _fakeResolve)
    monkeypatch.setattr(mod, "_loadActiveDataSource", _fakeLoadDataSource)
    monkeypatch.setattr(mod, "_getAdapter", lambda datasourceId, ds: _Adapter())
    monkeypatch.setattr(mod, "loadMdmDirectory", _fakeDirectory)

    result = await mod.deliveryTrackingHandler(None, {"monthsBack": 12}, _FakeCtx())

    assert _chars(result.data) <= OBSERVATION_BUDGET_CHARS
    assert result.data["suppliersTruncated"] is True
    assert result.data["ordersTruncated"] is True


class _FakeCtx:
    """最小 AgentToolContext 替身（本工具不读 ctx）。"""

    datasource = None
    userId = "budget_test"
