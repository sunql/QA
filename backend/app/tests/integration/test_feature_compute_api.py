"""Phase 4.3 Feature 计算 API 集成测试（真实 PG 5433 + 完整 API 链路）。

覆盖：
1. create 定义（ACTIVE）→ POST /{id}/compute（fake adapter）→ GET /{id}/values 断言落库
2. 重算幂等：同窗口二次 compute 覆盖不重复
3. compute-batch 只算 is_enabled=true 且 status=ACTIVE 的定义
4. value 与 value_text 双空行拒写（fake adapter 返回坏行 → 422）
"""

from __future__ import annotations

from decimal import Decimal
from itertools import count

from app.api.v1 import features as featuresModule

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

# 数据源名在同一测试内多次创建时需唯一（TRUNCATE 只在测试间生效）
_dsCounter = count(1)

_DATASOURCE_PAYLOAD: dict[str, object] = {
    "name": "feature-compute-ds",
    "type": "postgresql",
    "host": "db.example.com",
    "port": 5432,
    "databaseName": "appdb",
    "username": "u",
    "password": "p",
}

_BASE_PAYLOAD: dict[str, object] = {
    "featureName": "SUPPLIER_OTD_3M",
    "entityType": "SUPPLIER",
    "calculationLogic": "SELECT supplier_code AS entity_key, AVG(otd_rate) AS value FROM DWS GROUP BY supplier_code",
}


class _FakeAdapter:
    """注入的 fake 业务库适配器，返回固定两行供应商 OTD。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = rows or [
            {"entity_key": "Q630", "value": Decimal("0.9500")},
            {"entity_key": "B019", "value": Decimal("0.8000")},
        ]
        self.calls: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.calls.append(sql)
        return self._rows


async def _createTestDatasource(client) -> int:
    payload = dict(_DATASOURCE_PAYLOAD)
    payload["name"] = f"feature-compute-ds-{next(_dsCounter)}"
    resp = await client.post("/api/v1/datasources", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _payload(**overrides) -> dict[str, object]:
    p: dict[str, object] = dict(_BASE_PAYLOAD)
    p.update(overrides)
    return p


async def _createFeature(client, *, status: str = "ACTIVE", **overrides) -> int:
    ds_id = await _createTestDatasource(client)
    resp = await client.post(
        "/api/v1/features",
        json=_payload(datasourceId=ds_id, status=status, **overrides),
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _installFakeAdapter(monkeypatch, adapter) -> None:
    """把 feature compute service 的 adapter 解析替换为 fake（chat 同模式）。"""
    monkeypatch.setattr(
        featuresModule._computeService, "_adapterProvider", lambda dsId, ds: adapter
    )


class TestFeatureComputeApi:
    async def test_compute_then_list_values(self, client, monkeypatch) -> None:
        adapter = _FakeAdapter()
        _installFakeAdapter(monkeypatch, adapter)
        feature_id = await _createFeature(client)

        resp = await client.post(f"/api/v1/features/{feature_id}/compute", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["featureId"] == feature_id
        assert resp.json()["rows"] == 2

        values = await client.get(f"/api/v1/features/{feature_id}/values")
        assert values.status_code == 200
        rows = values.json()
        assert len(rows) == 2
        assert {r["entityKey"] for r in rows} == {"Q630", "B019"}
        # value 为 Decimal 字符串（Pydantic mode=json）
        byKey = {r["entityKey"]: r for r in rows}
        # NUMERIC(38,10) 落库补零到 10 位小数，按 Decimal 等值断言
        assert Decimal(byKey["Q630"]["value"]) == Decimal("0.9500")

    async def test_recompute_is_idempotent(self, client, monkeypatch) -> None:
        adapter = _FakeAdapter()
        _installFakeAdapter(monkeypatch, adapter)
        feature_id = await _createFeature(client)

        first = await client.post(f"/api/v1/features/{feature_id}/compute", headers=ADMIN_HEADERS)
        assert first.json()["rows"] == 2
        second = await client.post(f"/api/v1/features/{feature_id}/compute", headers=ADMIN_HEADERS)
        assert second.json()["rows"] == 2

        values = await client.get(f"/api/v1/features/{feature_id}/values")
        assert len(values.json()) == 2  # 同窗口覆盖，不产生重复行

    async def test_compute_batch_only_active_enabled(self, client, monkeypatch) -> None:
        adapter = _FakeAdapter()
        _installFakeAdapter(monkeypatch, adapter)
        active_id = await _createFeature(client, status="ACTIVE")
        await _createFeature(
            client, status="DRAFT", featureName="SUPPLIER_DRAFT_FEATURE"
        )
        await _createFeature(
            client, status="ACTIVE", featureName="SUPPLIER_DISABLED", isEnabled=False
        )

        resp = await client.post("/api/v1/features/compute-batch", headers=ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["totalRows"] == 2  # 仅 1 个 ACTIVE+enabled 特征 × 2 行
        assert [r["featureId"] for r in body["results"]] == [active_id]

    async def test_value_and_text_both_null_rejected(self, client, monkeypatch) -> None:
        adapter = _FakeAdapter(rows=[{"entity_key": "Q630", "value": None, "value_text": ""}])
        _installFakeAdapter(monkeypatch, adapter)
        feature_id = await _createFeature(client)

        resp = await client.post(f"/api/v1/features/{feature_id}/compute", headers=ADMIN_HEADERS)
        assert resp.status_code == 422

    async def test_compute_missing_feature_returns_404(self, client, monkeypatch) -> None:
        _installFakeAdapter(monkeypatch, _FakeAdapter())
        resp = await client.post("/api/v1/features/999999/compute", headers=ADMIN_HEADERS)
        assert resp.status_code == 404
