"""到货跟踪「业务现状」概览（欢迎态统计卡，零 LLM）。

**为什么独立于工具**：欢迎态要求「进入即出数」，走编排 run 要等 worker 且烧 token。
本模块直接跑**只读 SQL**（与 `delivery_tracking_query` 同一套绑定与 SQL 构造），
零 LLM 成本。

**与工具的差别**（刻意）：
- 只跑 `summary` + `bySupplier` 两段聚合，**不跑** `byOrder` / 订单总数
  （那两段各自再跑一遍窗口聚合 ⇒ 成本翻倍；欢迎态不需要明细）；
- 窗口默认**本期**（企业 24 日结算周期，`anchorDay=24` + `periodIndex=0`）
  —— 与「到货跟踪」场景的「本期到货概览」chip 同一口径；
- 不做观测字符预算裁剪（概览体量固定很小）。

数据源固定 THBI（本体绑定唯一确定），物理表列全部经 `resolveBinding` 解析。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.delivery_tracking_assembly import (
    _assembleSummary,
    _assembleSupplier,
    _supplierCodesOf,
    _withSupplierDirectory,
)
from app.services.delivery_tracking_query import (
    _resolveTrackedBindings,
    resolveWindow,
)
from app.services.delivery_tracking_sql import (
    buildDeliveryTrackingBySupplierSql,
    buildDeliveryTrackingSummarySql,
)
from app.services.entity_mapping_names import loadMdmDirectory
from app.services.procurement_datasource import (
    adapterFor,
    loadActiveDatasource,
)
from app.services.procurement_query_utils import SqlDialect

#: 企业默认月度结算周期锚点日（每月 24 日 → 次月 24 日）。
DEFAULT_ANCHOR_DAY = 24

#: 概览卡最多展示多少家供应商（按未到货订单数降序）。
OVERVIEW_SUPPLIER_LIMIT = 20


async def loadDeliveryTrackingOverview(
    session: AsyncSession,
    *,
    anchorDay: int = DEFAULT_ANCHOR_DAY,
    periodIndex: int = 0,
    supplierKeys: tuple[str, ...] = (),
    today: date | None = None,
) -> dict[str, Any]:
    """本期到货达成概览：窗口汇总 + 供应商分组（Top N，按未到货订单数降序）。

    :param anchorDay: 结算周期锚点日（默认 24，企业月度周期）。
    :param periodIndex: 周期期序（0=本期，-1=上期）。
    :param supplierKeys: 供应商编码过滤（空 = 全部）。
    :returns: ``{"window": {...}, "summary": {...}, "bySupplier": [...]}``
    """
    window = resolveWindow(
        today=today or date.today(),
        anchorDay=anchorDay,
        periodIndex=periodIndex,
    )
    order, receipt = await _resolveTrackedBindings(session)
    ds = await loadActiveDatasource(session, order.datasourceId)
    adapter = adapterFor(order.datasourceId, ds)
    dialect = SqlDialect.fromAdapter(adapter).name

    summaryRows = await adapter.execute_read_only(
        buildDeliveryTrackingSummarySql(
            order=order, receipt=receipt, window=window, dialect=dialect
        )
    )
    supplierRows = await adapter.execute_read_only(
        buildDeliveryTrackingBySupplierSql(
            order=order,
            receipt=receipt,
            window=window,
            supplierKeys=supplierKeys,
            limit=OVERVIEW_SUPPLIER_LIMIT,
            dialect=dialect,
        )
    )

    directory = await loadMdmDirectory(
        session, entityType="SUPPLIER", codes=_supplierCodesOf(supplierRows)
    )
    return {
        "window": {
            "dateFrom": window.dateFrom.isoformat(),
            "dateTo": window.dateTo.isoformat(),
            "mode": window.mode,
        },
        "summary": _assembleSummary(summaryRows[0] if summaryRows else {}),
        "bySupplier": [
            _assembleSupplier(row)
            for row in _withSupplierDirectory(supplierRows, directory)
        ],
    }
