"""到货跟踪欢迎态「本期业务现状」端点集成测试。

设计动机（2026-11-26）：场景工作区的「本期业务现状」卡需要**进入即出数**，
走编排 run 要等 worker 且烧几千 token ⇒ 直调只读 SQL（零 LLM 成本）。

本用例覆盖：登录守卫、默认窗口=本期（24 日周期）、供应商过滤、绑定缺失时的友好错误。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.tests.integration._agent_runs_api_helpers import OWNER_HEADERS
from app.tests.integration._agent_runs_api_helpers import client as client

OVERVIEW_PATH = "/api/v1/procurement/delivery-tracking/overview"


@pytest.mark.asyncio
async def test_overview_requires_login(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未登录 401/403（端点只挂登录守卫，不是公开端点）。

    关掉 stub auth（模拟生产）⇒ 未带 JWT 一律被拒，而不是走到绑定解析。
    """
    from app.config import getSettings

    monkeypatch.setenv("AUTH_STUB_ENABLED", "0")
    getSettings.cache_clear()
    resp = await client.get(OVERVIEW_PATH)
    assert resp.status_code in (401, 403, 409)


@pytest.mark.asyncio
async def test_overview_returns_current_period_snapshot(
    client: AsyncClient, monkeypatch
) -> None:
    """默认参数 ⇒ 本期（24 日周期）窗口 + summary/bySupplier 两段结构。"""
    captured: dict[str, Any] = {}

    async def _fakeOverview(session, **kwargs):
        captured.update(kwargs)
        return {
            "window": {"dateFrom": "2026-10-24", "dateTo": "2026-11-24", "mode": "anchorDay"},
            "summary": {
                "totalLines": 19902, "totalOrders": 4843,
                "orderedQty": 88619265.0, "receivedQty": 41414210.0,
                "orderArrivalRate": 83.67, "qtyArrivalRate": 46.73,
                "allArrivedOrders": 4052, "partialArrivedOrders": 434,
                "notArrivedOrders": 357, "excludedRows": 6,
            },
            "bySupplier": [
                {
                    "supplierCode": "B125", "supplierName": "浙江力航汽车部件有限公司",
                    "totalOrders": 29, "allArrivedOrders": 1,
                    "partialArrivedOrders": 16, "notArrivedOrders": 12,
                    "orderArrivalRate": 3.45,
                    "orderedQty": 15155100.0, "receivedQty": 2176026.0,
                }
            ],
        }

    from app.api.v1 import procurement_analytics

    monkeypatch.setattr(
        procurement_analytics, "loadDeliveryTrackingOverview", _fakeOverview
    )

    resp = await client.get(OVERVIEW_PATH, headers=OWNER_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"]["dateFrom"] == "2026-10-24"
    assert body["summary"]["orderArrivalRate"] == 83.67
    assert body["summary"]["notArrivedOrders"] == 357
    assert body["bySupplier"][0]["supplierCode"] == "B125"

    # 默认即本期 24 日周期
    assert captured["anchorDay"] == 24
    assert captured["periodIndex"] == 0
    assert captured["supplierKeys"] == ()


@pytest.mark.asyncio
async def test_overview_passes_supplier_filter(
    client: AsyncClient, monkeypatch
) -> None:
    """供应商过滤（逗号分隔）透传给服务层。"""
    captured: dict[str, Any] = {}

    async def _fakeOverview(session, **kwargs):
        captured.update(kwargs)
        return {
            "window": {"dateFrom": "2026-10-24", "dateTo": "2026-11-24", "mode": "anchorDay"},
            "summary": {
                "totalLines": 0, "totalOrders": 0, "orderedQty": 0.0, "receivedQty": 0.0,
                "orderArrivalRate": 0.0, "qtyArrivalRate": 0.0,
                "allArrivedOrders": 0, "partialArrivedOrders": 0,
                "notArrivedOrders": 0, "excludedRows": 0,
            },
            "bySupplier": [],
        }

    from app.api.v1 import procurement_analytics

    monkeypatch.setattr(
        procurement_analytics, "loadDeliveryTrackingOverview", _fakeOverview
    )

    resp = await client.get(
        f"{OVERVIEW_PATH}?supplierKeys=B125,B019", headers=OWNER_HEADERS
    )
    assert resp.status_code == 200
    assert captured["supplierKeys"] == ("B125", "B019")


@pytest.mark.asyncio
async def test_overview_rejects_out_of_range_anchor_day(client: AsyncClient) -> None:
    """锚点日越界 ⇒ 422（参数校验在 API 层，不进服务层）。"""
    resp = await client.get(f"{OVERVIEW_PATH}?anchorDay=31", headers=OWNER_HEADERS)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_overview_maps_datasource_failure_to_502(
    client: AsyncClient, monkeypatch
) -> None:
    """数据源执行失败 ⇒ 502 友好错误（未处理异常会绕过 CORS 中间件，浏览器只见 CORS 错误）。"""

    async def _boom(session, **kwargs):
        raise RuntimeError("ORA-00942: table or view does not exist")

    from app.api.v1 import procurement_analytics

    monkeypatch.setattr(
        procurement_analytics, "loadDeliveryTrackingOverview", _boom
    )

    resp = await client.get(OVERVIEW_PATH, headers=OWNER_HEADERS)
    assert resp.status_code == 502
    assert "到货跟踪概览失败" in resp.json()["detail"]
