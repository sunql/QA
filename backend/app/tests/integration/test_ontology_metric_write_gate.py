"""本体指标写入口 admin 门禁（真实 PG + HTTP 链路）。

背景：`POST/PUT/DELETE /api/v1/ontology/metrics` 原先只要求登录用户，任何账号都能改
指标定义。`ontology_metric` 没有 `object_owner` 列，无法复用 `ontology_class` 的
owner ACL ⇒ 只能按 admin 收口（N3，2026-11-05 缺陷优先级计划 §8）。

读接口（GET 列表 / 详情）保持所有登录用户可用。
"""

from __future__ import annotations

from httpx import AsyncClient

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
USER_HEADERS = {"X-User-Id": "normal-user", "X-User-Roles": "user"}

_METRIC_PAYLOAD = {
    "metricName": "sales_amount",
    "metricAlias": "销售总额",
    "formula": "SUM(order_amount)",
    "aggFunction": "SUM",
}

_METRIC_UPDATE = {"metricAlias": "订单总量", "aggFunction": "SUM"}


async def _createMetric(client: AsyncClient) -> int:
    resp = await client.post(
        "/api/v1/ontology/metrics", json=_METRIC_PAYLOAD, headers=ADMIN_HEADERS
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_non_admin_cannot_create_metric(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/ontology/metrics", json=_METRIC_PAYLOAD, headers=USER_HEADERS
    )

    assert resp.status_code == 403


async def test_non_admin_cannot_update_metric(client: AsyncClient) -> None:
    metricId = await _createMetric(client)

    resp = await client.put(
        f"/api/v1/ontology/metrics/{metricId}",
        json=_METRIC_UPDATE,
        headers=USER_HEADERS,
    )

    assert resp.status_code == 403


async def test_non_admin_cannot_delete_metric(client: AsyncClient) -> None:
    metricId = await _createMetric(client)

    resp = await client.delete(
        f"/api/v1/ontology/metrics/{metricId}", headers=USER_HEADERS
    )

    assert resp.status_code == 403


async def test_non_admin_can_read_metrics(client: AsyncClient) -> None:
    metricId = await _createMetric(client)

    listing = await client.get("/api/v1/ontology/metrics", headers=USER_HEADERS)
    detail = await client.get(
        f"/api/v1/ontology/metrics/{metricId}", headers=USER_HEADERS
    )

    assert listing.status_code == 200
    assert len(listing.json()) == 1
    assert detail.status_code == 200


async def test_admin_can_write_metrics(client: AsyncClient) -> None:
    metricId = await _createMetric(client)

    updated = await client.put(
        f"/api/v1/ontology/metrics/{metricId}",
        json=_METRIC_UPDATE,
        headers=ADMIN_HEADERS,
    )
    deleted = await client.delete(
        f"/api/v1/ontology/metrics/{metricId}", headers=ADMIN_HEADERS
    )

    assert updated.status_code == 200
    assert updated.json()["metricAlias"] == "订单总量"
    assert deleted.status_code == 204
