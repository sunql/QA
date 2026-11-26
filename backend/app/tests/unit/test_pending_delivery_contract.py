"""待到货 · 到货跟踪跨任务修复的契约测试（C1 / I2 / I3 / I5 / I6 / I7）。

- **C1**：基地绑定按需解析（不按基地过滤 ⇒ 不解析 DIM_FACILITY，缺绑定也能跑）；
- **I2**：两个工具时间窗统一为左闭右开；
- **I3**：脏阈值与收货聚合只有一份实现（`procurement_query_utils`）；
- **I5**：同日缓存 key 必须含 `limit`；
- **I6**：服务端不变式 ⇒ 500；数据源缺失 ⇒ 409；
- **I7**：两个工具的数据源入口收敛到中性模块。
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.domain.exceptions import (
    ConflictError,
    DataSourceInactiveError,
    InternalInvariantError,
    statusForError,
)
from app.services import pending_delivery_query as pending
from app.services.delivery_tracking_assembly import DeliveryTrackingInvariantError
from app.services.delivery_tracking_sql import OPEN_QTY_SANITY_MAX
from app.services.ontology_binding_resolver import ResolvedBinding
from app.services.procurement_query_utils import (
    OPEN_QTY_SANITY_MAX as UTILS_OPEN_QTY_SANITY_MAX,
)
from app.services.procurement_query_utils import (
    receiptAggregate,
)


def _orderBinding() -> ResolvedBinding:
    return ResolvedBinding(
        className=pending.ORDER_CLASS_NAME,
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_PURCHASE_ORDER_DTL",
        columns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "PO_QTY": "PO_QTY",
            "ORDER_DATE": "ORDER_DATE",
            "SUPPLIER_CODE": "SUPPLIER_CODE",
            "PUR_SITE_CODE": "PUR_SITE_CODE",
            "EXP_RCV_DATE": "EXP_RCV_DATE",
            "ITEM_CODE": "ITEM_CODE",
        },
    )


def _receiptBinding() -> ResolvedBinding:
    return ResolvedBinding(
        className=pending.RECEIPT_CLASS_NAME,
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DWD_GOODS_RECEIPT_DTL",
        columns={
            "PO_NO": "PO_NO",
            "PO_LINE_NO": "PO_LINE_NO",
            "RCV_QTY_STU": "RCV_QTY_STU",
            # 到货跟踪的可选明细列（`receiptAggregate` 参数控制是否 SELECT）
            "ITEM_DESC": "ITEM_DESC",
            "RCV_DATE": "RCV_DATE",
        },
    )


def _facilityBinding() -> ResolvedBinding:
    return ResolvedBinding(
        className=pending.FACILITY_CLASS_NAME,
        datasourceId=1,
        schemaName="THBI",
        sourceTable="DIM_FACILITY",
        columns={"FCY_0": "FCY_0", "FCYNAM_0": "FCYNAM_0"},
    )


def _filters(**kwargs: object) -> pending.PendingDeliveryFilters:
    return pending.PendingDeliveryFilters(
        orderBinding=_orderBinding(),
        receiptBinding=_receiptBinding(),
        facilityBinding=_facilityBinding(),
        **kwargs,  # type: ignore[arg-type]
    )


class _FakeAdapter:
    async def execute_read_only(self, sql: str):
        if "EXCLUDED_ROWS" in sql:
            return [{"OPEN_LINES": 0, "OPEN_QTY": 0,
                     "OVERDUE_LINES": 0, "EXCLUDED_ROWS": 0}]
        return []


async def _fakeLoadDatasource(session, datasourceId):
    return SimpleNamespace(id=datasourceId, name="THBI", is_active=True)


# ---------------------------------------------------------------- C1：按需解析基地


@pytest.mark.asyncio
async def test_handler_does_not_resolve_facility_without_site_filter(monkeypatch) -> None:
    """无 siteKeys ⇒ 不解析 DIM_FACILITY；缺该绑定也能跑 summary（降级）。"""
    resolvedClasses: list[str] = []

    async def _fakeResolve(session, *, className, requiredProperties, datasourceId=1):
        resolvedClasses.append(className)
        if className == pending.FACILITY_CLASS_NAME:
            raise AssertionError("无基地过滤时不应解析 DIM_FACILITY")
        return _orderBinding() if className == pending.ORDER_CLASS_NAME else _receiptBinding()

    async def _fakeNames(session, *, entityType, codes):
        return {}

    monkeypatch.setattr(pending, "resolveBinding", _fakeResolve)
    monkeypatch.setattr(pending, "_loadActiveDatasource", _fakeLoadDatasource)
    monkeypatch.setattr(pending, "_adapterFor", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(pending, "loadMdmNames", _fakeNames)
    pending._PENDING_RESULT_CACHE.clear()

    result = await pending.pendingDeliveryHandler(None, {"monthsBack": 12}, SimpleNamespace())

    assert resolvedClasses == [pending.ORDER_CLASS_NAME, pending.RECEIPT_CLASS_NAME]
    assert result.data["summary"]["openLines"] == 0
    pending._PENDING_RESULT_CACHE.clear()


@pytest.mark.asyncio
async def test_handler_resolves_facility_when_site_filter_present(monkeypatch) -> None:
    """有 siteKeys ⇒ 必须解析 DIM_FACILITY（基地名称过滤要用它的物理表）。"""
    resolvedClasses: list[str] = []

    async def _fakeResolve(session, *, className, requiredProperties, datasourceId=1):
        resolvedClasses.append(className)
        return {
            pending.ORDER_CLASS_NAME: _orderBinding(),
            pending.RECEIPT_CLASS_NAME: _receiptBinding(),
            pending.FACILITY_CLASS_NAME: _facilityBinding(),
        }[className]

    async def _fakeNames(session, *, entityType, codes):
        return {}

    monkeypatch.setattr(pending, "resolveBinding", _fakeResolve)
    monkeypatch.setattr(pending, "_loadActiveDatasource", _fakeLoadDatasource)
    monkeypatch.setattr(pending, "_adapterFor", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(pending, "loadMdmNames", _fakeNames)
    pending._PENDING_RESULT_CACHE.clear()

    await pending.pendingDeliveryHandler(
        None, {"monthsBack": 12, "siteKeys": ["T3"]}, SimpleNamespace()
    )

    assert resolvedClasses == [
        pending.ORDER_CLASS_NAME,
        pending.RECEIPT_CLASS_NAME,
        pending.FACILITY_CLASS_NAME,
    ]
    pending._PENDING_RESULT_CACHE.clear()


# ---------------------------------------------------------------- I2：左闭右开


def test_pending_explicit_window_is_left_closed_right_open() -> None:
    sql = _filters(dateFrom="2026-01-01", dateTo="2026-06-30").windowSql(12, date(2026, 11, 25))

    assert "DATE '2026-01-01'" in sql
    assert "< DATE '2026-06-30'" in sql
    assert "<= DATE '2026-06-30'" not in sql, "末日必须排除（与到货跟踪同口径）"


def test_pending_months_back_window_has_upper_bound_today() -> None:
    sql = _filters().windowSql(3, date(2026, 11, 25))

    assert ">= DATE '2026-08-25'" in sql
    assert "< DATE '2026-11-25'" in sql, "monthsBack 也必须排除今天（含未来下单）"


# ---------------------------------------------------------------- I3：口径单份实现


def test_dirty_threshold_is_single_shared_constant() -> None:
    assert OPEN_QTY_SANITY_MAX is UTILS_OPEN_QTY_SANITY_MAX
    assert pending.OPEN_QTY_SANITY_MAX is UTILS_OPEN_QTY_SANITY_MAX


def test_receipt_aggregate_is_single_shared_implementation() -> None:
    assert pending.receiptAggregate is receiptAggregate


def test_receipt_aggregate_supports_optional_detail_columns_and_semi_join() -> None:
    base = receiptAggregate(_receiptBinding())
    detail = receiptAggregate(_receiptBinding(), includeItemDesc=True, includeRcvDate=True)
    windowed = receiptAggregate(
        _receiptBinding(), poNoSubquery="SELECT PO.PO_NO FROM ORDER_T PO"
    )

    assert "ITEM_DESC" not in base and "RECEIPT_DATE" not in base
    assert "MAX(ITEM_DESC) AS ITEM_DESC" in detail
    assert "MAX(RCV_DATE) AS RECEIPT_DATE" in detail
    assert "WHERE PO_NO IN (SELECT PO.PO_NO FROM ORDER_T PO)" in windowed


# ---------------------------------------------------------------- I5：缓存 key 含 limit


def test_pending_cache_key_distinguishes_limit() -> None:
    common = {
        "datasourceId": 1,
        "monthsBack": 12,
        "filters": _filters(),
        "minOverdueDays": None,
        "today": date(2026, 11, 25),
    }

    assert pending._pendingCacheKey(**common, limit=20) != pending._pendingCacheKey(
        **common, limit=50
    )


# ---------------------------------------------------------------- 观测体积（Task 12）


def test_pending_delivery_payload_stays_within_observation_budget() -> None:
    """待到货工具的载荷必须**天然**在观测预算内（无需裁剪）。

    `topLines` 上限 `MAX_LIMIT`=50 行、`bySupplier` 上限 5 家 ⇒ 最坏情况也
    远小于 50,000 字符上限（真实栈实测 limit=50 时 16,298 字符）。
    本测试锁住这个前提：将来放宽 limit 时会先红。
    """
    from app.services.delivery_tracking_assembly import OBSERVATION_BUDGET_CHARS
    from app.services.pending_delivery_rows import camelPendingRow

    rows = [
        camelPendingRow(
            {
                "PO_NO": f"PO{index:04d}",
                "PO_LINE_NO": 3000,
                "ITEM_CODE": f"M{index:06d}",
                "MATERIAL_DESC": "物料描述" * 20,
                "SUPPLIER_CODE": f"S{index % 5:03d}",
                "SUPPLIER_NAME": "供应商名称",
                "ORDER_QTY": 1000.0,
                "RECEIVED_QTY": 100.0,
                "OPEN_QTY": 900.0,
                "EXPECTED_DATE": "2026-11-07",
                "OVERDUE_DAYS": 5,
            }
        )
        for index in range(50)
    ]
    suppliers = [
        {
            "supplierCode": f"S{index:03d}",
            "supplierName": "供应商名称",
            "openLines": 100,
            "openQty": 1000.0,
            "overdueLines": 10,
        }
        for index in range(5)
    ]
    result = pending._buildPendingToolResult(
        datasourceId=1,
        monthsBack=12,
        summary={"openLines": 5000, "openQty": 500000.0, "overdueLines": 500, "excludedRows": 0},
        bySupplier=suppliers,
        lineRows=rows,
    )

    assert len(json.dumps(result.data)) <= OBSERVATION_BUDGET_CHARS


# ---------------------------------------------------------------- I6：状态码


def test_service_invariant_error_maps_to_5xx() -> None:
    assert statusForError(InternalInvariantError("boom")) == 500
    assert statusForError(DeliveryTrackingInvariantError("三态不自洽")) == 500


@pytest.mark.asyncio
async def test_missing_datasource_is_conflict_409_not_404() -> None:
    from app.services.procurement_datasource import loadActiveDatasource

    class _EmptyResult:
        def scalars(self):
            return self

        def first(self):
            return None

    class _Session:
        async def execute(self, stmt):
            return _EmptyResult()

    with pytest.raises(ConflictError):
        await loadActiveDatasource(_Session(), 999)


@pytest.mark.asyncio
async def test_inactive_datasource_is_conflict_409() -> None:
    from app.services.procurement_datasource import loadActiveDatasource

    class _Result:
        def scalars(self):
            return self

        def first(self):
            return SimpleNamespace(id=1, is_active=False)

    class _Session:
        async def execute(self, stmt):
            return _Result()

    with pytest.raises(DataSourceInactiveError):
        await loadActiveDatasource(_Session(), 1)
