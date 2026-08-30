"""Phase 4.4 Feature 在线查询 API 集成测试（真实 PG 5433 + 完整 API 链路）。

覆盖：
1. create 定义 -> compute（fake adapter 落值）-> GET /by-name/{name}/values 各过滤组合
2. feature_name 不存在 -> 404；非法名（小写/连字符）-> 422
3. entity_keys 过滤与超限 422
4. valid_at 缺省取最新窗口；显式 valid_at 空窗口返回空 values
5. 无任何值的特征 -> 空 values + 今日占位 valid_at
"""

from __future__ import annotations

from decimal import Decimal
from datetime import date
from itertools import count

from app.api.v1 import features as featuresModule

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

_dsCounter = count(1)

_DATASOURCE_PAYLOAD: dict[str, object] = {
    "name": "feature-query-ds",
    "type": "postgresql",
    "host": "db.example.com",
    "port": 5432,
    "databaseName": "appdb",
    "username": "u",
    "password": "p",
}

_BASE_PAYLOAD: dict[str, object] = {
    "featureName": "SUPPLIER_OTD_3M",
    "featureAlias": "供应商3月准时交付率",
    "entityType": "SUPPLIER",
    "unit": "%",
    "calculationLogic": "SELECT supplier_code AS entity_key, AVG(otd_rate) AS value FROM DWS GROUP BY supplier_code",
}


class _FakeAdapter:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def execute_read_only(self, sql: str) -> list[dict]:
        return self._rows


async def _createTestDatasource(client) -> int:
    payload = dict(_DATASOURCE_PAYLOAD)
    payload["name"] = f"feature-query-ds-{next(_dsCounter)}"
    resp = await client.post("/api/v1/datasources", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _createFeature(client, **overrides) -> str:
    """创建 ACTIVE 特征并触发 compute 落值，返回 feature_name。"""
    ds_id = await _createTestDatasource(client)
    payload = dict(_BASE_PAYLOAD)
    payload.update(overrides)
    payload["datasourceId"] = ds_id
    resp = await client.post("/api/v1/features", json=payload, headers=ADMIN_HEADERS)
    assert resp.status_code == 201, resp.text
    feature_id = resp.json()["id"]
    compute = await client.post(
        f"/api/v1/features/{feature_id}/compute", headers=ADMIN_HEADERS
    )
    assert compute.status_code == 200, compute.text
    return payload["featureName"]


def _installFakeAdapter(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(
        featuresModule._computeService,
        "_adapterProvider",
        lambda dsId, ds: _FakeAdapter(rows),
    )


class TestQueryByName:
    async def test_query_defaults_latest_window(self, client, monkeypatch) -> None:
        _installFakeAdapter(
            monkeypatch,
            [
                {"entity_key": "Q630", "value": Decimal("0.9500")},
                {"entity_key": "B019", "value": Decimal("0.8000")},
            ],
        )
        name = await _createFeature(client)

        resp = await client.get(f"/api/v1/features/by-name/{name}/values", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["featureName"] == "SUPPLIER_OTD_3M"
        assert body["entityType"] == "SUPPLIER"
        assert body["unit"] == "%"
        assert body["validAt"] == date.today().isoformat()
        assert len(body["values"]) == 2
        byKey = {v["entityKey"]: v for v in body["values"]}
        assert Decimal(byKey["Q630"]["value"]) == Decimal("0.9500")

    async def test_query_entity_keys_filter(self, client, monkeypatch) -> None:
        _installFakeAdapter(
            monkeypatch,
            [
                {"entity_key": "Q630", "value": Decimal("0.95")},
                {"entity_key": "B019", "value": Decimal("0.80")},
            ],
        )
        name = await _createFeature(client)

        resp = await client.get(
            f"/api/v1/features/by-name/{name}/values",
            params={"entity_keys": "Q630"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        values = resp.json()["values"]
        assert len(values) == 1
        assert values[0]["entityKey"] == "Q630"

    async def test_query_explicit_empty_window(self, client, monkeypatch) -> None:
        _installFakeAdapter(monkeypatch, [{"entity_key": "Q630", "value": Decimal("0.95")}])
        name = await _createFeature(client)

        resp = await client.get(
            f"/api/v1/features/by-name/{name}/values",
            params={"valid_at": "2020-01-01"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["values"] == []

    async def test_feature_without_values_returns_empty(self, client) -> None:
        """未 compute 过的特征 -> 空 values + 今日占位 validAt（前端渲染空态）。"""
        ds_id = await _createTestDatasource(client)
        payload = dict(_BASE_PAYLOAD)
        payload["datasourceId"] = ds_id
        resp = await client.post("/api/v1/features", json=payload, headers=ADMIN_HEADERS)
        assert resp.status_code == 201

        query = await client.get(
            f"/api/v1/features/by-name/SUPPLIER_OTD_3M/values", headers=ADMIN_HEADERS
        )
        assert query.status_code == 200, query.text
        body = query.json()
        assert body["values"] == []
        assert body["validAt"] == date.today().isoformat()

    async def test_unknown_name_returns_404(self, client) -> None:
        resp = await client.get(
            "/api/v1/features/by-name/DOES_NOT_EXIST/values", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 404

    async def test_invalid_name_returns_422(self, client) -> None:
        for bad in ("supplier_otd_3m", "BAD-NAME", "x" * 101):
            resp = await client.get(
                f"/api/v1/features/by-name/{bad}/values", headers=ADMIN_HEADERS
            )
            assert resp.status_code == 422, f"{bad} should be 422, got {resp.status_code}"

    async def test_entity_keys_over_limit_returns_422(self, client, monkeypatch) -> None:
        _installFakeAdapter(monkeypatch, [{"entity_key": "Q630", "value": Decimal("0.95")}])
        name = await _createFeature(client)

        keys = ",".join(f"K{i}" for i in range(101))
        resp = await client.get(
            f"/api/v1/features/by-name/{name}/values",
            params={"entity_keys": keys},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 422

    async def test_pagination(self, client, monkeypatch) -> None:
        _installFakeAdapter(
            monkeypatch,
            [
                {"entity_key": f"K{i:02d}", "value": Decimal("0.5")} for i in range(5)
            ],
        )
        name = await _createFeature(client)

        page1 = await client.get(
            f"/api/v1/features/by-name/{name}/values",
            params={"limit": 2, "offset": 0},
            headers=ADMIN_HEADERS,
        )
        page2 = await client.get(
            f"/api/v1/features/by-name/{name}/values",
            params={"limit": 2, "offset": 2},
            headers=ADMIN_HEADERS,
        )
        assert [v["entityKey"] for v in page1.json()["values"]] == ["K00", "K01"]
        assert [v["entityKey"] for v in page2.json()["values"]] == ["K02", "K03"]
