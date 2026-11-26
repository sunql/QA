"""到货跟踪欢迎态「本期业务现状」概览单测（假适配器）。

**为什么有独立模块**：欢迎态要求「进入即出数」——走编排 run 要等 worker 且烧 token。
本模块直接跑只读 SQL（与 `delivery_tracking_query` 同一套绑定与 SQL 构造）。

本用例钉住三条口径：
1. **窗口 = 本期**（企业 24 日结算周期，`anchorDay=24` + `periodIndex=0`）；
2. **只跑两段聚合**（summary + bySupplier），**不跑**订单明细/订单总数
   —— 那是工具的活，欢迎态不需要（每段聚合都会重跑窗口聚合，成本翻倍）；
3. **供应商名称来自 MDM 目录**（不按 source_system 过滤，见 entity_mapping_names）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.services import delivery_tracking_overview as mod

_SUMMARY_ROW = {
    "TOTAL_LINES": 19902,
    "TOTAL_ORDERS": 4843,
    "ORDERED_QTY": 88619265.0,
    "RECEIVED_QTY": 41414210.0,
    "ALL_ARRIVED": 4052,
    "PARTIAL": 434,
    "NOT_ARRIVED": 357,
    "EXCLUDED_ROWS": 6,
}

_SUPPLIER_ROW = {
    "SUPPLIER_CODE": "B125",
    "TOTAL_ORDERS": 29,
    "ALL_ARRIVED": 1,
    "PARTIAL": 16,
    "NOT_ARRIVED": 12,
    "ORDERED_QTY": 15155100.0,
    "RECEIVED_QTY": 2176026.0,
    "EXCLUDED_ROWS": 0,
}


class _FakeAdapter:
    """最小 adapter 替身：按 SQL 里出现的表名返回对应行。"""

    def __init__(self) -> None:
        self.sqls: list[str] = []
        self.dialect = "oracle"

    async def execute_read_only(self, sql: str) -> list[dict[str, Any]]:
        self.sqls.append(sql)
        if "TOTAL_ORDERS" in sql and "SUPPLIER_CODE" not in sql:
            return [dict(_SUMMARY_ROW)]
        return [dict(_SUPPLIER_ROW)]


class _FakeDatasource:
    id = 1
    is_active = True


@pytest.fixture
def fakeAdapter(monkeypatch: pytest.MonkeyPatch) -> _FakeAdapter:
    adapter = _FakeAdapter()
    monkeypatch.setattr(mod, "_resolveTrackedBindings", _fakeBindings)
    monkeypatch.setattr(mod, "loadActiveDatasource", _fakeDatasource)
    monkeypatch.setattr(mod, "adapterFor", lambda _id, _ds: adapter)
    monkeypatch.setattr(mod, "loadMdmDirectory", _fakeDirectory)
    return adapter


async def _fakeBindings(_session: Any) -> tuple[Any, Any]:
    from app.services.ontology_binding_resolver import ResolvedBinding

    order = ResolvedBinding(
        className="DWD_PURCHASE_ORDER_DTL",
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
    receipt = ResolvedBinding(
        className="DWD_GOODS_RECEIPT_DTL",
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
    return order, receipt


async def _fakeDatasource(_session: Any, _id: int) -> _FakeDatasource:
    return _FakeDatasource()


async def _fakeDirectory(_session: Any, **_kwargs: Any) -> dict[str, dict[str, Any]]:
    return {"B125": {"name": "浙江力航汽车部件有限公司", "enterpriseKey": 123}}


@pytest.mark.asyncio
async def test_overview_uses_current_anchor_period(fakeAdapter: _FakeAdapter) -> None:
    """窗口 = 本期 24 日结算周期（基准日 2026-11-26 ⇒ [10-24, 11-24)）。"""
    result = await mod.loadDeliveryTrackingOverview(
        None,  # type: ignore[arg-type]
        today=date(2026, 11, 26),
    )

    assert result["window"] == {
        "dateFrom": "2026-10-24",
        "dateTo": "2026-11-24",
        "mode": "anchorDay",
    }


@pytest.mark.asyncio
async def test_overview_returns_summary_and_suppliers(fakeAdapter: _FakeAdapter) -> None:
    """summary 三态 + bySupplier（带 MDM 名称）。"""
    result = await mod.loadDeliveryTrackingOverview(
        None,  # type: ignore[arg-type]
        today=date(2026, 11, 26),
    )

    summary = result["summary"]
    assert summary["totalOrders"] == 4843
    assert summary["orderArrivalRate"] == 83.67
    assert summary["allArrivedOrders"] == 4052
    assert summary["excludedRows"] == 6

    assert len(result["bySupplier"]) == 1
    supplier = result["bySupplier"][0]
    assert supplier["supplierCode"] == "B125"
    assert supplier["supplierName"] == "浙江力航汽车部件有限公司"
    assert supplier["notArrivedOrders"] == 12


@pytest.mark.asyncio
async def test_overview_runs_only_two_aggregations(fakeAdapter: _FakeAdapter) -> None:
    """只跑 summary + bySupplier 两段 —— 不跑订单明细/订单总数（成本翻倍且用不上）。"""
    await mod.loadDeliveryTrackingOverview(
        None,  # type: ignore[arg-type]
        today=date(2026, 11, 26),
    )

    assert len(fakeAdapter.sqls) == 2
    joined = "\n".join(fakeAdapter.sqls)
    assert "SUPPLIER_CODE" in joined  # bySupplier 段
    assert "TOTAL_ORDERS" in joined  # summary 段
    # byOrder 段专有的列（明细行粒度）与订单总数段不得出现
    assert "OPEN_QTY" not in joined
    assert "COUNT(*)" not in joined


@pytest.mark.asyncio
async def test_overview_supports_previous_period(fakeAdapter: _FakeAdapter) -> None:
    """periodIndex=-1 ⇒ 上期（[09-24, 10-24)）。"""
    result = await mod.loadDeliveryTrackingOverview(
        None,  # type: ignore[arg-type]
        periodIndex=-1,
        today=date(2026, 11, 26),
    )

    assert result["window"]["dateFrom"] == "2026-09-24"
    assert result["window"]["dateTo"] == "2026-10-24"


@pytest.mark.asyncio
async def test_overview_passes_supplier_filter_to_sql(fakeAdapter: _FakeAdapter) -> None:
    """供应商过滤进 SQL（不是事后过滤）。"""
    await mod.loadDeliveryTrackingOverview(
        None,  # type: ignore[arg-type]
        supplierKeys=("B125",),
        today=date(2026, 11, 26),
    )

    assert any("'B125'" in sql for sql in fakeAdapter.sqls)
