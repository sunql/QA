"""到货跟踪 handler 单测：结果装配 + 供应商检索 + 工具装配（假适配器）。

口径（spec 5.3 / 5.7）：
- `summary` / `bySupplier` 的三态必须**互斥且穷尽**：三态之和 == 总订单数
  （订购量 ≤ 0 且未收货的行会让「全到货」与「未到货」同时命中 ⇒ 到货率 > 100%，
  本项目已知风险点 ⇒ 装配层显式校验，不自洽就 fail-closed）；
- 率值 = 分子 / 分母 × 100，保留 2 位小数；分母 0 ⇒ 0；
- 供应商检索复用 `Supplier360Service._resolveSupplier` 四段阶梯
  （编码 → 企业键 → 全名 → 简称），多命中 / 未命中都由它抛 `NotFoundError`，
  **绝不静默丢弃**用户给的供应商。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.exceptions import NotFoundError, ValidationError
from app.services import delivery_tracking_query as mod
from app.services.ontology_binding_resolver import ResolvedBinding

#: 本期实测汇总行（spec 5.3 探针基线：订单 4,844 / 全到 3,986 / 部分 448 / 未到 410）。
_SUMMARY_ROW = {
    "TOTAL_LINES": 19908,
    "TOTAL_ORDERS": 4844,
    "ORDERED_QTY": 98495265.0,
    "RECEIVED_QTY": 39904579.0,
    "ALL_ARRIVED": 3986,
    "PARTIAL": 448,
    "NOT_ARRIVED": 410,
    "EXCLUDED_ROWS": 0,
}


def _orderBinding() -> ResolvedBinding:
    """订单行类绑定（handler 的必需属性全给）。"""
    return ResolvedBinding(
        className=mod.ORDER_CLASS_NAME,
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_PURCHASE_ORDER_DTL",
        columns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "PO_QTY": "PO_QTY",
            "ORDER_DATE": "ORDER_DATE",
            "SUPPLIER_CODE": "SUPPLIER_CODE",
            "ITEM_CODE": "ITEM_CODE",
            "EXP_RCV_DATE": "EXP_RCV_DATE",
        },
    )


def _receiptBinding() -> ResolvedBinding:
    """收货行类绑定（含 `ITEM_DESC` / `RCV_DATE`：收货聚合 SELECT 需要）。"""
    return ResolvedBinding(
        className=mod.RECEIPT_CLASS_NAME,
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_GOODS_RECEIPT_DTL",
        columns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "RCV_QTY_STU": "RCV_QTY_STU",
            "RCV_DATE": "RCV_DATE",
            "ITEM_DESC": "ITEM_DESC",
        },
    )


def _orderBindingWithSupplierName() -> ResolvedBinding:
    """订单行类绑定 + **可选** `SUPPLIER_NAME`（spec 5.7 的 ERP 名回退来源）。"""
    order = _orderBinding()
    return ResolvedBinding(
        className=order.className,
        datasourceId=order.datasourceId,
        schemaName=order.schemaName,
        sourceTable=order.sourceTable,
        columns={**dict(order.columns), "SUPPLIER_NAME": "SUPPLIER_NAME"},
    )


class _FakeCtx:
    """最小 AgentToolContext 替身（本工具不读 ctx）。"""

    datasource = None
    userId = "smoke_p4"


def _supplierRow() -> dict:
    return {
        "SUPPLIER_CODE": "B125",
        "ERP_SUPPLIER_NAME": "力航-ERP 名",
        "TOTAL_ORDERS": 29,
        "ALL_ARRIVED": 1,
        "PARTIAL": 16,
        "NOT_ARRIVED": 12,
        "ORDERED_QTY": 15155100.0,
        "RECEIVED_QTY": 2176026.0,
        "EXCLUDED_ROWS": 0,
    }


def _supplierRowFallback() -> dict:
    """MDM 无行的供应商（只靠 SQL 的 ERP 名回退，spec 5.7）。"""
    return {
        "SUPPLIER_CODE": "C1",
        "ERP_SUPPLIER_NAME": "宁波备用件-ERP",
        "TOTAL_ORDERS": 1,
        "ALL_ARRIVED": 0,
        "PARTIAL": 0,
        "NOT_ARRIVED": 1,
        "ORDERED_QTY": 50.0,
        "RECEIVED_QTY": 0.0,
        "EXCLUDED_ROWS": 0,
    }


def _orderRows() -> list[dict]:
    return [
        {
            "PO_NO": "T22611POH0223",
            "PO_LINE_NO": 3000,
            "SUPPLIER_CODE": "B125",
            "ERP_SUPPLIER_NAME": "力航-ERP 名",
            "ORDER_DATE": "2026-10-28",
            "MATERIAL_CODE": "5A2100000002",
            "MATERIAL_DESC": "kun10/kun30/N1/N2方形焊接螺母M6",
            "ORDER_QTY": 1047000.0,
            "RECEIVED_QTY": 118261.0,
            "OPEN_QTY": 928739.0,
            "EXPECTED_DATE": "2026-11-07",
            "RECEIPT_DATE": "2026-11-24",
        }
    ]


# ---------------------------------------------------------------- 结果装配


def test_assemble_summary_computes_rates_and_states() -> None:
    """订单到货率 = 全到/总订单；量级率 = 收到/订购；脏行数原样透出。"""
    summary = mod._assembleSummary({**_SUMMARY_ROW, "EXCLUDED_ROWS": 6})

    assert summary == {
        "totalLines": 19908,
        "totalOrders": 4844,
        "orderedQty": 98495265.0,
        "receivedQty": 39904579.0,
        "orderArrivalRate": 82.29,
        "qtyArrivalRate": 40.51,
        "allArrivedOrders": 3986,
        "partialArrivedOrders": 448,
        "notArrivedOrders": 410,
        "excludedRows": 6,
    }


def test_assemble_summary_handles_zero_denominators() -> None:
    """空窗口 ⇒ 率值 0，不抛异常。"""
    summary = mod._assembleSummary(
        {
            "TOTAL_LINES": 0,
            "TOTAL_ORDERS": 0,
            "ORDERED_QTY": 0.0,
            "RECEIVED_QTY": 0.0,
            "ALL_ARRIVED": 0,
            "PARTIAL": 0,
            "NOT_ARRIVED": 0,
        }
    )

    assert summary["orderArrivalRate"] == 0.0
    assert summary["qtyArrivalRate"] == 0.0


def test_assemble_summary_is_case_insensitive_for_adapter_rows() -> None:
    """adapter 把 Oracle 列名小写化 ⇒ 装配必须大小写不敏感（否则全取 None）。"""
    lowered = {key.lower(): value for key, value in _SUMMARY_ROW.items()}

    summary = mod._assembleSummary(lowered)

    assert summary["totalOrders"] == 4844
    assert summary["allArrivedOrders"] == 3986
    assert summary["orderArrivalRate"] == 82.29


def test_assemble_summary_rejects_overlapping_states() -> None:
    """三态重叠（重复计数）⇒ fail-closed，不返回 >100% 的到货率。

    订购量 ≤ 0 且未收货的订单会同时落入「全到货」与「未到货」——
    这是本项目已知风险点，装配层必须挡住（而不是把错数给前端）。
    """
    with pytest.raises(mod.DeliveryTrackingInvariantError, match="三态计数不自洽"):
        mod._assembleSummary(
            {
                "TOTAL_ORDERS": 100,
                "ALL_ARRIVED": 60,
                "PARTIAL": 20,
                "NOT_ARRIVED": 30,
                "ORDERED_QTY": 1.0,
                "RECEIVED_QTY": 1.0,
            }
        )


def test_assemble_summary_accepts_states_summing_to_total() -> None:
    """互斥且穷尽的三态 ⇒ 正常返回（守住上面的校验不误杀）。"""
    summary = mod._assembleSummary(_SUMMARY_ROW)

    assert (
        summary["allArrivedOrders"] + summary["partialArrivedOrders"] + summary["notArrivedOrders"]
        == summary["totalOrders"]
    )


def test_order_status_from_lines() -> None:
    """行级 → 订单三态（欠交 > 0 且无收货 = 未到；有收货 = 部分；无欠交 = 全到）。"""
    assert mod._orderStatus([{"openQty": 0, "receivedQty": 5}]) == "all"
    assert mod._orderStatus([{"openQty": 3, "receivedQty": 5}]) == "partial"
    assert mod._orderStatus([{"openQty": 9, "receivedQty": 0}]) == "notArrived"


def test_order_status_treats_zero_qty_no_receipt_as_all_arrived() -> None:
    """订购量 ≤ 0 且未收货 ⇒ 「无欠交」= 全到，绝不重复算「未到」."""
    assert mod._orderStatus([{"openQty": 0, "receivedQty": 0}]) == "all"
    assert mod._orderStatus([{"openQty": -5, "receivedQty": 0}]) == "all"


def test_assemble_supplier_computes_rates_and_reads_mdm_fields() -> None:
    """供应商分组：率值 + MDM 名称/企业键（由 handler 注入行内）。"""
    supplier = mod._assembleSupplier(
        {
            **_supplierRow(),
            "SUPPLIER_NAME": "浙江力航汽车部件有限公司",
            "ENTERPRISE_KEY": 195779358766766,
        }
    )

    assert supplier["supplierCode"] == "B125"
    assert supplier["supplierName"] == "浙江力航汽车部件有限公司"
    assert supplier["enterpriseKey"] == 195779358766766
    assert supplier["totalOrders"] == 29
    assert supplier["orderArrivalRate"] == 3.45
    assert supplier["qtyArrivalRate"] == 14.36
    assert (
        supplier["allArrivedOrders"] + supplier["partialArrivedOrders"] + supplier["notArrivedOrders"]
        == supplier["totalOrders"]
    )


def test_assemble_supplier_without_mdm_row_has_null_name() -> None:
    """MDM 无对应行 ⇒ 名称为 None（不编造 ERP 名）。"""
    supplier = mod._assembleSupplier(_supplierRow())

    assert supplier["supplierName"] is None
    assert supplier["enterpriseKey"] is None


def test_assemble_orders_groups_lines_and_derives_status() -> None:
    """明细行 → 订单级：按 PO 聚合、状态来自行、日期/物料描述透出。"""
    rows = [
        {
            "PO_NO": "T1",
            "PO_LINE_NO": 3000,
            "SUPPLIER_CODE": "B125",
            "SUPPLIER_NAME": "浙江力航汽车部件有限公司",
            "ORDER_DATE": date(2026, 10, 28),
            "MATERIAL_CODE": "5A2100000002",
            "MATERIAL_DESC": "方形焊接螺母M6",
            "ORDER_QTY": 100.0,
            "RECEIVED_QTY": 40.0,
            "OPEN_QTY": 60.0,
            "EXPECTED_DATE": "2026-11-07",
            "RECEIPT_DATE": "2026-11-24",
        },
        {
            "PO_NO": "T1",
            "PO_LINE_NO": 3010,
            "SUPPLIER_CODE": "B125",
            "SUPPLIER_NAME": "浙江力航汽车部件有限公司",
            "ORDER_DATE": date(2026, 10, 28),
            "MATERIAL_CODE": "5A2100000003",
            "MATERIAL_DESC": "螺栓",
            "ORDER_QTY": 10.0,
            "RECEIVED_QTY": 10.0,
            "OPEN_QTY": 0.0,
            "EXPECTED_DATE": None,
            "RECEIPT_DATE": None,
        },
        {
            "PO_NO": "T2",
            "PO_LINE_NO": 1,
            "SUPPLIER_CODE": "C1",
            "ORDER_DATE": "2026-11-01",
            "MATERIAL_CODE": "M1",
            "MATERIAL_DESC": "垫片",
            "ORDER_QTY": 50.0,
            "RECEIVED_QTY": 0.0,
            "OPEN_QTY": 50.0,
            "EXPECTED_DATE": "2026-11-20",
            "RECEIPT_DATE": None,
        },
    ]

    orders = mod._assembleOrders(rows)

    assert [order["poNo"] for order in orders] == ["T1", "T2"]
    first = orders[0]
    assert first["supplierCode"] == "B125"
    assert first["supplierName"] == "浙江力航汽车部件有限公司"
    assert first["orderDate"] == "2026-10-28"
    assert first["status"] == "partial"
    assert first["totalLines"] == 2
    assert first["arrivedLines"] == 2
    assert first["notArrivedLines"] == 0
    assert [line["status"] for line in first["lines"]] == ["partial", "all"]
    assert first["lines"][0]["materialDesc"] == "方形焊接螺母M6"
    assert first["lines"][0]["expectedDate"] == "2026-11-07"
    assert first["lines"][0]["lastReceiptDate"] == "2026-11-24"
    assert first["lines"][1]["lastReceiptDate"] is None
    assert orders[1]["status"] == "notArrived"
    assert orders[1]["orderDate"] == "2026-11-01"


def test_assemble_orders_does_not_mutate_input_rows() -> None:
    """全局不可变约束：装配不得原地改 adapter 返回的行。"""
    rows = _orderRows()
    snapshot = [dict(row) for row in rows]

    mod._assembleOrders(rows)

    assert rows == snapshot


def _dirtyLineRow(poNo: str = "A", poLine: int = 2) -> dict:
    """脏行：欠交量 ≥ 1e6（与 SQL `_dirtyCondition` 同阈值）。"""
    return {
        "PO_NO": poNo,
        "PO_LINE_NO": poLine,
        "SUPPLIER_CODE": "B125",
        "ORDER_QTY": 1_200_000.0,
        "RECEIVED_QTY": 0.0,
        "OPEN_QTY": 1_200_000.0,
    }


def test_assemble_orders_excludes_dirty_lines_from_order_metrics() -> None:
    """订单级口径与 summary 对齐：脏行只留在 lines[]，不进三态/行数/数量。

    「正常行已全收 + 脏行未收」的订单：summary 计为全到货，byOrder 必须也是
    `all`（旧装配会因脏行判成 `notArrived` —— 同一订单两个结论）。
    """
    rows = [
        {
            "PO_NO": "A",
            "PO_LINE_NO": 1,
            "SUPPLIER_CODE": "B125",
            "ORDER_QTY": 100.0,
            "RECEIVED_QTY": 100.0,
            "OPEN_QTY": 0.0,
        },
        _dirtyLineRow("A", 2),
        {
            "PO_NO": "B",
            "PO_LINE_NO": 1,
            "SUPPLIER_CODE": "C1",
            "ORDER_QTY": 50.0,
            "RECEIVED_QTY": 0.0,
            "OPEN_QTY": 50.0,
        },
    ]
    summary = mod._assembleSummary(
        {
            "TOTAL_LINES": 2,
            "TOTAL_ORDERS": 2,
            "ORDERED_QTY": 150.0,
            "RECEIVED_QTY": 100.0,
            "ALL_ARRIVED": 1,
            "PARTIAL": 0,
            "NOT_ARRIVED": 1,
            "EXCLUDED_ROWS": 1,
        }
    )

    orders = mod._assembleOrders(rows)

    assert [order["poNo"] for order in orders] == ["A", "B"]
    first, second = orders
    # A：正常行全收 ⇒ 全到货（与 summary 的 ALL_ARRIVED 一致）
    assert first["status"] == "all"
    assert first["totalLines"] == 1
    assert first["arrivedLines"] == 1
    assert first["notArrivedLines"] == 0
    assert first["orderedQty"] == 100.0
    assert first["receivedQty"] == 100.0
    # 脏行仍在明细里（供人核查）+ 显式标记
    assert len(first["lines"]) == 2
    assert [line["isDirty"] for line in first["lines"]] == [False, True]
    assert first["hasDirtyLines"] is True
    # B：未到货，与 summary 的 NOT_ARRIVED 一致
    assert second["status"] == "notArrived"
    assert second["hasDirtyLines"] is False
    # 与 summary 的对齐：行数与三态
    assert sum(order["totalLines"] for order in orders) == summary["totalLines"]
    assert sum(1 for order in orders if order["status"] == "all") == summary["allArrivedOrders"]
    assert sum(1 for order in orders if order["status"] == "notArrived") == summary["notArrivedOrders"]


def test_assemble_orders_status_uses_sql_line_predicates_not_quantity_sum() -> None:
    """一正一负的两行不得抵消：SQL 判「有欠交行 + 有收货行」⇒ partial。"""
    rows = [
        {
            "PO_NO": "A",
            "PO_LINE_NO": 1,
            "SUPPLIER_CODE": "B125",
            "ORDER_QTY": 100.0,
            "RECEIVED_QTY": 250.0,
            "OPEN_QTY": -150.0,
        },
        {
            "PO_NO": "A",
            "PO_LINE_NO": 2,
            "SUPPLIER_CODE": "B125",
            "ORDER_QTY": 100.0,
            "RECEIVED_QTY": 0.0,
            "OPEN_QTY": 100.0,
        },
    ]

    order = mod._assembleOrders(rows)[0]

    # 若按 sum(openQty) = -50 判「全到」就与 SQL 的 OPEN_LINES>0 口径分叉
    assert order["status"] == "partial"


def test_assemble_orders_all_dirty_has_zero_valid_lines() -> None:
    """全脏行订单不进任何订单级统计（status 保持缺省，hasDirtyLines 为真）。"""
    order = mod._assembleOrders([_dirtyLineRow("A", 1), _dirtyLineRow("A", 2)])[0]

    assert order["totalLines"] == 0
    assert order["orderedQty"] == 0.0
    assert order["receivedQty"] == 0.0
    assert order["hasDirtyLines"] is True
    assert len(order["lines"]) == 2


def test_with_supplier_directory_prefers_mdm_then_erp_then_none() -> None:
    """名称优先级：MDM 名 > ERP 名 > None（spec 5.7）。"""
    rows = [
        {"SUPPLIER_CODE": "B125", "ERP_SUPPLIER_NAME": "B125-ERP"},
        {"SUPPLIER_CODE": "C1", "ERP_SUPPLIER_NAME": "C1-ERP"},
        {"SUPPLIER_CODE": "D9"},
    ]

    enriched = mod._withSupplierDirectory(rows, {"B125": {"name": "MDM 全名", "enterpriseKey": 7}})

    assert enriched[0]["SUPPLIER_NAME"] == "MDM 全名"
    assert enriched[1]["SUPPLIER_NAME"] == "C1-ERP"
    assert enriched[2]["SUPPLIER_NAME"] is None
    assert enriched[0]["ENTERPRISE_KEY"] == 7
    # 不可变：不往 adapter 返回的行里塞字段
    assert "SUPPLIER_NAME" not in rows[0]


# ---------------------------------------------------------------- 供应商检索


@pytest.mark.asyncio
async def test_resolve_supplier_keys_supports_name_and_enterprise_key(monkeypatch) -> None:
    """全名/简称/企业键 → 业务码；解析阶梯复用 supplier_360 的既有实现。"""
    mapping = {"钢创": "A002", "195779358766766": "B125", "B125": "B125"}

    async def _fakeResolveSupplier(session, key):
        return (1, mapping[str(key)], "名称")

    monkeypatch.setattr(mod, "_resolveSupplierKey", _fakeResolveSupplier)
    codes = await mod._resolveSupplierCodes(None, ("钢创", "195779358766766", "B125"))

    assert codes == ("A002", "B125")


@pytest.mark.asyncio
async def test_resolve_supplier_keys_dedupes_preserving_order(monkeypatch) -> None:
    """不同写法解析到同一业务码 ⇒ 去重且保留首次出现顺序。"""
    mapping = {"B125": "B125", "浙江力航汽车部件有限公司": "B125", "A002": "A002"}

    async def _fakeResolveSupplier(session, key):
        return (1, mapping[str(key)], "名称")

    monkeypatch.setattr(mod, "_resolveSupplierKey", _fakeResolveSupplier)
    codes = await mod._resolveSupplierCodes(None, ("B125", "A002", "浙江力航汽车部件有限公司"))

    assert codes == ("B125", "A002")


@pytest.mark.asyncio
async def test_resolve_supplier_keys_raises_validation_error_on_unknown(monkeypatch) -> None:
    """解析失败 ⇒ ValidationError（422）；与未知物料同类，不再落到误导性的 404。"""

    async def _fakeResolveSupplier(session, key):
        raise NotFoundError(f"未找到供应商：{key}")

    monkeypatch.setattr(mod, "_resolveSupplierKey", _fakeResolveSupplier)
    with pytest.raises(ValidationError, match="未找到供应商：查无此家"):
        await mod._resolveSupplierCodes(None, ("查无此家",))


# ---------------------------------------------------------------- handler 装配


@pytest.mark.asyncio
async def test_handler_returns_structured_payload(monkeypatch) -> None:
    """handler 返回 summary/bySupplier/byOrder 三段结构 + window + 数据层标识。"""
    captured: dict = {}

    async def _fakeResolve(session, *, className, requiredProperties, datasourceId=1):
        captured.setdefault("classes", []).append(className)
        captured.setdefault("required", {})[className] = tuple(requiredProperties)
        return _orderBinding() if className == mod.ORDER_CLASS_NAME else _receiptBinding()

    async def _fakeQuery(sql: str):
        captured.setdefault("sqls", []).append(sql)
        if "TOTAL_LINES" in sql:
            return [dict(_SUMMARY_ROW)]
        if "GROUP BY S.SUPPLIER_CODE" in sql:
            return [_supplierRow(), _supplierRowFallback()]
        if "AS TOTAL_ORDERS" in sql:
            return [{"TOTAL_ORDERS": 1}]
        return _orderRows()

    class _FakeAdapter:
        async def execute_read_only(self, sql: str):
            return await _fakeQuery(sql)

    async def _fakeResolveSupplier(session, key):
        return (1, {"钢创": "A002", "B125": "B125"}[str(key)], "名称")

    async def _fakeDirectory(session, *, entityType, codes):
        captured["directoryCodes"] = codes
        captured["directoryEntityType"] = entityType
        return {
            "B125": {
                "name": "浙江力航汽车部件有限公司",
                "enterpriseKey": 195779358766766,
            }
        }

    async def _fakeLoadDataSource(session, datasourceId):
        return {"id": datasourceId}

    monkeypatch.setattr(mod, "resolveBinding", _fakeResolve)
    monkeypatch.setattr(mod, "_resolveSupplierKey", _fakeResolveSupplier)
    monkeypatch.setattr(mod, "_loadActiveDataSource", _fakeLoadDataSource)
    monkeypatch.setattr(mod, "_getAdapter", lambda datasourceId, ds: _FakeAdapter())
    monkeypatch.setattr(mod, "loadMdmDirectory", _fakeDirectory)

    result = await mod.deliveryTrackingHandler(
        None,
        {"anchorDay": 24, "periodIndex": 0, "limit": 50, "supplierKeys": ["钢创"], "orderKeys": [123]},
        _FakeCtx(),
    )
    data = result.data

    expectedWindow = mod.resolveWindow(today=date.today(), anchorDay=24, periodIndex=0)
    assert data["window"] == {
        "dateFrom": expectedWindow.dateFrom.isoformat(),
        "dateTo": expectedWindow.dateTo.isoformat(),
        "mode": "anchorDay",
    }
    assert data["via"] == "business_db"
    assert data["datasourceId"] == 1
    assert data["dataLayer"] == "thbi_ods_erp"
    assert data["summary"]["orderArrivalRate"] == 82.29
    assert data["summary"]["excludedRows"] == 0
    assert (
        data["summary"]["allArrivedOrders"]
        + data["summary"]["partialArrivedOrders"]
        + data["summary"]["notArrivedOrders"]
        == data["summary"]["totalOrders"]
    )
    assert data["bySupplier"][0]["supplierCode"] == "B125"
    assert data["bySupplier"][0]["supplierName"] == "浙江力航汽车部件有限公司"
    assert data["bySupplier"][0]["enterpriseKey"] == 195779358766766
    # MDM 无行 ⇒ 回退 SQL 带出的 ERP 名（spec 5.7）
    assert data["bySupplier"][1]["supplierCode"] == "C1"
    assert data["bySupplier"][1]["supplierName"] == "宁波备用件-ERP"
    assert data["bySupplier"][1]["enterpriseKey"] is None
    assert data["byOrder"][0]["status"] == "partial"
    assert data["byOrder"][0]["orderDate"] == "2026-10-28"
    assert data["byOrder"][0]["supplierName"] == "浙江力航汽车部件有限公司"
    assert data["byOrder"][0]["hasDirtyLines"] is False
    # 本体绑定：两段解析、顺序固定，收货侧必需属性含聚合所需的日期/物料描述
    assert captured["classes"] == [mod.ORDER_CLASS_NAME, mod.RECEIPT_CLASS_NAME]
    assert {"RCV_DATE", "ITEM_DESC"} <= set(captured["required"][mod.RECEIPT_CLASS_NAME])
    # MDM 名称按实际出现的供应商编码取一次（去重保序；与待到货共用同一读取函数）
    assert captured["directoryCodes"] == ("B125", "C1")
    assert captured["directoryEntityType"] == "SUPPLIER"
    # 订单总数 == 样本数 ⇒ 未截断
    assert data["ordersTruncated"] is False
    # 供应商解析后的业务码进 SQL；数字订单号先转字符串（`_inClause` 拒非 str）
    byOrderSql = next(sql for sql in captured["sqls"] if "ORDER BY OPEN_QTY DESC" in sql)
    assert "O.SUPPLIER_CODE IN ('A002')" in byOrderSql
    assert "O.PO_NO IN ('123')" in byOrderSql


@pytest.mark.asyncio
async def test_handler_marks_orders_truncated_when_sample_is_smaller(monkeypatch) -> None:
    """I1：订单总数 > 明细样本数 ⇒ `ordersTruncated=True`（前端不得当全量计数）。"""

    async def _fakeResolve(session, *, className, requiredProperties, datasourceId=1):
        return _orderBinding() if className == mod.ORDER_CLASS_NAME else _receiptBinding()

    class _Adapter:
        async def execute_read_only(self, sql: str):
            if "TOTAL_LINES" in sql:
                return [dict(_SUMMARY_ROW)]
            if "GROUP BY S.SUPPLIER_CODE" in sql:
                return [_supplierRow()]
            if "AS TOTAL_ORDERS" in sql:
                return [{"TOTAL_ORDERS": 99}]
            return _orderRows()

    async def _fakeDirectory(session, *, entityType, codes):
        return {}

    async def _fakeLoadDataSource(session, datasourceId):
        return {"id": datasourceId}

    monkeypatch.setattr(mod, "resolveBinding", _fakeResolve)
    monkeypatch.setattr(mod, "_loadActiveDataSource", _fakeLoadDataSource)
    monkeypatch.setattr(mod, "_getAdapter", lambda i, ds: _Adapter())
    monkeypatch.setattr(mod, "loadMdmDirectory", _fakeDirectory)

    result = await mod.deliveryTrackingHandler(None, {"monthsBack": 12}, _FakeCtx())

    assert result.data["ordersTruncated"] is True


@pytest.mark.asyncio
async def test_handler_rejects_caller_specified_datasource() -> None:
    """THBI 硬规则：数据源不是工具参数 ⇒ 显式拒绝，不静默忽略。"""
    with pytest.raises(ValidationError, match="不接受调用方指定数据源"):
        await mod.deliveryTrackingHandler(None, {"datasourceId": 2}, _FakeCtx())


@pytest.mark.asyncio
async def test_handler_rejects_non_integer_limit() -> None:
    with pytest.raises(ValidationError, match="limit 必须是整数"):
        await mod.deliveryTrackingHandler(None, {"limit": "50"}, _FakeCtx())


@pytest.mark.asyncio
async def test_handler_rejects_scalar_supplier_keys() -> None:
    """供应商列表必须是列表/字符串；数字等标量 fail-closed（否则会被逐字拆开）。"""
    with pytest.raises(ValidationError, match="supplierKeys 必须是字符串列表"):
        await mod.deliveryTrackingHandler(None, {"supplierKeys": 123}, _FakeCtx())


@pytest.mark.asyncio
async def test_handler_fails_closed_on_unknown_supplier(monkeypatch) -> None:
    """用户给了查不到的供应商 ⇒ 整个调用失败（不是静默查全部/丢一个）。"""

    async def _fakeResolveSupplier(session, key):
        raise NotFoundError(f"未找到供应商：{key}")

    monkeypatch.setattr(mod, "_resolveSupplierKey", _fakeResolveSupplier)
    with pytest.raises(ValidationError, match="未找到供应商：查无此家"):
        await mod.deliveryTrackingHandler(None, {"supplierKeys": ["查无此家"]}, _FakeCtx())


# ---------------------------------------------------------------- 可选 ERP 名（SQL）


def test_by_supplier_sql_carries_optional_erp_supplier_name() -> None:
    """ERP 名回退（spec 5.7）：`SUPPLIER_NAME` 已绑定 ⇒ 内层聚合 + 外层带出。"""
    sql = mod.buildDeliveryTrackingBySupplierSql(
        order=_orderBindingWithSupplierName(),
        receipt=_receiptBinding(),
        window=mod.resolveWindow(today=date(2026, 11, 25), monthsBack=12),
    )

    assert "MAX(O.SUPPLIER_NAME) AS ERP_SUPPLIER_NAME" in sql
    assert "MAX(S.ERP_SUPPLIER_NAME) AS ERP_SUPPLIER_NAME" in sql


def test_by_order_sql_carries_optional_erp_supplier_name() -> None:
    """明细行也带 ERP 名（MDM 无行时按订单展示回退名）。"""
    sql = mod.buildDeliveryTrackingByOrderSql(
        order=_orderBindingWithSupplierName(),
        receipt=_receiptBinding(),
        window=mod.resolveWindow(today=date(2026, 11, 25), monthsBack=12),
    )

    assert "O.SUPPLIER_NAME AS ERP_SUPPLIER_NAME" in sql


def test_erp_supplier_name_is_optional_and_absent_when_unbound() -> None:
    """`SUPPLIER_NAME` 未绑定 ⇒ 三段 SQL 都不出现该列（不 fail-closed）。"""
    order, receipt = _orderBinding(), _receiptBinding()
    window = mod.resolveWindow(today=date(2026, 11, 25), monthsBack=12)

    sqls = {
        "summary": mod.buildDeliveryTrackingSummarySql(order=order, receipt=receipt, window=window),
        "bySupplier": mod.buildDeliveryTrackingBySupplierSql(order=order, receipt=receipt, window=window),
        "byOrder": mod.buildDeliveryTrackingByOrderSql(order=order, receipt=receipt, window=window),
    }

    for name, sql in sqls.items():
        assert "ERP_SUPPLIER_NAME" not in sql, name
