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


# =============================================================================
# P4a：formula 含 {time_scope} 占位符 ⇒ time_column 必填（反向不强制）
# =============================================================================


async def test_create_metric_with_placeholder_without_time_column_rejected(client: AsyncClient) -> None:
    payload = {
        **_METRIC_PAYLOAD,
        "formula": "SELECT COUNT(*) FROM ORDERS WHERE {time_scope}",
        "executionSource": "ESCAPE_HATCH",
        # 无 timeColumn
    }
    resp = await client.post("/api/v1/ontology/metrics", json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code == 422, resp.text


async def test_create_metric_with_placeholder_and_time_column_ok(client: AsyncClient) -> None:
    payload = {
        **_METRIC_PAYLOAD,
        "formula": "SELECT COUNT(*) FROM ORDERS WHERE {time_scope}",
        "executionSource": "ESCAPE_HATCH",
        "timeColumn": "INVOICE_DATE",
    }
    resp = await client.post("/api/v1/ontology/metrics", json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code == 201, resp.text


async def test_update_metric_to_placeholder_without_column_rejected(client: AsyncClient) -> None:
    """partial update：formula 改为含占位符、time_column 未传且原值空 ⇒ 422。"""
    metric_id = await _createMetric(client)  # 现有 helper，formula 无占位符
    resp = await client.put(
        f"/api/v1/ontology/metrics/{metric_id}",
        json={"formula": "SELECT COUNT(*) FROM ORDERS WHERE {time_scope}"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422, resp.text


async def test_update_metric_placeholder_with_existing_column_ok(client: AsyncClient) -> None:
    """先建（占位符+列），再 update 别的合法字段 ⇒ 正常（列已满足约束）。"""
    payload = {
        **_METRIC_PAYLOAD,
        "formula": "SELECT COUNT(*) FROM ORDERS WHERE {time_scope}",
        "executionSource": "ESCAPE_HATCH",
        "timeColumn": "INVOICE_DATE",
    }
    resp = await client.post("/api/v1/ontology/metrics", json=payload, headers=ADMIN_HEADERS)
    metric_id = resp.json()["id"]
    resp = await client.put(
        f"/api/v1/ontology/metrics/{metric_id}",
        json={"metricAlias": "别名更新"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
